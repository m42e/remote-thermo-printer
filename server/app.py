"""FastAPI backend: HTTP API to create receipts + WebSocket endpoint for clients."""

from __future__ import annotations

import asyncio
import logging
import secrets
from contextlib import suppress
from pathlib import Path
from typing import Literal, Optional, Union

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
from starlette.middleware.sessions import SessionMiddleware

from common.protocol import (
    Ack,
    Alignment,
    ClientHello,
    ErrorMessage,
    Font,
    ImageElement,
    PdfElement,
    Receipt,
    TextElement,
    Welcome,
    image_receipt,
    parse_client_message,
    pdf_receipt,
    raw_receipt,
)
from server.auth import require_auth
from server.auth import router as auth_router
from server.config import get_settings
from server.manager import ConnectionManager
from server.receiptline import ReceiptLineError, render_escpos, render_svg
from server.updates import current_revision, get_update_message

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("rtp.server")

STATIC_DIR = Path(__file__).parent / "static"

_settings = get_settings()
if _settings.oidc_active and not _settings.session_secret:
    logger.warning(
        "RTP_SERVER_SESSION_SECRET is not set; using a random key "
        "(users are logged out on every restart)."
    )

app = FastAPI(title="remote-thermo-printer", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)
# Signs the session cookie used to keep OIDC-authenticated users logged in.
app.add_middleware(
    SessionMiddleware,
    secret_key=_settings.session_secret or secrets.token_urlsafe(48),
    session_cookie="rtp_session",
    max_age=_settings.session_ttl_hours * 3600,
    same_site="lax",
    https_only=bool(_settings.public_url and _settings.public_url.startswith("https")),
)
app.include_router(auth_router)

manager = ConnectionManager()



# --------------------------------------------------------------------------- #
# Request models / helpers
# --------------------------------------------------------------------------- #
class SubmitRequest(BaseModel):
    receipt: Receipt
    target: Optional[str] = None


class TextRequest(BaseModel):
    text: str
    align: Alignment = Alignment.left
    bold: bool = False
    underline: int = 0
    double_width: bool = False
    double_height: bool = False
    font: Font = Font.a
    cut: bool = True
    open_drawer: bool = False
    title: Optional[str] = None
    target: Optional[str] = None


class ReceiptLineRequest(BaseModel):
    doc: str
    cpl: int = Field(default=_settings.receiptline_cpl, ge=24, le=96)
    encoding: str = "multilingual"
    command: Literal["escpos", "epson", "generic"] = "escpos"
    upside_down: bool = False
    spacing: bool = False
    cutting: bool = True
    margin: int = Field(default=0, ge=0, le=24)
    margin_right: int = Field(default=0, ge=0, le=24)
    title: Optional[str] = None
    target: Optional[str] = None


class ReceiptLinePreviewRequest(BaseModel):
    doc: str
    cpl: int = Field(default=_settings.receiptline_cpl, ge=24, le=96)
    encoding: str = "multilingual"
    upside_down: bool = False
    spacing: bool = False
    margin: int = Field(default=0, ge=0, le=24)
    margin_right: int = Field(default=0, ge=0, le=24)


def _jobs_response(jobs) -> dict:
    return {"count": len(jobs), "jobs": [job.info() for job in jobs]}


def _receiptline_printer(
    request: Union[ReceiptLineRequest, ReceiptLinePreviewRequest]
) -> dict:
    printer = {
        "cpl": request.cpl,
        "encoding": request.encoding,
        "upsideDown": request.upside_down,
        "spacing": request.spacing,
        "margin": request.margin,
        "marginRight": request.margin_right,
    }
    if isinstance(request, ReceiptLineRequest):
        printer.update({"command": request.command, "cutting": request.cutting})
    return printer


# --------------------------------------------------------------------------- #
# HTTP API
# --------------------------------------------------------------------------- #
@app.get("/api/health")
async def health() -> dict:
    """Unauthenticated liveness probe; exposes counts only, no printer detail."""
    return {
        "status": "ok",
        "printers_online": len(manager.printers()),
        "pending_jobs": manager.pending_count,
    }


@app.get("/api/client/version")
async def client_version() -> dict:
    """The client code bundle this server ships (for diagnostics / OTA checks)."""
    update = get_update_message()
    return {
        "version": update.version,
        "revision": update.revision,
        "files": len(update.files),
        "auto_update": _settings.auto_update,
    }


@app.get("/api/printers", dependencies=[Depends(require_auth)])
async def list_printers() -> dict:
    return {"printers": manager.printers()}


@app.get("/api/jobs", dependencies=[Depends(require_auth)])
async def list_jobs(limit: int = Query(default=100, ge=1, le=1000)) -> dict:
    return {"jobs": manager.jobs(limit=limit)}


@app.post("/api/receipts", dependencies=[Depends(require_auth)])
async def submit_receipt(request: SubmitRequest) -> dict:
    jobs = await manager.submit(request.receipt, request.target)
    return _jobs_response(jobs)


@app.post("/api/receipts/text", dependencies=[Depends(require_auth)])
async def submit_text(request: TextRequest) -> dict:
    receipt = Receipt(
        title=request.title,
        cut=request.cut,
        open_drawer=request.open_drawer,
        elements=[
            TextElement(
                content=request.text,
                align=request.align,
                bold=request.bold,
                underline=request.underline,
                double_width=request.double_width,
                double_height=request.double_height,
                font=request.font,
            )
        ],
    )
    jobs = await manager.submit(receipt, request.target)
    return _jobs_response(jobs)


