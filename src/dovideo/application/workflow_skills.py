"""Packaged, read-only phase skills. Each activated body is pinned before model execution."""

from functools import cache
from hashlib import sha256
from importlib.resources import files
import json

from .edit_timeline import EditError
from .spec_contracts import tool_catalog
from .workflow_identity import digest_json, generation_key


@cache
def metadata():
    package = files("dovideo").joinpath("harness_skills")
    return {item["id"]: item for item in json.loads(package.joinpath("registry.json").read_text("utf8"))}


def catalog_summary():
    return [{k: item[k] for k in ("id", "version", "description")} for item in metadata().values()]


def definitions_for(phase, allowed):
    catalog = tool_catalog()
    wires = {catalog.tools[name].provider_name for name in allowed}
    definitions = [tool for tool in catalog.provider_tools(phase) if tool["function"]["name"] in wires]
    return definitions


def tool_digest(phase, allowed):
    return digest_json(definitions_for(phase, allowed))


def use_skill(repo, row, skill_id, phase_allowed, *, purpose="tools"):
    phase = row.state["phase"]
    slot = generation_key(row.checkpoint, f"{phase}:{skill_id}:{purpose}")

    def load():
        item = metadata()[skill_id]
        if phase not in item["phases"]:
            raise EditError(422, "SKILL_PHASE", "此阶段不可加载指定技能")
        catalog = tool_catalog()
        allowed = sorted(
            name
            for name in phase_allowed.intersection(item["tools"])
            if name in catalog.tools and phase in catalog.tools[name].phases
        )
        path = files("dovideo").joinpath("harness_skills", f"{skill_id}.md")
        body = path.read_bytes()
        return dict(
            id=skill_id,
            version=item["version"],
            content=body.decode("utf8"),
            content_hash=sha256(body).hexdigest(),
            allowed=allowed,
            tool_schema_hash=tool_digest(phase, allowed),
        )

    snapshot = repo.pin_skill(row, slot, load)
    allowed = set(snapshot["allowed"])
    catalog = tool_catalog()
    if not allowed <= phase_allowed or any(
        name not in catalog.tools or phase not in catalog.tools[name].phases for name in allowed
    ):
        raise EditError(409, "TOOL_SCHEMA_CHANGED", "此阶段的可用工具有变化，请开启新任务")
    return snapshot, definitions_for(phase, allowed), allowed


def skill_identity(snapshot):
    return (
        {k: snapshot[k] for k in ("id", "version", "content_hash", "tool_schema_hash")} if snapshot else None
    )


def skill_guidance(snapshot):
    return "\nTask skill guidance:\n" + snapshot["content"] if snapshot else ""
