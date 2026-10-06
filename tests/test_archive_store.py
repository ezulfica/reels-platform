from __future__ import annotations

import hashlib
import io
import sqlite3
import sys
from types import SimpleNamespace
from pathlib import Path

import pytest

from adapters import archive_store


class FakeStore:
    def __init__(self):
        self.objects: dict[str, tuple[bytes, archive_store.ObjectInfo]] = {}
        self.put_calls = 0

    def head(self, key: str):
        stored = self.objects.get(key)
        return stored[1] if stored else None

    def put(
        self, source: Path, key: str, *, sha256: str, size_bytes: int, content_type: str
    ):
        assert content_type == "video/mp4"
        self.put_calls += 1
        data = source.read_bytes()
        info = archive_store.ObjectInfo(key, f"fake://bucket/{key}", sha256, size_bytes)
        self.objects[key] = (data, info)
        return info

    def get(self, key: str, destination: Path):
        data, info = self.objects[key]
        destination.write_bytes(data)
        return info

    def delete(self, key: str):
        self.objects.pop(key, None)


class MissingS3Object(Exception):
    response = {"ResponseMetadata": {"HTTPStatusCode": 404}, "Error": {"Code": "404"}}


class FakeS3Client:
    def __init__(self):
        self.objects = {}

    def head_object(self, *, Bucket, Key):
        try:
            data, metadata = self.objects[(Bucket, Key)]
        except KeyError as error:
            raise MissingS3Object from error
        return {"ContentLength": len(data), "Metadata": metadata}

    def upload_file(self, filename, bucket, key, *, ExtraArgs):
        self.objects[(bucket, key)] = (
            Path(filename).read_bytes(),
            ExtraArgs["Metadata"].copy(),
        )

    def get_object(self, *, Bucket, Key):
        data, metadata = self.objects[(Bucket, Key)]
        return {
            "Body": io.BytesIO(data),
            "ContentLength": len(data),
            "Metadata": metadata,
        }

    def delete_object(self, *, Bucket, Key):
        self.objects.pop((Bucket, Key), None)


