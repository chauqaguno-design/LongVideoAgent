"""Score immutable candidate/answer results against an independently annotated manifest."""

import argparse
import hashlib
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from dovideo.evaluation.video_grounding import evaluate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("predictions", type=Path)
    parser.add_argument("--dev", type=Path, help="Required for a blinded release evaluation")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--audit",
        type=Path,
        action="append",
        default=[],
        help="Measured JSON control receipt whose raw SHA-256 is pinned in predictions",
    )
    args = parser.parse_args()
    try:
        content, output = args.manifest.read_bytes(), args.predictions.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        report = evaluate(
            json.loads(content),
            json.loads(output),
            dev=json.loads(args.dev.read_bytes()) if args.dev else None,
            manifest_sha256=digest,
            audit_paths=args.audit,
        )
        report.update(manifest_sha256=digest, predictions_sha256=hashlib.sha256(output).hexdigest())
    except (OSError, ValueError, KeyError, TypeError, AttributeError, IndexError) as exc:
        report = dict(quality_status="BLOCKED", blind_quality_passed=False, reason=str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(mode="w", encoding="utf8", dir=args.output.parent, delete=False) as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(f.name, args.output)
    print(json.dumps(report.get("metrics", report), indent=2))
    return {"FAILED": 1, "BLOCKED": 2}.get(report["quality_status"], 0)


if __name__ == "__main__":
    raise SystemExit(main())
