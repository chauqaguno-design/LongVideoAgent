"""Independent controls required in addition to retrieval scores in release handbook §3."""

from collections import defaultdict
import hashlib
import json


CONTROLS = {
    "tenant_isolation": ("api", "retrieval", "artifact", "cache", "worker", "deletion"),
    "published_generation": ("no_mixed_generations", "no_building_visibility"),
    "edit_integrity": (
        "source_ranges",
        "revision",
        "constraints",
        "full_decode",
        "audio_mapping",
        "subtitle_mapping",
    ),
    "idempotent_effects": ("same_key", "duplicate_delivery", "worker_takeover", "crash_recovery"),
}


def control_gates(predictions, paths=()):
    """Load actual evidence bytes, bound by digest to the frozen predictions and by commit to this run."""
    expected = predictions.get("audit_evidence", [])
    observations = defaultdict(list)
    observed_digests = set()
    for path in paths:
        raw = path.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if not isinstance(expected, list) or digest not in expected:
            raise ValueError("audit receipt digest is not pinned by predictions")
        observed_digests.add(digest)
        report = json.loads(raw)
        if report.get("commit") != predictions["commit"] or not report.get("test_run"):
            raise ValueError("audit receipt does not identify the measured build and test run")
        for item in report["checks"]:
            category, name = item["control"], item["case"]
            if name not in CONTROLS.get(category, ()):
                raise ValueError("unknown release control")
            tested, bad = item["tested"], item["violations"]
            if type(tested) is not int or tested < 1 or type(bad) is not int or not 0 <= bad <= tested:
                raise ValueError("invalid operational control denominator")
            if category == "tenant_isolation" and len(set(item.get("workspaces", []))) < 2:
                raise ValueError("isolation evidence must exercise at least two workspaces")
            observations[(category, name)].append(bad)
    gates, missing = {}, []
    for category, names in CONTROLS.items():
        absent = [n for n in names if (category, n) not in observations]
        missing.extend(category + "." + n for n in absent)
        violations = sum(sum(observations[category, n]) for n in names)
        gates[category] = False if violations else None if absent else True
    if isinstance(expected, list) and set(expected) - observed_digests:
        missing.extend("receipt." + digest for digest in sorted(set(expected) - observed_digests))
        gates["pinned_receipts"] = None
    return gates, missing


def status(split, gates):
    if split == "DEV":
        return "DEVELOPMENT_ONLY"
    if any(v is False for v in gates.values()):
        return "FAILED"
    if any(v is None for v in gates.values()):
        return "BLOCKED"
    return "PASSED"
