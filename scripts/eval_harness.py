"""Exercise the real CutSieve workers and HTTP API with scripted or live model turns."""

import argparse
from array import array
from base64 import b64decode
from contextlib import nullcontext
from hashlib import file_digest
from io import BytesIO
import json
import mimetypes
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from uuid import uuid4

from fastapi.testclient import TestClient
from PIL import Image

from cutsieve.infrastructure.edit_media import media_binaries
from cutsieve.infrastructure.upload_parts import part_size
from cutsieve.presentation.api.app import create_app, load_local_dotenv

ROOT = Path(__file__).resolve().parents[1]
TERMINAL = {"SUCCEEDED", "PARTIAL", "FAILED", "AWAITING_INPUT", "CANCELLED", "SUPERSEDED"}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def body(response):
    response.raise_for_status()
    return response.json()


def wait(client, endpoint, timeout):
    deadline = monotonic() + timeout
    while monotonic() < deadline:
        state = body(client.get(endpoint))
        if state["status"] in TERMINAL:
            return state
        sleep(.2)
    raise TimeoutError(f"Timed out waiting for {endpoint}")


def ingest(client, video, folder, *, reuse=False):
    cache = folder / "eval-source.json"
    with video.open("rb") as source:
        fingerprint = file_digest(source, "sha256").hexdigest()
    if reuse and cache.is_file():
        saved = json.loads(cache.read_text(encoding="utf-8"))
        if saved["sha256"] == fingerprint:
            existing = client.get("/v1/media-revisions/" + saved["asset"]["media_revision_id"])
            if existing.is_success:
                return saved["project"], saved["asset"]
    project = body(client.post("/v1/projects", json={"title": "Harness evaluation"}))["project_id"]
    size = video.stat().st_size
    mime = mimetypes.guess_type(video.name)[0] or "video/mp4"
    upload = body(client.post(f"/v1/projects/{project}/assets", json=dict(
        filename=video.name, size_bytes=size, content_type=mime, sha256=fingerprint,
    )))
    parts = []
    with video.open("rb") as source:
        for part in upload["parts"]:
            payload = source.read(part_size(size)) if part["headers"] else source.read()
            response = client.put(part["url"], content=payload, headers=part["headers"])
            response.raise_for_status()
            parts.append(dict(part_number=part["part_number"], etag=response.headers["etag"]))
    asset = body(client.post(f"/v1/uploads/{upload['upload_id']}/complete", json=dict(
        parts=parts, idempotency_key="eval-upload", sha256=fingerprint,
    )))
    cache.write_text(json.dumps(dict(sha256=fingerprint, project=project, asset=asset)), encoding="utf-8")
    return project, asset


def wait_index(client, asset, timeout):
    runs = body(client.get(f"/v1/assets/{asset['asset_id']}/processing-runs"))["items"]
    indexed = wait(client, "/v1/processing-runs/" + runs[0]["processing_run_id"], timeout)
    require(indexed["status"] in {"SUCCEEDED", "PARTIAL"}, f"Indexing finished {indexed['status']}")


def start_turn(client, project, asset, message, **extra):
    received = body(client.post(f"/v1/projects/{project}/conversation-runs", json=dict(
        message=message, media_revision_id=asset["media_revision_id"], idempotency_key=str(uuid4()), **extra,
    )))
    return received["run_id"]


def read_turn(client, run_id):
    endpoint = "/v1/workflow-runs/" + run_id
    state = wait(client, endpoint, 330)
    harness = body(client.get(endpoint + "/harness"))
    result = body(client.get(endpoint + "/result")) if state["status"] == "SUCCEEDED" else {}
    return harness, result


def download(client, artifact_id, path):
    artifact = body(client.get("/v1/artifacts/" + artifact_id))
    with client.stream("GET", artifact["url"]) as response, path.open("wb") as output:
        response.raise_for_status()
        for chunk in response.iter_bytes():
            output.write(chunk)
    return artifact


