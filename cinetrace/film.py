"""Command-line entry point for a searchable, playable movie timeline."""

import argparse
from html import escape
import json
import os
from pathlib import Path
import sqlite3
import sys
from urllib.parse import urlsplit

from dotenv import load_dotenv

from .film_index import WINDOW_MS, index_film, read_index
from .media import probe


ROOT = Path(__file__).resolve().parents[1]


def clock(ms):
    seconds = ms // 1000
    return f"{seconds // 3600:02}:{seconds // 60 % 60:02}:{seconds % 60:02}"


def play_button(item):
    start, end = item["start_ms"], item["end_ms"]
    return f'<button data-start="{start / 1000}" data-end="{end / 1000}">{clock(start)} – {clock(end)}</button>'


def write_report(output, movie, index, search=None):
    """Plain local HTML: model text stays escaped and playback uses the original file."""
    title = escape(index["title"])
    parts = [f"""<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title} · CineTrace</title>
<style>body{{max-width:860px;margin:auto;padding:24px;font:16px/1.65 system-ui,sans-serif;color:#20302f}}
video{{display:block;width:100%;max-height:46vh;background:#151919}}button{{cursor:pointer;margin-right:8px}}
li{{margin:9px 0}}small{{color:#52635f}}summary{{cursor:pointer}}</style>
<h1>{title}</h1><video id="film" controls preload="metadata" src="{escape(movie.resolve().as_uri())}"></video>"""]
    if search:
        labels = {"verified": "已核验", "partial": "部分吻合", "no_match": "未找到可确认的片段"}
        parts.append(f'<h2>检索结果</h2><p>{escape(labels[search["status"]])}</p>')
        match = search.get("match")
        if match:
            parts.append(f'<p>{play_button(match)} {escape(match["reason"])}</p>')
            if match["missing"]:
                parts.append(f'<p>待确认：{escape("、".join(match["missing"]))}</p>')
    parts.append(f'<h2>全片概述</h2><p>{escape(index["overview"])}</p><h2>剧情时间线</h2><ol>')
    for event in index["events"]:
        parts.append(f'<li>{play_button(event)} {escape(event["description"])}</li>')
    parts.append('</ol><details><summary>查看抽样画面记录和对应台词</summary><ol>')
    for row in index["windows"]:
        facts = [row["scene"], *row["people"], *row["objects"], *row["actions"]]
        parts.append(f'<li>{play_button(row)} {escape(" · ".join(filter(None, facts)))}')
        if row["transcript"]:
            parts.append(f'<br><small>{escape(row["transcript"])}</small>')
        parts.append('</li>')
    parts.append('</ol></details>')
    sources = index["background"].get("sources", [])
    if sources:
        parts.append('<h2>影片背景资料</h2><ul>')
        for source in sources:
            url = source["url"]
            if urlsplit(url).scheme in {"https", "http"}:
                parts.append(f'<li><a href="{escape(url)}">{escape(source["title"] or url)}</a></li>')
        parts.append('</ul>')
    parts.append("""<script>
const film = document.querySelector('video');
let end = null;
document.addEventListener('click', e => {
  const button = e.target.closest('button[data-start]');
  if (!button) return;
  const play = () => {end = Number(button.dataset.end); film.currentTime = Number(button.dataset.start); film.play().catch(() => {});};
  if (film.readyState) play(); else film.addEventListener('loadedmetadata', play, {once:true});
});
film.addEventListener('timeupdate', () => {if (end !== null && film.currentTime >= end) {end = null; film.pause();}});
</script></html>""")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text("\n".join(parts), encoding="utf-8")


def cli(argv=None):
    load_dotenv(ROOT / ".env")
    parser = argparse.ArgumentParser(description="建立电影时间线，按剧情查找原片区间。")
    actions = parser.add_subparsers(dest="command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("movie", type=Path, help="本地 MP4 原片")
    common.add_argument("--data-dir", type=Path, default=Path(os.getenv("CINETRACE_DATA") or ROOT / "data"))
    actions.add_parser("plan", parents=[common], help="只检查原片和预计窗口数")
    index = actions.add_parser("index", parents=[common], help="断点续建画面索引和全片概述")
    index.add_argument("--title", default="", help="已知片名，可带上映年份")
    index.add_argument("--no-web", action="store_true", help="跳过联网查片，仍调用模型处理画面与时间线")
    index.add_argument("--srt", type=Path, help="使用与此版本原片对齐的外部 SRT")
    timeline = actions.add_parser("timeline", parents=[common], help="读取已生成的时间线")
    find = actions.add_parser("find", parents=[common], help="用自然语言查找并回看候选片段")
    find.add_argument("query", help="例如：两个人在哪里开车追逐？")
    find.add_argument("--budget", type=int, default=3, help="最多回看几次候选原片，默认 3")
    for sub in (index, timeline, find):
        sub.add_argument("--html", type=Path, help="另存可点击回看的本地 HTML")
    args = parser.parse_args(argv)
    if not args.movie.is_file():
        parser.error("原片路径不存在")
    if getattr(args, "html", None) and args.html.suffix.lower() not in {".htm", ".html"}:
        parser.error("--html 请指定 .html 文件")
    if args.command == "find" and (not args.query.strip() or args.budget < 0):
        parser.error("查询文本不能为空，复核预算应大于等于零")
    if args.command == "plan":
        info = probe(args.movie)
        return {"duration_ms": info["duration_ms"], "has_audio": info["has_audio"],
                "window_seconds": WINDOW_MS // 1000, "sample_seconds": 5,
                "windows": (info["duration_ms"] + WINDOW_MS - 1) // WINDOW_MS, "model_calls": 0}
    from .film_models import FilmModels
    if args.command == "index":
        if args.srt and not args.srt.is_file():
            parser.error("SRT 文件不存在")
        report = index_film(args.movie, args.data_dir, FilmModels.from_env(), title=args.title, use_web=not args.no_web,
                            srt=args.srt, progress=lambda s: print(s, file=sys.stderr, flush=True))
    else:
        report = read_index(args.movie, args.data_dir)
    search = None
    if args.command == "find":
        from .film_search import find_scene
        models = FilmModels.from_env() if args.budget else None
        search = find_scene(args.movie, args.query, report["windows"], report["background"], models,
                            budget=args.budget, dialogue=report.get("dialogue"))
    if args.html:
        write_report(args.html, args.movie, report, search)
        print(str(args.html.resolve()), file=sys.stderr)
    return search or report


def main():
    try:
        print(json.dumps(cli(), ensure_ascii=False, indent=2))
    except (ValueError, RuntimeError, OSError, sqlite3.Error) as exc:
        print(f"处理未完成：{exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
