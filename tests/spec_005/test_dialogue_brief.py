"""AT-025 exercises the public edit workflow with a known spoken word and timestamped captions."""

from array import array
from math import sqrt
import os
from pathlib import Path
import shutil
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from cutsieve.application.edit_instructions import literal_plan
from cutsieve.application.evidence_search import exclude_subtitles
from cutsieve.application.workflow_state import NeedsInput
from cutsieve.presentation.api.app import create_app


def test_subtitle_exclusions_use_covered_half_open_intervals():
    plan = literal_plan("字幕必须包含「Ticket」，不得包含字幕「spoiler」，目标时长2秒")
    assert plan["query"] == "Ticket" and plan["must_exclude"] == ["spoiler"]
    hits = [dict(media_revision_id="m", range=dict(start_us=n, end_us=n + 10)) for n in (0, 10, 20, 30)]
    row = dict(
        source="embedded-subtitle", media_revision_id="m", claim="EXCLUDE", range=dict(start_us=19, end_us=20)
    )
    covered = {"m": [dict(start_us=0, end_us=30)]}
    assert exclude_subtitles(hits, ["exclude"], [row], covered) == [hits[0], hits[2]]
    with pytest.raises(NeedsInput, match="缺少.*字幕"):
        exclude_subtitles(hits, ["exclude"], [], {})
    assert literal_plan("包含字幕「Ticket」，排除字幕「end」，总时长2秒，保留讲话并移除所有红色物体") is None


