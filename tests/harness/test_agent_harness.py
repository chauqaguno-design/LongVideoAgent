"""Native chat protocol and durable HTTP behavior with deterministic model responses."""

import hashlib
import json
from base64 import b64decode
from io import BytesIO
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import update

from cutsieve.infrastructure.persistence.edit_tables import jobs
from cutsieve.infrastructure.persistence.source_tables import sources
from cutsieve.infrastructure.persistence.workflow_clock import db_now
from cutsieve.infrastructure.persistence.workflow_tables import runs
from cutsieve.presentation.api.app import create_app


def body(response):
    response.raise_for_status()
    return response.json()


def create_project(client, title="Fixture"):
    return body(client.post("/v1/projects", json={"title": title}))["project_id"]


def start_chat(client, project, message, **fields):
    request = dict(message=message, idempotency_key=str(uuid4()), **fields)
    response = client.post(f"/v1/projects/{project}/conversation-runs", json=request)
    assert response.status_code == 202, response.text
    accepted = response.json()
    return "/v1/workflow-runs/" + accepted["run_id"], accepted


def last_tool(request, call_id):
    message = request["messages"][-1]
    assert message["role"] == "tool" and message["tool_call_id"] == call_id
    content = message["content"]
    return json.loads(content[0]["text"] if isinstance(content, list) else content)


def wait_for_index(client, asset, wait_state):
    path = f"/v1/assets/{asset['asset_id']}/processing-runs"
    job = body(client.get(path))["items"][0]["processing_run_id"]
    return wait_state(client, "/v1/processing-runs/" + job, ("SUCCEEDED", "PARTIAL"))


@pytest.mark.real_media
def test_search_answer_across_available_assets_and_direct_image_input(
    tmp_path, fake_agent, sample_video, upload_video, wait_state, workflow_events,
):
    tool, reply = fake_agent.tool, fake_agent.reply
    with TestClient(create_app(work_dir=tmp_path, agent_transport=fake_agent)) as client:
        project = create_project(client)
        asset = upload_video(client, project, sample_video, "original.mp4")
        other = upload_video(client, project, sample_video, "second.mp4")
        wait_for_index(client, asset, wait_state)
        wait_for_index(client, other, wait_state)
        client.post(f"/v1/projects/{project}/assets", json=dict(
            filename="incomplete.mp4", size_bytes=100, content_type="video/mp4",
        )).raise_for_status()
        with BytesIO() as buffer:
            Image.new("RGB", (20, 10), "blue").save(buffer, "PNG")
            photo = buffer.getvalue()
        image = body(client.post(f"/v1/projects/{project}/query-images",
                                files={"file": ("query.png", photo, "image/png")}))["artifact_id"]
        other_project = create_project(client)
        wrong = client.post(f"/v1/projects/{other_project}/conversation-runs", json=dict(
            idempotency_key="wrong-project", message="Look at the photo", image_artifact_id=image,
        ))
        assert wrong.status_code == 404

        def overview(request):
            system = request["messages"][0]["content"]
            for filename in ("original.mp4", "second.mp4", "Editing rules", "Visual verification"):
                assert filename in system
            assert "incomplete.mp4" not in system
            message = request["messages"][-1]
            assert message["role"] == "user" and image in message["content"][0]["text"]
            assert b64decode(message["content"][1]["image_url"]["url"].split(",", 1)[1]) == photo
            assert len(request["tools"]) == 8 and request["tool_choice"] == "auto"
            return reply(tool("overview", "get_media_overview"))

        def search(request):
            sources = last_tool(request, "overview")["assets"]
            assert {item["asset_id"] for item in sources} == {asset["asset_id"], other["asset_id"]}
            assert all(item["has_subtitles"] and item["processing_status"] in ("SUCCEEDED", "PARTIAL") for item in sources)
            assert sources[0]["viewing_notes"]["chapters"]
            return reply(tool("search", "search_evidence", dict(
                query='"Where is my ticket?"', mode="LOCAL", image_artifact_id=image, asset_id=asset["asset_id"],
            )))

        def answer(request):
            result = last_tool(request, "search")
            hit = next(hit for hit in result["hits"] if "Where is my ticket?" in hit["text"])
            assert hit["start_s"] == .3 and hit["end_us"] == 900_000
            return reply(text="The question is spoken at 0.300–0.900 seconds.")

        fake_agent.responses = [overview, search, answer]
        path, accepted = start_chat(client, project, "When is the ticket question?", media_revision_id=None,
                                    image_artifact_id=image)
        state = wait_state(client, path)
        assert state["usage"] == dict(model_calls=3, tool_calls=2, images=1, input_tokens=300, output_tokens=60,
                                      estimated_cost_cny=0.0, max_steps=15, max_images=24)
        result = body(client.get(path + "/result"))
        assert set(result) == {"answer", "citations", "tool_steps", "edit_project_id", "revision_id",
                               "preview_artifact_id", "export_artifact_id", "warnings"}
        proof = next(c for c in result["citations"] if c["text"] == "Where is my ticket?")
        assert proof["media_revision_id"] == asset["media_revision_id"] and proof["start_us"] == 300_000
        turns = body(client.get(path + "/conversation"))["turns"]
        assert turns[0]["media_revision_id"] is None and turns[0]["image_artifact_id"] == image
        assert turns[0]["answer"] == result["answer"] and turns[0]["tool_steps"] == result["tool_steps"]
        kinds = [event["type"] for event in workflow_events(client, path, accepted["last_event_id"])]
        assert kinds == ["STATUS", "TOOL_STARTED", "TOOL_FINISHED", "TOOL_STARTED", "TOOL_FINISHED", "RUN_COMPLETED"]


@pytest.mark.real_media
def test_multimodal_tool_message_then_atomic_edit_and_parent_inheritance(
    tmp_path, fake_agent, sample_video, upload_video, wait_state, workflow_events,
):
    tool, reply = fake_agent.tool, fake_agent.reply
    with TestClient(create_app(work_dir=tmp_path, agent_transport=fake_agent)) as client:
        project = create_project(client)
        asset = upload_video(client, project, sample_video)
        asset_id = asset["asset_id"]

        def edit(request):
            frames = last_tool(request, "frames")["frames"]
            image = request["messages"][-1]["content"][1]
            assert image["type"] == "image_url" and image["image_url"]["url"].startswith("data:image/jpeg;base64,")
            pixels = b64decode(image["image_url"]["url"].split(",", 1)[1])
            with Image.open(BytesIO(pixels)) as jpeg:
                assert jpeg.format == "JPEG" and jpeg.width == 768
            assert frames[0]["timestamp_us"] == 500_000
            return reply(tool("edit", "edit_timeline", dict(
                title="Cut from source", clips=[dict(asset_id=asset_id, start_s=.4, end_s=.8, audio_policy="mute")],
            )))

        fake_agent.responses = [reply(tool("frames", "inspect_frames", dict(asset_id=asset_id, timestamps_s=[.5]))),
                                edit, reply(text="Saved the 0.400–0.800 second cut.")]
        path, accepted = start_chat(client, project, "Inspect 0.5 seconds and save a short muted cut.",
                                    media_revision_id=asset["media_revision_id"])
        assert wait_state(client, path)["usage"]["images"] == 1
        result = body(client.get(path + "/result"))
        assert result["preview_artifact_id"] is None and result["citations"][0]["source"] == "ffmpeg-frame"
        edit_id, revision = result["edit_project_id"], result["revision_id"]
        snapshot = body(client.get(f"/v1/edit-projects/{edit_id}/revisions/{revision}"))
        assert snapshot["clips"][0]["audio_policy"] == "mute"
        timeline = [e["payload"] for e in workflow_events(client, path) if e["type"] == "TIMELINE_UPDATED"]
        assert timeline == [dict(edit_project_id=edit_id, revision_id=revision, revision_no=1)]

        def read_parent(request):
            history = request["messages"]
            assert edit_id in history[0]["content"] and revision in history[0]["content"]
            assert "Inspect 0.5 seconds" in history[1]["content"]
            assert "Saved the" in history[2]["content"] and "Prior citations" in history[2]["content"]
            return reply(tool("read-parent", "get_timeline"))

        fake_agent.responses = [read_parent, reply(text="The current cut is 0.4 seconds long.")]
        child, child_run = start_chat(client, project, "How long is it?", parent_run_id=accepted["run_id"])
        wait_state(client, child)
        turns = body(client.get(child + "/conversation"))["turns"]
        assert [turn["run_id"] for turn in turns] == [accepted["run_id"], child_run["run_id"]]
        assert turns[-1]["parent_run_id"] == accepted["run_id"] and turns[-1]["edit_project_id"] == edit_id


@pytest.mark.real_media
def test_model_recovers_from_invalid_json_scope_empty_search_and_timestamp(
    tmp_path, fake_agent, sample_video, upload_video, wait_state,
):
    tool, reply = fake_agent.tool, fake_agent.reply
    with TestClient(create_app(work_dir=tmp_path, agent_transport=fake_agent)) as client:
        project = create_project(client)
        asset = upload_video(client, project, sample_video)
        outsider = upload_video(client, create_project(client, "Other"), sample_video)

        def next_after(call_id, code, *calls):
            def respond(request):
                result = last_tool(request, call_id)
                assert result["ok"] is False and result["code"] == code
                assert result["message"] and result["hint"]
                return reply(*calls) if calls else reply(text="Saved the first second.")
            return respond

        fake_agent.responses = [
            reply(tool("json", "edit_timeline", "{broken")),
            next_after("json", "INVALID_TOOL_ARGUMENTS",
                       tool("outside", "search_evidence", dict(query="test", asset_id=outsider["asset_id"]))),
            next_after("outside", "ASSET_NOT_FOUND", tool("empty", "search_evidence")),
            next_after("empty", "EMPTY_QUERY", tool("range", "edit_timeline", dict(clips=[dict(start_s=0, end_s=99)]))),
            next_after("range", "INVALID_RANGE", tool("valid", "edit_timeline", dict(clips=[dict(start_s=0, end_s=1)]))),
            reply(text="Saved the first second."),
        ]
        path, _ = start_chat(client, project, "Save the first second.", media_revision_id=asset["media_revision_id"])
        wait_state(client, path)
        result = body(client.get(path + "/result"))
        assert [step["ok"] for step in result["tool_steps"]] == [False, False, False, False, True]
        projects = body(client.get(f"/v1/projects/{project}/edit-projects"))["items"]
        assert len(projects) == 1 and projects[0]["head_revision"] == 1


