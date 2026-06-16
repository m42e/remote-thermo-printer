"""Wire protocol shared by the backend and the Raspberry Pi client.

A *receipt* is an ordered list of *elements*. Each element is one of:

* :class:`TextElement`  – formatted text
* :class:`ImageElement` – a raster image (PNG/JPEG/GIF/BMP), base64 encoded
* :class:`PdfElement`   – a PDF document, base64 encoded (rendered page by page)

Receipts are submitted to the backend over HTTP and forwarded to a connected
client over a WebSocket using the small message envelope defined at the bottom
of this module.
"""

from __future__ import annotations

import base64
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional, Union
from uuid import uuid4

from pydantic import BaseModel, Field, TypeAdapter
from typing_extensions import Annotated, Literal

PROTOCOL_VERSION = 1


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Alignment(str, Enum):
    left = "left"
    center = "center"
    right = "right"


class Font(str, Enum):
    a = "a"
    b = "b"


# --------------------------------------------------------------------------- #
# Receipt elements
# --------------------------------------------------------------------------- #
class TextElement(BaseModel):
    """A run of text with ESC/POS formatting options."""

    type: Literal["text"] = "text"
    content: str
    align: Alignment = Alignment.left
    bold: bool = False
    underline: int = Field(default=0, ge=0, le=2)
    double_width: bool = False
    double_height: bool = False
    font: Font = Font.a


class ImageElement(BaseModel):
    """A raster image, base64 encoded (PNG, JPEG, GIF or BMP)."""

    type: Literal["image"] = "image"
    data: str = Field(repr=False)
    align: Alignment = Alignment.center
    high_density: bool = True
    impl: Literal["bitImageRaster", "graphics", "bitImageColumn"] = "bitImageRaster"

    def decoded(self) -> bytes:
        return base64.b64decode(self.data)


class PdfElement(BaseModel):
    """A PDF document, base64 encoded. Every page is rendered and printed."""

    type: Literal["pdf"] = "pdf"
    data: str = Field(repr=False)
    align: Alignment = Alignment.center
    dpi: int = Field(default=203, ge=72, le=600)

    def decoded(self) -> bytes:
        return base64.b64decode(self.data)


ReceiptElement = Annotated[
    Union[TextElement, ImageElement, PdfElement],
    Field(discriminator="type"),
]


class Receipt(BaseModel):
    """A full receipt: a sequence of elements plus paper-handling options."""

    id: str = Field(default_factory=lambda: uuid4().hex)
    title: Optional[str] = None
    elements: List[ReceiptElement] = Field(default_factory=list)
    cut: bool = True
    feed_before_cut: int = Field(default=0, ge=0, le=10)
    open_drawer: bool = False
    created_at: datetime = Field(default_factory=_utcnow)


# --------------------------------------------------------------------------- #
# Convenience constructors
# --------------------------------------------------------------------------- #
def text_receipt(content: str, **kwargs: object) -> Receipt:
    """Build a receipt containing a single text element."""
    text_kwargs = {
        k: kwargs.pop(k)
        for k in ("align", "bold", "underline", "double_width", "double_height", "font")
        if k in kwargs
    }
    return Receipt(elements=[TextElement(content=content, **text_kwargs)], **kwargs)


def image_receipt(data: bytes, **kwargs: object) -> Receipt:
    """Build a receipt containing a single image element from raw bytes."""
    element_kwargs = {
        k: kwargs.pop(k) for k in ("align", "high_density", "impl") if k in kwargs
    }
    encoded = base64.b64encode(data).decode("ascii")
    return Receipt(elements=[ImageElement(data=encoded, **element_kwargs)], **kwargs)


def pdf_receipt(data: bytes, **kwargs: object) -> Receipt:
    """Build a receipt containing a single PDF element from raw bytes."""
    element_kwargs = {k: kwargs.pop(k) for k in ("align", "dpi") if k in kwargs}
    encoded = base64.b64encode(data).decode("ascii")
    return Receipt(elements=[PdfElement(data=encoded, **element_kwargs)], **kwargs)


# --------------------------------------------------------------------------- #
# WebSocket envelope: client -> server
# --------------------------------------------------------------------------- #
class ClientHello(BaseModel):
    """First message a client sends after connecting."""

    type: Literal["hello"] = "hello"
    protocol: int = PROTOCOL_VERSION
    printer_id: str
    name: Optional[str] = None
    token: Optional[str] = None


class Ack(BaseModel):
    """Sent by the client after it tried to print a job."""

    type: Literal["ack"] = "ack"
    job_id: str
    status: Literal["printed", "error"]
    detail: Optional[str] = None


class ClientStatus(BaseModel):
    """Optional heartbeat / readiness update from the client."""

    type: Literal["status"] = "status"
    state: str
    detail: Optional[str] = None


ClientMessage = Annotated[
    Union[ClientHello, Ack, ClientStatus],
    Field(discriminator="type"),
]


# --------------------------------------------------------------------------- #
# WebSocket envelope: server -> client
# --------------------------------------------------------------------------- #
class Welcome(BaseModel):
    type: Literal["welcome"] = "welcome"
    protocol: int = PROTOCOL_VERSION
    message: Optional[str] = None


class JobMessage(BaseModel):
    type: Literal["job"] = "job"
    job_id: str
    receipt: Receipt


class ErrorMessage(BaseModel):
    type: Literal["error"] = "error"
    detail: str


ServerMessage = Annotated[
    Union[Welcome, JobMessage, ErrorMessage],
    Field(discriminator="type"),
]


_client_message_adapter: TypeAdapter = TypeAdapter(ClientMessage)
_server_message_adapter: TypeAdapter = TypeAdapter(ServerMessage)


def parse_client_message(data: object) -> Union[ClientHello, Ack, ClientStatus]:
    """Validate a raw dict into one of the client message models."""
    return _client_message_adapter.validate_python(data)


def parse_server_message(data: object) -> Union[Welcome, JobMessage, ErrorMessage]:
    """Validate a raw dict into one of the server message models."""
    return _server_message_adapter.validate_python(data)
