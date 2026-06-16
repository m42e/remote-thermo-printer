"""FastAPI backend: HTTP API to create receipts + WebSocket endpoint for clients."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from pathlib import Path
from typing import Optional

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

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
)
from server.config import get_settings
from server.manager import ConnectionManager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
logger = logging.getLogger("rtp.server")

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="remote-thermo-printer", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

manager = ConnectionManager()


# --------------------------------------------------------------------------- #
# Authentication
# --------------------------------------------------------------------------- #
def require_token(
    authorization: Optional[str] = Header(default=None),
    x_token: Optional[str] = Header(default=None),
    token: Optional[str] = Query(default=None),
) -> None:
    """Validate the shared secret for HTTP endpoints (no-op when unset)."""
    settings = get_settings()
    if not settings.token:
        return
    provided: Optional[str] = None
    if authorization and authorization.lower().startswith("bearer "):
        provided = authorization[7:].strip()
    provided = provided or x_token or token
    if provided != settings.token:
        raise HTTPException(status_code=401, detail="Invalid or missing token")


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


def _jobs_response(jobs) -> dict:
    return {"count": len(jobs), "jobs": [job.info() for job in jobs]}


# --------------------------------------------------------------------------- #
# HTTP API
# --------------------------------------------------------------------------- #
@app.get("/api/health")
async def health() -> dict:
    return {
        "status": "ok",
        "printers": manager.printers(),
        "pending_jobs": manager.pending_count,
    }


@app.get("/api/printers", dependencies=[Depends(require_token)])
async def list_printers() -> dict:
    return {"printers": manager.printers()}


@app.get("/api/jobs", dependencies=[Depends(require_token)])
async def list_jobs(limit: int = Query(default=100, ge=1, le=1000)) -> dict:
    return {"jobs": manager.jobs(limit=limit)}


@app.post("/api/receipts", dependencies=[Depends(require_token)])
async def submit_receipt(request: SubmitRequest) -> dict:
    jobs = await manager.submit(request.receipt, request.target)
    return _jobs_response(jobs)


@app.post("/api/receipts/text", dependencies=[Depends(require_token)])
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


_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".bmp")


@app.post("/api/upload", dependencies=[Depends(require_token)])
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

    if settings.token and message.token != settings.token:
        logger.warning("Rejected client %s: invalid token", message.printer_id)
        await _close_with_error(websocket, "invalid token", 4401)
        return

    connection = await manager.connect(websocket, message.printer_id, message.name)
    with suppress(Exception):
        await websocket.send_json(Welcome(message="connected").model_dump(mode="json"))
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