@pytest.mark.real_media
def test_combined_brief_selects_supported_dialogue_and_reports_conflicts(tmp_path, monkeypatch):
    ffmpeg = os.getenv("CUTSIEVE_FFMPEG") or shutil.which("ffmpeg", path=os.getenv("CUTSIEVE_FFMPEG_DIR"))
    if not ffmpeg:
        pytest.skip("FFmpeg required")
    monkeypatch.setenv("CUTSIEVE_WORKFLOW_LIVE_MODEL", "0")

    def ff(*args):
        return subprocess.run(
            [ffmpeg, "-v", "error", "-nostdin", *args], check=True, stdout=subprocess.PIPE
        ).stdout

    # ticket.flac contains one locally synthesized utterance of “Ticket”. Repeat the source voice.
    speech = Path(__file__).parent / "fixtures/ticket.flac"
    captions = tmp_path / "truth.srt"
    captions.write_text(
        "1\n00:00:00,000 --> 00:00:02,000\nTicket [spoiler]\n\n"
        "2\n00:00:02,000 --> 00:00:03,000\nTicket.\n\n"
        "3\n00:00:03,000 --> 00:00:04,000\nTicket!\n",
        encoding="utf8",
    )
    source, silent = tmp_path / "source.mp4", tmp_path / "silent.mp4"
    ff(
        "-f",
        "lavfi",
        "-i",
        "testsrc2=s=128x72:r=25:d=5",
        "-i",
        str(speech),
        "-i",
        str(captions),
        "-map",
        "0:v",
        "-map",
        "1:a",
        "-map",
        "2:s",
        "-af",
        "apad,atrim=end_sample=16000,aloop=loop=4:size=16000",
        "-c:v",
        "libx264",
        "-c:a",
        "aac",
        "-c:s",
        "mov_text",
        str(source),
    )
    ff("-i", str(source), "-map", "0:v", "-map", "0:s", "-c", "copy", str(silent))

    with TestClient(create_app(work_dir=tmp_path / "app")) as client:

        def post(path, **body):
            response = client.post(path, json=body)
            assert response.is_success, response.text
            return response.json()

        project = post("/v1/projects", title="字幕筛选")["project_id"]

        def upload(path):
            data = path.read_bytes()
            asset = post(
                f"/v1/projects/{project}/assets",
                filename=path.name,
                size_bytes=len(data),
                content_type="video/mp4",
            )
            put = client.put(asset["parts"][0]["url"], content=data)
            return post(
                f"/v1/uploads/{asset['upload_id']}/complete",
                idempotency_key="complete-" + path.stem,
                parts=[dict(part_number=1, etag=put.headers["ETag"])],
            )["media_revision_id"]

        def edit(revision, text, key):
            job = post(
                "/v1/workflow-runs",
                scope=dict(project_id=project, media_revision_ids=[revision]),
                message=text,
                intent="EDIT",
                idempotency_key="edit-brief-" + key,
            )
            path = "/v1/workflow-runs/" + job["run_id"]
            for _ in range(400):
                state = client.get(path).json()
                if state["status"] in {"SUCCEEDED", "AWAITING_INPUT", "FAILED"}:
                    assert state["status"] != "FAILED", state
                    assert state["budget_usage"]["model_calls"] == 0
                    return state, (
                        client.get(path + "/result").json() if state["status"] == "SUCCEEDED" else None
                    )
                time.sleep(0.1)
            pytest.fail("The edit workflow did not reach a terminal or clarification state")

        revision = upload(source)
        instruction = "总时长2.5秒，保留讲话，包含字幕「Ticket」，排除字幕「spoiler」，并导出"
        state, result = edit(revision, instruction, "combined")
        assert state["status"] == "SUCCEEDED", state
        brief = result["brief"]
        assert brief["duration_target_us"] == 2_500_000 and brief["duration_tolerance_us"] == 2_000_000
        assert brief["audio_policy"] == "KEEP_SOURCE"
        assert (
            brief["must_include"] == ["Ticket"]
            and brief["must_exclude"] == ["spoiler"]
            and brief["goal"] == instruction
        )
        timeline = client.get(
            f"/v1/edit-projects/{result['edit_project_id']}/revisions/{result['revision_id']}"
        ).json()
        assert [(v["source_start_us"], v["source_end_us"]) for v in timeline["clips"]] == [
            (2_000_000, 3_000_000),
            (3_000_000, 4_000_000),
        ]
        assert timeline["total_duration_us"] == 2_000_000
        for clip in timeline["clips"]:
            assert clip["audio_policy"] == "keep" and clip["evidence_ids"]
            evidence = client.get("/v1/evidence/" + clip["evidence_ids"][0]).json()
            assert evidence["media_revision_id"] == revision and evidence["support"] == "SUPPORTED"
            assert evidence["claim"] in ("Ticket.", "Ticket!")
        export = client.get("/v1/artifacts/" + result["export_artifact_id"]).json()
        output = tmp_path / "selection.mp4"
        output.write_bytes(client.get(export["url"]).content)
        ff("-xerror", "-i", str(output), "-f", "null", "-")

        def envelope(path, start):
            pcm = array(
                "h",
                ff(
                    "-ss",
                    str(start),
                    "-i",
                    str(path),
                    "-t",
                    "2",
                    "-vn",
                    "-ar",
                    "16000",
                    "-ac",
                    "1",
                    "-f",
                    "s16le",
                    "-",
                ),
            )
            return [sqrt(sum(v * v for v in pcm[i : i + 320]) / 320) for i in range(0, 32000, 320)]

        original, rendered = envelope(source, 2), envelope(output, 0)
        for a, b in ((original[:50], rendered[:50]), (original[50:], rendered[50:])):
            assert max(a) > 200 and max(b) > 200
            assert (
                sum(x * y for x, y in zip(a, b)) / sqrt(sum(x * x for x in a) * sum(y * y for y in b)) > 0.95
            )

        for text, media, key, reason in (
            (instruction.replace("2.5秒", "5秒"), revision, "duration", "目标 5 秒"),
            (
                instruction.replace("排除字幕「spoiler」", "排除字幕「Ticket」"),
                revision,
                "impossible",
                "包含了要求排除",
            ),
            (
                instruction.replace("「Ticket」，排除字幕「spoiler」", "「spoiler」，排除字幕「Ticket」"),
                revision,
                "overlap",
                "均与要求排除的字幕重叠",
            ),
            (instruction, upload(silent), "silent", "没有原音轨"),
            (
                instruction.replace("包含字幕", "包含台词").replace("排除字幕", "排除台词"),
                revision,
                "audible-exclusion",
                "排除声音或画面",
            ),
        ):
            state, result = edit(media, text, key)
            assert state["status"] == "AWAITING_INPUT" and result is None
            assert reason in state["awaiting_input"]["question"]
