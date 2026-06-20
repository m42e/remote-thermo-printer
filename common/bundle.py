"""Deterministic source bundle shared by the backend and the client.

Both sides agree on *which* files make up the over-the-air updatable client and
on *how* to fingerprint them, so the server can tell a connecting client whether
its running code matches the code the server is currently shipping.

The "bundle" is simply the Python source of the :data:`BUNDLE_PACKAGES`
packages.  Its *revision* is a single SHA-256 fingerprint over every file's path
and contents, so any change to the shipped code yields a new revision and an
older client knows it is out of date — no manual version bumping required.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Dict, List, Tuple

# Packages that are streamed to the Raspberry Pi client and may be updated
# over the air. ``common`` is shared wire/protocol code; ``client`` is the
# device runtime. The backend (``server``) is intentionally excluded.
BUNDLE_PACKAGES: Tuple[str, ...] = ("common", "client")


def bundle_root() -> Path:
    """Directory that contains the bundled top-level packages.

    Both ``common`` and ``client`` are installed as siblings — either in the
    repository checkout (development) or in ``site-packages`` (an installed
    deployment). This module lives inside ``common``, so its grandparent is the
    directory holding all bundle packages.
    """
    return Path(__file__).resolve().parent.parent


def _iter_package_files(package: str) -> List[Path]:
    """Return the sorted ``*.py`` files of a bundle package (no bytecode)."""
    package_dir = bundle_root() / package
    if not package_dir.is_dir():
        return []
    files = [
        path
        for path in package_dir.rglob("*.py")
        if path.is_file() and "__pycache__" not in path.parts
    ]
    return sorted(files)


def iter_source_files() -> Dict[str, bytes]:
    """Map each bundled file's POSIX-relative path to its raw bytes.

    Reading bytes (not text) keeps the fingerprint independent of any newline
    translation, so the server and client agree on the revision exactly.
    """
    root = bundle_root()
    sources: Dict[str, bytes] = {}
    for package in BUNDLE_PACKAGES:
        for path in _iter_package_files(package):
            rel = path.relative_to(root).as_posix()
            sources[rel] = path.read_bytes()
    return sources


def file_sha256(data: bytes) -> str:
    """Hex SHA-256 of a single file's contents."""
    return hashlib.sha256(data).hexdigest()


def compute_manifest(sources: Dict[str, bytes]) -> Dict[str, str]:
    """Map each file path to the hex SHA-256 of its contents."""
    return {path: file_sha256(data) for path, data in sources.items()}


def compute_revision(manifest: Dict[str, str]) -> str:
    """Collapse a manifest into one stable fingerprint of the whole bundle."""
    digest = hashlib.sha256()
    for path in sorted(manifest):
        digest.update(path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(manifest[path].encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def current_revision() -> str:
    """Fingerprint of the bundle as it exists on disk for this process."""
    return compute_revision(compute_manifest(iter_source_files()))
