"""Run the client with: ``python -m client`` or ``rtp-client``."""

from __future__ import annotations

import asyncio
import logging

from client.config import get_settings
from client.runner import PrinterClient


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    settings = get_settings()
    logger = logging.getLogger("rtp.client")
    logger.info(
        "Starting printer client '%s' (connection=%s)",
        settings.printer_id,
        settings.connection.value,
    )
    client = PrinterClient(settings)
    try:
        asyncio.run(client.run_forever())
    except KeyboardInterrupt:
        logger.info("Shutting down")


if __name__ == "__main__":
    main()
