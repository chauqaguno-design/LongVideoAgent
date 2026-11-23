"""Restore a stopped workspace into a separate MySQL database, bucket, Redis DB and decoder cache."""
from dataclasses import replace
from hashlib import sha256
import os
from pathlib import Path
import re
import subprocess
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from minio.commonconfig import CopySource
from sqlalchemy.engine import make_url

from dovideo.infrastructure import R2Settings, create_r2_infrastructure
from dovideo.presentation.api.app import create_app
from dovideo.presentation.api.r2_runtime import ProductionR2Services

pytestmark = pytest.mark.skipif(not os.getenv("VIDEOMIND_FIXTURE_MYSQL"), reason="requires isolated R2 snapshot fixture")


def test_restore_evidence_and_export_without_old_cache(tmp_path):
    container = os.environ["VIDEOMIND_FIXTURE_MYSQL"]
    assert re.fullmatch(r"videomind-accept-[a-f0-9]{10}-mysql", container)
    source = replace(R2Settings.from_environment(), media_workspace=tmp_path / "source")
    restore_db = "vm_restore_" + uuid4().hex[:10]
    url = make_url(source.database_url).set(database=restore_db)
    target = replace(source, database_url=url.render_as_string(hide_password=False), minio_bucket="restore-" + uuid4().hex,
                     redis_url=source.redis_url.rsplit("/", 1)[0] + "/8", media_workspace=tmp_path / "restored")
    auth = dict(username="restore_" + uuid4().hex[:8], password=uuid4().hex)

    def client(settings):
        return TestClient(create_app(services=ProductionR2Services(create_r2_infrastructure(settings))))

    def login(c):
        c.headers["Authorization"] = "Bearer " + c.post("/user/login", json=auth).json()["data"]["token"]

    def post(c, path, **payload):
        reply = c.post("/v1" + path, json=payload)
        reply.raise_for_status()
        return reply.json()

    def wait(c, path):
        for _ in range(300):
            run = c.get("/v1" + path).json()
            if run["status"] not in {"RUNNING", "QUEUED", "WAITING_JOB", "VALIDATING"}:
                return run
            time.sleep(.1)
        raise TimeoutError(path)

    subtitle = tmp_path / "line.srt"
    subtitle.write_text("1\n00:00:00,200 --> 00:00:01,400\nWhere is my ticket?\n", encoding="utf8")
    movie = tmp_path / "story.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=blue:size=96x64:rate=25:d=2",
                    "-i", str(subtitle), "-c:v", "libx264", "-c:s", "mov_text", str(movie)], check=True)
    with client(source) as c:
        c.post("/user/register", json=dict(auth, nickname="restore")).raise_for_status()
        login(c)
        project = post(c, "/projects", title="Restore")["project_id"]
        upload = post(c, f"/projects/{project}/assets", filename=movie.name, size_bytes=movie.stat().st_size,
                      content_type="video/mp4")
        etag = c.put(upload["parts"][0]["url"], content=movie.read_bytes()).headers["ETag"]
        asset = post(c, "/uploads/" + upload["upload_id"] + "/complete", idempotency_key="restore-film",
                     parts=[dict(part_number=1, etag=etag)])
        scope = dict(project_id=project, media_revision_ids=[asset["media_revision_id"]])
        run = post(c, "/workflow-runs", scope=scope, intent="EDIT", message="剪出台词「Where is my ticket?」",
                   idempotency_key="restore-quote")
        base = "/workflow-runs/" + run["run_id"]
        assert wait(c, base)["status"] == "SUCCEEDED"
        plan = c.get("/v1" + base + "/result").json()
        preview = c.get("/v1/artifacts/" + plan["preview_artifact_id"]).json()
        assert sha256(c.get(preview["url"]).content).hexdigest() == preview["sha256"]
        png = subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(movie), "-frames:v", "1",
                                       "-c:v", "png", "-f", "image2pipe", "pipe:1"])
        image = c.post(f"/v1/projects/{project}/query-images", files={"file": ("source.png", png, "image/png")}).json()
        count_run = post(c, f"/projects/{project}/search-workflows", idempotency_key="restore-count",
                         search=dict(scope=scope, text="字幕：Where is my ticket?", query_mode="COUNT", top_k=1))
        count_url = "/workflow-runs/" + count_run["run_id"]
        assert wait(c, count_url)["status"] == "SUCCEEDED"
        result_id = c.get("/v1" + count_url + "/result").json()["complete_event_list_artifact_id"]

    # The fixture's application is stopped. Take a consistent SQL dump plus an independent bucket copy.
    mysql = ["docker", "exec", "-i", container, "sh", "-c"]
    dump = subprocess.check_output([*mysql, 'MYSQL_PWD="$MYSQL_PASSWORD" exec mysqldump -u "$MYSQL_USER" '
                                    '--single-transaction --no-tablespaces "$MYSQL_DATABASE"'])
    grant = f"CREATE DATABASE {restore_db}; GRANT ALL ON {restore_db}.* TO 'vmaccept'@'%';"
    subprocess.run([*mysql, 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" exec mysql -uroot'], input=grant.encode(), check=True)
    subprocess.run([*mysql, f'MYSQL_PWD="$MYSQL_PASSWORD" exec mysql -u "$MYSQL_USER" {restore_db}'],
                   input=dump, check=True)
    infra = create_r2_infrastructure(target)
    try:
        objects = infra.minio_client
        objects.make_bucket(target.minio_bucket)
        for item in objects.list_objects(source.minio_bucket, recursive=True):
            objects.copy_object(target.minio_bucket, item.object_name, CopySource(source.minio_bucket, item.object_name))
    finally:
        infra.close()

    with client(target) as c:
        assert c.get("/v1" + base + "/result").status_code == 401
        login(c)
        assert c.get("/v1" + base + "/result").json() == plan
        restored = c.get("/v1/artifacts/" + preview["artifact_id"]).json()
        assert sha256(c.get(restored["url"]).content).hexdigest() == preview["sha256"]
        search = post(c, "/search", scope=scope, text="Where is my ticket?", query_mode="LOCAL", top_k=5)
        proof = search["hits"][0]["evidence_ids"][0]
        assert "ticket" in c.get("/v1/evidence/" + proof).json()["claim"]
        restored_image = c.get("/v1/artifacts/" + image["artifact_id"]).json()
        assert c.get(restored_image["url"]).content == png
        locations = c.get(f"/v1/projects/{project}/query-results/{result_id}/occurrences").json()
        assert len(locations["items"]) == 1 and "ticket" in locations["items"][0]["text"]
        export = post(c, "/render-jobs", revision_id=plan["revision_id"], profile="EXPORT", idempotency_key="restore-export")
        job = wait(c, "/render-jobs/" + export["render_job_id"])
        assert job["status"] == "SUCCEEDED", job
        meta = c.get("/v1/artifacts/" + job["validation_report_id"]).json()
        assert c.get(meta["url"]).json()["full_decode"]
