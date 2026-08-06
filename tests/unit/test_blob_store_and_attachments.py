from __future__ import annotations

from pathlib import Path

from agent.memory.attachments import (
    attachment_kind_for_filename,
    guess_media_type,
    is_allowed_attachment_filename,
)
from agent.memory.blob_store import LocalBlobStore, build_blob_store
from agent.memory import FileRunStore


def test_local_blob_store_roundtrip(tmp_path: Path) -> None:
    store = LocalBlobStore(tmp_path / "blobs")
    meta = store.put(b"hello", kind="artifact", media_type="text/plain", session_id="s1")
    assert meta["size"] == 5
    assert store.get(meta["blob_id"]) == b"hello"
    assert store.delete(meta["blob_id"]) is True


def test_build_blob_store_defaults_to_local(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("MOLMIND_BLOB_STORE_URL", raising=False)
    store = build_blob_store(blob_root=tmp_path / "b")
    assert isinstance(store, LocalBlobStore)
    meta = store.put(b"x", kind="sdf")
    assert store.get(meta["blob_id"]) == b"x"


def test_attachment_allowlist_and_kind() -> None:
    assert is_allowed_attachment_filename("a.sdf")
    assert is_allowed_attachment_filename("note.PDF")
    assert is_allowed_attachment_filename("pic.PNG")
    assert is_allowed_attachment_filename("readme.md")
    assert not is_allowed_attachment_filename("evil.exe")
    assert attachment_kind_for_filename("x.sdf") == "sdf"
    assert attachment_kind_for_filename("x.pdf") == "pdf"
    assert attachment_kind_for_filename("x.png") == "image"
    assert guess_media_type("x.csv") == "text/csv"


def test_stage_non_sdf_attachment(tmp_path: Path) -> None:
    store = FileRunStore(root=tmp_path / "runs")
    session = store.create(client_id="att-client-0001")
    meta = store.stage_attachment(
        session,
        filename="brief.pdf",
        content=b"%PDF-1.4",
        media_type="application/pdf",
    )
    assert meta["kind"] == "pdf"
    assert meta["filename"] == "brief.pdf"
    loaded = store.read_staged_attachment(session, meta["attachment_id"])
    assert loaded is not None
    assert loaded[1] == b"%PDF-1.4"