@pytest.fixture
def archive_conn(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE media (
        shortcode TEXT PRIMARY KEY, mp4_path TEXT, sha256 TEXT, bytes INTEGER,
        duration_s REAL, width INTEGER, height INTEGER, validated_at TEXT,
        archive_uri TEXT
    )""")
    conn.execute("CREATE TABLE extraction (shortcode TEXT PRIMARY KEY, mode TEXT)")
    conn.execute(
        "CREATE TABLE repertoire_entry (shortcode TEXT PRIMARY KEY, content_kind TEXT)"
    )
    return conn


def classify_repertoire(
    conn, shortcode="ABC", content_kind="recipe", mode="repertoire"
):
    conn.execute("INSERT INTO extraction VALUES (?, ?)", (shortcode, mode))
    if mode == "repertoire":
        conn.execute(
            "INSERT INTO repertoire_entry VALUES (?, ?)", (shortcode, content_kind)
        )


def add_validated_video(conn, path: Path, shortcode="ABC", content_kind="recipe"):
    data = path.read_bytes()
    classify_repertoire(conn, shortcode, content_kind)
    conn.execute(
        "INSERT INTO media(shortcode, mp4_path, sha256, bytes, duration_s, width, height, validated_at) "
        "VALUES (?, ?, ?, ?, 1.0, 720, 1280, '2026-09-13T00:00:00+00:00')",
        (shortcode, str(path), hashlib.sha256(data).hexdigest(), len(data)),
    )


def test_archives_validated_original_by_hash_and_deduplicates(archive_conn, tmp_path):
    source = tmp_path / "original.mp4"
    source.write_bytes(b"validated original")
    add_validated_video(archive_conn, source)
    add_validated_video(archive_conn, source, "DEF")
    store = FakeStore()

    first = archive_store.archive_validated_original(archive_conn, "ABC", store)
    again = archive_store.archive_validated_original(archive_conn, "DEF", store)

    assert first == again
    assert store.put_calls == 1
    assert first.key.endswith(f"{first.sha256}.mp4")
    assert [
        row[0]
        for row in archive_conn.execute(
            "SELECT archive_uri FROM media ORDER BY shortcode"
        )
    ] == [first.uri, first.uri]


def test_archive_refuses_missing_validation_or_changed_source(archive_conn, tmp_path):
    source = tmp_path / "original.mp4"
    source.write_bytes(b"original")
    classify_repertoire(archive_conn)
    archive_conn.execute(
        "INSERT INTO media(shortcode, mp4_path) VALUES ('ABC', ?)", (str(source),)
    )
    store = FakeStore()
    with pytest.raises(ValueError, match="validate-video"):
        archive_store.archive_validated_original(archive_conn, "ABC", store)
    assert store.put_calls == 0

    add_validated_video(archive_conn, source, "DEF")
    source.write_bytes(b"changed after validation")
    with pytest.raises(ValueError, match="changed since validation"):
        archive_store.archive_validated_original(archive_conn, "DEF", store)
    assert store.put_calls == 0


@pytest.mark.parametrize(
    "mode,kind", [("recommandation", None), ("repertoire", "place")]
)
def test_archive_excludes_recommendation_and_non_repertoire_items(
    archive_conn, tmp_path, mode, kind
):
    source = tmp_path / "recommendation.mp4"
    source.write_bytes(b"video")
    classify_repertoire(archive_conn, content_kind=kind or "", mode=mode)
    data = source.read_bytes()
    archive_conn.execute(
        "INSERT INTO media(shortcode, mp4_path, sha256, bytes, duration_s, width, height, validated_at) "
        "VALUES ('ABC', ?, ?, ?, 1, 720, 1280, 'validated')",
        (str(source), hashlib.sha256(data).hexdigest(), len(data)),
    )

    with pytest.raises(ValueError, match="only classified repertoire reels"):
        archive_store.archive_validated_original(archive_conn, "ABC", FakeStore())


def test_archive_refuses_remote_object_with_wrong_hash(archive_conn, tmp_path):
    source = tmp_path / "original.mp4"
    source.write_bytes(b"original")
    add_validated_video(archive_conn, source)
    store = FakeStore()
    good = archive_store.archive_validated_original(archive_conn, "ABC", store)
    store.objects[good.key] = (
        b"bad",
        archive_store.ObjectInfo(good.key, good.uri, "0" * 64, good.size_bytes),
    )

    with pytest.raises(ValueError, match="checksum or size mismatch"):
        archive_store.archive_validated_original(archive_conn, "ABC", store)
    assert store.put_calls == 1


def test_restore_checks_bytes_and_preserves_destination_on_corruption(tmp_path):
    store = FakeStore()
    source = tmp_path / "original.mp4"
    source.write_bytes(b"original")
    info = store.put(
        source,
        "sha256/00/hash.mp4",
        sha256=hashlib.sha256(b"original").hexdigest(),
        size_bytes=8,
        content_type="video/mp4",
    )
    target = archive_store.restore_object(
        store, info, tmp_path / "restored" / "video.mp4"
    )
    assert target.read_bytes() == b"original"

    store.objects[info.key] = (b"corrupt!", info)
    with pytest.raises(ValueError, match="failed checksum"):
        archive_store.restore_object(store, info, target)
    assert target.read_bytes() == b"original"


def test_archive_configuration_is_disabled_without_explicit_selection(monkeypatch):
    monkeypatch.setenv("REELS_ARCHIVE_BACKEND", "disabled")
    monkeypatch.setenv("REELS_ARCHIVE_BUCKET", "")
    monkeypatch.setenv("REELS_ARCHIVE_ENDPOINT", "")
    monkeypatch.setenv("REELS_ARCHIVE_REGION", "")
    monkeypatch.setenv("REELS_ARCHIVE_PROFILE", "")
    monkeypatch.setenv("REELS_ARCHIVE_PREFIX", "reels-originals")

    assert archive_store.settings() == archive_store.ArchiveConfig(
        backend="disabled",
        bucket="",
        endpoint="",
        region="",
        profile="",
        prefix="reels-originals",
    )


def test_s3_configuration_accepts_an_optional_compatible_endpoint(monkeypatch):
    monkeypatch.setenv("REELS_ARCHIVE_BACKEND", "s3")
    monkeypatch.setenv("REELS_ARCHIVE_BUCKET", "chosen-later")
    monkeypatch.setenv("REELS_ARCHIVE_ENDPOINT", "https://objects.example.test")
    monkeypatch.setenv("REELS_ARCHIVE_REGION", "eu-west-3")
    monkeypatch.setenv("REELS_ARCHIVE_PROFILE", "archive-profile")
    monkeypatch.setenv("REELS_ARCHIVE_PREFIX", "/private/videos/")

    assert archive_store.settings() == archive_store.ArchiveConfig(
        backend="s3",
        bucket="chosen-later",
        endpoint="https://objects.example.test",
        region="eu-west-3",
        profile="archive-profile",
        prefix="private/videos",
    )


def test_s3_adapter_upload_head_restore_and_delete(tmp_path):
    client = FakeS3Client()
    store = archive_store.S3ObjectStore(client, "my-archive")
    source = tmp_path / "original.mp4"
    source.write_bytes(b"local original")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()

    assert store.head("sha256/00/object.mp4") is None
    info = store.put(
        source,
        "sha256/00/object.mp4",
        sha256=digest,
        size_bytes=source.stat().st_size,
        content_type="video/mp4",
    )
    assert info == archive_store.ObjectInfo(
        "sha256/00/object.mp4",
        "s3://my-archive/sha256/00/object.mp4",
        digest,
        source.stat().st_size,
    )

    destination = tmp_path / "restored.mp4"
    restored = store.get(info.key, destination)
    assert restored == info and destination.read_bytes() == source.read_bytes()
    store.delete(info.key)
    assert store.head(info.key) is None


def test_build_store_uses_boto3_profile_region_and_endpoint(monkeypatch):
    fake_client = FakeS3Client()
    calls = {}

    class FakeSession:
        def __init__(self, *, profile_name):
            calls["profile"] = profile_name

        def client(self, service, **kwargs):
            calls["client"] = (service, kwargs)
            return fake_client

    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(Session=FakeSession))
    settings = archive_store.ArchiveConfig(
        backend="s3",
        bucket="existing-bucket",
        endpoint="",
        region="eu-west-3",
        profile="personal",
        prefix="reels-originals",
    )

    store = archive_store.build_store(settings)

    assert isinstance(store, archive_store.S3ObjectStore)
    assert calls == {
        "profile": "personal",
        "client": ("s3", {"region_name": "eu-west-3", "endpoint_url": None}),
    }


def test_build_store_refuses_disabled_or_incomplete_configuration():
    with pytest.raises(ValueError, match="backend is disabled"):
        archive_store.build_store(
            archive_store.ArchiveConfig(
                backend="disabled",
                bucket="",
                endpoint="",
                region="",
                profile="",
                prefix="reels-originals",
            )
        )
    with pytest.raises(ValueError, match="BUCKET is required"):
        archive_store.build_store(
            archive_store.ArchiveConfig(
                backend="s3",
                bucket="",
                endpoint="",
                region="",
                profile="",
                prefix="reels-originals",
            )
        )
