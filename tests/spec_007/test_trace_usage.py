"""Exercise the real aiohttp wire protocol, retry, and durable usage telemetry."""

import json
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Thread

import pytest
from fastapi.testclient import TestClient

from cutsieve.application.agent_harness import decode_assistant
from cutsieve.application.edit_timeline import EditError
from cutsieve.infrastructure.providers.config import ProviderConfig
from cutsieve.presentation.api.app import create_app


@contextmanager
def model_endpoint():
    payloads, release = [], Event()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            payloads.append((self.path, self.headers.get("Authorization"), request))
            if len(payloads) == 1:
                release.wait(5)
                return
            body = json.dumps(dict(
                model="provider/deepseek-v4-flash-2026-10-25",
                choices=[dict(message=dict(role="assistant", content="The model is responding."))],
                usage=dict(prompt_tokens=120, completion_tokens=40),
            )).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}/v1", payloads
        finally:
            release.set()
            server.shutdown()
            thread.join()


def create_run(client):
    project = client.post("/v1/projects", json={"title": "Telemetry"}).json()["project_id"]
    response = client.post(f"/v1/projects/{project}/conversation-runs",
                           json=dict(message="Are you responding?", idempotency_key="model-telemetry"))
    assert response.status_code == 202
    return "/v1/workflow-runs/" + response.json()["run_id"]


def test_native_aiohttp_timeout_then_failed_resume_without_cost_gate(tmp_path, wait_state, workflow_events):
    with model_endpoint() as (url, requests):
        app = create_app(work_dir=tmp_path)
        config = dict(
            CUTSIEVE_MODEL_BASE_URL=url,
            CUTSIEVE_MODEL_API_KEY="fixture-key",
            CUTSIEVE_MODEL_MODEL="deepseek-v4-flash",
            CUTSIEVE_MODEL_TIMEOUT_SECONDS="0.3",
            CUTSIEVE_CHAT_LIVE_MODEL="0",
            CUTSIEVE_CHAT_MAX_COST_MICROUNITS="0",
            CUTSIEVE_FLASH_CNY_INPUT_MICROS_PER_MILLION="1000000",
            CUTSIEVE_FLASH_CNY_OUTPUT_MICROS_PER_MILLION="2000000",
        )
        app.state.workflow_worker.harness.env = config
        with TestClient(app) as client:
            path = create_run(client)
            failed = wait_state(client, path, ("FAILED",))
            assert failed["error"]["code"] == "MODEL_TIMEOUT" and failed["error"]["retryable"]
            assert failed["usage"]["model_calls"] == 1 and failed["usage"]["input_tokens"] == 0
            assert workflow_events(client, path)[-1]["type"] == "RUN_FAILED"

        restarted = create_app(work_dir=tmp_path)
        restarted.state.workflow_worker.harness.env = config
        with TestClient(restarted) as client:
            retry = client.post(path + "/resume", json=dict(
                expected_state_version=failed["state_version"], message="Please retry.", idempotency_key="retry-model",
            ))
            assert retry.status_code == 202
            state = wait_state(client, path)
            assert state["usage"]["model_calls"] == 2
            assert state["usage"]["input_tokens"] == 120 and state["usage"]["output_tokens"] == 40
            assert state["usage"]["estimated_cost_cny"] == pytest.approx(.0002)
            assert state["budget_usage"]["committed_microunits"] == 200
            assert workflow_events(client, path, failed["last_event_id"])[-1]["type"] == "RUN_COMPLETED"
        assert len(requests) == 2
        for path, auth, payload in requests:
            assert path == "/v1/chat/completions" and auth == "Bearer fixture-key"
            assert payload["model"] == "deepseek-v4-flash" and len(payload["tools"]) == 8
            assert "response_format" not in payload
        assert requests[1][2]["messages"][-1] == dict(role="user", content="Please retry.")


@pytest.mark.parametrize("env", [{}, dict(CUTSIEVE_MODEL_BASE_URL="http://localhost/v1", CUTSIEVE_WORKFLOW_LIVE_MODEL="0")])
def test_model_not_configured_error_is_actionable(tmp_path, wait_state, env):
    app = create_app(work_dir=tmp_path)
    app.state.workflow_worker.harness.env = env
    with TestClient(app) as client:
        state = wait_state(client, create_run(client), ("FAILED",))
        assert state["error"]["code"] == "MODEL_NOT_CONFIGURED"
        assert "CUTSIEVE_MODEL_BASE_URL" in state["error"]["message"]
        assert state["awaiting_input"] is None and state["usage"]["model_calls"] == 0


def test_configured_request_options_and_model_alias(tmp_path, fake_agent, wait_state):
    app = create_app(work_dir=tmp_path, agent_transport=fake_agent)
    app.state.workflow_worker.harness.env = dict(
        CUTSIEVE_MODEL_BASE_URL="https://api.deepseek.com/v1",
        CUTSIEVE_MODEL_API_KEY="fake-key", CUTSIEVE_MODEL_MODEL="deepseek-v4-flash",
    )
    fake_agent.responses = [fake_agent.reply(text="Ready.")]
    with TestClient(app) as client:
        wait_state(client, create_run(client))
    assert fake_agent.requests[0]["thinking"] == {"type": "disabled"}
    config = ProviderConfig("http://localhost/v1", "alias", response_model="vendor/deepseek-v4-flash")
    with pytest.raises(EditError) as error:
        decode_assistant({"model": "deepseek-v4-pro"}, config)
    assert error.value.code == "MODEL_MISMATCH"
