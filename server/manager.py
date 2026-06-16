"""Connection registry and job routing for the backend.

The manager keeps track of:

* connected printer clients (one :class:`Connection` per WebSocket),
* a queue of jobs per connection,
* a list of *pending* jobs that arrived while no suitable client was online,
* the lifecycle state of every job that was ever submitted.

Delivery is *at-least-once*: if a client disconnects before acknowledging a
job, the job is requeued so the next matching client prints it.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, List, Optional
from uuid import uuid4

from fastapi import WebSocket

from common.protocol import Ack, JobMessage, Receipt

logger = logging.getLogger("rtp.server")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobState(str, Enum):
    pending = "pending"   # waiting for a client to come online
    sent = "sent"         # delivered to a client, awaiting acknowledgement
    printed = "printed"   # client confirmed a successful print
    error = "error"       # client reported a failure


class JobRecord:
    """A single print job targeted at exactly one printer."""

    def __init__(self, receipt: Receipt, target: Optional[str]) -> None:
        self.job_id: str = uuid4().hex
        self.receipt: Receipt = receipt
        self.target: Optional[str] = target
        self.printer_id: Optional[str] = None
        self.state: JobState = JobState.pending
        self.detail: Optional[str] = None
        self.created_at: datetime = _utcnow()
        self.updated_at: datetime = _utcnow()

    def info(self) -> dict:
        """A small, JSON-safe summary (without the heavy element payloads)."""
        return {
            "job_id": self.job_id,
            "receipt_id": self.receipt.id,
            "title": self.receipt.title,
            "state": self.state.value,
            "target": self.target,
            "printer_id": self.printer_id,
            "detail": self.detail,
            "element_kinds": [el.type for el in self.receipt.elements],
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


class Connection:
    """A live WebSocket connection to one printer client."""

    def __init__(self, websocket: WebSocket, printer_id: str, name: Optional[str]) -> None:
        self.websocket = websocket
        self.printer_id = printer_id
        self.name = name
        self.queue: "asyncio.Queue[JobRecord]" = asyncio.Queue()
        self.inflight: Dict[str, JobRecord] = {}
        self.connected_at = _utcnow()
        self.sender_task: Optional[asyncio.Task] = None

    def info(self) -> dict:
        return {
            "printer_id": self.printer_id,
            "name": self.name,
            "connected_at": self.connected_at.isoformat(),
            "queued": self.queue.qsize(),
            "inflight": len(self.inflight),
        }


class ConnectionManager:
    def __init__(self) -> None:
        self._connections: Dict[str, Connection] = {}
        self._pending: List[JobRecord] = []
        self._jobs: Dict[str, JobRecord] = {}
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ #
    # Connection lifecycle
    # ------------------------------------------------------------------ #
    async def connect(self, websocket: WebSocket, printer_id: str, name: Optional[str]) -> Connection:
        connection = Connection(websocket, printer_id, name)
        async with self._lock:
            existing = self._connections.get(printer_id)
            if existing is not None:
                logger.info("Replacing existing connection for printer %s", printer_id)
                if existing.sender_task is not None:
                    existing.sender_task.cancel()
                try:
                    await existing.websocket.close(code=4000)
                except Exception:  # pragma: no cover - best effort
                    pass
            self._connections[printer_id] = connection
            # Hand over any pending jobs that this printer can take.
            remaining: List[JobRecord] = []
            for job in self._pending:
                if job.target is None or job.target == printer_id:
                    connection.queue.put_nowait(job)
                else:
                    remaining.append(job)
            self._pending = remaining
        logger.info("Printer %s connected (%s)", printer_id, name or "unnamed")
        return connection

    async def disconnect(self, connection: Connection) -> None:
        async with self._lock:
            if self._connections.get(connection.printer_id) is connection:
                del self._connections[connection.printer_id]
            requeued: List[JobRecord] = list(connection.inflight.values())
            connection.inflight.clear()
            while not connection.queue.empty():
                requeued.append(connection.queue.get_nowait())
            for job in requeued:
                job.state = JobState.pending
                job.printer_id = None
                job.updated_at = _utcnow()
                self._pending.append(job)
        if requeued:
            logger.info(
                "Printer %s disconnected, requeued %d job(s)",
                connection.printer_id,
                len(requeued),
            )
        else:
            logger.info("Printer %s disconnected", connection.printer_id)

    # ------------------------------------------------------------------ #
    # Sender loop (one task per connection)
    # ------------------------------------------------------------------ #
    async def sender_loop(self, connection: Connection) -> None:
        while True:
            job = await connection.queue.get()
            message = JobMessage(job_id=job.job_id, receipt=job.receipt)
            try:
                await connection.websocket.send_json(message.model_dump(mode="json"))
            except Exception:
                # Delivery failed: put the job back so another client gets it.
                async with self._lock:
                    job.state = JobState.pending
                    job.printer_id = None
                    job.updated_at = _utcnow()
                    self._pending.append(job)
                raise
            job.state = JobState.sent
            job.printer_id = connection.printer_id
            job.updated_at = _utcnow()
            connection.inflight[job.job_id] = job
            logger.info("Sent job %s to printer %s", job.job_id, connection.printer_id)

    # ------------------------------------------------------------------ #
    # Incoming client messages
    # ------------------------------------------------------------------ #
    async def handle_ack(self, ack: Ack) -> None:
        async with self._lock:
            job = self._jobs.get(ack.job_id)
            if job is None:
                logger.warning("Received ack for unknown job %s", ack.job_id)
                return
            job.state = JobState.printed if ack.status == "printed" else JobState.error
            job.detail = ack.detail
            job.updated_at = _utcnow()
            connection = self._connections.get(job.printer_id) if job.printer_id else None
            if connection is not None:
                connection.inflight.pop(job.job_id, None)
        logger.info("Job %s -> %s (%s)", ack.job_id, ack.status, ack.detail or "")

    # ------------------------------------------------------------------ #
    # Job submission
    # ------------------------------------------------------------------ #
    async def submit(self, receipt: Receipt, target: Optional[str] = None) -> List[JobRecord]:
        """Queue a receipt for printing.

        * ``target`` set      -> exactly that printer (queued/pending).
        * ``target`` is None  -> every currently connected printer; if none are
          online the job is held until the next client connects.
        """
        created: List[JobRecord] = []
        async with self._lock:
            if target is not None:
                targets = [target]
            else:
                targets = list(self._connections.keys())

            if not targets:
                job = JobRecord(receipt, target)
                self._jobs[job.job_id] = job
                self._pending.append(job)
                created.append(job)
                logger.info("Queued job %s (no printer online yet)", job.job_id)
                return created

            for printer_id in targets:
                job = JobRecord(receipt, printer_id if target is not None else None)
                self._jobs[job.job_id] = job
                connection = self._connections.get(printer_id)
                if connection is not None:
                    connection.queue.put_nowait(job)
                else:
                    self._pending.append(job)
                created.append(job)
        return created

    # ------------------------------------------------------------------ #
    # Read-only views for the HTTP API
    # ------------------------------------------------------------------ #
    def printers(self) -> List[dict]:
        return [c.info() for c in self._connections.values()]

    def jobs(self, limit: int = 100) -> List[dict]:
        records = sorted(self._jobs.values(), key=lambda j: j.created_at, reverse=True)
        return [j.info() for j in records[:limit]]

    @property
    def pending_count(self) -> int:
        return len(self._pending)
