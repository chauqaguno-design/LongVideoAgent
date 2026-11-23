"""Opt-in acceptance on a fresh disposable RabbitMQ container, using real publisher confirms."""

import os
import secrets
import subprocess
import time
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, update
from dovideo.application.edit_sources import EditSources
from dovideo.infrastructure.persistence.edit_repository import EditRepository
from dovideo.infrastructure.persistence.outbox_tables import outbox, inbox
from dovideo.infrastructure.persistence.work_outbox import Rabbit, WorkOutbox, emit
from dovideo.infrastructure.persistence.sqlalchemy import Base


def docker(*args):
    return subprocess.run(
        ["docker", *args], capture_output=True, check=True, text=True, timeout=70
    ).stdout.strip()


def ready(broker):
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            with broker.connection.clone() as conn:
                conn.ensure_connection(max_retries=0)
                return
        except Exception:
            time.sleep(0.5)
    pytest.fail("isolated RabbitMQ did not start")


@pytest.mark.skipif(
    not os.getenv("VIDEOMIND_TEST_RABBIT_IMAGE"), reason="Requires explicit isolated Docker RabbitMQ fixture"
)
def test_real_confirm_reconnect_duplicate_and_lost_ack(tmp_path):
    image = os.environ["VIDEOMIND_TEST_RABBIT_IMAGE"]
    password = secrets.token_hex(12)
    container = docker(
        "run",
        "--pull=never",
        "-d",
        "--name",
        "vm-outbox-test-" + uuid4().hex[:12],
        "-p",
        "127.0.0.1::5672",
        "-e",
        "RABBITMQ_DEFAULT_USER=vmtest",
        "-e",
        "RABBITMQ_DEFAULT_PASS=" + password,
        image,
    )
    try:
        port = docker("port", container, "5672/tcp").rsplit(":", 1)[-1]
        broker = Rabbit(f"amqp://vmtest:{password}@127.0.0.1:{port}/%2F")
        ready(broker)
        version = docker("exec", container, "rabbitmqctl", "version")
        engine = create_engine(f"sqlite:///{tmp_path}/outbox.sqlite")
        Base.metadata.create_all(engine, tables=[outbox, inbox])
        repo = EditRepository(EditSources(engine, tmp_path))
        relay = WorkOutbox(repo, broker)
        key = str(uuid4())
        with repo.write() as db:
            emit(db, key, str(uuid4()), str(uuid4()), "workflow.event")
        claimed = relay.claim()
        assert claimed == (key, 1)
        broker.publish(key)
        # The broker confirmed the persistent message; the dispatcher died before saving SENT.
        docker("stop", "-t", "10", container)
        with repo.write() as db:
            db.execute(update(outbox).where(outbox.c.id == key).values(lease=0))
        assert relay.dispatch_one() is False
        with repo.sessions() as db:
            pending = db.execute(select(outbox).where(outbox.c.id == key)).one()
            assert pending.status == "PENDING" and pending.last_error == "BROKER_UNAVAILABLE"
        docker("start", container)
        port = docker("port", container, "5672/tcp").rsplit(":", 1)[-1]
        relay.broker = broker = Rabbit(f"amqp://vmtest:{password}@127.0.0.1:{port}/%2F")
        ready(broker)
        outcomes = []

        class LostAck(Exception):
            pass

        def die_after_commit(event_id):
            outcomes.append(relay.receive(event_id))
            raise LostAck

        with pytest.raises(LostAck):
            broker.consume(die_after_commit)
        with repo.write() as db:
            db.execute(update(outbox).where(outbox.c.id == key).values(available=0))
        assert relay.dispatch_one()
        broker.consume(lambda event_id: outcomes.append(relay.receive(event_id)))
        with repo.sessions() as db:
            assert db.execute(select(outbox.c.status)).scalar() == "SENT"
            assert db.execute(select(inbox.c.id)).scalars().all() == [key]
        assert outcomes == [True, False]
        print("confirmed persistent deliveries, restart and lost acknowledgment; RabbitMQ", version)
        engine.dispose()
    finally:
        docker("rm", "-fv", container)
