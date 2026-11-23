from urllib.parse import urldefrag, urljoin
from pathlib import Path
import re
from uuid import uuid4
import yaml

from fastapi.testclient import TestClient
from referencing import Registry, Resource

from dovideo.presentation.api.app import create_app
from dovideo.application.spec_contracts import validate_contract


def test_advertised_revision_contract_resolves_over_http(tmp_path):
    with TestClient(create_app(work_dir=tmp_path)) as client:
        openapi = client.get("/openapi.json")
        api = openapi.json()
        operation = api["paths"]["/v1/edit-projects/{edit_project_id}/operations"]["post"]
        assert operation["operationId"] == "patchTimeline"
        assert operation["security"] == [{"BearerAuth": []}]
        assert api["components"]["securitySchemes"]["BearerAuth"]["scheme"] == "bearer"
        target = operation["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        absolute = urljoin(str(openapi.url), target)
        schema_url, _ = urldefrag(absolute)
        response = client.get(schema_url)
        assert response.status_code == 200
        document = Resource.from_contents(response.json())
        registry = Registry().with_resources([(schema_url, document), (document.id(), document)])
        patch = registry.resolver().lookup(absolute)
        assert "expected_revision" in patch.contents["required"]
        item = patch.contents["properties"]["operations"]["items"]
        assert patch.resolver.lookup(item["$ref"]).contents == response.json()["$defs"]["TimelinePatchOperation"]
        for status, schema in [("201", "OperationReceipt"), ("200", "OperationReceipt"), ("409", "Error")]:
            ref = operation["responses"][status]["content"]["application/json"]["schema"]["$ref"]
            assert registry.resolver().lookup(urljoin(str(openapi.url), ref)).contents == response.json()["$defs"][schema]


def test_schema_contains_implemented_and_custom_routes_only(tmp_path):
    with TestClient(create_app(work_dir=tmp_path)) as client:
        paths = client.get("/openapi.json").json()["paths"]
        assert paths["/v1/media-revisions/{media_revision_id}"]["get"]["operationId"] == "getMediaRevision"
        assert set(paths["/v1/assets/{asset_id}"]) == {"get", "delete"}
        assert paths["/v1/workflow-runs"]["post"]["requestBody"]["content"]["application/json"]["schema"]["$ref"].endswith("/WorkflowCreate")
        assert "post" in paths["/v1/projects/{project_id}/imports/{legacy_media_id}"]
        assert "post" in paths["/user/login"]
        source = Path("docs/videomind-spec/contracts/openapi.yaml").read_text(encoding="utf-8")
        for path, methods in yaml.safe_load(source)["paths"].items():
            for verb in {"get", "post", "patch", "delete"} & methods.keys():
                assert paths[path][verb]["operationId"] == methods[verb]["operationId"]


def test_all_spec_operations_return_typed_errors_and_json_bodies_reject_unknown_fields(tmp_path):
    target = yaml.safe_load(Path("docs/videomind-spec/contracts/openapi.yaml").read_text(encoding="utf8"))
    with TestClient(create_app(work_dir=tmp_path)) as client:
        credentials = dict(username="contract_user", password="local-test-password")
        client.post("/user/register", json=dict(credentials, nickname="contract")).raise_for_status()
        login = client.post("/user/login", json=credentials)
        token = login.json()["data"]["token"]
        for template, operations in target["paths"].items():
            url = re.sub(r"\{\w+\}", str(uuid4()), template)
            for verb, operation in operations.items():
                content = operation.get("requestBody", {}).get("content", {})
                body = {"json": {"unsupported_field": True}} if "application/json" in content else {}
                denied = client.request(verb, url, **body)
                assert denied.status_code == 401, (operation["operationId"], denied.text)
                validate_contract("Error", denied.json())
                if body:
                    invalid = client.request(verb, url, headers={"Authorization": "Bearer " + token}, **body)
                    assert invalid.status_code == 422, (operation["operationId"], invalid.text)
                    validate_contract("Error", invalid.json())


def test_browser_can_adjust_budgets_resume_event_streams_and_upload_parts(tmp_path):
    with TestClient(create_app(work_dir=tmp_path)) as client:
        for method, path, headers in (
            ("PATCH", "/v1/workflow-runs/run/budget", "Authorization, Content-Type"),
            ("GET", "/v1/workflow-runs/run/events", "Authorization, Last-Event-ID"),
            ("PUT", "/v1/source-uploads/upload", "Content-Range"),
        ):
            response = client.options(path, headers={
                "Origin": "http://localhost:5173", "Access-Control-Request-Method": method,
                "Access-Control-Request-Headers": headers,
            })
            assert response.status_code == 200
            assert response.headers["Access-Control-Allow-Origin"] == "http://localhost:5173"