def decode(path):
    tools = media_binaries()
    subprocess.run([tools["ffmpeg"], "-v", "error", "-xerror", "-i", str(path),
                    "-map", "0:v:0", "-map", "0:a?", "-f", "null", "-"], check=True, timeout=120)
    probe = subprocess.check_output([tools["ffprobe"], "-v", "error", "-show_streams",
                                     "-of", "json", str(path)], timeout=30)
    return json.loads(probe)["streams"]


def check_fades(path, clips):
    signal = array("f")
    signal.frombytes(subprocess.check_output([
        media_binaries()["ffmpeg"], "-v", "error", "-i", str(path), "-map", "0:a:0",
        "-ac", "1", "-ar", "8000", "-f", "f32le", "-",
    ], timeout=60))

    def rms(start, end):
        values = signal[round(start * 8000):round(end * 8000)]
        return (sum(x * x for x in values) / len(values)) ** .5

    at = 0
    for length in clips:
        mid = rms(at + .4, at + .6)
        require(mid > .025, "Missing audible clip interior")
        require(max(rms(at + .01, at + .06), rms(at + length - .07, at + length - .02)) < mid * .4,
                "Decoded audio did not fade at both edges")
        at += length


def synthetic_video(folder):
    ffmpeg = media_binaries()["ffmpeg"]
    captions = folder / "offline.srt"
    captions.write_text(
        "1\n00:00:00,100 --> 00:00:01,200\n今天机器人飞向太空\n\n"
        "2\n00:00:02,100 --> 00:00:03,200\nThe dragon is named Scales.\n\n"
        "3\n00:00:04,100 --> 00:00:05,200\n机器正常运转，飞船停泊\n\n"
        "4\n00:00:06,100 --> 00:00:07,200\n看见黑衣人走来\n",
        encoding="utf-8",
    )
    args = [ffmpeg, "-v", "error", "-y", "-filter_complex_threads", "1"]
    for i in range(8):
        args.extend(["-f", "lavfi", "-i", f"color={'white' if i % 2 else 'black'}:"
                     f"s=160x90:r=12:d={15 if i == 7 else 1.5}"])
    args.extend(["-f", "lavfi", "-i", "sine=frequency=523:sample_rate=48000:duration=25.5",
                 "-i", str(captions)])
    graph = "".join(f"[{i}:v]" for i in range(8)) + "concat=n=8:v=1:a=0[v]"
    video = folder / "offline.mp4"
    subprocess.run([*args, "-filter_complex", graph, "-map", "[v]", "-map", "8:a", "-map", "9:s",
                    "-c:v", "libx264", "-threads", "2", "-c:a", "aac", "-c:s", "mov_text", str(video)],
                   check=True, timeout=60)
    return video


def tool(call_id, name, **args):
    return dict(id=call_id, type="function", function=dict(name=name, arguments=json.dumps(args)))


def estimated_tokens(messages):
    # Deterministic proxy for the offline transport, which has no provider tokenizer.
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            content = [part["text"] if part["type"] == "text" else "x" * 1024 for part in content]
        total += (len(json.dumps(dict(message, content=content), ensure_ascii=False)) + 3) // 4
    return total


class ScriptedModel:
    def __init__(self):
        self.turns = []
        self.requests = []
        self.responses = []

    async def post(self, _url, *, headers, json, timeout):
        messages = json["messages"]
        self.requests.append(messages)
        answer = self.turns.pop(0)
        if callable(answer):
            answer = answer(messages)
        message = dict(role="assistant", content=answer if isinstance(answer, str) else None)
        if not isinstance(answer, str):
            message["tool_calls"] = [answer] if isinstance(answer, dict) else list(answer)
        self.responses.append(message)
        return dict(model=json["model"], choices=[dict(message=message)],
                    usage=dict(prompt_tokens=estimated_tokens(messages), completion_tokens=24))


def tool_payload(messages, call_id):
    content = next(msg["content"] for msg in messages if msg.get("tool_call_id") == call_id)
    return json.loads(content[0]["text"] if isinstance(content, list) else content)


