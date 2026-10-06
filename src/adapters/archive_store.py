"""Provider-neutral contract for archiving validated original videos.

The AWS S3 adapter is opt-in and satisfies ``ObjectStore``. Tests use simulated
S3 clients; there is no automatic selection or upload command.
"""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from config import settings as config
from domain.repertoire import CONTENT_KINDS


@dataclass(frozen=True)
class ArchiveConfig:
    backend: str
    bucket: str
    endpoint: str
    region: str
    profile: str
    prefix: str


def settings() -> ArchiveConfig:
    """Read non-secret archive settings. Credentials stay with the SDK/runtime."""
    return ArchiveConfig(
        backend=config.archive_backend(),
        bucket=config.archive_bucket(),
        endpoint=config.archive_endpoint(),
        region=config.archive_region(),
        profile=config.archive_profile(),
        prefix=config.archive_prefix(),
    )


@dataclass(frozen=True)
class ObjectInfo:
    key: str
    uri: str
    sha256: str
    size_bytes: int


class ObjectStore(Protocol):
    """Small object-store surface; implementations own credentials and retries."""

    def head(self, key: str) -> ObjectInfo | None: ...

    def put(
        self, source: Path, key: str, *, sha256: str, size_bytes: int, content_type: str
    ) -> ObjectInfo: ...

    def get(self, key: str, destination: Path) -> ObjectInfo: ...

    def delete(self, key: str) -> None: ...


class S3ObjectStore:
    """AWS S3 adapter using boto3's standard credential chain."""

    def __init__(self, client, bucket: str):
        if not bucket:
            raise ValueError("REELS_ARCHIVE_BUCKET is required for S3")
        self.client = client
        self.bucket = bucket

    def _uri(self, key: str) -> str:
        return f"s3://{self.bucket}/{key}"

    def _info(self, key: str, response: dict) -> ObjectInfo:
        return ObjectInfo(
            key=key,
            uri=self._uri(key),
            sha256=response.get("Metadata", {}).get("sha256", ""),
            size_bytes=int(response.get("ContentLength", -1)),
        )

    def head(self, key: str) -> ObjectInfo | None:
        try:
            response = self.client.head_object(Bucket=self.bucket, Key=key)
        except Exception as error:
            response = getattr(error, "response", {})
            code = str(response.get("ResponseMetadata", {}).get("HTTPStatusCode", ""))
            error_code = str(response.get("Error", {}).get("Code", ""))
            if code == "404" or error_code in {"404", "NoSuchKey", "NotFound"}:
                return None
            raise
        return self._info(key, response)

    def put(
        self, source: Path, key: str, *, sha256: str, size_bytes: int, content_type: str
    ) -> ObjectInfo:
        self.client.upload_file(
            str(source),
            self.bucket,
            key,
            ExtraArgs={"ContentType": content_type, "Metadata": {"sha256": sha256}},
        )
        stored = self.head(key)
        if stored is None or not _matches(
            stored, key=key, sha256=sha256, size_bytes=size_bytes
        ):
            raise ValueError(f"S3 object verification failed after upload: {key}")
        return stored

    def get(self, key: str, destination: Path) -> ObjectInfo:
        response = self.client.get_object(Bucket=self.bucket, Key=key)
        body = response["Body"]
        try:
            with destination.open("wb") as output:
                shutil.copyfileobj(body, output)
        finally:
            body.close()
        return self._info(key, response)

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)


def build_store(archive_config: ArchiveConfig | None = None) -> S3ObjectStore:
    """Construct the explicitly selected S3 adapter; never creates a bucket."""
    selected = archive_config or settings()
    if selected.backend != "s3":
        raise ValueError("remote archive backend is disabled")
    if not selected.bucket:
        raise ValueError("REELS_ARCHIVE_BUCKET is required for S3")
    try:
        import boto3
    except ImportError as error:
        raise RuntimeError(
            "Install the project S3 extra to use the archive backend"
        ) from error
    session = boto3.Session(profile_name=selected.profile or None)
    client = session.client(
        "s3",
        region_name=selected.region or None,
        endpoint_url=selected.endpoint or None,
    )
    return S3ObjectStore(client, selected.bucket)


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _object_key(prefix: str, sha256: str) -> str:
    root = prefix.strip("/")
    relative = f"sha256/{sha256[:2]}/{sha256}.mp4"
    return f"{root}/{relative}" if root else relative


