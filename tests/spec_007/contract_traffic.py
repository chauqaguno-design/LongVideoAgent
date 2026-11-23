"""Check actual HTTP exchanges against the versioned specification."""
import json

from jsonschema import Draft202012Validator, FormatChecker
from starlette.routing import compile_path
import yaml

from dovideo.application.spec_contracts import contracts_dir, definitions, validate_contract, validator


class ContractTraffic:
    def __init__(self):
        document = yaml.safe_load((contracts_dir() / "openapi.yaml").read_text(encoding="utf8"))
        self.routes = [(compile_path(path)[0], methods) for path, methods in document["paths"].items()]
        self.operations = {op["operationId"] for _, methods in self.routes for op in methods.values()}
        self.success = set()
        self.missing_examples = set(definitions())

    def collect_examples(self, value):
        if isinstance(value, list):
            for item in value:
                self.collect_examples(item)
        if isinstance(value, dict):
            for name in tuple(self.missing_examples):
                if set(definitions()[name].get("required", ())) <= value.keys() and validator(name).is_valid(value):
                    assert not validator(name).is_valid(dict(value, unsupported_field=True)), name
                    self.missing_examples.remove(name)
            for child in value.values():
                self.collect_examples(child)

    def check(self, response):
        if response.is_success:
            if response.request.headers.get("Content-Type") == "application/json":
                self.collect_examples(json.loads(response.request.content))
            if response.headers.get("Content-Type") == "application/json":
                response.read()
                self.collect_examples(response.json())
        for path, methods in self.routes:
            if path.fullmatch(response.request.url.path):
                operation = methods.get(response.request.method.lower())
                if operation:
                    self.check_response(operation, response)
                return

    def check_response(self, operation, response):
        name = operation["operationId"]
        status = str(response.status_code)
        assert status in operation["responses"], (name, status)
        mime = response.headers["content-type"].split(";")[0]
        content = operation["responses"][status]["content"]
        assert mime in content, (name, mime)
        response.read()
        if mime == "text/event-stream":
            for line in response.text.splitlines():
                if line.startswith("data: "):
                    event = json.loads(line[6:])
                    validate_contract("Event", event)
                    self.collect_examples(event)
        else:
            body_schema = json.loads(json.dumps(content[mime]["schema"]).replace("./schemas/domain.schema.json#", "#"))
            validator = Draft202012Validator(dict(body_schema, **{"$defs": definitions()}), format_checker=FormatChecker())
            error = next(validator.iter_errors(response.json()), None)
            assert error is None, (name, error.json_path, error.message)
        if response.is_success:
            self.success.add(name)
