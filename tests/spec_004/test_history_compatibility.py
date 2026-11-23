from pathlib import Path
from dovideo.domain import AgentState
from dovideo.infrastructure.persistence import JsonCheckpointCodec


def test_baseline_checkpoint_decodes_without_provider():
    legacy = (Path(__file__).parent / "fixtures/legacy-checkpoint.json").read_bytes()
    state = JsonCheckpointCodec().decode(legacy, AgentState)
    assert state.goal == "goal" and state.result.title == "title"
    assert state.plan.tasks == ("task",)