def _matches(info: ObjectInfo, *, key: str, sha256: str, size_bytes: int) -> bool:
    return (
        info.key == key
        and info.sha256 == sha256
        and info.size_bytes == size_bytes
        and bool(info.uri)
    )


def archive_validated_original(
    conn: sqlite3.Connection,
    shortcode: str,
    store: ObjectStore,
    *,
    prefix: str = "reels-originals",
) -> ObjectInfo:
    """Store one validated repertoire original by hash and persist its URI.

    The caller must deliberately construct/pass an ObjectStore. This function
    never selects a provider from configuration and has no CLI auto-invocation.
    """
    row = conn.execute(
        """SELECT m.mp4_path, m.sha256, m.bytes, m.duration_s, m.width, m.height,
                  m.validated_at, e.mode, re.content_kind
           FROM media m
           LEFT JOIN extraction e ON e.shortcode = m.shortcode
           LEFT JOIN repertoire_entry re ON re.shortcode = m.shortcode
           WHERE m.shortcode = ?""",
        (shortcode,),
    ).fetchone()
    if not row or not row["mp4_path"]:
        raise ValueError(f"no local video for {shortcode}")
    if row["mode"] != "repertoire" or row["content_kind"] not in CONTENT_KINDS:
        raise ValueError(
            f"only classified repertoire reels are eligible for archive: {shortcode}"
        )
    path = Path(row["mp4_path"])
    if not row["validated_at"] or not row["sha256"]:
        raise ValueError(f"video must pass validate-video before archive: {shortcode}")
    if not path.is_file() or path.stat().st_size <= 0:
        raise ValueError(f"missing or empty video for {shortcode}")
    size_bytes = path.stat().st_size
    sha256 = str(row["sha256"])
    if _hash_file(path) != sha256:
        raise ValueError(f"video changed since validation: {shortcode}")
    if (
        not row["bytes"]
        or int(row["bytes"]) != size_bytes
        or not row["duration_s"]
        or float(row["duration_s"]) <= 0
        or not row["width"]
        or int(row["width"]) <= 0
        or not row["height"]
        or int(row["height"]) <= 0
    ):
        raise ValueError(f"video validation facts are incomplete or stale: {shortcode}")

    key = _object_key(prefix, sha256)
    existing = store.head(key)
    if existing is None:
        info = store.put(
            path, key, sha256=sha256, size_bytes=size_bytes, content_type="video/mp4"
        )
    else:
        info = existing
    if not _matches(info, key=key, sha256=sha256, size_bytes=size_bytes):
        raise ValueError(f"remote object checksum or size mismatch for {shortcode}")
    conn.execute(
        "UPDATE media SET archive_uri=? WHERE shortcode=?", (info.uri, shortcode)
    )
    conn.commit()
    return info


def restore_object(store: ObjectStore, info: ObjectInfo, destination: Path) -> Path:
    """Restore an object and verify bytes before making the destination usable."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{destination.name}.",
        suffix=".restore",
        dir=destination.parent,
        delete=False,
    ) as handle:
        temporary = Path(handle.name)
    try:
        downloaded = store.get(info.key, temporary)
        if not _matches(
            downloaded, key=info.key, sha256=info.sha256, size_bytes=info.size_bytes
        ):
            raise ValueError("remote object metadata does not match the archive record")
        if (
            not temporary.is_file()
            or temporary.stat().st_size != info.size_bytes
            or _hash_file(temporary) != info.sha256
        ):
            raise ValueError("restored video failed checksum or size verification")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination
