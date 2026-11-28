import json
import shutil
import subprocess
import time
from contextlib import contextmanager
from copy import deepcopy

import pytest

from cutsieve.infrastructure.edit_media import media_binaries


@pytest.fixture
def use_workspace(monkeypatch):
    from cutsieve.presentation.api.app import local_workspace_user

    @contextmanager
    def select(client, user_id):
        with monkeypatch.context() as patch:
            patch.setitem(client.app.dependency_overrides, local_workspace_user, lambda: {"id": user_id})
            yield

    return select


class ScriptedAgent:
    def __init__(self):
        self.responses = []
        self.requests = []

    @staticmethod
    def tool(call_id, name, args=None):
        arguments = args if isinstance(args, str) else json.dumps(args or {})
        return dict(id=call_id, type="function", function=dict(name=name, arguments=arguments))

    @staticmethod
    def reply(*calls, text=None):
        message = dict(role="assistant", content=text)
        if calls:
            message["tool_calls"] = list(calls)
        return message

    async def post(self, url, *, headers, json, timeout):
        self.requests.append(deepcopy(json))
        assert self.responses, "Unexpected model request"
        response = self.responses.pop(0)
        message = response(json) if callable(response) else response
        return dict(
            model=json["model"], choices=[dict(message=message)],
            usage=dict(prompt_tokens=100, completion_tokens=20),
        )


@pytest.fixture
def fake_agent():
    return ScriptedAgent()


@pytest.fixture(scope="session")
def sample_video(tmp_path_factory):
    ffmpeg = media_binaries()["ffmpeg"]
    if not shutil.which(ffmpeg):
        pytest.skip("FFmpeg required")
    folder = tmp_path_factory.mktemp("harness-media")
    caption, video = folder / "dialogue.srt", folder / "source.mp4"
    caption.write_text(
        "1\n00:00:00,300 --> 00:00:00,900\nWhere is my ticket?\n\n"
        "2\n00:00:01,200 --> 00:00:01,700\nYour ticket is here.\n",
        encoding="utf-8",
    )
    subprocess.run([
        ffmpeg, "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=960x540:r=10:d=2",
        "-i", str(caption), "-c:v", "libx264", "-threads", "2", "-c:s", "mov_text", str(video),
    ], check=True)
    return video


@pytest.fixture
def upload_video():
    def upload(client, project, path, filename=None):
        data = path.read_bytes()
        response = client.post(f"/v1/projects/{project}/assets", json=dict(
            filename=filename or path.name, size_bytes=len(data), content_type="video/mp4",
        ))
        response.raise_for_status()
        slot = response.json()
        part = client.put(slot["parts"][0]["url"], content=data)
        part.raise_for_status()
        response = client.post(f"/v1/uploads/{slot['upload_id']}/complete", json=dict(
            idempotency_key="complete-source", parts=[dict(part_number=1, etag=part.headers["ETag"])],
        ))
        response.raise_for_status()
        return response.json()

    return upload


@pytest.fixture
def wait_state():
    def wait(client, path, statuses=("SUCCEEDED",), timeout=25):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = client.get(path)
            response.raise_for_status()
            state = response.json()
            if state["status"] in statuses:
                return state
            if state["status"] in ("FAILED", "CANCELLED", "SUCCEEDED", "PARTIAL", "AWAITING_INPUT"):
                pytest.fail(f"Expected {statuses}, got {state}")
            time.sleep(.04)
        pytest.fail(f"Timed out waiting for {statuses} at {path}")

    return wait


@pytest.fixture
def workflow_events():
    def read(client, path, cursor=None):
        headers = {"Last-Event-ID": cursor} if cursor else {}
        response = client.get(path + "/events", headers=headers)
        response.raise_for_status()
        assert response.headers["content-type"].startswith("text/event-stream")
        return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]

    return read