def report_row(results, case, operation):
    start = monotonic()
    try:
        detail = operation()
        results.append((case, monotonic() - start, detail, "PASS"))
    except (ValueError, OSError, TimeoutError, subprocess.SubprocessError) as exc:
        results.append((case, monotonic() - start, str(exc).splitlines()[0], "FAIL"))


def offline(folder):
    for key in ("CUTSIEVE_SEARCH_ENCODER_URL", "CUTSIEVE_MEDIA_AUTO_VISUAL", "CUTSIEVE_WHISPER_DIR",
                "CUTSIEVE_WHISPER_MODEL_ROOT", "CUTSIEVE_PADDLE_OCR_DIR"):
        os.environ.pop(key, None)
    os.environ.update(CUTSIEVE_WORKFLOWS_ENABLED="true", CUTSIEVE_MAX_MEDIA_FRAMES="2048",
                      CUTSIEVE_HARNESS_MAX_STEPS="15", CUTSIEVE_HARNESS_MAX_IMAGES="24")
    model = ScriptedModel()
    video = synthetic_video(folder)
    app = create_app(work_dir=folder, agent_transport=model)
    app.state.workflow_worker.harness.env = dict(CUTSIEVE_COMPACT_MAX_MESSAGES="10",
                                                 CUTSIEVE_COMPACT_CHAR_LIMIT="100000")
    results = []
    with TestClient(app) as client:
        project, asset = ingest(client, video, folder)
        wait_index(client, asset, 60)
        catalog = app.state.processing.catalog
        workspace, revision = asset["workspace_id"], asset["media_revision_id"]

        def retrieval():
            queries = {"机器人": 100_000, "太空": 100_000, "黑衣人": 6_100_000, "Scales": 2_100_000,
                       "dragon": 2_100_000}
            timings = []
            for query, timestamp in queries.items():
                start = monotonic()
                result = body(client.post("/v1/search", json=dict(
                    scope=dict(project_id=project, media_revision_ids=[revision]),
                    query_mode="LOCAL", text=query, top_k=5,
                )))
                timings.append((monotonic() - start) * 1000)
                require(bool(result["hits"]), f"Missing search result for {query}")
                hit = result["hits"][0]
                require(any(r["route"] == "fts5" for r in hit["route_ranks"]), f"FTS5 was not used for {query}")
                evidence = body(client.get("/v1/evidence/" + hit["evidence_ids"][0]))
                require(evidence["range"]["start_us"] == timestamp, f"Wrong timestamp for {query}")
            return f"{len(queries)}/{len(queries)} top hits, mean {sum(timings) / len(timings):.1f} ms"

        def sampling():
            _, rows = catalog.search_scope(workspace, project, [revision])
            frames = sorted(r["frame"]["actual_us"] for r in rows if r["source"] == "sampled-frame")
            require(len(frames) == 8, f"Expected seven short-shot midpoints and one static sample, got {frames}")
            require(all(abs(at - (i * 1.5 + .75) * 1e6) <= 100_000 for i, at in enumerate(frames[:7])),
                    f"Short-shot samples missed midpoints: {frames}")
            return f"{len(frames)} frames across 8 shots, static tail deduplicated"

        report_row(results, "SQLite FTS5 Chinese and English", retrieval)
        report_row(results, "Adaptive shot sampling and static dedup", sampling)

        captured = {}

        def repair(messages):
            captured["recovery"] = tool_payload(messages, "invalid")
            asset_revision = captured["recovery"]["details"]["valid_assets"][0]["media_revision_id"]
            return tool("edit", "edit_timeline", audio_fade_ms=300, clips=[
                dict(start_s=.1, end_s=1.3), dict(asset_id=asset_revision, start_s=2, end_s=3.2),
            ])

        def render(messages):
            captured["edit"] = tool_payload(messages, "edit")
            return tool("render", "render_video", profile="EXPORT", format="mp4")

        def inspect(messages):
            captured["render"] = tool_payload(messages, "render")
            return tool("sheet", "inspect_frames", artifact_id=captured["render"]["artifact_id"],
                        cut_points=True, contact_sheet=True)

        def advance(messages):
            captured["sheet"] = next(msg["content"] for msg in messages if msg.get("tool_call_id") == "sheet")
            return tool("overview", "get_media_overview")

        model.turns = [
            tool("invalid", "edit_timeline", clips=[dict(asset_id=str(uuid4()), start_s=.1, end_s=1.3)]),
            repair, render, inspect, advance, tool("timeline", "get_timeline"),
            tool("transcript", "read_transcript", asset_id=asset["asset_id"], start_s=0, end_s=8),
            "Saved and verified the two-clip export.",
        ]
        started = monotonic()
        run = start_turn(client, project, asset, "Create a faded two-clip export and inspect both sides of the cut.")
        harness, output = read_turn(client, run)
        turn_latency = monotonic() - started

        def self_repair():
            receipt = captured.get("edit", {})
            require(captured.get("recovery", {}).get("code") == "ASSET_NOT_FOUND", "Missing asset recovery")
            require(receipt.get("clip_count") == 2 and receipt.get("audio_fade_ms") == 300, "Incomplete edit receipt")
            require(all(clip["asset_id"] == asset["asset_id"] for clip in receipt["clips"]), "Asset resolution failed")
            failure, success = harness["tool_steps"][:2]
            require(success["step_index"] - failure["step_index"] == 1 and success["repaired"],
                    "The edit did not repair in one model turn")
            return f"2 clips, revision {receipt['revision_no']}, repaired in 1 turn ({turn_latency:.2f} s workflow)"

        def context():
            sheet = captured.get("sheet")
            require(isinstance(sheet, list) and len(sheet) == 2, "Expected one contact-sheet image")
            with Image.open(BytesIO(b64decode(sheet[1]["image_url"]["url"].split(",", 1)[1]))) as image:
                require(image.format == "JPEG" and image.size == (768, 216), "Unexpected grid layout")
            require(len(json.loads(sheet[0]["text"])["frames"]) == 2, "Contact sheet lacks both cut frames")
            events = harness["trace_events"]
            usage = harness["usage"]
            saved = 0
            for event in events:
                if event["kind"] == "COMPACTION":
                    sizes = re.search(r"(\d+) → (\d+) characters", event["summary"])
                    if sizes:
                        saved += int(sizes[1]) - int(sizes[2])
            later = model.requests[5]
            require(all(part.get("type") != "image_url" for msg in later if msg["role"] == "tool"
                        for part in (msg["content"] if isinstance(msg["content"], list) else [])),
                    "A historical image remained in the model request")
            require(usage["images_unloaded"] == 1 and usage["compactions"] >= 1 and saved > 0,
                    "History counters or compaction savings missing")
            require(sum(e["input_tokens"] for e in events) == usage["input_tokens"], "Token trace mismatch")
            baseline = list(model.requests[0])
            for response, request in zip(model.responses, model.requests[1:]):
                ids = {call["id"] for call in response["tool_calls"]}
                baseline.append(response)
                baseline.extend(msg for msg in request if msg.get("tool_call_id") in ids)
            final_saved = estimated_tokens(baseline) - estimated_tokens(model.requests[-1])
            require(final_saved > 0, "Final request did not save estimated prompt tokens")
            return (f"2 frames in 1 image, {usage['images_unloaded']} unloaded, "
                    f"{usage['compactions']} compactions, {final_saved} estimated final-turn tokens saved")

        def render_check():
            path = folder / "faded.mp4"
            require(bool(output.get("export_artifact_id")), "No export artifact was produced")
            download(client, output["export_artifact_id"], path)
            decode(path)
            check_fades(path, (1.2, 1.2))
            require(any(e["kind"] == "RENDER_WAIT" for e in harness["trace_events"]), "Render wait trace missing")
            return "300 ms fades verified at all four edges after full decode"

        report_row(results, "Self-guided timeline repair", self_repair)
        report_row(results, "Contact sheet and context compaction", context)
        report_row(results, "FFmpeg audio fades and decode", render_check)
    return ["Case", "Verification (s)", "Result detail", "Passed"], results


