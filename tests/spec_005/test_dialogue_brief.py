"""Ground an edit in real subtitle cues and verify the source audio in the export."""

import json
from array import array
from math import sqrt
import os
from pathlib import Path
import shutil
import subprocess

import pytest
from fastapi.testclient import TestClient

from cutsieve.application.edit_timeline import EditError
from cutsieve.application.evidence_search import exclude_subtitles
from cutsieve.presentation.api.app import create_app


def test_subtitle_exclusions_use_covered_half_open_intervals():
    hits = [dict(media_revision_id="m", range=dict(start_us=n, end_us=n + 10)) for n in (0, 10, 20, 30)]
    row = dict(
        source="embedded-subtitle", media_revision_id="m", claim="EXCLUDE", range=dict(start_us=19, end_us=20)
    )
    covered = {"m": [dict(start_us=0, end_us=30)]}
    assert exclude_subtitles(hits, ["exclude"], [row], covered) == [hits[0], hits[2]]
    with pytest.raises(EditError, match="缺少.*字幕"):
        exclude_subtitles(hits, ["exclude"], [], {})


@pytest.mark.real_media
def test_subtitle_selection_keeps_source_speech(tmp_path, fake_agent, wait_state):
    ffmpeg = os.getenv("CUTSIEVE_FFMPEG") or shutil.which("ffmpeg", path=os.getenv("CUTSIEVE_FFMPEG_DIR"))
    if not ffmpeg:
        pytest.skip("FFmpeg required")

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
    source = tmp_path / "source.mp4"
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
    with TestClient(create_app(work_dir=tmp_path / "app", agent_transport=fake_agent)) as client:

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
            )

        asset = upload(source)
        jobs = client.get(f"/v1/assets/{asset['asset_id']}/processing-runs").json()["items"]
        wait_state(client, "/v1/processing-runs/" + jobs[0]["processing_run_id"], ("SUCCEEDED", "PARTIAL"))
        revision = asset["media_revision_id"]
        tool, reply = fake_agent.tool, fake_agent.reply

        def select_spoken_intervals(request):
            hits = json.loads(request["messages"][-1]["content"])["hits"]
            clips = [
                dict(media_revision_id=revision, start_s=hit["start_s"], end_s=hit["end_s"],
                     evidence_ids=[hit["evidence_id"]])
                for hit in hits if hit["source"] == "embedded-subtitle" and "spoiler" not in hit["text"]
            ]
            assert len(clips) == 2
            return reply(tool("cut", "edit_timeline", dict(clips=clips)), tool("render", "render_video", dict(profile="EXPORT")))

        fake_agent.responses = [
            reply(tool("subtitle", "search_evidence", dict(query='subtitle: "Ticket"', mode="ALL"))),
            select_spoken_intervals,
            reply(text="已导出两个包含 Ticket 的片段，保留原声。"),
        ]
        instruction = "总时长2.5秒，保留讲话，包含字幕「Ticket」，排除字幕「spoiler」，并导出"
        job = post(f"/v1/projects/{project}/conversation-runs", media_revision_id=revision, message=instruction,
                   idempotency_key="edit-spoken-lines")
        path = "/v1/workflow-runs/" + job["run_id"]
        wait_state(client, path)
        result = client.get(path + "/result").json()
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