@app.post("/api/receipts/receiptline", dependencies=[Depends(require_auth)])
async def submit_receiptline(request: ReceiptLineRequest) -> dict:
    if not request.doc.strip():
        raise HTTPException(status_code=400, detail="ReceiptLine document is empty")

    try:
        commands = render_escpos(request.doc, _receiptline_printer(request))
    except ReceiptLineError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    receipt = raw_receipt(commands, title=request.title, cut=False)
    jobs = await manager.submit(receipt, request.target)
    return _jobs_response(jobs)


@app.post("/api/receiptline/preview", dependencies=[Depends(require_auth)])
async def preview_receiptline(request: ReceiptLinePreviewRequest) -> dict:
    if not request.doc.strip():
        return {"svg": ""}

    try:
        svg = render_svg(request.doc, _receiptline_printer(request))
    except ReceiptLineError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return {"svg": svg}


_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".bmp")


@app.post("/api/upload", dependencies=[Depends(require_auth)])
async def upload(
    file: UploadFile = File(...),
    target: Optional[str] = Form(default=None),
    align: Alignment = Form(default=Alignment.center),
    dpi: int = Form(default=203),
    cut: bool = Form(default=True),
    title: Optional[str] = Form(default=None),
) -> dict:
    """Create a receipt from an uploaded image or PDF file."""
    data = await file.read()
    settings = get_settings()
    if len(data) > settings.max_upload_mb * 1024 * 1024:
        raise HTTPException(status_code=413, detail="File too large")

    content_type = (file.content_type or "").lower()
    name = (file.filename or "").lower()

    if content_type == "application/pdf" or name.endswith(".pdf"):
        receipt = pdf_receipt(data, align=align, dpi=dpi, cut=cut, title=title)
    elif content_type.startswith("image/") or name.endswith(_IMAGE_EXTS):
        receipt = image_receipt(data, align=align, cut=cut, title=title)
    else:
        raise HTTPException(
            status_code=415,
            detail="Unsupported file type; expected an image or PDF",
        )

    jobs = await manager.submit(receipt, target)
    return _jobs_response(jobs)


# --------------------------------------------------------------------------- #
# WebSocket endpoint for printer clients
# --------------------------------------------------------------------------- #
async def _close_with_error(websocket: WebSocket, detail: str, code: int) -> None:
    with suppress(Exception):
        await websocket.send_json(ErrorMessage(detail=detail).model_dump(mode="json"))
    with suppress(Exception):
        await websocket.close(code=code)


async def _maybe_offer_update(websocket: WebSocket, hello: ClientHello) -> None:
    """Send the current client bundle when the device is running older code.

    The client applies it and re-execs, so we simply log the offer here and let
    at-least-once job delivery cover the brief reconnect.
    """
    if not get_settings().auto_update:
        return
    server_revision = current_revision()
    if hello.bundle_revision == server_revision:
        return
    update = get_update_message()
    with suppress(Exception):
        await websocket.send_json(update.model_dump(mode="json"))
    logger.info(
        "Offered update to printer %s (%s -> %s, %d file(s))",
        hello.printer_id,
        (hello.bundle_revision or "unknown")[:12],
        server_revision[:12],
        len(update.files),
    )


@app.websocket("/ws")
async def printer_socket(websocket: WebSocket) -> None:
    await websocket.accept()
    settings = get_settings()

    # The first message must be a hello so we know who is connecting.
    try:
        raw = await websocket.receive_json()
    except WebSocketDisconnect:
        return
    except Exception:
        await _close_with_error(websocket, "expected a JSON hello message", 4400)
        return

    try:
        message = parse_client_message(raw)
    except ValidationError as exc:
        await _close_with_error(websocket, f"invalid hello message: {exc}", 4400)
        return

    if not isinstance(message, ClientHello):
        await _close_with_error(websocket, "first message must be of type 'hello'", 4400)
        return

    printer_token = settings.effective_printer_token
    if printer_token and not (
        message.token and secrets.compare_digest(message.token, printer_token)
    ):
        logger.warning("Rejected client %s: invalid token", message.printer_id)
        await _close_with_error(websocket, "invalid token", 4401)
        return

    connection = await manager.connect(websocket, message.printer_id, message.name)
    with suppress(Exception):
        await websocket.send_json(Welcome(message="connected").model_dump(mode="json"))
    # Offer a code update before any jobs flow, so an out-of-date device reloads
    # first instead of printing with stale code.
    await _maybe_offer_update(websocket, message)
    connection.sender_task = asyncio.create_task(manager.sender_loop(connection))

    try:
        while True:
            try:
                raw = await websocket.receive_json()
            except WebSocketDisconnect:
                break
            except Exception:
                # Malformed frame or broken socket: stop handling this client.
                break
            try:
                client_msg = parse_client_message(raw)
            except ValidationError:
                continue
            if isinstance(client_msg, Ack):
                await manager.handle_ack(client_msg)
    finally:
        if connection.sender_task is not None:
            connection.sender_task.cancel()
            with suppress(asyncio.CancelledError):
                await connection.sender_task
        await manager.disconnect(connection)


# --------------------------------------------------------------------------- #
# Web UI (served last so the API routes take precedence)
# --------------------------------------------------------------------------- #
@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")
