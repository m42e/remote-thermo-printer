"""ReceiptLine integration via the upstream Node.js package."""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
from typing import Any, Dict


class ReceiptLineError(Exception):
    """Raised when ReceiptLine conversion is unavailable or fails."""


_NODE_SCRIPT = r"""
const fs = require('fs');
let receiptline;
try {
  receiptline = require('receiptline');
} catch (error) {
  console.error('The receiptline npm package is not installed. Run: npm install');
  process.exit(10);
}

const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const output = receiptline.transform(input.doc, input.printer);
if (input.binary) {
  process.stdout.write(Buffer.from(output, 'binary').toString('base64'));
} else {
  process.stdout.write(output);
}
"""


def _transform(doc: str, printer: Dict[str, Any], *, binary: bool) -> bytes:
    if shutil.which("node") is None:
        raise ReceiptLineError("Node.js is required for ReceiptLine rendering")

    payload = json.dumps({"doc": doc, "printer": printer, "binary": binary}).encode(
        "utf-8"
    )
    try:
        result = subprocess.run(
            ["node", "-e", _NODE_SCRIPT],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
        )
    except subprocess.TimeoutExpired as exc:
        raise ReceiptLineError("ReceiptLine rendering timed out") from exc
    except OSError as exc:
        raise ReceiptLineError(f"Could not run Node.js: {exc}") from exc

    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise ReceiptLineError(detail or "ReceiptLine rendering failed")

    return result.stdout


def render_escpos(doc: str, printer: Dict[str, Any]) -> bytes:
    """Transform ReceiptLine markdown into ESC/POS command bytes."""
    output = _transform(doc, printer, binary=True)

    try:
        return base64.b64decode(output, validate=True)
    except Exception as exc:
        raise ReceiptLineError("ReceiptLine produced invalid command output") from exc


def render_svg(doc: str, printer: Dict[str, Any]) -> str:
    """Transform ReceiptLine markdown into SVG markup for previewing."""
    preview_printer = {**printer, "command": "svg"}
    output = _transform(doc, preview_printer, binary=False)
    return output.decode("utf-8")