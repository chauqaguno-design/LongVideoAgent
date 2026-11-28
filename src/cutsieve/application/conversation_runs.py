"""Read the explicit, project-scoped parent chain for a conversation."""


def turn_history(repo, db, workspace, run_id):
    turns = []
    while run_id:
        row = repo.readable(db, workspace, run_id)
        cp = row.checkpoint
        output = {}
        if key := row.state["result_artifact_id"]:
            output = repo.document(workspace, key, session=db).body
        turns.append(dict(
            run_id=row.id,
            parent_run_id=cp.spec.parent_run_id,
            message=cp.spec.message,
            media_revision_id=cp.spec.media_revision_id,
            image_artifact_id=cp.spec.image_artifact_id,
            status=row.status,
            answer=output.get("answer"),
            tool_steps=[step.model_dump() for step in cp.tool_steps],
            citations=[citation.model_dump() for citation in cp.citations],
            clarifications=[item.model_dump() for item in cp.clarifications],
            edit_project_id=cp.edit_project_id or output.get("edit_project_id"),
            revision_id=cp.revision_id or output.get("revision_id"),
            preview_artifact_id=cp.preview_artifact_id or output.get("preview_artifact_id"),
            export_artifact_id=cp.export_artifact_id or output.get("export_artifact_id"),
        ))
        run_id = cp.spec.parent_run_id
    return list(reversed(turns))


def conversation(repo, workspace, run_id):
    with repo.sessions() as db:
        return {"turns": turn_history(repo, db, workspace, run_id)}