def live(folder, video, *, reuse, index_timeout):
    app = create_app(work_dir=folder)
    require(app.state.workflow_worker.harness.configuration() is not None, "Live model is not configured")
    results = []
    with TestClient(app) as client:
        project, asset = ingest(client, video, folder, reuse=reuse)
        wait_index(client, asset, index_timeout)
        _, evidence = app.state.processing.catalog.search_scope(
            asset["workspace_id"], project, [asset["media_revision_id"]],
        )
        dialogue = sorted((r for r in evidence if r["source"] == "embedded-subtitle" or
                           (r["modality"] == "ASR" and r["support"] == "SUPPORTED")),
                          key=lambda r: r["range"]["start_us"])
        chinese = [r for r in dialogue if re.search(r"[\u3400-\u9fff]", r["claim"])]
        first = next((r for r in chinese if "机器人" in r["claim"] and "太空" in r["claim"]),
                     chinese[0] if chinese else None)
        distractor = next((r for r in chinese if r is not first and "机器人" in r["claim"]), None)
        deep = next((r for r in dialogue if 900_000_000 <= r["range"]["start_us"] < 1_380_000_000
                     and sum(d["claim"] == r["claim"] for d in dialogue) == 1), None)

        def mentions_time(answer, time_us):
            stamp = time_us / 1e6
            minutes, seconds = divmod(stamp, 60)
            return re.search(rf"(?<![\d.]){stamp:g}(?!\d)|(?<!\d)0?{int(minutes)}:{int(seconds):02d}(?!\d)",
                             answer)

        def exact(target, excluded=None):
            def check(result):
                require(target is not None, "The input video has no benchmark dialogue in the required range")
                citations = result["citations"]
                require(any(
                    c["text"] == target["claim"] and c["start_us"] == target["range"]["start_us"]
                    and c["media_revision_id"] == asset["media_revision_id"] for c in citations
                ), "No citation matched the ground-truth transcript")
                answer = result["answer"]
                require(mentions_time(answer, target["range"]["start_us"]),
                        "The answer omitted the expected timestamp")
                if excluded:
                    require(excluded["claim"] not in answer, "The answer repeated the distractor")
            return check

        def revision(result):
            return body(client.get(f"/v1/edit-projects/{result['edit_project_id']}/revisions/{result['revision_id']}"))

        def export(result, format_name, *, muted=False):
            rev = revision(result)
            require(len(rev["clips"]) == 2 and rev["audio_fade_ms"] == 300, "Unexpected saved revision")
            if muted:
                require(all(c["audio_policy"] == "mute" for c in rev["clips"]), "Some clips are still audible")
                require([(c["media_revision_id"], c["source_start_us"], c["source_end_us"]) for c in rev["clips"]]
                        == [(c["media_revision_id"], c["source_start_us"], c["source_end_us"])
                            for c in original_clips], "Refinement changed the clip ranges or order")
            else:
                require([(c["source_start_us"], c["source_end_us"]) for c in rev["clips"]]
                        == [(4_000_000, 7_000_000), (27_000_000, 31_000_000)], "Wrong source ranges")
                require(all(c["audio_policy"] == "keep" for c in rev["clips"]), "Rough cut muted the source")
            path = folder / f"live-export.{format_name}"
            artifact = download(client, result["export_artifact_id"], path)
            require(artifact["mime_type"] == f"video/{format_name}", "Wrong export container")
            audio = any(s["codec_type"] == "audio" for s in decode(path))
            require(audio != muted, "Unexpected rendered audio stream layout")
            return rev["clips"]

        def run_case(case, prompt, check, **fields):
            started, harness, run_id = monotonic(), {}, None
            try:
                run_id = start_turn(client, project, asset, prompt, **fields)
                harness, result = read_turn(client, run_id)
                require(harness["status"] == "SUCCEEDED", f"Workflow ended {harness['status']}")
                check(result)
                detail, passed = "", "PASS"
            except Exception as exc:
                detail, passed = str(exc).splitlines()[0] or type(exc).__name__, "FAIL"
                if run_id and not harness:
                    response = client.get(f"/v1/workflow-runs/{run_id}/harness")
                    harness = response.json() if response.is_success else {}
            usage = harness.get("usage", {})
            metrics = [usage.get(key, 0) for key in (
                "model_calls", "tool_calls", "images_unloaded", "compactions", "input_tokens", "output_tokens",
            )]
            repairs = sum(s["repaired"] for s in harness.get("tool_steps", []))
            results.append((case, monotonic() - started, *metrics[:2], repairs, *metrics[2:],
                            f"{usage.get('estimated_cost_cny', 0):.6f}", detail, passed))
            return run_id if passed == "PASS" else None

        target = first["claim"] if first else "机器人想去太空"
        cue = deep["claim"] if deep else "黑衣人"
        run_case("Chinese dialogue and distractor",
                 f'找出原片中这句对白“{target}”的准确起始时间。只引用原句和出现时间，排除类似对白。',
                 exact(first, distractor))
        run_case("Deep retrieval at 15–23 minutes",
                 f'在 15:00 到 23:00 之间定位原片对白“{cue}”，引用原句的准确开始时间。',
                 exact(deep))
        original_clips = []

        def rough_cut(result):
            original_clips.extend(export(result, "mp4"))

        parent = run_case("Two-clip 300 ms fade and MP4",
                          "Create a timeline with source clips 4–7s and 27–31s in that order. "
                          "Keep source audio, use audio_fade_ms=300 and export MP4.", rough_cut)
        run_case("Muted WebM refinement",
                 "Keep the previous timeline's two clip ranges and order. Mute both clips and export WebM.",
                 lambda result: export(result, "webm", muted=True), **({"parent_run_id": parent} if parent else {}))
    return ["Case", "latency_s", "model_calls", "tool_calls", "self_repairs", "images_unloaded", "compactions",
            "input_tokens", "output_tokens", "cost_cny", "Detail", "passed"], results


