"""Transactional wake-ups for SQL-backed work. The broker carries only an event UUID."""

import asyncio
import logging
import os
from queue import Empty
import time

from sqlalchemy import and_, insert, or_, select, update

from .outbox_tables import inbox, outbox

log = logging.getLogger(__name__)


def emit(db, key, workspace, resource, kind):
    db.execute(
        insert(outbox).values(
            id=key,
            workspace=workspace,
            resource=resource,
            kind=kind,
            status="PENDING",
            available=0,
            lease=0,
            attempt=0,
        )
    )


class Rabbit:
    def __init__(self, url):
        from kombu import Connection, Queue

        self.connection = Connection(
            url, connect_timeout=2, transport_options={"confirm_publish": True}, heartbeat=20
        )
        self.queue = Queue("videomind.work.v1", routing_key="videomind.work.v1", durable=True)

    def publish(self, key):
        with self.connection.clone() as conn:
            conn.Producer().publish(
                {"event_id": key},
                exchange="",
                routing_key=self.queue.name,
                declare=[self.queue],
                serializer="json",
                delivery_mode=2,
                mandatory=True,
                retry=False,
                confirm_timeout=3,
            )

    def consume(self, receive):
        with self.connection.clone() as conn, conn.SimpleQueue(self.queue) as queue:
            try:
                message = queue.get(block=False)
            except Empty:
                return
            payload = message.payload
            if not isinstance(payload, dict) or not isinstance(payload.get("event_id"), str):
                message.reject(requeue=False)
                return
            receive(payload["event_id"])
            message.ack()


class WorkOutbox:
    def __init__(self, repo, broker=None):
        self.repo = repo
        self.broker = broker

    @classmethod
    def configured(cls, repo):
        mode = os.getenv("DOVIDEO_WORKFLOW_OUTBOX_TRANSPORT", "local")
        if mode not in ("rabbitmq", "local"):
            raise ValueError("Unsupported workflow outbox transport")
        return cls(repo, Rabbit(os.environ["DOVIDEO_BROKER_URL"]) if mode == "rabbitmq" else None)

    def claim(self):
        now = int(time.time())
        eligible = or_(
            and_(outbox.c.status == "PENDING", outbox.c.available <= now),
            and_(outbox.c.status == "LEASED", outbox.c.lease <= now),
        )
        with self.repo.write() as db:
            event = db.execute(
                select(outbox)
                .where(eligible)
                .order_by(outbox.c.available, outbox.c.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            ).first()
            if not event:
                return None
            db.execute(
                update(outbox)
                .where(outbox.c.id == event.id)
                .values(status="LEASED", lease=now + 15, attempt=event.attempt + 1)
            )
            return event.id, event.attempt + 1

    def receive(self, key):
        # Derive recipient and kind from the committed event; never accept them from the broker.
        with self.repo.write() as db:
            event = db.execute(select(outbox).where(outbox.c.id == key).with_for_update()).first()
            if not event or db.execute(select(inbox.c.id).where(inbox.c.id == key)).first():
                return False
            if event.kind in ("render.finished", "processing.finished"):
                from .workflow_tables import runs

                waiting = db.execute(
                    select(runs)
                    .where(runs.c.workspace_id == event.workspace, runs.c.status == "WAITING_JOB")
                    .with_for_update()
                ).all()
                for row in waiting:
                    if event.resource in row.state["pending_job_ids"]:
                        db.execute(update(runs).where(runs.c.id == row.id).values(ready_at=0))
            db.execute(insert(inbox).values(id=key, accepted=int(time.time())))
            return True

    def sent(self, key, attempt, error=None):
        retry = error and attempt < 8
        status = "PENDING" if retry else "DEAD" if error else "SENT"
        with self.repo.write() as db:
            db.execute(
                update(outbox)
                .where(outbox.c.id == key, outbox.c.attempt == attempt, outbox.c.status == "LEASED")
                .values(
                    status=status,
                    last_error="BROKER_UNAVAILABLE" if error else None,
                    lease=0,
                    available=int(time.time()) + min(60, 2 ** min(attempt, 6)) if retry else 0,
                )
            )

    def dispatch_one(self):
        claimed = self.claim()
        if claimed:
            key, attempt = claimed
            try:
                if self.broker:
                    self.broker.publish(key)
                else:
                    self.receive(key)
            except Exception:
                self.sent(key, attempt, error=True)
                return False
            self.sent(key, attempt)
        if self.broker:
            self.broker.consume(self.receive)
        return bool(claimed)

    async def start(self):
        self.stopping = asyncio.Event()
        self.task = asyncio.create_task(self.serve())

    async def stop(self):
        self.stopping.set()
        await self.task

    async def serve(self):
        while not self.stopping.is_set():
            try:
                if await asyncio.to_thread(self.dispatch_one):
                    continue
            except Exception as exc:
                # Broker error strings can include a connection URL.
                log.warning("Outbox delivery unavailable (%s)", type(exc).__name__)
            try:
                await asyncio.wait_for(self.stopping.wait(), timeout=1)
            except TimeoutError:
                pass
