"""Correlate a local provider socket timeout and a real, fully decoded export."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
from threading import Event, Thread

from fastapi.testclient import TestClient
import pytest

from cutsieve.application.workflow_state import DEFAULT_BUDGET
from cutsieve.infrastructure.providers.workflow_flash import FlashPlanner
from cutsieve.presentation.api.app import create_app
from test_workspace_contract import json_body, prepare_workspace, wait_for


@contextmanager
def slow_endpoint():
    received, release = Event(), Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            received.set()
            release.wait(5)

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/v1", received
        finally:
            release.set()
            server.shutdown()
            thread.join()


def events(client, run):
    response = client.get(f"/v1/workflow-runs/{run}/events")
    response.raise_for_status()
    values = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
    assert all(v["run_id"] == run for v in values)
    return [v["payload"] for v in values]


@pytest.mark.real_media
def test_socket_timeout_and_decoded_export_have_correlated_durable_traces(tmp_path, monkeypatch):
    ffmpeg = os.getenv("CUTSIEVE_FFMPEG") or shutil.which("ffmpeg", path=os.getenv("CUTSIEVE_FFMPEG_DIR"))
    if not ffmpeg:
        pytest.skip("FFmpeg required")
    monkeypatch.setenv("CUTSIEVE_WORKFLOW_LIVE_MODEL", "0")
    root = tmp_path / "app"
    with TestClient(create_app(work_dir=root)) as client, slow_endpoint() as (url, received):
        movie = prepare_workspace(client, tmp_path, ffmpeg, "trace_editor")
        model = "deepseek-flash-local-test"
        planner = FlashPlanner({
            "CUTSIEVE_WORKFLOW_LIVE_MODEL": "1", "CUTSIEVE_MODEL_BASE_URL": url,
            "CUTSIEVE_MODEL_MODEL": "deepseek-flash", "CUTSIEVE_MODEL_API_KEY": "loopback-fixture-only",
            "CUTSIEVE_MODEL_VERSION": model, "CUTSIEVE_MODEL_NATIVE_TOOLS": "1",
            "CUTSIEVE_MODEL_TIMEOUT_SECONDS": "0.25", "CUTSIEVE_WORKSPACE_CNY_MAX_COST_MICROUNITS": "50000",
            "CUTSIEVE_FLASH_CNY_INPUT_MICROS_PER_MILLION": "200", "CUTSIEVE_FLASH_CNY_OUTPUT_MICROS_PER_MILLION": "600",
            "CUTSIEVE_FLASH_PRICE_VERSION": "fixture-price-2026",
        })
        client.app.state.workflow_worker.planner = planner
        failed = json_body(client.post("/v1/workflow-runs", json=dict(scope=movie["scope"], intent="EDIT",
            message="找人物犹豫后才开口的片段", budget=dict(DEFAULT_BUDGET, max_cost_microunits=50000),
            idempotency_key="timeout-audit")))["run_id"]
        state = wait_for(client, "/v1/workflow-runs/" + failed, {"QUEUED", "RUNNING", "WAITING_JOB"})
        assert received.is_set() and state["status"] == "FAILED" and state["error"]["code"] == "MODEL_TIMEOUT"
        assert state["error"]["retryable"]

    # Read the public trace and signed validation report on a fresh API instance.
    with TestClient(create_app(work_dir=root)) as client:
        timeout = events(client, failed)
        final = next(p for p in timeout if p.get("activity") == "model.failed")
        start = next(p for p in timeout if p.get("activity") == "model.reserved")
        usage = json_body(client.get("/v1/workflow-runs/" + failed))["budget_usage"]
        assert start["request_id"] == final["request_id"]
        assert final["resolved_model"] == model and final["error_code"] == "MODEL_TIMEOUT"
        assert final["latency_ms"] >= 200 and final["usage_known"] is False
        assert final["input_tokens"] is final["output_tokens"] is final["charged_microunits"] is None
        assert final["uncertain_microunits"] == start["reserved_microunits"] == usage["uncertain_microunits"] > 0
        assert final["price_version"] == start["price_version"] == usage["price_version"] == "fixture-price-2026"
        assert final["currency"] == usage["currency"] == "CNY"
        assert usage["model_calls"] == 1 and usage["reserved_microunits"] == usage["committed_microunits"] == 0

        render = events(client, movie["run"])
        done = next(p for p in render if p.get("activity") == "render.finished" and p["profile"] == "EXPORT")
        submit = next(p for p in render if p.get("activity") == "tool.finished" and p.get("render_job_id") == movie["job"])
        assert submit["tool"] == "render.submit" and submit["latency_ms"] >= 0
        assert submit["render_job_id"] == done["render_job_id"] == movie["job"]
        assert submit["revision_id"] == done["revision_id"] == movie["revision"]
        assert done["artifact_id"] == movie["export"]
        audit = json_body(client.get("/v1/artifacts/" + done["validation_report_id"]))
        report = json_body(client.get(audit["url"]))
        assert report["full_decode"] and report["render_latency_ms"] > 0
        assert report["render_job_id"] == movie["job"] and report["revision_id"] == movie["revision"]
        assert json_body(client.get("/v1/workflow-runs/" + movie["run"]))["budget_usage"]["model_calls"] == 0
        if dest := os.getenv("CUTSIEVE_TEST_TRACE_OUTPUT"):
            proof = dict(external_model_calls=0, loopback_http_calls=1,
                         timeout=dict(run_id=failed, reserved=start, settlement=final, usage=usage),
                         render=dict(run_id=movie["run"], submission=submit, completion=done,
                                     render_latency_ms=report["render_latency_ms"], full_decode=True))
            Path(dest).write_text(json.dumps(proof, ensure_ascii=False, indent=2), encoding="utf8")
