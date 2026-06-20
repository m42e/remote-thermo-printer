"""Build the client code update that the backend streams to connected devices.

The backend ships the same ``common`` and ``client`` source it was installed
with (see :mod:`common.bundle`). The bundle is read once and cached, since a
running server's own source never changes.
"""

from __future__ import annotations

import base64
from functools import lru_cache

from common import __version__
from common.bundle import compute_manifest, compute_revision, iter_source_files
from common.protocol import UpdateFile, UpdateMessage


@lru_cache(maxsize=1)
def get_update_message() -> UpdateMessage:
    """The current client bundle as a ready-to-send :class:`UpdateMessage`."""
    sources = iter_source_files()
    manifest = compute_manifest(sources)
    revision = compute_revision(manifest)
    files = [
        UpdateFile(
            path=path,
            sha256=manifest[path],
            data=base64.b64encode(sources[path]).decode("ascii"),
        )
        for path in sorted(sources)
    ]
    return UpdateMessage(revision=revision, version=__version__, files=files)


def current_revision() -> str:
    """Fingerprint of the client bundle this server is shipping."""
    return get_update_message().revision
