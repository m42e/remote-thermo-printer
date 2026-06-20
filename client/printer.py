"""Turn :class:`~common.protocol.Receipt` objects into physical prints.

This module is the only place that talks to ``python-escpos``. It knows how to
build a printer connection from the client configuration and how to render each
supported element type (text, image, PDF).
"""

from __future__ import annotations

import io
import logging
from typing import List, Optional

from PIL import Image

from client.config import ClientSettings, ConnectionType
from common.protocol import ImageElement, PdfElement, RawElement, Receipt, TextElement

logger = logging.getLogger("rtp.client.printer")


class PrinterError(Exception):
    """Raised when a receipt cannot be printed."""


def _to_int(value: Optional[str]) -> Optional[int]:
    """Parse an int that may be written as decimal or hex ("0x04b8")."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return int(text, 16) if text.lower().startswith("0x") else int(text, 0)


def _apply_media_width(device, width: int) -> None:
    """Teach the printer profile how wide the paper is (in dots).

    python-escpos needs ``media.width.pixels`` to center images and to avoid a
    noisy warning on every ``image()`` call. We override it on a per-instance
    copy so the shared default profile is left untouched.
    """
    if not width or width <= 0:
        return
    try:
        import copy

        profile = device.profile
        data = copy.deepcopy(profile.profile_data)
        data.setdefault("media", {}).setdefault("width", {})["pixels"] = int(width)
        profile.profile_data = data
    except Exception:  # pragma: no cover - profile internals are best effort
        pass


def build_printer(settings: ClientSettings):
    """Create a python-escpos printer instance from the configuration."""
    try:
        from escpos import printer as escpos_printer
    except ImportError as exc:  # pragma: no cover - dependency missing
        raise PrinterError(
            "python-escpos is not installed; run `pip install python-escpos`"
        ) from exc

    common_kwargs = {}
    if settings.printer_profile:
        common_kwargs["profile"] = settings.printer_profile

    conn = settings.connection
    if conn == ConnectionType.dummy:
        device = escpos_printer.Dummy(**common_kwargs)

    elif conn == ConnectionType.usb:
        vendor = _to_int(settings.usb_vendor_id)
        product = _to_int(settings.usb_product_id)
        if vendor is None or product is None:
            raise PrinterError(
                "USB connection requires RTP_CLIENT_USB_VENDOR_ID and "
                "RTP_CLIENT_USB_PRODUCT_ID"
            )
        usb_kwargs = dict(common_kwargs, idVendor=vendor, idProduct=product)
        in_ep = _to_int(settings.usb_in_ep)
        out_ep = _to_int(settings.usb_out_ep)
        if in_ep is not None:
            usb_kwargs["in_ep"] = in_ep
        if out_ep is not None:
            usb_kwargs["out_ep"] = out_ep
        device = escpos_printer.Usb(**usb_kwargs)

    elif conn == ConnectionType.network:
        if not settings.net_host:
            raise PrinterError("Network connection requires RTP_CLIENT_NET_HOST")
        device = escpos_printer.Network(
            host=settings.net_host, port=settings.net_port, **common_kwargs
        )

    elif conn == ConnectionType.serial:
        device = escpos_printer.Serial(
            devfile=settings.serial_devfile,
            baudrate=settings.serial_baudrate,
            **common_kwargs,
        )

    elif conn == ConnectionType.file:
        device = escpos_printer.File(devfile=settings.file_devfile, **common_kwargs)

    elif conn == ConnectionType.cups:
        cups_kwargs = dict(common_kwargs)
        if settings.cups_printer_name:
            cups_kwargs["printer_name"] = settings.cups_printer_name
        device = escpos_printer.CupsPrinter(**cups_kwargs)

    else:  # pragma: no cover - guarded by the enum
        raise PrinterError(f"Unsupported connection type: {conn}")

    _apply_media_width(device, settings.printer_width)
    return device


def render_pdf(data: bytes, dpi: int) -> List[Image.Image]:
    """Render every page of a PDF into a list of PIL images using PyMuPDF."""
    try:
        import fitz  # PyMuPDF
    except ImportError:
        try:
            import pymupdf as fitz  # newer PyMuPDF exposes this name
        except ImportError as exc:
            raise PrinterError(
                "PDF support requires PyMuPDF; run `pip install pymupdf`"
            ) from exc

    images: List[Image.Image] = []
    zoom = dpi / 72.0
    matrix = fitz.Matrix(zoom, zoom)
    with fitz.open(stream=data, filetype="pdf") as doc:
        if doc.page_count == 0:
            raise PrinterError("PDF has no pages")
        for page in doc:
            pixmap = page.get_pixmap(matrix=matrix)
            mode = "RGBA" if pixmap.alpha else "RGB"
            image = Image.frombytes(mode, (pixmap.width, pixmap.height), pixmap.samples)
            images.append(image)
    return images


def trim_blank_margin(image: Image.Image, threshold: int = 250) -> Image.Image:
    """Crop white page margins from rendered PDFs without touching content."""
    mask = image.convert("L").point(
        lambda pixel: 0 if pixel >= threshold else 255, "1"
    )
    bbox = mask.getbbox()
    return image.crop(bbox) if bbox else image


# The height field of the GS v 0 / GS ( L raster commands is two bytes, so a
# single pass can cover at most 65535 dot rows.
_MAX_RASTER_HEIGHT = 65535


def _fragment_height(image_height: int) -> int:
    """Choose a fragment height that prints an image in as few passes as possible.

    python-escpos defaults to splitting images taller than 960 dots into
    separate raster commands. On Epson TM printers (e.g. the TM-T70II) every
    split leaves a faint horizontal line: the print motor briefly stops between
    commands and python-escpos re-dithers each fragment on its own, so the
    dither pattern restarts at the boundary. Printing the whole image in one
    pass keeps the motor running and the dithering continuous.
    """
    return max(960, min(image_height, _MAX_RASTER_HEIGHT))


class ReceiptPrinter:
    """Render receipts onto an ESC/POS printer described by ``settings``."""

    def __init__(self, settings: ClientSettings) -> None:
        self.settings = settings

    # -- public API --------------------------------------------------------- #
    def print_receipt(self, receipt: Receipt) -> None:
        """Open a fresh connection, render the receipt and close again.

        A new connection per receipt keeps USB/network printers from getting
        stuck in a bad state after an error. This method is blocking and is
        meant to be run in a worker thread.
        """
        try:
            printer = build_printer(self.settings)
        except PrinterError:
            raise
        except Exception as exc:  # device not found, etc.
            raise PrinterError(f"Could not open printer: {exc}") from exc

        try:
            self._render(printer, receipt)
        finally:
            try:
                printer.close()
            except Exception:  # pragma: no cover - best effort
                pass

    # -- rendering ---------------------------------------------------------- #
    def _render(self, printer, receipt: Receipt) -> None:
        for element in receipt.elements:
            if isinstance(element, TextElement):
                self._render_text(printer, element)
            elif isinstance(element, ImageElement):
                self._render_image(printer, element)
            elif isinstance(element, PdfElement):
                self._render_pdf(printer, element)
            elif isinstance(element, RawElement):
                self._render_raw(printer, element)
            else:  # pragma: no cover - guarded by the protocol union
                raise PrinterError(f"Unknown element type: {element!r}")

        if receipt.feed_before_cut:
            printer.ln(receipt.feed_before_cut)
        if receipt.open_drawer:
            try:
                printer.cashdraw(2)
            except Exception as exc:  # pragma: no cover - hardware dependent
                logger.warning("Could not open cash drawer: %s", exc)
        if receipt.cut:
            try:
                printer.cut()
            except Exception as exc:
                logger.warning("Cut not supported, feeding instead: %s", exc)
                printer.ln(3)

    def _render_text(self, printer, element: TextElement) -> None:
        printer.set_with_default(
            align=element.align.value,
            font=element.font.value,
            bold=element.bold,
            underline=element.underline,
            double_width=element.double_width,
            double_height=element.double_height,
        )
        text = element.content
        if not text.endswith("\n"):
            text += "\n"
        printer.text(text)
        printer.set_with_default()

    def _render_image(self, printer, element: ImageElement) -> None:
        try:
            image = Image.open(io.BytesIO(element.decoded()))
            image.load()
        except Exception as exc:
            raise PrinterError(f"Invalid image data: {exc}") from exc
        image = self._prepare_image(image)
        printer.set_with_default(align=element.align.value)
        printer.image(
            image,
            high_density_vertical=element.high_density,
            high_density_horizontal=element.high_density,
            impl=element.impl,
            fragment_height=_fragment_height(image.height),
        )
        printer.set_with_default()

    def _render_pdf(self, printer, element: PdfElement) -> None:
        pages = render_pdf(element.decoded(), dpi=element.dpi)
        printer.set_with_default(align=element.align.value)
        for page in pages:
            image = self._prepare_image(trim_blank_margin(page))
            printer.image(
                image,
                impl="bitImageRaster",
                fragment_height=_fragment_height(image.height),
            )
        printer.set_with_default()

    def _render_raw(self, printer, element: RawElement) -> None:
        try:
            printer._raw(element.decoded())
        except AttributeError as exc:
            raise PrinterError("Printer backend does not support raw commands") from exc

    def _prepare_image(self, image: Image.Image) -> Image.Image:
        """Flatten transparency, scale to the print width and dither to 1-bit.

        The black/white conversion happens here, once, over the whole image.
        Left to python-escpos it would be redone for each fragment of a tall
        image, which makes the dither pattern restart at every fragment
        boundary and shows up as horizontal lines on the print.
        """
        if image.mode in ("RGBA", "LA") or (
            image.mode == "P" and "transparency" in image.info
        ):
            background = Image.new("RGBA", image.size, (255, 255, 255, 255))
            image = Image.alpha_composite(background, image.convert("RGBA"))
        image = image.convert("L")

        width = self.settings.printer_width
        if width and image.width > width:
            height = max(1, round(image.height * width / image.width))
            image = image.resize((width, height))

        # Floyd-Steinberg dither to pure black/white in a single pass.
        return image.convert("1")