@pytest.mark.real_media
def test_edit_timeline_autocreates_and_uses_optimistic_head_after_conflict(
    tmp_path, fake_agent, sample_video, upload_video, wait_state, workflow_events,
):
    tool, reply = fake_agent.tool, fake_agent.reply
    app = create_app(work_dir=tmp_path, agent_transport=fake_agent)
    with TestClient(app) as client:
        project = create_project(client)
        asset = upload_video(client, project, sample_video)

        def outside_edit(request):
            view = last_tool(request, "read")
            app.state.edit_worker.repo.patch(asset["workspace_id"], view["edit_project_id"], dict(
                expected_revision=1, idempotency_key="external-editor", reason="External caption change",
                operations=[dict(op="set_caption_policy", caption_policy="preserve_source")],
            ))
            return reply(tool("stale", "edit_timeline", dict(clips=[dict(start_s=.6, end_s=.9)])))

        def refresh(request):
            response = last_tool(request, "stale")
            assert response["code"] == "REVISION_CONFLICT" and "get_timeline" in response["hint"]
            return reply(tool("refresh", "get_timeline"))

        def correct(request):
            latest = last_tool(request, "refresh")
            assert latest["head_revision_no"] == 2 and latest["caption_policy"] == "preserve_source"
            return reply(tool("append", "edit_timeline", dict(expected_revision=latest["head_revision_no"], operations=[
                dict(op="append_clip", clip=dict(media_revision_id=asset["media_revision_id"],
                                                 source_start_us=600_000, source_end_us=900_000)),
            ])))

        fake_agent.responses = [
            reply(tool("create", "edit_timeline", dict(clips=[dict(start_s=0, end_s=.5)])), tool("read", "get_timeline")),
            outside_edit, refresh, correct, reply(text="Both clips are saved."),
        ]
        path, _ = start_chat(client, project, "Assemble two clips.")
        wait_state(client, path)
        result = body(client.get(path + "/result"))
        stored = body(client.get(f"/v1/edit-projects/{result['edit_project_id']}/revisions/{result['revision_id']}"))
        assert stored["caption_policy"] == "preserve_source" and stored["revision_no"] == 3
        assert [(c["source_start_us"], c["source_end_us"]) for c in stored["clips"]] == [(0, 500_000), (600_000, 900_000)]
        assert [e["payload"]["revision_no"] for e in workflow_events(client, path) if e["type"] == "TIMELINE_UPDATED"] == [1, 3]


@pytest.mark.real_media
def test_real_render_suspends_then_continues_after_restart(
    tmp_path, fake_agent, sample_video, upload_video, wait_state, workflow_events, monkeypatch,
):
    tool, reply = fake_agent.tool, fake_agent.reply
    app = create_app(work_dir=tmp_path, agent_transport=fake_agent)

    async def defer_render_worker():
        pass

    monkeypatch.setattr(app.state.edit_worker, "start", defer_render_worker)
    fake_agent.responses = [reply(tool("edit", "edit_timeline", dict(clips=[dict(start_s=0, end_s=.8)])),
                                  tool("render", "render_video"), tool("read-after", "get_timeline"))]
    with TestClient(app) as client:
        project = create_project(client)
        asset = upload_video(client, project, sample_video)
        path, accepted = start_chat(client, project, "Save a cut and render a preview.")
        waiting = wait_state(client, path, ("WAITING_JOB",))
        job_id, = waiting["pending_job_ids"]
        repo = app.state.workflow_worker.repo
        with repo.sessions() as db:
            cp = repo.readable(db, asset["workspace_id"], accepted["run_id"]).checkpoint
            assert cp.pending_render_tool_call.tool_call_id == "render"
            assert cp.pending_render_tool_call.render_job_id == job_id
            assert cp.messages[-1]["tool_call_id"] == "edit"
        assert body(client.get(f"/v1/render-jobs/{job_id}"))["status"] == "QUEUED"
        assert len(fake_agent.requests) == 1

    def verify(request):
        timeline = last_tool(request, "read-after")
        render = json.loads(request["messages"][-2]["content"])
        assert request["messages"][-2]["tool_call_id"] == "render"
        assert render["status"] == "SUCCEEDED" and render["duration_s"] == .8 and render["mime_type"] == "video/mp4"
        assert render["revision_id"] == timeline["head_revision_id"] and render["render_job_id"] == job_id
        return reply(tool("review", "inspect_frames", dict(artifact_id=render["artifact_id"], timestamps_s=[.4])))

    def answer(request):
        assert last_tool(request, "review")["frames"][0]["timestamp_s"] == .4
        assert request["messages"][-1]["content"][1]["image_url"]
        return reply(text="The preview is ready.")

    fake_agent.responses = [verify, answer]
    with TestClient(create_app(work_dir=tmp_path, agent_transport=fake_agent)) as client:
        state = wait_state(client, path)
        result = body(client.get(path + "/result"))
        assert state["usage"]["images"] == 1 and state["usage"]["tool_calls"] == 4
        ready, = [e["payload"] for e in workflow_events(client, path) if e["type"] == "ARTIFACT_READY"]
        assert ready == dict(kind="PREVIEW", artifact_id=result["preview_artifact_id"],
                             edit_project_id=result["edit_project_id"], revision_id=result["revision_id"])
        artifact = body(client.get("/v1/artifacts/" + ready["artifact_id"]))
        data = client.get(artifact["url"]).content
        assert data[4:8] == b"ftyp" and hashlib.sha256(data).hexdigest() == artifact["sha256"]
        render_job = body(client.get(f"/v1/render-jobs/{job_id}"))
        report = body(client.get("/v1/artifacts/" + render_job["validation_report_id"]))
        assert body(client.get(report["url"]))["full_decode"] is True