def markdown(mode, headers, rows):
    def cell(value):
        return str(value).replace("|", "\\|").replace("\n", " ")

    def line(values):
        return "| " + " | ".join(map(cell, values)) + " |"

    lines = [f"# CutSieve harness evaluation ({mode})", "", line(headers), line(["---"] * len(headers))]
    lines.extend(line((case, f"{seconds:.3f}", *rest)) for case, seconds, *rest in rows)
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("offline", "live"), required=True)
    parser.add_argument("--video", type=Path, default=ROOT / "work/samples/blender_cinema_27m.mp4")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--work-dir", type=Path, help="Reuse an isolated live ingestion cache")
    parser.add_argument("--index-timeout", type=float, default=3600)
    args = parser.parse_args()
    if args.mode == "offline" and args.work_dir:
        parser.error("--work-dir is for live evaluation")
    if args.mode == "live":
        load_local_dotenv(ROOT / ".env")
        if not args.video.is_file():
            parser.error(f"Video does not exist: {args.video}")
    work_dir = nullcontext(args.work_dir) if args.work_dir else TemporaryDirectory(prefix="cutsieve-eval-")
    try:
        with work_dir as location:
            folder = Path(location).resolve()
            folder.mkdir(parents=True, exist_ok=True)
            headers, rows = (offline(folder) if args.mode == "offline" else
                             live(folder, args.video, reuse=bool(args.work_dir), index_timeout=args.index_timeout))
    except Exception as exc:
        print(f"Harness evaluation failed: {exc}", file=sys.stderr)
        return 1
    report = markdown(args.mode, headers, rows)
    if sys.stdout.encoding and not sys.stdout.encoding.lower().startswith("utf"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(report, end="")
    out = args.out or (Path("eval_harness_report.md") if args.mode == "live" else None)
    if out:
        out.write_text(report, encoding="utf-8")
    return int(any(row[-1] != "PASS" for row in rows))


if __name__ == "__main__":
    raise SystemExit(main())
