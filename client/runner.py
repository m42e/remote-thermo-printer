"""WebSocket client loop: connect, receive jobs, print, acknowledge, reconnect."""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress

import websockets
from websockets.exceptions import WebSocketException

from client.config import ClientSettings
from client.printer import ReceiptPrinter
from client.updater import UpdateError, apply_update, restart
from common import __version__
from common.bundle import current_revision
from common.protocol import (
    Ack,
    ClientHello,
    ErrorMessage,
    JobMessage,
    UpdateMessage,
    Welcome,
    parse_server_message,
)

logger = logging.getLogger("rtp.client")

# Receipts with images / PDFs can be a few MB once base64 encoded.
_MAX_MESSAGE_BYTES = 64 * 1024 * 1024


class PrinterClient:
    def __init__(self, settings: ClientSettings) -> None:
        self.settings = settings
        self.printer = ReceiptPrinter(settings)

    async def run_forever(self) -> None:
        """Maintain a connection to the backend, reconnecting with backoff."""
        delay = self.settings.reconnect_min_seconds
        while True:
            try:
                await self._session()
                delay = self.settings.reconnect_min_seconds
            except (OSError, WebSocketException) as exc:
                logger.warning("Connection problem: %s", exc)
            except Exception:  # pragma: no cover - defensive
                logger.exception("Unexpected client error")
            logger.info("Reconnecting in %.1fs", delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, self.settings.reconnect_max_seconds)

    async def _session(self) -> None:
        url = self.settings.server_url
        logger.info("Connecting to %s", url)
        async with websockets.connect(
            url,
            ping_interval=20,
            ping_timeout=20,
            max_size=_MAX_MESSAGE_BYTES,
        ) as websocket:
            hello = ClientHello(
                printer_id=self.settings.printer_id,
                name=self.settings.printer_name,
                token=self.settings.token,
                version=__version__,
                bundle_revision=current_revision(),
            )
            await websocket.send(hello.model_dump_json())
            logger.info("Registered as printer '%s'", self.settings.printer_id)

            async for raw in websocket:
                try:
                    payload = json.loads(raw)
                    message = parse_server_message(payload)
                except Exception:
                    logger.warning("Ignoring malformed message from server")
                    continue

                if isinstance(message, JobMessage):
                    await self._handle_job(websocket, message)
                elif isinstance(message, UpdateMessage):
                    await self._handle_update(websocket, message)
                elif isinstance(message, Welcome):
                    logger.info("Server welcome: %s", message.message or "connected")
                elif isinstance(message, ErrorMessage):
                    logger.error("Server reported error: %s", message.detail)

    async def _handle_job(self, websocket, message: JobMessage) -> None:
        receipt = message.receipt
        logger.info(
            "Received job %s: %d element(s) [%s]",
            message.job_id,
            len(receipt.elements),
            ", ".join(el.type for el in receipt.elements) or "empty",
        )
        try:
            # Printing is blocking; run it off the event loop so the WebSocket
            # keepalive keeps flowing even during long prints.
            await asyncio.to_thread(self.printer.print_receipt, receipt)
            ack = Ack(job_id=message.job_id, status="printed")
            logger.info("Job %s printed", message.job_id)
        except Exception as exc:
            logger.exception("Failed to print job %s", message.job_id)
            ack = Ack(job_id=message.job_id, status="error", detail=str(exc)[:300])
        await websocket.send(ack.model_dump_json())

    async def _handle_update(self, websocket, message: UpdateMessage) -> None:
        """Install a pushed code update and reload, unless opted out."""
        revision = message.revision
        if not self.settings.auto_update:
            logger.info(
                "Update %s available but auto-update is disabled", revision[:12]
            )
            return
        if revision == current_revision():
            # Already up to date (e.g. a redundant offer); nothing to do.
            return
        logger.info(
            "Applying update %s (%d file(s))", revision[:12], len(message.files)
        )
        try:
            apply_update(message)
        except UpdateError as exc:
            logger.error("Update failed, staying on current code: %s", exc)
            return
        except Exception:  # pragma: no cover - defensive
            logger.exception("Unexpected error applying update; staying on current code")
            return
        # New code is on disk. Close the socket so the server requeues anything
        # in flight, then re-exec into the updated code (never returns).
        with suppress(Exception):
            await websocket.close()
        restart()
