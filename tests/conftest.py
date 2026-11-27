from contextlib import contextmanager

import pytest

from cutsieve.application.edit_instructions import PROFILE
from cutsieve.application.edit_timeline import uid
from cutsieve.application.workflow_checkpoint import (
    Checkpoint, ContextManifest, EditBrief, PlanAttempt, RepairHistory, RunSpec,
)


@pytest.fixture
def use_workspace(monkeypatch):
    from cutsieve.presentation.api.app import local_workspace_user

    @contextmanager
    def select(client, user_id):
        with monkeypatch.context() as patch:
            patch.setitem(client.app.dependency_overrides, local_workspace_user, lambda: {"id": user_id})
            yield

    return select


@pytest.fixture
def make_checkpoint():
    def make(*, scope=None, **constraints):
        scope = scope or dict(project_id=uid(), media_revision_ids=[uid()])
        defaults = dict(
            brief_id=uid(), project_id=scope["project_id"], goal="Find the requested clip",
            source_media_revision_ids=scope["media_revision_ids"], selection_mode="SEMANTIC",
            duration_target_us=None, duration_tolerance_us=0, order_policy="USER_ORDER",
            audio_policy="KEEP_SOURCE", must_include=[], must_exclude=[], output_profile=PROFILE,
        )
        brief = EditBrief.model_validate(defaults | constraints)
        return Checkpoint(
            spec=RunSpec(scope=scope, message=brief.goal, brief=brief, created=0),
            attempt=PlanAttempt(), context=ContextManifest(), repair=RepairHistory(),
        )
    return make
