"""Cross-node assertions shared by the isolated MySQL/MinIO media journey."""
from hashlib import sha256
import time
from uuid import uuid4


def create_render(client, project, asset):
    def post(path, **body):
        response = client.post("/v1" + path, json=dict(body, idempotency_key=str(uuid4())))
        response.raise_for_status()
        return response.json()

    edit = post("/edit-projects", project_id=project, title="跨节点导出",
                output_profile=dict(width=96, height=64, fps_num=25, fps_den=1, audio_sample_rate=48000))
    clip = dict(media_revision_id=asset["media_revision_id"], source_start_us=0, source_end_us=800000,
                audio_policy="keep", evidence_ids=[])
    revision = post("/edit-projects/" + edit["edit_project_id"] + "/operations", expected_revision=0,
                    reason="截取开头", operations=[{"op": "append_clip", "clip": clip}])
    job = post("/render-jobs", revision_id=revision["revision_id"], profile="EXPORT")
    for _ in range(200):
        job = client.get("/v1/render-jobs/" + job["render_job_id"]).json()
        if job["status"] in {"SUCCEEDED", "FAILED"}:
            break
        time.sleep(.15)
    assert job["status"] == "SUCCEEDED", job
    return job


def check_render(client, job):
    worker = client.app.state.edit_worker
    for field in ("artifact_id", "compiled_manifest_artifact_id", "validation_report_id"):
        artifact = client.get("/v1/artifacts/" + job[field]).json()
        row = worker.artifact_record(artifact["workspace_id"], job[field])
        cache = worker.root / row.relative_path
        assert not cache.exists()
        data = client.get(artifact["url"])
        data.raise_for_status()
        assert sha256(data.content).hexdigest() == artifact["sha256"]
        assert cache.is_file()
        if field == "validation_report_id":
            assert data.json()["full_decode"] and data.json()["duration_us"] == 800000
