# Remote Thermo Printer

Print receipts on an Epson (ESC/POS) thermal printer from anywhere.

A small **backend** accepts receipts (text, images, PDFs) over an HTTP API or a
built‑in web UI and queues them. A lightweight **client** runs on a Raspberry Pi
next to the printer, keeps a WebSocket open to the backend, and prints every job
the moment it arrives using [python-escpos](https://github.com/python-escpos/python-escpos).

```mermaid
flowchart LR
    UI["Web UI / HTTP client"] -->|POST receipt| S[Backend server]
    S -->|WebSocket job| C["Raspberry Pi client"]
    C -->|ESC/POS| P[("Epson thermal printer")]
    C -.->|ack printed / error| S
```

## Features

- **Three content types**: formatted **text**, **images** (PNG/JPEG/GIF/BMP) and
  **PDF** (every page is rendered and printed).
- **Push printing**: jobs are delivered instantly over a persistent WebSocket.
- **Reliable delivery**: jobs submitted while the printer is offline are queued;
  un‑acknowledged jobs are requeued if the client drops (at‑least‑once).
- **Multiple printers**: target a specific printer by id or broadcast to all.
- **Many printer backends**: USB, network, serial, raw device file, CUPS, or a
  `dummy` printer for testing without hardware.
- **Web UI** for quickly creating receipts, plus a CLI example and a JSON API.
- **Optional token auth** for both the HTTP API and the WebSocket.

## Project layout

```
common/   shared wire protocol (Pydantic models) used by both sides
server/   FastAPI backend: HTTP API, WebSocket endpoint, web UI
client/   Raspberry Pi client: WebSocket loop + python-escpos rendering
examples/ command-line sender
```

## Installation

Requires Python 3.9+.

**Backend** (any always-on machine):

```bash
pip install ".[server]"
```

**Client** (the Raspberry Pi attached to the printer):

```bash
pip install ".[client]"
```

On the Pi a few system packages help with USB and PDF rendering:

```bash
sudo apt install libusb-1.0-0   # USB printers
# PyMuPDF ships wheels for 64-bit Raspberry Pi OS; no extra packages needed.
```

For local development with the end-to-end smoke test, install everything:

```bash
pip install ".[dev]"
```

## Configuration

Copy `.env.example` to `.env` and edit it. Backend reads `RTP_SERVER_*`, the
client reads `RTP_CLIENT_*`; both can share one file.

Key client settings:

| Variable | Meaning |
| --- | --- |
| `RTP_CLIENT_SERVER_URL` | Backend WebSocket URL, e.g. `ws://host:8000/ws` |
| `RTP_CLIENT_CONNECTION` | `usb` \| `network` \| `serial` \| `file` \| `cups` \| `dummy` |
| `RTP_CLIENT_PRINTER_ID` | Unique id used to target this printer |
| `RTP_CLIENT_PRINTER_WIDTH` | Print head width in dots (80mm≈576, 58mm≈384) |
| `RTP_CLIENT_TOKEN` | Shared secret (must match `RTP_SERVER_TOKEN`) |

Per-connection settings (USB vendor/product id, network host/port, serial
device, etc.) are documented inline in `.env.example`.

## Running the backend

```bash
rtp-server          # or: python -m server
```

Then open <http://localhost:8000> for the web UI. The API listens on the same
host/port.

### With Docker

A `Dockerfile` for the backend is included (the web UI is bundled in the image):

```bash
docker build -t remote-thermo-printer-server .
docker run -p 8000:8000 remote-thermo-printer-server
```

Configure it via environment variables (and/or a mounted `.env`):

```bash
docker run -p 8000:8000 \
  -e RTP_SERVER_TOKEN=secret \
  remote-thermo-printer-server
# or mount a .env file:
docker run -p 8000:8000 -v "$PWD/.env:/app/.env:ro" remote-thermo-printer-server
```

The image runs as a non-root user and exposes port 8000 with a `/api/health`
healthcheck. Point your Raspberry Pi client at `ws://<docker-host>:8000/ws`.

## Running the client (Raspberry Pi)

```bash
rtp-client          # or: python -m client
```

Example for a USB Epson printer:

```bash
export RTP_CLIENT_SERVER_URL=ws://192.168.1.10:8000/ws
export RTP_CLIENT_CONNECTION=usb
export RTP_CLIENT_USB_VENDOR_ID=0x04b8
export RTP_CLIENT_USB_PRODUCT_ID=0x0202
rtp-client
```

Find your USB ids with `lsusb` (Epson's vendor id is usually `04b8`). For a
networked printer set `RTP_CLIENT_CONNECTION=network` and `RTP_CLIENT_NET_HOST`.

### Run the client as a service

Create `/etc/systemd/system/rtp-client.service`:

```ini
[Unit]
Description=Remote Thermo Printer client
After=network-online.target
Wants=network-online.target

[Service]
WorkingDirectory=/home/pi/remote-thermo-printer
EnvironmentFile=/home/pi/remote-thermo-printer/.env
ExecStart=/home/pi/remote-thermo-printer/.venv/bin/rtp-client
Restart=always
RestartSec=3
User=pi

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now rtp-client
```

## HTTP API

All endpoints accept the token via `Authorization: Bearer <token>`,
an `X-Token` header, or a `?token=` query parameter (only required when
`RTP_SERVER_TOKEN` is set).

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/api/health` | Status + connected printers (no auth) |
| `GET` | `/api/printers` | Connected printers |
| `GET` | `/api/jobs?limit=N` | Recent jobs and their state |
| `POST` | `/api/receipts` | Submit a full receipt (JSON, see below) |
| `POST` | `/api/receipts/text` | Quick text receipt |
| `POST` | `/api/upload` | Upload an image or PDF (multipart form) |
| `WS` | `/ws` | Printer client connection |

A full receipt is a list of elements:

```jsonc
{
  "receipt": {
    "title": "Order #1234",
    "cut": true,
    "elements": [
      { "type": "text", "content": "ACME Corp", "align": "center", "bold": true,
        "double_width": true, "double_height": true },
      { "type": "text", "content": "Thank you!\n" },
      { "type": "image", "data": "<base64 png/jpg>", "align": "center" },
      { "type": "pdf",   "data": "<base64 pdf>", "dpi": 203 }
    ]
  },
  "target": null            // null = all connected printers, or a printer id
}
```

Quick text receipt:

```bash
curl -X POST http://localhost:8000/api/receipts/text \
  -H 'Content-Type: application/json' \
  -d '{"text":"Hello world","align":"center","bold":true}'
```

Upload an image or PDF:

```bash
curl -X POST http://localhost:8000/api/upload \
  -F file=@logo.png -F align=center
```

## Command-line sender

```bash
python examples/send_receipt.py text "Hello world" --center --bold
python examples/send_receipt.py image logo.png
python examples/send_receipt.py pdf invoice.pdf --dpi 203
# point elsewhere / authenticate:
python examples/send_receipt.py --server http://192.168.1.10:8000 --token secret \
    text "Hi" --target pi-printer-1
```

## Notes

- **Image & PDF sizing**: images and rendered PDF pages wider than
  `RTP_CLIENT_PRINTER_WIDTH` dots are scaled down to fit the paper.
- **PDF rendering** uses PyMuPDF (`pymupdf`). Each page becomes one bitmap.
- **Security**: set `RTP_SERVER_TOKEN` (and the matching `RTP_CLIENT_TOKEN`) when
  the backend is reachable beyond a trusted LAN, and terminate TLS (`wss://`,
  `https://`) with a reverse proxy in front of the backend.
- **Testing without hardware**: keep `RTP_CLIENT_CONNECTION=dummy`; the client
  acknowledges jobs as printed without touching a device.

## License

MIT
