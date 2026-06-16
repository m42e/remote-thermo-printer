"""Run the backend with: ``python -m server`` or ``rtp-server``."""

from __future__ import annotations

import uvicorn

from server.config import get_settings


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "server.app:app",
        host=settings.host,
        port=settings.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
