"""Blob storage: where media bytes live.

``raw_ref`` has always been documented as "a pointer to blob storage", but no
blob storage existed — for WhatsApp media it held a Meta media id, a pointer
into someone else's API that no extractor could open. This is the store that
makes the column mean what the schema says.

The properties under test are mostly about containment. Blobs are personal
correspondence: an attacker-controlled key must not escape the root, and the
store must never be asked to hold anything it cannot later find.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from catchment.storage.blobs import (
    BlobNotFound,
    FilesystemBlobStore,
    InvalidBlobKey,
    parse_ref,
)


@pytest.fixture
def store(tmp_path: Path) -> FilesystemBlobStore:
    return FilesystemBlobStore(root=tmp_path)


def test_a_stored_blob_can_be_read_back(store: FilesystemBlobStore) -> None:
    ref = store.put("whatsapp/media-1.ogg", b"audio-bytes")

    assert store.open(ref) == b"audio-bytes"


def test_the_ref_is_not_a_filesystem_path(store: FilesystemBlobStore) -> None:
    """A ref goes in the database and outlives this backend.

    Storing an absolute path would tie every row to one machine's directory
    layout and leak it to anything that reads the column.
    """
    ref = store.put("whatsapp/media-1.ogg", b"x")

    assert ref.startswith("blob://")
    assert str(store.root) not in ref


def test_a_ref_survives_a_round_trip(store: FilesystemBlobStore) -> None:
    ref = store.put("whatsapp/media-1.ogg", b"x")

    assert parse_ref(ref) == "whatsapp/media-1.ogg"


def test_writes_are_atomic(store: FilesystemBlobStore, tmp_path: Path) -> None:
    """A reader must never observe a partially written blob.

    Media downloads are the intended writer and can fail mid-stream; a torn
    file would be handed to an extractor as if it were complete.
    """
    ref = store.put("a/b.bin", b"first")
    store.put("a/b.bin", b"second-and-longer")

    assert store.open(ref) == b"second-and-longer"
    assert list(tmp_path.rglob("*.tmp")) == [], "no temporary files left behind"


def test_exists_reports_without_reading(store: FilesystemBlobStore) -> None:
    ref = store.put("a/b.bin", b"x")

    assert store.exists(ref) is True
    assert store.exists("blob://a/missing.bin") is False


def test_reading_a_missing_blob_raises(store: FilesystemBlobStore) -> None:
    with pytest.raises(BlobNotFound):
        store.open("blob://nope/gone.bin")


def test_deleting_is_idempotent(store: FilesystemBlobStore) -> None:
    ref = store.put("a/b.bin", b"x")

    store.delete(ref)
    store.delete(ref)

    assert store.exists(ref) is False


# --------------------------------------------------------------------------- #
# Containment
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "key",
    [
        "../escape.bin",
        "a/../../escape.bin",
        "/absolute.bin",
        "a//b.bin",
        "",
        "   ",
        "a/./b.bin",
        "a/\x00b.bin",
    ],
)
def test_a_key_can_never_escape_the_root(
    store: FilesystemBlobStore, key: str
) -> None:
    """Keys are built from source ids, which arrive from outside.

    A media id is chosen by WhatsApp, and a future connector may derive a key
    from something a sender controls outright. Traversal is refused rather than
    normalised, because silently rewriting a key makes two different inputs
    collide on one blob.
    """
    with pytest.raises(InvalidBlobKey):
        store.put(key, b"x")


def test_a_traversing_ref_is_refused_on_read(store: FilesystemBlobStore) -> None:
    """Refs come back out of the database, which is not a trust boundary."""
    with pytest.raises(InvalidBlobKey):
        store.open("blob://../../etc/passwd")


def test_a_ref_from_another_scheme_is_refused(store: FilesystemBlobStore) -> None:
    """Guards against an s3:// ref reaching the filesystem store after a
    backend swap and being read as a relative path."""
    with pytest.raises(InvalidBlobKey):
        store.open("s3://bucket/key.bin")


def test_nested_keys_create_their_directories(store: FilesystemBlobStore) -> None:
    ref = store.put("whatsapp/2026/07/media-1.ogg", b"x")

    assert store.open(ref) == b"x"


def test_the_root_is_created_if_absent(tmp_path: Path) -> None:
    store = FilesystemBlobStore(root=tmp_path / "does" / "not" / "exist")

    ref = store.put("a.bin", b"x")

    assert store.open(ref) == b"x"


def test_blob_bytes_are_never_logged(
    store: FilesystemBlobStore, caplog: pytest.LogCaptureFixture
) -> None:
    """Same rule as message bodies: metadata yes, content never."""
    secret = b"a voice note transcript would live here"

    with caplog.at_level("DEBUG"):
        ref = store.put("a/b.ogg", secret)
        store.open(ref)

    emitted = " ".join(r.getMessage() + str(r.__dict__) for r in caplog.records)
    assert secret.decode() not in emitted
