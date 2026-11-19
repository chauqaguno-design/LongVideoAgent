"""Send one local MP4 clip to Qwen3-VL-Plus and print events on the source timeline."""

import argparse
import base64
import getpass
import json
import math
import os
import subprocess
import sys
from http.client import HTTPException
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


def clip_duration(path):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=nw=1:nk=1", str(path.resolve())],
            capture_output=True, text=True, check=True,
        )
        return float(result.stdout.strip())
    except (OSError, subprocess.CalledProcessError, ValueError) as exc:
        raise ValueError("无法探测时长，请安装 ffprobe 或传入实际片段时长 --duration-s。") from exc


def post_clip(path, duration, base_url, key):
    size = path.stat().st_size
    if path.suffix.lower() != ".mp4" or not 0 < 4 * ((size + 2) // 3) < 10_000_000:
        raise ValueError("请提供非空 MP4，Base64 编码后需小于 10 MB。")
    data = base64.b64encode(path.read_bytes()).decode("ascii")
    prompt = (
        f"只描述画面可见的动作，用 0 到 {duration:g} 之间的片内秒数标注起止。"
        '只输出 JSON 对象，格式为 {"events":[{"start_s":1.0,"end_s":2.5,"action":"动作描述"}]}。'
        '没有可确认的动作时返回 {"events":[]}，不推断人物台词。'
    )
    content = [
        {"type": "video_url", "video_url": {"url": "data:video/mp4;base64," + data}, "fps": 2},
        {"type": "text", "text": prompt},
    ]
    body = {"model": "qwen3-vl-plus", "messages": [{"role": "user", "content": content}],
            "max_tokens": 600, "enable_thinking": False}
    request = Request(base_url.rstrip("/") + "/chat/completions", data=json.dumps(body).encode(),
                      headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    try:
        with urlopen(request, timeout=120) as response:
            return json.load(response)
    except HTTPError as exc:
        raise RuntimeError(f"百炼返回 HTTP {exc.code}，请核对地域、Key、模型权限和额度。") from exc
    except (OSError, HTTPException) as exc:
        raise RuntimeError(f"百炼连接或读取失败：{exc}") from exc


def source_events(result, duration, start_ms):
    message = result["choices"][0]["message"]["content"]
    if not isinstance(message, str) or not message.strip():
        raise ValueError("缺少模型输出内容。")
    text = message.strip()
    if text.startswith("```json") and text.endswith("```"):
        text = text[7:-3].strip()
    events = json.loads(text)["events"]
    if not isinstance(events, list):
        raise ValueError("events 必须是数组。")
    normalized = []
    for event in events:
        begin, end, action = event["start_s"], event["end_s"], event["action"]
        if type(begin) not in (float, int) or type(end) not in (float, int):
            raise ValueError("动作起止必须是数字。")
        if not (0 <= begin < end <= duration) or round(begin * 1000) >= round(end * 1000):
            raise ValueError("动作起止超出片段或区间无效。")
        if not isinstance(action, str) or not action.strip():
            raise ValueError("缺少动作描述。")
        normalized.append({"start_ms": start_ms + round(begin * 1000),
                           "end_ms": start_ms + round(end * 1000), "action": action.strip()})
    return normalized


def main():
    parser = argparse.ArgumentParser(description="用百炼 qwen3-vl-plus 核对一个 MP4 片段的可见动作。")
    parser.add_argument("clip", type=Path, help="本地短 MP4")
    parser.add_argument("--start-ms", type=int, default=0, help="片段在原片的起始毫秒，默认 0")
    parser.add_argument("--duration-s", type=float, help="片段实际秒数，省略时通过 ffprobe 读取")
    args = parser.parse_args()
    if not args.clip.is_file() or args.start_ms < 0:
        parser.error("需要已有的短片文件，且 --start-ms 必须大于等于 0。")
    try:
        duration = args.duration_s if args.duration_s is not None else clip_duration(args.clip)
        if not math.isfinite(duration) or not 2 <= duration <= 3600:
            raise ValueError("此模型的片段时长范围是 2 至 3600 秒。")
        base_url = (os.getenv("DASHSCOPE_BASE_URL") or input("百炼兼容 Base URL：")).strip()
        url = urlsplit(base_url)
        if url.scheme != "https" or not url.hostname or not url.path.rstrip("/").endswith("/compatible-mode/v1"):
            raise ValueError("请复制百炼控制台的 HTTPS 兼容地址，末尾应为 /compatible-mode/v1。")
        key = (os.getenv("DASHSCOPE_API_KEY") or getpass.getpass("百炼 API Key：")).strip()
        if not key:
            raise ValueError("需要百炼 API Key。")
        result = post_clip(args.clip, duration, base_url, key)
    except (json.JSONDecodeError, UnicodeDecodeError):
        print("百炼响应格式错误：服务返回的内容无法解析为 JSON。", file=sys.stderr)
        return 3
    except (OSError, ValueError, RuntimeError, EOFError) as exc:
        print(f"请求未完成：{exc}", file=sys.stderr)
        return 2
    try:
        events = source_events(result, duration, args.start_ms)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        print(f"模型响应格式错误：{exc}", file=sys.stderr)
        return 3
    print(json.dumps({"events": events, "usage": result.get("usage")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
