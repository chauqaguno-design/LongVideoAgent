"""Verify a dialogue search, an evidence-backed answer and a playable edit on a local deployment."""

import argparse
import hashlib
import json
import os
import time
from pathlib import Path
from uuid import uuid4

import httpx


def verify(client, project, revision, quote, output, screenshot=None):
    def api(method, path, **kwargs):
        response = client.request(method, "/v1" + path, **kwargs)
        response.raise_for_status()
        return response.json()

    def artifact(key):
        item = api("GET", "/artifacts/" + key)
        response = client.get(item["url"])
        response.raise_for_status()
        assert hashlib.sha256(response.content).hexdigest() == item["sha256"]
        return response.content

    scope = dict(project_id=project, media_revision_ids=[revision])
    search = api("POST", "/search", json=dict(scope=scope, text=quote, query_mode="LOCAL", top_k=10))
    matches = []
    for hit in search["hits"]:
        for key in hit["evidence_ids"]:
            item = api("GET", "/evidence/" + key)
            if quote.casefold() in item["claim"].casefold():
                matches.append(dict(text=item["claim"], range=item["range"], evidence_id=key))
    if not matches:
        raise ValueError("Quote was not found in source evidence")

    def workflow(intent, message):
        job = api(
            "POST",
            "/workflow-runs",
            json=dict(
                scope=scope, intent=intent, message=message, idempotency_key="film-journey-" + str(uuid4())
            ),
        )
        url = "/workflow-runs/" + job["run_id"]
        deadline = time.monotonic() + 180
        while job["status"] in ("QUEUED", "RUNNING", "WAITING_JOB") and time.monotonic() < deadline:
            time.sleep(0.4)
            job = api("GET", url)
        if job["status"] != "SUCCEEDED":
            raise RuntimeError(f"{intent}: {job['status']} {job['error'] or job['awaiting_input']}")
        return job, api("GET", url + "/result")

    qa, answer = workflow("QUESTION", f"哪一段说了「{quote}」？")
    edit, plan = workflow("EDIT", f"剪出台词「{quote}」并保留原声，并导出")
    rendered = api("GET", "/render-jobs/" + plan["render_job_id"])
    assert rendered["status"] == "SUCCEEDED" and rendered["revision_id"] == plan["revision_id"]
    timeline = api("GET", "/edit-projects/" + plan["edit_project_id"] + "/revisions/" + plan["revision_id"])
    assert any(set(c["evidence_ids"]) & {m["evidence_id"] for m in matches} for c in timeline["clips"])
    assert answer["claims"] and any(c["evidence_ids"] for c in answer["claims"])
    output.mkdir(parents=True, exist_ok=True)
    exported = api("GET", "/render-jobs/" + plan["export_job_id"])
    assert exported["profile"] == "EXPORT" and exported["revision_id"] == rendered["revision_id"]
    video = artifact(plan["export_artifact_id"])
    (output / "dialogue.mp4").write_bytes(video)
    validation = json.loads(artifact(plan["export_validation_report_id"]))
    assert validation["full_decode"]
    report = dict(
        matches=matches,
        answer=answer,
        edit=plan,
        duration_us=validation["duration_us"],
        export_sha256=hashlib.sha256(video).hexdigest(),
        full_decode=validation["full_decode"],
        project_id=project,
        qa_run=qa["run_id"],
        edit_run=edit["run_id"],
        model_calls=qa["budget_usage"]["model_calls"] + edit["budget_usage"]["model_calls"],
    )
    if screenshot:
        with screenshot.open("rb") as file:
            ref = api("POST", f"/projects/{project}/query-images", files={"file": (screenshot.name, file)})
        image = api(
            "POST",
            "/search",
            json=dict(scope=scope, query_mode="LOCAL", top_k=5, image_artifact_id=ref["artifact_id"]),
        )
        assert image["hits"]
        report["screenshot_hits"] = image["hits"]
    (output / "journey.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--project", required=True)
    p.add_argument("--revision", required=True)
    p.add_argument("--quote", required=True)
    p.add_argument("--screenshot", type=Path)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--port", type=int, default=8173)
    args = p.parse_args()
    with httpx.Client(base_url=f"http://127.0.0.1:{args.port}", trust_env=False, timeout=45) as client:
        auth = client.post(
            "/user/login",
            json=dict(
                username=os.environ["VIDEOMIND_TEST_USER"], password=os.environ["VIDEOMIND_TEST_PASSWORD"]
            ),
        )
        auth.raise_for_status()
        client.headers["Authorization"] = "Bearer " + auth.json()["data"]["token"]
        result = verify(client, args.project, args.revision, args.quote, args.output, args.screenshot)
        print(f"Verified dialogue edit: {result['duration_us']} us; model calls: {result['model_calls']}")
