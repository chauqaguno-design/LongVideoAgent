"""Run upload acceptance against isolated local containers; remove only the containers we created."""

import argparse
from contextlib import ExitStack
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
from uuid import uuid4

from alembic import command
from alembic.runtime.migration import MigrationContext
import pymysql
from sqlalchemy import create_engine

from dovideo.infrastructure.persistence.migrations import (
    migration_config,
    require_schema_head,
    upgrade_schema,
)

IMAGES = {
    "mysql": "mysql:8.0.43",
    "redis": "redis:7.2.10-alpine",
    "minio": "milvusdb/minio:RELEASE.2024-12-18T13-15-44Z",
    "rabbit": "rabbitmq:4-management",
}
ROOT = Path(__file__).resolve().parents[1]


def start_service(stack, name, kind, port, env=None, args=()):
    values = {**os.environ, **(env or {})}
    flags = [v for key in (env or {}) for v in ("--env", key)]
    command = [
        "docker",
        "run",
        "--pull=never",
        "--rm",
        "-d",
        "--name",
        name + "-" + kind,
        "-p",
        f"127.0.0.1::{port}",
        *flags,
        IMAGES[kind],
        *args,
    ]
    container = subprocess.check_output(command, env=values, text=True).strip()
    stack.callback(
        subprocess.run, ["docker", "rm", "-fv", container], stdout=subprocess.DEVNULL, check=True
    )
    binding = subprocess.check_output(["docker", "port", container, f"{port}/tcp"], text=True).strip()
    return int(binding.rsplit(":", 1)[1])


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "work/spec-evidence/processing/services.json")
    parser.add_argument("--legacy", action="store_true", help="Also verify legacy broker and storage integrations")
    parser.add_argument(
        "--extra-test", action="append", default=[], help="Additional local pytest file or node"
    )
    options = parser.parse_args()
    options.output.parent.mkdir(parents=True, exist_ok=True)
    name = "videomind-accept-" + uuid4().hex[:10]
    password = secrets.token_hex(18)
    admin_password = secrets.token_hex(18)
    with ExitStack() as stack:
        mysql = start_service(
            stack,
            name,
            "mysql",
            3306,
            {
                "MYSQL_USER": "vmaccept",
                "MYSQL_PASSWORD": password,
                "MYSQL_DATABASE": "vm_accept",
                "MYSQL_ROOT_PASSWORD": admin_password,
                **({"MYSQL_ROOT_HOST": "%"} if options.legacy else {}),
            },
            ["--default-authentication-plugin=mysql_native_password"],
        )
        redis = start_service(stack, name, "redis", 6379)
        minio = start_service(
            stack,
            name,
            "minio",
            9000,
            {"MINIO_ROOT_USER": "vmaccept", "MINIO_ROOT_PASSWORD": password},
            ["server", "/data"],
        )
        deadline = time.monotonic() + 120
        while True:
            try:
                conn = pymysql.connect(
                    host="127.0.0.1",
                    port=mysql,
                    user="vmaccept",
                    password=password,
                    database="vm_accept",
                    connect_timeout=2,
                )
                conn.close()
                break
            except pymysql.MySQLError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Isolated MySQL did not become ready") from None
                time.sleep(1)
        url = f"mysql+pymysql://vmaccept:{password}@127.0.0.1:{mysql}/vm_accept"
        engine = create_engine(url)
        try:
            upgrade_schema(engine)
            with engine.connect() as db:
                config = migration_config()
                config.attributes["connection"] = db
                command.downgrade(config, "0004_media_content_identity")
                db.commit()
            upgrade_schema(engine)
            require_schema_head(engine)
            with engine.connect() as db:
                head = MigrationContext.configure(db).get_current_revision()
        finally:
            engine.dispose()
        environment = dict(
            os.environ,
            DOVIDEO_PROFILE="production",
            DOVIDEO_UPLOAD_LIVE="1",
            VIDEOMIND_FIXTURE_MYSQL=name + "-mysql",
            DOVIDEO_MEDIA_SIGNING_KEY=secrets.token_hex(32),
            DOVIDEO_DATABASE_URL=url,
            DOVIDEO_REDIS_URL=f"redis://127.0.0.1:{redis}/0",
            DOVIDEO_MINIO_ENDPOINT=f"127.0.0.1:{minio}",
            DOVIDEO_MINIO_ACCESS_KEY="vmaccept",
            DOVIDEO_MINIO_SECRET_KEY=password,
            DOVIDEO_MINIO_BUCKET="vm-accept",
            DOVIDEO_MINIO_SECURE="false",
            DOVIDEO_QDRANT_URL="http://127.0.0.1:1",
            DOVIDEO_QDRANT_COLLECTION="upload_acceptance_unused",
        )
        if ffmpeg_dir := environment.get("DOVIDEO_FFMPEG_DIR"):
            environment["PATH"] = os.pathsep.join((ffmpeg_dir, os.environ["PATH"]))
        started = time.monotonic()
        tests = [
            "tests/infrastructure/test_upload_redis_minio_live.py",
            "tests/media_ingestion/test_minio_live.py",
            *options.extra_test,
        ]
        if options.legacy:
            rabbit = start_service(stack, name, "rabbit", 5672,
                                   {"RABBITMQ_DEFAULT_USER": "vmaccept", "RABBITMQ_DEFAULT_PASS": password})
            from kombu import Connection
            broker_url = f"amqp://vmaccept:{password}@127.0.0.1:{rabbit}//"
            with Connection(broker_url, connect_timeout=2) as broker:
                broker.ensure_connection(max_retries=25, interval_start=1, interval_max=2)
            environment.update(
                DOVIDEO_GOVERNANCE_BROKER=broker_url,
                DOVIDEO_GOVERNANCE_REDIS=f"redis://127.0.0.1:{redis}/9",
                DOVIDEO_MEMORY_REDIS_LIVE="1", DOVIDEO_TASK_LEASE_LIVE="1", DOVIDEO_AI_LIMITER_LIVE="1",
                DOVIDEO_ENGINEERING_LIVE="1",
                DOVIDEO_ENGINEERING_ADMIN_URL=f"mysql+pymysql://root:{admin_password}@127.0.0.1:{mysql}/mysql",
            )
            tests.extend(str(p.relative_to(ROOT)) for p in sorted((ROOT / "tests/infrastructure").glob("*live.py"))
                         if str(p.relative_to(ROOT)).replace("\\", "/") not in tests)
            tests.extend(["tests/application/test_m1_first_turn_continuity.py",
                          "tests/infrastructure/test_response_files.py::test_real_redis_cross_instance_grants_expire"])
        run = subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "--tb=short", *tests],
            cwd=ROOT,
            env=environment,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        sanitized = run.stdout.replace(password, "[redacted]").replace(admin_password, "[redacted]")
        print(sanitized)
        options.output.write_text(
            json.dumps(
                dict(
                    images=IMAGES,
                    migrations_head=head,
                    upgrade_runs=2,
                    intermediate_downgrade="0004_media_content_identity",
                    tests=tests,
                    exit_code=run.returncode,
                    elapsed_seconds=round(time.monotonic() - started, 3),
                    outcome=sanitized.strip().splitlines()[-1],
                    external_model_calls=0,
                ),
                indent=2,
            ),
            encoding="utf-8",
        )
        return run.returncode


if __name__ == "__main__":
    raise SystemExit(main())
