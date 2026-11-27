"""Plan sampling over the whole source before spending frame budget."""

from uuid import UUID, uuid5

WINDOW_US = 60_000_000
POLICY = "windows-60s-four-centres-v1"
PIPELINE = "cutsieve-media-v1"
SEGMENT_STEP_US = 8_000_000
SEGMENT_LENGTH_US = 10_000_000


def identity(namespace, key):
    return str(uuid5(UUID(namespace), str(key)))


def windows(duration):
    return [(start, min(start + WINDOW_US, duration)) for start in range(0, duration, WINDOW_US)]


def sample_points(start, end, slot):
    return start + (2 * slot + 1) * (end - start) // 8


def segments(duration):
    start = 0
    while start < duration:
        end = min(start + SEGMENT_LENGTH_US, duration)
        yield start, end
        if end == duration:
            break
        start += SEGMENT_STEP_US


def segment_bounds(actual_us, duration):
    """The last short tail stays inside the segment that already spans it."""
    last = max(0, (duration - SEGMENT_LENGTH_US + SEGMENT_STEP_US - 1) // SEGMENT_STEP_US)
    start = min(actual_us // SEGMENT_STEP_US, last) * SEGMENT_STEP_US
    return start, min(start + SEGMENT_LENGTH_US, duration)


def frame_quotas(count, budget):
    if not count:
        return []
    base, extra = divmod(min(budget, count * 8), count)
    chosen = {round(i * (count - 1) / max(1, extra - 1)) for i in range(extra)}
    return [base + int(i in chosen) for i in range(count)]


def coverage(run, index, span, stage, frames=(), reason=None, *, modality="VISUAL", evidence=()):
    start, end = span
    times = sorted({start, end, *(f["actual_us"] for f in frames if start < f["actual_us"] < end)})
    key = f"{index}:{modality}:{stage}"
    return dict(
        coverage_id=identity(run.id, "coverage:" + key),
        workspace_id=run.workspace,
        project_id=run.project,
        media_revision_id=run.revision,
        processing_run_id=run.id,
        range=dict(start_us=start, end_us=end),
        stage=stage,
        protocol_id=POLICY,
        modality=modality,
        policy_version=POLICY,
        query_fingerprint=None,
        query_run_id=None,
        frame_artifact_ids=list(dict.fromkeys(f["artifact_id"] for f in frames)),
        evidence_ids=list(evidence),
        failure_reason=reason,
        max_observation_gap_us=(
            max((b - a for a, b in zip(times, times[1:])), default=None) if frames else None
        ),
    )
