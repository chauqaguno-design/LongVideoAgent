"""Transactional local wake-ups for SQLite-backed work."""

import asyncio
import logging
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


class WorkOutbox:
    def __init__(self, repo):
        self.repo = repo
        self.task = None

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
        # The committed event identifies the workspace and job that may wake a run.
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
                    last_error="DELIVERY_FAILED" if error else None,
                    lease=0,
                    available=int(time.time()) + min(60, 2 ** min(attempt, 6)) if retry else 0,
                )
            )

    def dispatch_one(self):
        claimed = self.claim()
        if not claimed:
            return False
        key, attempt = claimed
        try:
            self.receive(key)
        except Exception as exc:
            log.warning("Outbox delivery failed (%s)", type(exc).__name__)
            self.sent(key, attempt, error=True)
            return False
        self.sent(key, attempt)
        return True

    async def start(self):
        self.stopping = asyncio.Event()
        self.task = asyncio.create_task(self.serve())

    async def stop(self):
        if self.task is not None:
            self.stopping.set()
            await self.task
            self.task = None

    async def serve(self):
        while not self.stopping.is_set():
            try:
                if await asyncio.to_thread(self.dispatch_one):
                    continue
            except Exception as exc:
                log.warning("Outbox delivery unavailable (%s)", type(exc).__name__)
            try:
                await asyncio.wait_for(self.stopping.wait(), timeout=1)
            except TimeoutError:
                pass
