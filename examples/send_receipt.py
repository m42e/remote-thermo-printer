#!/usr/bin/env python3
"""Send a receipt to the backend from the command line.

Examples
--------
    python examples/send_receipt.py text "Hello world" --center --bold
    python examples/send_receipt.py image logo.png
    python examples/send_receipt.py pdf invoice.pdf --dpi 203

Use ``--server`` / ``--token`` / ``--target`` to point at your backend.
Only depends on the standard library plus this project's ``common`` package.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

from common.protocol import image_receipt, pdf_receipt, text_receipt


def post(server: str, path: str, payload: dict, token: str | None) -> dict:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(server.rstrip("/") + path, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        raise SystemExit(f"Server returned {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Could not reach {server}: {exc.reason}") from exc


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", default="http://localhost:8000")
    parser.add_argument("--token", default=None)
    parser.add_argument("--target", default=None, help="Target printer id (default: all)")

    sub = parser.add_subparsers(dest="command", required=True)

    text = sub.add_parser("text", help="Print a line of text")
    text.add_argument("text")
    text.add_argument("--center", action="store_true")
    text.add_argument("--right", action="store_true")
    text.add_argument("--bold", action="store_true")
    text.add_argument("--big", action="store_true", help="Double width and height")

    image = sub.add_parser("image", help="Print an image file")
    image.add_argument("path")

    pdf = sub.add_parser("pdf", help="Print a PDF file")
    pdf.add_argument("path")
    pdf.add_argument("--dpi", type=int, default=203)

    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)

    if args.command == "text":
        align = "center" if args.center else "right" if args.right else "left"
        receipt = text_receipt(
            args.text,
            align=align,
            bold=args.bold,
            double_width=args.big,
            double_height=args.big,
        )
    elif args.command == "image":
        with open(args.path, "rb") as handle:
            receipt = image_receipt(handle.read())
    elif args.command == "pdf":
        with open(args.path, "rb") as handle:
            receipt = pdf_receipt(handle.read(), dpi=args.dpi)
    else:  # pragma: no cover - argparse enforces this
        raise SystemExit("unknown command")

    payload = {"receipt": receipt.model_dump(mode="json"), "target": args.target}
    result = post(args.server, "/api/receipts", payload, args.token)
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")


if __name__ == "__main__":
    main()
