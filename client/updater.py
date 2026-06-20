"""Apply an over-the-air client code update and reload into the new code.

Updates arrive as an :class:`~common.protocol.UpdateMessage` over the existing
WebSocket. We verify every file, stage it next to its target, swap the staged
files in atomically, then re-execute the process so the new code runs without
any manual restart (systemd keeps the same unit running).

Security: the server is trusted (the device already authenticated to it), but we
still defend against a malformed or hostile bundle — each file path must stay
inside one of the bundled packages (no path traversal), its contents must match
the advertised SHA-256, and it must compile before anything is written.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path, PurePosixPath
from typing import List, Tuple

from common.bundle import BUNDLE_PACKAGES, bundle_root, file_sha256
from common.protocol import UpdateMessage

logger = logging.getLogger("rtp.client.updater")

# Suffix for files staged on disk before the atomic swap-in.
_STAGING_SUFFIX = ".rtp-new"


class UpdateError(Exception):
    """Raised when an update cannot be safely applied."""


def _safe_target(root: Path, rel_path: str) -> Path:
    """Resolve a bundle-relative path to an absolute target, safely.

    Rejects absolute paths, parent-directory escapes and any file outside the
    known bundle packages so a bad server can never write arbitrary locations.
    """
    pure = PurePosixPath(rel_path)
    if pure.is_absolute() or any(part in ("", "..") for part in pure.parts):
        raise UpdateError(f"unsafe update path: {rel_path!r}")
    if not pure.parts or pure.parts[0] not in BUNDLE_PACKAGES:
        raise UpdateError(f"update path outside bundle packages: {rel_path!r}")
    if pure.suffix != ".py":
        raise UpdateError(f"refusing non-Python update file: {rel_path!r}")
    target = (root / pure).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError as exc:  # pragma: no cover - defensive
        raise UpdateError(f"update path escapes bundle root: {rel_path!r}") from exc
    return target


def _stage_files(update: UpdateMessage, root: Path) -> List[Tuple[Path, Path]]:
    """Validate and write every file to ``<target>.rtp-new``.

    Returns ``(staging_path, target_path)`` pairs. Nothing in the live tree is
    touched yet, so a failure here leaves the running install intact.
    """
    staged: List[Tuple[Path, Path]] = []
    try:
        for file in update.files:
            data = file.decoded()
            actual = file_sha256(data)
            if actual != file.sha256:
                raise UpdateError(
                    f"checksum mismatch for {file.path}: "
                    f"expected {file.sha256[:12]}, got {actual[:12]}"
                )
            target = _safe_target(root, file.path)
            try:
                compile(data, str(target), "exec")
            except SyntaxError as exc:
                raise UpdateError(f"update file {file.path} does not compile: {exc}") from exc

            target.parent.mkdir(parents=True, exist_ok=True)
            staging = target.with_name(target.name + _STAGING_SUFFIX)
            with open(staging, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            staged.append((staging, target))
    except Exception:
        _cleanup_staging(staged)
        raise
    return staged


def _cleanup_staging(staged: List[Tuple[Path, Path]]) -> None:
    for staging, _ in staged:
        try:
            staging.unlink()
        except OSError:
            pass


def apply_update(update: UpdateMessage) -> None:
    """Verify and atomically install an update bundle on disk.

    Raises :class:`UpdateError` (leaving the current install untouched) if the
    bundle is invalid or cannot be written.
    """
    if not update.files:
        raise UpdateError("update contained no files")
    root = bundle_root()
    staged = _stage_files(update, root)
    # All files validated and staged: swap them in. ``os.replace`` is atomic
    # within a directory, so each file flips from old to new in one step.
    try:
        for staging, target in staged:
            os.replace(staging, target)
    except OSError as exc:
        _cleanup_staging(staged)
        raise UpdateError(f"failed to install update file: {exc}") from exc
    logger.info(
        "Applied update %s (%d file(s))", update.revision[:12], len(update.files)
    )


def restart() -> "None":
    """Re-execute this process so the freshly written code is loaded.

    Replaces the current process image (same PID / systemd unit), so there is no
    manual restart and the client reconnects on its own.
    """
    logger.info("Restarting client to load updated code")
    for handler in logging.getLogger().handlers:
        try:
            handler.flush()
        except Exception:  # pragma: no cover - best effort
            pass
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(sys.executable, [sys.executable, "-m", "client"])


def apply_and_restart(update: UpdateMessage) -> None:
    """Install the update and, on success, reload into the new code."""
    apply_update(update)
    restart()
