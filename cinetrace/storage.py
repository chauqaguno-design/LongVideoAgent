"""Persistent upload sessions and the local processing queue."""

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException


PART_SIZE = 5 * 1024 * 1024
SUMMARY = "id, name, size, duration_ms, status, stage, progress, error, created_at"
CONTEXT_KEYS = ("tracks", "evidence", "chunks", "scenes", "index")


class Store:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.upload_lock = threading.Lock()
        with self.db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS videos (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, size INTEGER NOT NULL,
                    fingerprint TEXT NOT NULL, sha256 TEXT, duration_ms INTEGER,
                    status TEXT NOT NULL DEFAULT 'uploading', stage TEXT NOT NULL DEFAULT '上传中',
                    progress INTEGER NOT NULL DEFAULT 0, error TEXT, context TEXT NOT NULL DEFAULT '{}',
                    notify_pending INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                    UNIQUE(name, size, fingerprint)
                );
                CREATE TABLE IF NOT EXISTS upload_parts (
                    video_id TEXT NOT NULL, part_index INTEGER NOT NULL, sha256 TEXT NOT NULL,
                    PRIMARY KEY(video_id, part_index)
                );
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(videos)")}
            if "notify_pending" not in columns:
                db.execute("ALTER TABLE videos ADD COLUMN notify_pending INTEGER NOT NULL DEFAULT 0")
            db.execute("UPDATE videos SET status='queued', stage='准备恢复' WHERE status='processing'")

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.root / "cinetrace.sqlite3", timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def list_videos(self):
        with self.db() as db:
            return [dict(r) for r in db.execute(f"SELECT {SUMMARY} FROM videos ORDER BY created_at DESC, rowid DESC")]

    def video(self, video_id):
        with self.db() as db:
            row = db.execute(f"SELECT {SUMMARY}, context FROM videos WHERE id=?", (video_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "找不到这份视频")
        info = dict(row)
        context = json.loads(info.pop("context"))
        return {"tracks": {}, "evidence": [], "chunks": [], "scenes": [],
                "index": {"status": "pending"}, **context, **info}

    def start_upload(self, name, size, fingerprint):
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO videos(id,name,size,fingerprint) VALUES(?,?,?,?)",
                       (uuid4().hex, name, size, fingerprint))
            row = db.execute("SELECT id FROM videos WHERE name=? AND size=? AND fingerprint=?",
                             (name, size, fingerprint)).fetchone()
            video_id = row["id"]
            received = db.execute("SELECT part_index FROM upload_parts WHERE video_id=? ORDER BY part_index",
                                  (video_id,)).fetchall()
        return {"id": video_id, "part_size": PART_SIZE, "received": [r[0] for r in received]}

    def media_path(self, video_id):
        return self.root / video_id / "original.mp4"

    def put_part(self, video_id, index, data, digest):
        if hashlib.sha256(data).hexdigest() != digest:
            raise HTTPException(422, "分片 SHA-256 校验失败，请重新上传该分片")
        with self.upload_lock, self.db() as db:
            video = db.execute("SELECT size,status FROM videos WHERE id=?", (video_id,)).fetchone()
            if video is None:
                raise HTTPException(404, "找不到上传会话")
            remaining = video["size"] - index * PART_SIZE
            if index < 0 or remaining <= 0 or len(data) != min(PART_SIZE, remaining):
                raise HTTPException(422, "分片编号或长度与上传会话不符")
            previous = db.execute("SELECT sha256 FROM upload_parts WHERE video_id=? AND part_index=?",
                                  (video_id, index)).fetchone()
            if previous and previous["sha256"] != digest:
                raise HTTPException(409, "同编号分片内容冲突，请重新选择同一文件")
            if previous:
                return
            if video["status"] != "uploading":
                raise HTTPException(409, "该视频已完成上传")
            directory = self.root / video_id / "parts"
            directory.mkdir(parents=True, exist_ok=True)
            temp = directory / "writing.tmp"
            temp.write_bytes(data)
            temp.replace(directory / str(index))
            db.execute("INSERT INTO upload_parts VALUES(?,?,?)", (video_id, index, digest))

    def finish_upload(self, video_id, expected_hash, probe):
        with self.upload_lock:
            with self.db() as db:
                video = db.execute("SELECT size,status,sha256,fingerprint FROM videos WHERE id=?", (video_id,)).fetchone()
                if video is None:
                    raise HTTPException(404, "找不到上传会话")
                if video["status"] != "uploading":
                    if expected_hash and expected_hash != video["sha256"]:
                        raise HTTPException(409, "已上传原片的 SHA-256 与本次提交不符")
                    return
                parts = db.execute("SELECT part_index,sha256 FROM upload_parts WHERE video_id=? ORDER BY part_index",
                                   (video_id,)).fetchall()
            count = (video["size"] + PART_SIZE - 1) // PART_SIZE
            if len(parts) != count:
                raise HTTPException(409, f"还有 {count - len(parts)} 个分片待上传")
            fingerprint = hashlib.sha256(b"".join(bytes.fromhex(row["sha256"]) for row in parts)).hexdigest()
            if fingerprint != video["fingerprint"]:
                with self.db() as db:
                    db.execute("DELETE FROM upload_parts WHERE video_id=?", (video_id,))
                raise HTTPException(409, "分片列表与原片指纹不符，请重新选择文件上传")
            original = self.media_path(video_id)
            directory = original.parent / "parts"
            temp = original.with_name("assembling.mp4")
            digest = hashlib.sha256()
            try:
                with temp.open("wb") as output:
                    for part in parts:
                        path = directory / str(part["part_index"])
                        data = path.read_bytes() if path.is_file() else b""
                        if hashlib.sha256(data).hexdigest() != part["sha256"]:
                            with self.db() as db:
                                db.execute("DELETE FROM upload_parts WHERE video_id=? AND part_index=?",
                                           (video_id, part["part_index"]))
                            raise HTTPException(409, "已保存的分片校验失败，请重新选择文件续传")
                        output.write(data)
                        digest.update(data)
                checksum = digest.hexdigest()
                if expected_hash and expected_hash != checksum:
                    raise HTTPException(422, "完整文件 SHA-256 校验失败")
                try:
                    duration = int(probe(temp)["duration_ms"])
                    if duration <= 0:
                        raise ValueError("视频时长无效")
                except (ValueError, RuntimeError, OSError) as exc:
                    raise HTTPException(422, f"无法读取视频：{exc}") from exc
                temp.replace(original)
                with self.db() as db:
                    db.execute("UPDATE videos SET sha256=?,duration_ms=?,status='queued',stage='等待处理',notify_pending=1 WHERE id=?",
                               (checksum, duration, video_id))
            finally:
                temp.unlink(missing_ok=True)
            for part in parts:
                (directory / str(part["part_index"])).unlink(missing_ok=True)

    def retry(self, video_id):
        video = self.video(video_id)
        if video["status"] == "uploading":
            raise HTTPException(409, "请先完成上传")
        with self.db() as db:
            db.execute("""UPDATE videos SET status='queued',stage='等待重试',error=NULL,notify_pending=1
                          WHERE id=? AND status IN ('failed','partial')""", (video_id,))

    def claim(self):
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT id FROM videos WHERE status='queued' ORDER BY created_at, rowid LIMIT 1").fetchone()
            if row is not None:
                db.execute("UPDATE videos SET status='processing',stage='开始处理',error=NULL WHERE id=?", (row["id"],))
                return row["id"]
        return None

    def pending_notifications(self):
        with self.db() as db:
            return [r["id"] for r in db.execute("SELECT id FROM videos WHERE notify_pending=1 ORDER BY created_at LIMIT 64")]

    def mark_notified(self, video_id):
        with self.db() as db:
            db.execute("UPDATE videos SET notify_pending=0 WHERE id=?", (video_id,))

    def save_context(self, video_id, context):
        payload = {key: context[key] for key in CONTEXT_KEYS if key in context}
        with self.db() as db:
            db.execute("UPDATE videos SET status=?,stage=?,progress=?,error=?,context=? WHERE id=?",
                       (context["status"], context["stage"], context["progress"], context.get("error"),
                        json.dumps(payload, ensure_ascii=False), video_id))
