"""Blob storage backends: local filesystem and S3-compatible object stores."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse


class BlobStore(Protocol):
    def put(
        self,
        content: bytes,
        *,
        kind: str,
        media_type: str = "application/octet-stream",
        session_id: str = "",
    ) -> dict: ...

    def get(self, blob_id: str) -> bytes: ...

    def delete(self, blob_id: str) -> bool: ...


class LocalBlobStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _index_path(self, blob_id: str) -> Path:
        return self.root / "_index" / blob_id

    def put(
        self,
        content: bytes,
        *,
        kind: str,
        media_type: str = "application/octet-stream",
        session_id: str = "",
    ) -> dict:
        blob_id = uuid.uuid4().hex
        relative_path = f"{kind}/{blob_id[:2]}/{blob_id}"
        target = self.root / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        digest = hashlib.sha256(content).hexdigest()
        meta = {
            "blob_id": blob_id,
            "sha256": digest,
            "size": len(content),
            "media_type": media_type,
            "kind": kind,
            "relative_path": relative_path,
            "session_id": session_id,
            "storage": "file",
        }
        index = self._index_path(blob_id)
        index.parent.mkdir(parents=True, exist_ok=True)
        index.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        return meta

    def _resolve_path(self, blob_id: str) -> Path | None:
        index = self._index_path(blob_id)
        if index.is_file():
            meta = json.loads(index.read_text(encoding="utf-8"))
            return self.root / str(meta["relative_path"])
        for path in self.root.glob(f"**/{blob_id}"):
            if path.is_file() and path.name == blob_id:
                return path
        return None

    def get(self, blob_id: str) -> bytes:
        path = self._resolve_path(blob_id)
        if path is None or not path.is_file():
            raise FileNotFoundError(blob_id)
        return path.read_bytes()

    def delete(self, blob_id: str) -> bool:
        path = self._resolve_path(blob_id)
        index = self._index_path(blob_id)
        removed = False
        if path is not None and path.is_file():
            path.unlink()
            removed = True
        if index.is_file():
            index.unlink()
            removed = True
        return removed


class S3BlobStore:
    """S3-compatible blob store (AWS S3, MinIO, etc.)."""

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str | None = None,
        prefix: str = "agent-blobs",
        access_key: str | None = None,
        secret_key: str | None = None,
        region: str = "us-east-1",
    ) -> None:
        try:
            import boto3
            from botocore.client import Config
        except ImportError as exc:
            raise RuntimeError(
                "S3 blob store requires boto3. Install with: pip install boto3"
            ) from exc
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self._client = boto3.client(
            "s3",
            endpoint_url=endpoint_url or None,
            aws_access_key_id=access_key or os.environ.get("AWS_ACCESS_KEY_ID") or None,
            aws_secret_access_key=secret_key
            or os.environ.get("AWS_SECRET_ACCESS_KEY")
            or None,
            region_name=region,
            config=Config(s3={"addressing_style": "path"}),
        )
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        try:
            self._client.head_bucket(Bucket=self.bucket)
        except Exception:
            try:
                self._client.create_bucket(Bucket=self.bucket)
            except Exception:
                # Bucket may already exist or be externally managed.
                pass

    def _key(self, kind: str, blob_id: str) -> str:
        return f"{self.prefix}/{kind}/{blob_id[:2]}/{blob_id}"

    def _meta_key(self, blob_id: str) -> str:
        return f"{self.prefix}/_index/{blob_id}.json"

    def put(
        self,
        content: bytes,
        *,
        kind: str,
        media_type: str = "application/octet-stream",
        session_id: str = "",
    ) -> dict:
        blob_id = uuid.uuid4().hex
        key = self._key(kind, blob_id)
        digest = hashlib.sha256(content).hexdigest()
        meta = {
            "blob_id": blob_id,
            "sha256": digest,
            "size": len(content),
            "media_type": media_type,
            "kind": kind,
            "relative_path": key,
            "session_id": session_id,
            "storage": "s3",
            "bucket": self.bucket,
        }
        self._client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=content,
            ContentType=media_type or "application/octet-stream",
            Metadata={"sha256": digest, "kind": kind},
        )
        self._client.put_object(
            Bucket=self.bucket,
            Key=self._meta_key(blob_id),
            Body=json.dumps(meta, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json",
        )
        return meta

    def get(self, blob_id: str) -> bytes:
        meta_obj = self._client.get_object(Bucket=self.bucket, Key=self._meta_key(blob_id))
        meta = json.loads(meta_obj["Body"].read().decode("utf-8"))
        key = str(meta.get("relative_path") or "")
        if not key:
            raise FileNotFoundError(blob_id)
        obj = self._client.get_object(Bucket=self.bucket, Key=key)
        return obj["Body"].read()

    def delete(self, blob_id: str) -> bool:
        try:
            meta_obj = self._client.get_object(Bucket=self.bucket, Key=self._meta_key(blob_id))
            meta = json.loads(meta_obj["Body"].read().decode("utf-8"))
            key = str(meta.get("relative_path") or "")
        except Exception:
            return False
        removed = False
        if key:
            try:
                self._client.delete_object(Bucket=self.bucket, Key=key)
                removed = True
            except Exception:
                pass
        try:
            self._client.delete_object(Bucket=self.bucket, Key=self._meta_key(blob_id))
            removed = True
        except Exception:
            pass
        return removed


def build_blob_store(*, blob_root: Path | None = None) -> BlobStore:
    """Build blob backend from MOLMIND_BLOB_STORE_URL or local directory."""
    url = str(os.environ.get("MOLMIND_BLOB_STORE_URL") or "").strip()
    if not url:
        root = blob_root or Path(
            str(os.environ.get("MOLMIND_BLOB_ROOT") or "").strip()
            or (Path(__file__).resolve().parents[2] / "data" / "agent_runs" / "blobs")
        )
        return LocalBlobStore(root)
    parsed = urlparse(url)
    scheme = (parsed.scheme or "").lower()
    if scheme in {"file", ""}:
        path = parsed.path or url.removeprefix("file://")
        return LocalBlobStore(Path(path))
    if scheme in {"s3", "minio"}:
        # s3://bucket/prefix or minio://bucket/prefix with endpoint via env
        bucket = parsed.netloc or parsed.path.lstrip("/").split("/", 1)[0]
        prefix = ""
        if parsed.netloc and parsed.path:
            prefix = parsed.path.lstrip("/")
        elif "/" in parsed.path.lstrip("/"):
            _, prefix = parsed.path.lstrip("/").split("/", 1)
        endpoint = str(os.environ.get("MOLMIND_S3_ENDPOINT") or "").strip() or None
        return S3BlobStore(
            bucket=bucket,
            endpoint_url=endpoint,
            prefix=prefix or "agent-blobs",
            access_key=str(os.environ.get("MOLMIND_S3_ACCESS_KEY") or "").strip() or None,
            secret_key=str(os.environ.get("MOLMIND_S3_SECRET_KEY") or "").strip() or None,
            region=str(os.environ.get("MOLMIND_S3_REGION") or "us-east-1").strip(),
        )
    raise RuntimeError(f"Unsupported MOLMIND_BLOB_STORE_URL scheme: {scheme!r}")


def attachment_kind_for_filename(filename: str) -> str:
    name = (filename or "").lower()
    if name.endswith(".sdf"):
        return "sdf"
    if name.endswith(".pdf"):
        return "pdf"
    if name.endswith((".png", ".jpg", ".jpeg", ".webp", ".gif")):
        return "image"
    if name.endswith((".txt", ".md", ".csv", ".tsv", ".json")):
        return "document"
    if name.endswith((".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx")):
        return "document"
    return "file"


ALLOWED_ATTACHMENT_EXTENSIONS = frozenset(
    {
        ".sdf",
        ".pdf",
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".gif",
        ".txt",
        ".md",
        ".csv",
        ".tsv",
        ".json",
        ".doc",
        ".docx",
    }
)


def is_allowed_attachment_filename(filename: str) -> bool:
    name = (filename or "").lower()
    return any(name.endswith(ext) for ext in ALLOWED_ATTACHMENT_EXTENSIONS)
