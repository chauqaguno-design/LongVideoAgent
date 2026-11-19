"""Build a time-aligned film index. Each completed model call is a SQLite checkpoint."""

import hashlib
import json
import os
from contextlib import contextmanager, closing
from pathlib import Path
import sqlite3

from . import film_media
from .media import probe, windows


WINDOW_MS = 30_000
GROUP_SIZE = 20  # Organize up to ten minutes of evidence per text request.
RECIPE = "film-index-v1"


def file_hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def film_hash(path, directory):
    """Reuse the digest on repeated questions about the same local file."""
    path = Path(path).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    stat = path.stat()
    signature = f"{stat.st_size}/{stat.st_mtime_ns}/{stat.st_ctime_ns}/{stat.st_ino}"
    with closing(sqlite3.connect(directory / "files.sqlite3", timeout=30)) as db, db:
        db.execute("CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, signature TEXT, digest TEXT)")
        key = os.path.normcase(str(path))
        row = db.execute("SELECT digest FROM files WHERE path=? AND signature=?", (key, signature)).fetchone()
        if row:
            return row[0]
        digest = file_hash(path)
        db.execute("INSERT OR REPLACE INTO files VALUES (?,?,?)", (key, signature, digest))
        return digest


@contextmanager
def database(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=150)) as db:
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
            CREATE TABLE IF NOT EXISTS steps (
                run TEXT NOT NULL, step TEXT NOT NULL, slot INTEGER NOT NULL,
                value TEXT NOT NULL, PRIMARY KEY (run, step, slot)
            );
            CREATE TABLE IF NOT EXISTS active (id INTEGER PRIMARY KEY CHECK(id=1), run TEXT NOT NULL);
        """)
        yield db


def cached(db, run, step, slot, produce):
    # Serialize contenders before a paid call. A failed call leaves the step retryable.
    with db:
        db.execute("BEGIN IMMEDIATE")
        key = (run, step, slot)
        row = db.execute("SELECT value FROM steps WHERE run=? AND step=? AND slot=?", key).fetchone()
        if row:
            return json.loads(row[0])
        value = produce()
        db.execute("INSERT INTO steps VALUES (?,?,?,?)", (*key, json.dumps(value, ensure_ascii=False)))
        return value


def dialogue_in(lines, start, end):
    return "\n".join(row["text"] for row in lines if row["start_ms"] < end and row["end_ms"] > start)


def film_clues(lines):
    """Sample dialogue from across the film, without sending the whole transcript to search."""
    if not lines:
        return []
    count = min(12, len(lines))
    return [lines[i * (len(lines) - 1) // max(1, count - 1)]["text"][:180] for i in range(count)]


def observe_window(path, start, end, lines, models, background):
    transcript = dialogue_in(lines, start, end)
    frames = film_media.frames_for_window(path, start, end)
    details = models.observe(frames, transcript, background)
    return {**details, "start_ms": start, "end_ms": end, "transcript": transcript,
            "sample_ms": [t for t, _ in frames]}


def index_film(path, data_dir, models, *, title="", use_web=True, srt=None, progress=lambda message: None):
    path, data_dir = Path(path).resolve(), Path(data_dir)
    info = probe(path)
    digest = film_hash(path, data_dir / "films")
    info["sha256"] = digest
    duration = info["duration_ms"]
    db_path = data_dir / "films" / (digest + ".sqlite3")
    speech_config = (file_hash(srt) if srt else None, os.getenv("CINETRACE_WHISPER_MODEL", "turbo"),
                     os.getenv("CINETRACE_LANGUAGE"))
    config = [RECIPE, title or path.stem, use_web, speech_config,
              os.getenv("DASHSCOPE_VISION_MODEL", "qwen3-vl-plus"), os.getenv("DEEPSEEK_MODEL", "deepseek-flash")]
    run = hashlib.sha256(json.dumps(config, ensure_ascii=False).encode()).hexdigest()
    dialogue_key = json.dumps(speech_config)
    with database(db_path) as db:
        progress("读取带时间的语音或字幕")
        lines = cached(db, dialogue_key, "dialogue", 0,
                       lambda: film_media.read_srt(srt) if srt else film_media.dialogue_for_film(path, info, data_dir))
        hint = title or path.stem
        background = {"title": hint, "confirmed": bool(title), "aliases": [], "characters": [],
                      "events": [], "sources": [], "alternatives": []}
        if use_web:
            progress("核对片名与公开资料")
            clues = film_clues(lines)
            if not title:
                opening = cached(db, run, "window", 0, lambda: observe_window(
                    path, 0, min(WINDOW_MS, duration), lines, models, background))
                clues.append("片头画面线索（可能与片名无关）：" + "、".join(opening["objects"]))
            background = cached(db, run, "background", 0, lambda: models.research(hint, clues))
            if not background["confirmed"] and background["alternatives"]:
                choices = "、".join(background["alternatives"])
                raise ValueError(f"影片有多个候选：{choices}。请用 --title 指定片名和年份，或用 --no-web 建匿名索引。")
        records = []
        for start, end in windows(0, duration, WINDOW_MS):
            progress(f"读取画面 {start / 1000:g}–{end / 1000:g} 秒")
            records.append(cached(db, run, "window", start, lambda: observe_window(
                path, start, end, lines, models, background)))

        events = []
        for offset in range(0, len(records), GROUP_SIZE):
            group = records[offset:offset + GROUP_SIZE]
            progress(f"整理 {group[0]['start_ms'] / 1000:g} 秒起的事件")
            groups = cached(db, run, "group", offset, lambda: models.organize(group))
            for item in groups:
                events.append({"start_ms": group[item["first"]]["start_ms"],
                               "end_ms": group[item["last"]]["end_ms"], "description": item["description"]})

        progress("生成全片概述")
        overview = cached(db, run, "summary", 0, lambda: models.summarize(events))
        result = {"title": background["title"] or hint, "duration_ms": duration, "background": background,
                  "overview": overview, "events": events}
        with db:
            saved_timeline = {**result, "dialogue_key": dialogue_key}
            db.execute("INSERT OR REPLACE INTO steps VALUES (?,?,?,?)",
                       (run, "timeline", 0, json.dumps(saved_timeline, ensure_ascii=False)))
            db.execute("INSERT OR REPLACE INTO active VALUES (1,?)", (run,))
    return {**result, "windows": records, "dialogue": lines}


def read_index(path, data_dir):
    directory = Path(data_dir) / "films"
    if not directory.is_dir():
        raise ValueError("尚未建立该原片的索引，请先运行 index。")
    db_path = directory / (film_hash(path, directory) + ".sqlite3")
    if not db_path.is_file():
        raise ValueError("尚未建立该原片的索引，请先运行 index。")
    with database(db_path) as db:
        row = db.execute("SELECT run FROM active WHERE id=1").fetchone()
        if not row:
            raise ValueError("索引尚未完成，用相同的 index 命令可继续。")
        run = row[0]
        result = json.loads(db.execute(
            "SELECT value FROM steps WHERE run=? AND step='timeline' AND slot=0", (run,)).fetchone()[0])
        dialogue_key = result.pop("dialogue_key", None)
        if dialogue_key is not None:
            result["dialogue"] = json.loads(db.execute(
                "SELECT value FROM steps WHERE run=? AND step='dialogue' AND slot=0", (dialogue_key,)).fetchone()[0])
        result["windows"] = [json.loads(r[0]) for r in db.execute(
            "SELECT value FROM steps WHERE run=? AND step='window' ORDER BY slot", (run,))]
        return result