def test_ask_user_persists_options_and_resumes_original_tool_call(
    tmp_path, fake_agent, wait_state, workflow_events,
):
    tool, reply = fake_agent.tool, fake_agent.reply
    question, options = "Which part should I use?", ["Opening", "Closing"]
    fake_agent.responses = [reply(tool("ask", "ask_user", dict(question=question, options=options)))]
    app = create_app(work_dir=tmp_path, agent_transport=fake_agent)
    with TestClient(app) as client:
        project = create_project(client)
        path, accepted = start_chat(client, project, "Choose an excerpt.")
        waiting = wait_state(client, path, ("AWAITING_INPUT",))
        assert waiting["awaiting_input"] == dict(question=question, options=options)
        assert workflow_events(client, path)[-1]["payload"] == waiting["awaiting_input"]
        repo = app.state.workflow_worker.repo
        with repo.edits.write() as db:
            row = repo.readable(db, waiting["workspace_id"], accepted["run_id"])
            row.checkpoint.paused_at = db_now(db) - 400
            db.execute(update(runs).where(runs.c.id == row.id).values(
                checkpoint=row.checkpoint, deadline=db_now(db) - 100,
            ))

    def final(request):
        assert last_tool(request, "ask") == dict(ok=True, question=question, reply="Opening")
        return reply(text="Opening selected.")

    fake_agent.responses = [final]
    with TestClient(create_app(work_dir=tmp_path, agent_transport=fake_agent)) as client:
        payload = dict(expected_state_version=waiting["state_version"], message="Opening", idempotency_key="choose-first")
        stale = client.post(path + "/resume", json=dict(payload, expected_state_version=0))
        assert stale.status_code == 409
        resumed = client.post(path + "/resume", json=payload)
        assert resumed.status_code == 202
        wait_state(client, path)
        assert client.post(path + "/resume", json=payload).json() == resumed.json()
        turn, = body(client.get(path + "/conversation"))["turns"]
        assert turn["clarifications"] == [dict(question=question, reply="Opening")]
        assert turn["tool_steps"][0]["ok"] is True
        types = [e["type"] for e in workflow_events(client, path, waiting["last_event_id"])]
        assert types == ["TOOL_FINISHED", "STATUS", "STATUS", "RUN_COMPLETED"]


def test_max_steps_fails_clearly_and_bounds_model_calls(tmp_path, fake_agent, wait_state, workflow_events, monkeypatch):
    monkeypatch.setenv("CUTSIEVE_HARNESS_MAX_STEPS", "2")
    fake_agent.responses = [fake_agent.reply(fake_agent.tool(str(i), "get_media_overview")) for i in range(2)]
    with TestClient(create_app(work_dir=tmp_path, agent_transport=fake_agent)) as client:
        path, _ = start_chat(client, create_project(client), "Keep looking.")
        state = wait_state(client, path, ("FAILED",))
        assert state["error"]["code"] == "MAX_STEPS_EXCEEDED" and state["awaiting_input"] is None
        assert state["usage"]["tool_calls"] == state["usage"]["model_calls"] == state["usage"]["max_steps"] == 2
        assert len(fake_agent.requests) == 2
        assert workflow_events(client, path)[-1]["type"] == "RUN_FAILED"


