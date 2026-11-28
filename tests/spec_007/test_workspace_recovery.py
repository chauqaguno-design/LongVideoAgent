"""Restore source bytes, pinned evidence, timeline and render into an empty directory."""
import hashlib
import json
import os
import shutil
import subprocess
import time

import pytest
from fastapi.testclient import TestClient

from cutsieve.presentation.api.app import create_app
from scripts.workspace_snapshot import backup, restore


@pytest.mark.real_media
def test_restore_and_render(tmp_path, fake_agent, wait_state):
    ffmpeg = shutil.which("ffmpeg", path=os.getenv("CUTSIEVE_FFMPEG_DIR"))
    if not ffmpeg:
        pytest.skip("Release recovery requires FFmpeg")
    film, subs = tmp_path / "fixture.mp4", tmp_path / "line.srt"
    subs.write_text("1\n00:00:00,200 --> 00:00:01,600\nWhere is my ticket?\n", encoding="utf-8")
    subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "color=red:size=96x64:rate=25:duration=2",
                    "-i", str(subs), "-c:v", "libx264", "-c:s", "mov_text", str(film)], check=True)
    data = film.read_bytes()

    def post(c, path, payload):
        r = c.post("/v1" + path, json=payload)
        r.raise_for_status()
        return r.json()

    def wait(c, path):
        for _ in range(300):
            run = c.get("/v1" + path).json()
            if run["status"] in {"SUCCEEDED", "FAILED", "PARTIAL", "AWAITING_INPUT"}:
                return run
            time.sleep(.1)
        raise TimeoutError(path)

    src = tmp_path / "source"
    with TestClient(create_app(work_dir=src, agent_transport=fake_agent)) as c:
        pid = post(c, "/projects", {"title": "恢复演练"})["project_id"]
        upload = post(c, f"/projects/{pid}/assets", dict(filename="story.mp4", size_bytes=len(data), content_type="video/mp4"))
        etag = c.put(upload["parts"][0]["url"], content=data).headers["ETag"]
        asset = post(c, f"/uploads/{upload['upload_id']}/complete", dict(idempotency_key="complete-source",
                     parts=[dict(part_number=1, etag=etag)]))
        index = c.get(f"/v1/assets/{asset['asset_id']}/processing-runs").json()["items"][0]["processing_run_id"]
        wait_state(c, "/v1/processing-runs/" + index, ("SUCCEEDED", "PARTIAL"))
        scope = dict(project_id=pid, media_revision_ids=[asset["media_revision_id"]])
        tool, reply = fake_agent.tool, fake_agent.reply

        def cut(request):
            hits = json.loads(request["messages"][-1]["content"])["hits"]
            hit = next(h for h in hits if h["source"] == "embedded-subtitle")
            return reply(tool("edit", "edit_timeline", dict(clips=[
                dict(start_s=hit["start_s"], end_s=hit["end_s"], evidence_ids=[hit["evidence_id"]]),
            ])), tool("preview", "render_video"))

        fake_agent.responses = [
            reply(tool("lookup", "search_evidence", dict(query='"Where is my ticket?"'))),
            cut, reply(text="预览已生成。"),
        ]
        workflow = post(c, "/workflow-runs", dict(scope=scope, intent="EDIT", message="剪出台词「Where is my ticket?」",
                        idempotency_key="restore-preview"))
        rid = workflow["run_id"]
        assert wait(c, "/workflow-runs/" + rid)["status"] == "SUCCEEDED"
        plan = c.get(f"/v1/workflow-runs/{rid}/result").json()
        old = c.get("/v1/artifacts/" + plan["preview_artifact_id"]).json()
        assert c.get(old["url"]).status_code == 200
    target, bundle = tmp_path / "restored", tmp_path / "backup"
    count = backup(src, bundle)
    assert restore(bundle, target) == count

    with TestClient(create_app(work_dir=target)) as c:
        assert c.get(f"/v1/workflow-runs/{rid}/result").json() == plan
        assert c.get(old["url"]).status_code in {403, 404}
        new = c.get("/v1/artifacts/" + old["artifact_id"]).json()
        assert hashlib.sha256(c.get(new["url"]).content).hexdigest() == old["sha256"]
        found = post(c, "/search", dict(scope=scope, text="Where is my ticket?", query_mode="LOCAL", top_k=5))
        assert found["hits"]
        assert any("ticket" in c.get("/v1/evidence/" + eid).json()["claim"] for hit in found["hits"] for eid in hit["evidence_ids"])
        export = post(c, "/render-jobs", dict(revision_id=plan["revision_id"], profile="EXPORT", idempotency_key="after-restore"))
        assert wait(c, "/render-jobs/" + export["render_job_id"])["status"] == "SUCCEEDED"
    # A damaged backup cannot replace a workspace or leave a half-visible restore.
    source_blob = next((bundle / "files/editing/sources").glob("*/*"))
    source_blob.write_bytes(b"damaged fixture")
    with pytest.raises(ValueError, match="Checksum mismatch"):
        restore(bundle, tmp_path / "bad-restore")
    assert not (tmp_path / "bad-restore").exists()
