"""Back up a stopped local CutSieve workspace, or restore it to a new directory."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
from tempfile import TemporaryDirectory


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def absolute_path(path):
    path = Path(path).resolve()
    # Staging adds two path components to long content-addressed filenames.
    if os.name == "nt" and not str(path).startswith("\\\\?\\"):
        return Path("\\\\?\\UNC\\" + str(path)[2:] if str(path).startswith("\\\\") else "\\\\?\\" + str(path))
    return path


def new_directory(path):
    path = absolute_path(path)
    if path.exists():
        raise ValueError(f"Destination already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def backup(workspace, destination):
    source = absolute_path(workspace)
    destination = new_directory(destination)
    if not (source / "editing/editing.sqlite3").is_file() or destination.is_relative_to(source):
        raise ValueError("Use a local workspace and a separate backup destination")
    with TemporaryDirectory(dir=destination.parent) as temp:
        bundle = Path(temp) / "snapshot"
        files = {}
        for path in sorted(source.rglob("*")):
            if not path.is_file() or path.is_symlink() or path.name == ".media-url-key" or path.name.endswith(("-wal", "-shm", "-journal")):
                continue
            name = path.relative_to(source).as_posix()
            copy = bundle / "files" / name
            copy.parent.mkdir(parents=True, exist_ok=True)
            if path.suffix == ".sqlite3":
                with closing(sqlite3.connect(path)) as live, closing(sqlite3.connect(copy)) as db:
                    live.execute("PRAGMA query_only=ON")
                    live.backup(db)
            else:
                shutil.copyfile(path, copy)
            files[name] = digest(copy)
        (bundle / "manifest.json").write_text(json.dumps({"version": 1, "sha256": files}, indent=2), encoding="utf-8")
        bundle.rename(destination)
    return len(files)


def restore(snapshot, destination):
    snapshot = absolute_path(snapshot)
    destination = new_directory(destination)
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    if manifest["version"] != 1:
        raise ValueError("Unknown snapshot format")
    source = snapshot / "files"
    with TemporaryDirectory(dir=destination.parent) as temp:
        restored = Path(temp) / "workspace"
        for name, expected in manifest["sha256"].items():
            path = (source / name).resolve()
            if not path.is_relative_to(source) or Path(name).is_absolute() or ".." in Path(name).parts:
                raise ValueError("Invalid snapshot path")
            copy = restored / name
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, copy)
            if digest(copy) != expected:
                raise ValueError(f"Checksum mismatch: {name}")
        restored.rename(destination)
    return len(manifest["sha256"])


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["backup", "restore"])
    p.add_argument("source", type=Path)
    p.add_argument("destination", type=Path)
    args = p.parse_args()
    operation = backup if args.action == "backup" else restore
    print(f"{operation(args.source, args.destination)} files verified")
