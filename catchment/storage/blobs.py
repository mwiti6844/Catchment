"""Where media bytes live.

``items.raw_ref`` has always been documented as a pointer to blob storage. This
is the storage. Bytes stay out of Postgres — a table holding voice notes and
images would make every backup, every replica and every ``SELECT *`` a copy of
the user's media.

The refs written here (``blob://<key>``) are opaque on purpose. They go into a
database column that outlives this backend, so they carry no absolute path and
no hostname: swapping the filesystem store for S3 later changes an
implementation, not every row already written.

Only a local filesystem implementation exists, which is the right size for a
personal pipeline. The protocol is what makes that reversible.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Final, Protocol, runtime_checkable

from catchment.logging_config import get_logger, log_context

logger = get_logger(__name__)

SCHEME: Final[str] = "blob://"


class BlobError(RuntimeError):
    """Base class for blob storage failures."""


class BlobNotFound(BlobError):
    """Raised when a ref points at nothing."""


class InvalidBlobKey(BlobError):
    """Raised when a key or ref could not be trusted.

    Separate from :class:`BlobNotFound` deliberately: "this key is malformed or
    tries to escape the root" is a different event from "this blob is gone",
    and only one of them is worth alerting on.
    """


def make_ref(key: str) -> str:
    """Build the ref stored in ``items.raw_ref``."""
    return f"{SCHEME}{validate_key(key)}"


def parse_ref(ref: str) -> str:
    """Recover the key from a ref, rejecting anything not ours.

    Refs are read back out of the database. That is not a trust boundary — a
    row written by an older version, or by a different backend, must not be
    resolved against the local filesystem as if it were a relative path.
    """
    if not ref.startswith(SCHEME):
        raise InvalidBlobKey(f"ref does not use the {SCHEME} scheme")
    return validate_key(ref[len(SCHEME) :])


def validate_key(key: str) -> str:
    """Return ``key`` unchanged, or raise if it cannot be trusted.

    Keys are derived from source ids chosen by the platform we ingested from,
    so they arrive from outside. Traversal is *refused* rather than normalised:
    rewriting ``a/../b`` to ``b`` would let two distinct keys collide on one
    blob, which is a data-loss bug wearing the costume of a security fix.
    """
    if not key or not key.strip():
        raise InvalidBlobKey("blob key must not be empty")
    if "\x00" in key:
        raise InvalidBlobKey("blob key must not contain a null byte")
    if key.startswith("/") or key.startswith("\\"):
        raise InvalidBlobKey("blob key must be relative")

    parts = key.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise InvalidBlobKey("blob key must not contain empty or relative segments")
    if os.sep != "/" and os.sep in key:
        raise InvalidBlobKey(f"blob key must not contain {os.sep!r}")
    return key


@runtime_checkable
class BlobStore(Protocol):
    """Somewhere to put bytes that are too big, or too private, for a row."""

    def put(self, key: str, data: bytes) -> str:
        """Store ``data`` and return the ref to record on the item."""
        ...

    def open(self, ref: str) -> bytes:
        """Return the bytes behind ``ref``."""
        ...

    def exists(self, ref: str) -> bool:
        """Report whether ``ref`` resolves, without reading it."""
        ...

    def delete(self, ref: str) -> None:
        """Remove ``ref``. Deleting what is already gone is not an error."""
        ...


class FilesystemBlobStore:
    """Blobs as files under a root directory.

    The root must be a volume in production rather than a container layer —
    it holds ingested media, so losing it loses content that cannot be
    re-fetched once the platform's own links expire.
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, key: str, data: bytes) -> str:
        """Write ``data`` atomically and return its ref.

        Atomicity matters more than it looks: the intended writer is a media
        download, which can fail mid-stream. A torn file left at the final path
        would be handed to an extractor as though it were complete.
        """
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)

        # Same directory as the target, so the rename stays on one filesystem.
        handle, temporary = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(handle, "wb") as file:
                file.write(data)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

        logger.info("blob stored", extra=log_context(key=key, bytes=len(data)))
        return make_ref(key)

    def open(self, ref: str) -> bytes:
        path = self._path(parse_ref(ref))
        try:
            return path.read_bytes()
        except FileNotFoundError:
            raise BlobNotFound(f"no blob at {ref}") from None

    def exists(self, ref: str) -> bool:
        return self._path(parse_ref(ref)).is_file()

    def delete(self, ref: str) -> None:
        self._path(parse_ref(ref)).unlink(missing_ok=True)

    def _path(self, key: str) -> Path:
        """Resolve a validated key to a path inside the root.

        The containment check is repeated here rather than trusted from
        ``validate_key`` alone: a symlink inside the root can redirect an
        otherwise blameless key, and that is only visible after resolution.
        """
        candidate = (self.root / validate_key(key)).resolve()
        root = self.root.resolve()
        if not candidate.is_relative_to(root):
            raise InvalidBlobKey("blob key resolves outside the store root")
        return candidate
