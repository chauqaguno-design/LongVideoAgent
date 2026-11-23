import json
from uuid import uuid4

from jsonschema import Draft202012Validator
import pytest

from dovideo.application.spec_contracts import ToolCatalog, contracts_dir, tool_catalog


def test_all_tool_input_output_schemas_are_portable_and_names_round_trip():
    catalog = tool_catalog()
    assert len(catalog.tools) == len(catalog.by_provider) == 14
    for tool in catalog.tools.values():
        assert catalog.for_provider_call(tool.provider_name, "COORDINATOR") is tool
        assert tool.timeout_seconds > 0
        for schema in (tool.input_schema, tool.output_schema):
            Draft202012Validator.check_schema(schema)
            assert '"$ref"' not in json.dumps(schema)
    assert {entry["function"]["name"] for entry in catalog.provider_tools("COORDINATOR")} == set(catalog.by_provider)


def test_provider_names_are_unique_and_not_privilege_grants():
    rows = json.loads((contracts_dir() / "tool-registry.json").read_text(encoding="utf8"))["tools"]
    with pytest.raises(ValueError):
        ToolCatalog([rows[0], dict(rows[1], provider_name=rows[0]["provider_name"])])
    catalog = tool_catalog()
    exposed = {spec["function"]["name"] for spec in catalog.provider_tools("MATERIAL")}
    assert "video__search_evidence" in exposed and "edit__apply_patch" not in exposed
    with pytest.raises(ValueError):
        catalog.for_provider_call("edit__apply_patch","EDITOR")
    with pytest.raises(ValueError):
        catalog.for_provider_call("video.get_segment","COORDINATOR")


def test_server_identity_is_not_provider_input_and_time_unit_is_explicit():
    tool = tool_catalog().for_provider_call("video__get_segment","MATERIAL")
    args = {"media_revision_id":str(uuid4()), "range":{"start_us":100_000,"end_us":500_000}}
    tool.validate(args)
    for invalid in (dict(args, workspace_id=str(uuid4())), dict(args, range={"start_ms":100,"end_ms":500}),
                    dict(args, range={"start_us":0.01,"end_us":10})):
        with pytest.raises(ValueError):
            tool.validate(invalid)
    with pytest.raises(ValueError):
        tool.validate({"ok":True}, output=True)


def test_each_registered_tool_accepts_its_wire_example_and_rejects_extra_fields():
    def example(name):
        return json.loads((contracts_dir() / "examples" / f"{name}.json").read_text(encoding="utf8"))

    ref = str(uuid4())
    interval = dict(start_us=0, end_us=1_000_000)
    segment = dict(media_revision_id=ref, range=interval)
    key = dict(idempotency_key="fixture-action")
    inspection = dict(job_id=ref, kind="INSPECTION")
    view = example("job-partial")
    receipt = dict(operation_id=ref, edit_project_id=ref, revision_id=ref, revision_no=1,
                   request_hash="a" * 64, clip_id_assignments=[], replayed=False, **key)
    cases = {
        "video.search_evidence": (example("search-text"), example("search-candidates-partial")),
        "video.get_segment": (segment, dict(evidence_ids=[ref], artifact_ids=[ref], range=interval)),
        "video.get_context_window": (dict(evidence_id=ref, before_us=0, after_us=0),
                                     dict(evidence_ids=[ref], range=interval)),
        "video.inspect_overview": (dict(segment, **key), inspection),
        "video.inspect_clip": (dict(segment, max_frames=4, **key), inspection),
        "video.read_region": (dict(frame_artifact_id=ref, crop=dict(x=0, y=0, width=16, height=16), **key), inspection),
        "video.check_coverage": (dict(segment, query_run_id=ref, protocol_id="fixture"),
                                 dict(status="PARTIAL", unverified_ranges=[interval])),
        "audio.inspect_transcript": (segment, dict(observation_ids=[ref], quality_flags=[], conflict_evidence_ids=[])),
        "edit.get_timeline": (dict(edit_project_id=ref, revision_id=ref), example("timeline-revision")),
        "edit.apply_patch": (dict(edit_project_id=ref, patch=example("timeline-patch")), receipt),
        "edit.validate_timeline": (dict(revision_id=ref), dict(valid=True, errors=[], warnings=[])),
        "render.submit": (example("render-create"), example("render-succeeded")),
        "jobs.get": (dict(job_id=ref), view),
        "jobs.cancel": (dict(job_id=ref, **key), view),
    }
    tools = tool_catalog().tools
    assert cases.keys() == tools.keys()
    for name, (request, result) in cases.items():
        for body, output in ((request, False), (result, True)):
            tool = tools[name]
            tool.validate(body, output=output)
            with pytest.raises(ValueError):
                tool.validate(dict(body, unsupported_field=True), output=output)
