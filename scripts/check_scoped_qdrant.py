"""Check pre-rank scope filters against embedded Qdrant or a dedicated test server."""

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4
from dovideo.infrastructure.vector.evidence_index import EvidenceIndex, point


def check(path=None, url=None):
    index = EvidenceIndex(path=path, url=url)
    model = dict(version="scope-smoke-v1", dimensions=3)
    ws, project, mid, source_gen, segment = (str(uuid4()) for _ in range(5))

    def row(**changes):
        datum = dict(
            workspace_id=ws,
            project_id=project,
            media_revision_id=mid,
            source_generation_id=source_gen,
            segment_id=segment,
            evidence_id=str(uuid4()),
            range={"start_us": 0, "end_us": 1_000_000},
        )
        return dict(datum, **changes)

    correct = row()
    closer = [
        row(workspace_id=str(uuid4())),
        row(project_id=str(uuid4())),
        row(source_generation_id=str(uuid4())),
        row(media_revision_id=str(uuid4())),
        row(range={"start_us": 9_000_000, "end_us": 10_000_000}),
    ]
    try:
        index.write(model, [point(correct, [0.8, 0.6, 0])] + [point(r, [1, 0, 0]) for r in closer])
        top = index.rank(model, [1, 0, 0], [correct], 1)
        assert [k for k, _ in top] == [correct["evidence_id"]]
        return dict(
            engine="Qdrant HTTP" if url else "qdrant-client embedded",
            inserted=6,
            closer_ineligible_points=5,
            top_k=1,
            scoped_correct_top1=True,
            score=top[0][1],
            tenant_leaks=0,
        )
    finally:
        index.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--qdrant-url", help="A dedicated test server for the probe collection")
    args = parser.parse_args()
    with TemporaryDirectory() as root:
        report = check(None if args.qdrant_url else root, args.qdrant_url)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf8")
    print(report)
