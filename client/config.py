"""Client configuration, read from environment variables / .env."""

from __future__ import annotations

from enum import Enum
from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


class ConnectionType(str, Enum):
    usb = "usb"
    network = "network"
    serial = "serial"
    file = "file"
    cups = "cups"
    dummy = "dummy"


class ClientSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RTP_CLIENT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- backend connection ---
    server_url: str = "ws://localhost:8000/ws"
    token: Optional[str] = None
    printer_id: str = "pi-printer-1"
    printer_name: Optional[str] = None
    reconnect_min_seconds: float = 1.0
    reconnect_max_seconds: float = 30.0

    # --- printer behaviour ---
    connection: ConnectionType = ConnectionType.dummy
    # Width of the print head in dots; wider images/PDF pages are scaled down.
    printer_width: int = 576
    # Optional python-escpos profile (needed for hardware image centering).
    printer_profile: Optional[str] = None

    # --- usb (hex like "0x04b8" or decimal accepted) ---
    usb_vendor_id: Optional[str] = None
    usb_product_id: Optional[str] = None
    usb_in_ep: Optional[str] = None
    usb_out_ep: Optional[str] = None

    # --- network ---
    net_host: Optional[str] = None
    net_port: int = 9100

    # --- serial ---
    serial_devfile: str = "/dev/ttyUSB0"
    serial_baudrate: int = 9600

    # --- file (raw device node) ---
    file_devfile: str = "/dev/usb/lp0"

    # --- cups ---
    cups_printer_name: Optional[str] = None


@lru_cache
def get_settings() -> ClientSettings:
    return ClientSettings()
