"""Validate v1 requests against the same contracts used by the frontend and tools."""
from copy import deepcopy
from dataclasses import dataclass
import json
import re
import sysconfig
from functools import cache
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker


@cache
def contracts_dir() -> Path:
    source = Path(__file__).resolve().parents[3] / "docs/videomind-spec/contracts"
    if source.is_dir():
        return source
    return Path(sysconfig.get_path("data")) / "share/videomind/contracts"


@cache
def definitions() -> dict:
    path = contracts_dir() / "schemas/domain.schema.json"
    return json.loads(path.read_text(encoding="utf-8"))["$defs"]


@cache
def validator(name):
    schema = {"$ref": f"#/$defs/{name}", "$defs": definitions()}
    return Draft202012Validator(schema, format_checker=FormatChecker())


def validate_contract(name: str, body: dict) -> None:
    error = next(validator(name).iter_errors(body), None)
    if error:
        path = ".".join(map(str, error.absolute_path)) or name
        raise ValueError(f"{path}: invalid {error.validator}")


def _bundle(node):
    """Inline the domain definitions so providers never resolve paths or external URLs."""
    if isinstance(node, list):
        return [_bundle(item) for item in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node:
        ref = node["$ref"]
        match = re.fullmatch(r"(?:\./schemas/domain\.schema\.json)?#/\$defs/(\w+)", ref)
        if not match or match[1] not in definitions():
            raise ValueError("Unsupported tool schema reference")
        resolved = _bundle(definitions()[match[1]])
        siblings = {key: _bundle(value) for key, value in node.items() if key != "$ref"}
        return {"allOf": [resolved, siblings]} if siblings else resolved
    return {key: _bundle(value) for key, value in node.items()}


@dataclass(frozen=True)
class ToolContract:
    name: str
    provider_name: str
    roles: tuple[str, ...]
    effect: str
    timeout_seconds: int
    idempotency_required: bool
    server_injected: tuple[str, ...]
    description: str
    input_schema: dict
    output_schema: dict

    def validate(self, value, *, output=False):
        schema = self.output_schema if output else self.input_schema
        error = next(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(value), None)
        if error:
            path = ".".join(map(str, error.absolute_path)) or self.name
            raise ValueError(f"{path}: invalid {error.validator}")


class ToolCatalog:
    """Role-scoped, bidirectional mapping between internal and provider tool names."""

    def __init__(self, rows):
        self.tools = {}
        self.by_provider = {}
        for row in rows:
            name, wire = row["name"], row["provider_name"]
            if name in self.tools or wire in self.by_provider or not re.fullmatch(r"[\w-]{1,64}", wire, re.ASCII):
                raise ValueError("Duplicate tool identity or invalid provider name")
            tool = ToolContract(name, wire, tuple(row["allowed_roles"]), row["effect"],
                                row["timeout_seconds"], row["idempotency_required"],
                                tuple(row["server_injected"]), row["notes"],
                                _bundle(row["input_schema"]), _bundle(row["output_schema"]))
            self.tools[name] = tool
            self.by_provider[wire] = tool

    def for_provider_call(self, wire, role):
        tool = self.by_provider.get(wire)
        if tool is None or role not in tool.roles:
            raise ValueError("Unknown or unauthorized tool")
        return tool

    def provider_tools(self, role):
        return [{"type": "function", "function": {"name": t.provider_name,
                 "description": t.description, "parameters": deepcopy(t.input_schema)}}
                for t in self.tools.values() if role in t.roles]


@cache
def tool_catalog():
    registry = json.loads((contracts_dir() / "tool-registry.json").read_text(encoding="utf8"))
    return ToolCatalog(registry["tools"])