@pytest.mark.real_media
def test_image_limit_feedback_and_large_text_tool_result_artifact(
    tmp_path, fake_agent, sample_video, upload_video, wait_state, monkeypatch,
):
    monkeypatch.setenv("CUTSIEVE_HARNESS_MAX_IMAGES", "1")
    tool, reply = fake_agent.tool, fake_agent.reply
    app = create_app(work_dir=tmp_path, agent_transport=fake_agent)
    with TestClient(app) as client:
        project = create_project(client)
        asset = upload_video(client, project, sample_video)
        asset_id = asset["asset_id"]
        long_transcript = "一段很长的对话。" * 1250
        monkeypatch.setattr(app.state.processing.catalog, "search_scope", lambda *args, **kw: (
            "test-generation", [dict(media_revision_id=asset["media_revision_id"], source="embedded-subtitle",
                                     evidence_id=str(uuid4()), support="SUPPORTED", claim=long_transcript,
                                     range=dict(start_us=300_000, end_us=900_000))],
        ))
        compact = {}

        def examine_next(request):
            assert last_tool(request, "over")["code"] == "MAX_IMAGES_EXCEEDED"
            return reply(tool("one", "inspect_frames", dict(asset_id=asset_id, timestamps_s=[.4])))

        def read(request):
            assert last_tool(request, "again")["remaining_images"] == 0
            return reply(tool("transcript", "read_transcript", dict(asset_id=asset_id, start_s=0, end_s=400)))

        def final(request):
            compact.update(last_tool(request, "transcript"))
            assert compact["ok"] and compact["end_s"] == 2 and compact["summary"]
            assert len(request["messages"][-1]["content"]) <= 8000
            return reply(text="I inspected one frame and read the transcript.")

        fake_agent.responses = [
            reply(tool("over", "inspect_frames", dict(asset_id=asset_id, count=2))), examine_next,
            reply(tool("again", "inspect_frames", dict(asset_id=asset_id, count=1))), read, final,
        ]
        path, _ = start_chat(client, project, "Check the frames and read the dialogue.")
        state = wait_state(client, path)
        assert state["usage"]["images"] == state["usage"]["max_images"] == 1
        artifact = body(client.get("/v1/artifacts/" + compact["artifact_id"]))
        assert artifact["kind"] == "TOOL_RESULT"
        assert body(client.get(artifact["url"]))["rows"][0]["text"] == long_transcript


