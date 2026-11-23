"""List or remove expired duplicate parts and obsolete attempts in a local VideoMind workspace."""
import argparse
from pathlib import Path
from contextlib import ExitStack

from sqlalchemy import create_engine
from dovideo.application.edit_sources import EditSources
from dovideo.infrastructure.persistence.edit_cleanup import cleanup_files


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("workspace", nargs="?", type=Path, help="Local service work directory, e.g. work/r1-media")
    p.add_argument("--r2", action="store_true", help="Use the configured R2 database and object store")
    p.add_argument("--retention-hours", type=float, default=24)
    p.add_argument("--apply", action="store_true")
    args = p.parse_args()
    if bool(args.workspace) == args.r2:
        p.error("Supply one local workspace or --r2")
    with ExitStack() as stack:
        if args.r2:
            from dovideo.infrastructure import R2Settings, create_r2_infrastructure
            from dovideo.infrastructure.object_sources import SourceObjects
            infra = create_r2_infrastructure(R2Settings.from_environment(require_production=True))
            stack.callback(infra.close)
            store = EditSources(infra.engine, infra.settings.media_workspace / "editing")
            store.objects = SourceObjects(infra.minio_client, infra.settings.minio_bucket)
        else:
            root = (args.workspace / "editing").resolve()
            db = root / "editing.sqlite3"
            if not db.is_file():
                p.error("The editing database was not found")
            engine = create_engine(f"sqlite:///{db.as_posix()}")
            stack.callback(engine.dispose)
            store = EditSources(engine, root)
        for name in cleanup_files(store, older_than_seconds=args.retention_hours * 3600, apply=args.apply):
            print(name)