@pytest.mark.real_media
@pytest.mark.parametrize("outcome", ["cancel-run", "cancel-job", "failed-job"])
def test_pending_render_cancellation_and_terminal_error_feedback(
    tmp_path, fake_agent, sample_video, upload_video, wait_state, workflow_events, monkeypatch, outcome,
):
    tool, reply = fake_agent.tool, fake_agent.reply
    app = create_app(work_dir=tmp_path, agent_transport=fake_agent)

    async def delay_render():
        pass

    monkeypatch.setattr(app.state.edit_worker, "start", delay_render)

    def finish(request):
        result = last_tool(request, "render")
        expected = "FAILED" if outcome == "failed-job" else "CANCELLED"
        assert result["status"] == expected and result["ok"] is False
        assert result["code"] and result["hint"] and result["artifact_id"] is None
        return reply(text="The preview could not be rendered.")

    fake_agent.responses = [
        reply(tool("edit", "edit_timeline", dict(clips=[dict(start_s=0, end_s=.5)])), tool("render", "render_video")),
        finish,
    ]
    with TestClient(app) as client:
        project = create_project(client)
        upload_video(client, project, sample_video)
        path, _ = start_chat(client, project, "Render a preview.")
        waiting = wait_state(client, path, ("WAITING_JOB",))
        job_id, = waiting["pending_job_ids"]
        if outcome == "cancel-run":
            cancelled = client.post(path + "/cancel")
            assert cancelled.status_code == 202 and cancelled.json()["status"] == "CANCELLED"
            job = body(client.get("/v1/render-jobs/" + job_id))
            assert job["status"] == "CANCELLED" and job["cancel_requested_at"]
            turn, = body(client.get(path + "/conversation"))["turns"]
            assert turn["tool_steps"][-1]["status"] == "CANCELLED"
            assert workflow_events(client, path)[-1]["type"] == "RUN_CANCELLED"
            assert len(fake_agent.requests) == 1
        else:
            if outcome == "cancel-job":
                client.post("/v1/render-jobs/" + job_id + "/cancel", json={"idempotency_key": "cancel-from-editor"}).raise_for_status()
            else:
                with app.state.edit_worker.repo.write() as db:
                    from cutsieve.infrastructure.persistence.edit_repository import one

                    row = one(db, jobs.select().where(jobs.c.id == job_id))
                    db.execute(update(jobs).where(jobs.c.id == job_id).values(
                        status="FAILED", cache_slot=None,
                        result=dict(row.result, error=dict(code="TEST_RENDER_FAILED", message="Failed to encode.")),
                    ))
            wait_state(client, path)
            assert body(client.get(path + "/result"))["preview_artifact_id"] is None
            kinds = [event["type"] for event in workflow_events(client, path)]
            assert "ARTIFACT_READY" not in kinds and kinds[-1] == "RUN_COMPLETED"


def test_transcript_paginates_long_windows_without_losing_time_bounds(tmp_path, fake_agent, wait_state, monkeypatch):
    tool, reply = fake_agent.tool, fake_agent.reply
    app = create_app(work_dir=tmp_path, agent_transport=fake_agent)
    with TestClient(app) as client:
        project = create_project(client)
        upload = body(client.post(f"/v1/projects/{project}/assets", json=dict(
            filename="long.mp4", size_bytes=1, content_type="video/mp4",
        )))
        store = app.state.edit_sources
        with store.sessions.begin() as db:
            source = db.execute(sources.select().where(sources.c.upload == upload["upload_id"])).one()
            db.execute(update(sources).where(sources.c.id == source.id).values(
                digest="a" * 64, receipt="b" * 64,
                probe=dict(duration_us=400_000_000, streams=[], source_origin=dict(num=0, den=1)),
            ))
        spans = []

        def aligned(_workspace, _project, media, *, span):
            spans.append(span)
            assert media == [source.revision]
            return "generation", [dict(
                media_revision_id=source.revision, source="embedded-subtitle", evidence_id=str(uuid4()),
                support="SUPPORTED", claim="Window dialogue", range=dict(start_us=span["start_us"], end_us=span["end_us"]),
            )]

        monkeypatch.setattr(app.state.processing.catalog, "search_scope", aligned)

        def paginate(call_id, follow_id):
            def follow(request):
                result = last_tool(request, call_id)
                if follow_id:
                    return reply(tool(follow_id, "read_transcript", result["next"]))
                assert result["next"] is None and result["end_s"] == 400
                return reply(text="Read the aligned dialogue across the three windows.")
            return follow

        fake_agent.responses = [
            reply(tool("first", "read_transcript", dict(asset_id=source.id, start_s=10, end_s=900))),
            paginate("first", "second"), paginate("second", "third"), paginate("third", None),
        ]
        path, _ = start_chat(client, project, "Read the entire transcript.")
        wait_state(client, path)
        assert spans == [dict(start_us=start * 1_000_000, end_us=end * 1_000_000)
                         for start, end in ((10, 190), (190, 370), (370, 400))]
