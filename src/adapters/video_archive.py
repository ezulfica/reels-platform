"""Validate local reel archives and derive lightweight viewing assets on demand."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from pathlib import Path

from storage.database import now


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _probe(path: Path) -> dict:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=codec_type,codec_name,width,height",
        "-of",
        "json",
        str(path),
    ]
    completed = subprocess.run(command, check=True, capture_output=True, text=True)
    payload = json.loads(completed.stdout)
    streams = payload.get("streams", [])
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    if not video:
        raise ValueError("no video stream")
    duration = float(payload.get("format", {}).get("duration") or 0)
    if duration <= 0:
        raise ValueError("missing duration")
    return {
        "duration_s": duration,
        "width": video.get("width"),
        "height": video.get("height"),
        "video_codec": video.get("codec_name"),
        "audio_codec": audio.get("codec_name") if audio else None,
    }


def _proxy_command(source: Path, proxy: Path, max_height: int) -> list[str]:
    """Build a portable proxy command without filter-expression commas.

    FFmpeg treats an unescaped comma inside `min(720,ih)` as a filter separator.
    Escaping it preserves `-2`'s even-width calculation, which libx264 requires,
    while avoiding upscaling a naturally smaller video.
    """
    return [
        "ffmpeg",
        "-y",
        "-i",
        str(source),
        "-vf",
        f"scale=-2:min({max_height}\\,ih)",
        "-c:v",
        "libx264",
        "-crf",
        "28",
        "-preset",
        "medium",
        "-c:a",
        "aac",
        "-b:a",
        "96k",
        "-movflags",
        "+faststart",
        str(proxy),
    ]


def validate(conn: sqlite3.Connection, shortcode: str) -> dict:
    """Check that the downloaded source is playable and persist immutable facts."""
    row = conn.execute(
        "SELECT mp4_path FROM media WHERE shortcode = ?", (shortcode,)
    ).fetchone()
    if not row or not row["mp4_path"]:
        raise ValueError(f"no local video for {shortcode}")
    path = Path(row["mp4_path"])
    try:
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError("missing or empty video")
        info = _probe(path)
    except (
        OSError,
        ValueError,
        subprocess.SubprocessError,
        json.JSONDecodeError,
    ) as error:
        conn.execute(
            "UPDATE media SET validation_error=?, validated_at=? WHERE shortcode=?",
            (str(error)[:500], now(), shortcode),
        )
        conn.commit()
        raise ValueError(f"invalid video for {shortcode}: {error}") from error
    conn.execute(
        """UPDATE media SET bytes=?, duration_s=?, sha256=?, width=?, height=?, video_codec=?, audio_codec=?,
                             validated_at=?, validation_error=NULL WHERE shortcode=?""",
        (
            path.stat().st_size,
            info["duration_s"],
            _sha256(path),
            info["width"],
            info["height"],
            info["video_codec"],
            info["audio_codec"],
            now(),
            shortcode,
        ),
    )
    conn.commit()
    return info


def make_first_frame(conn: sqlite3.Connection, shortcode: str) -> str:
    """Derive a stable first frame for navigation, without creating a proxy."""
    row = conn.execute(
        "SELECT mp4_path FROM media WHERE shortcode=?", (shortcode,)
    ).fetchone()
    if not row or not row["mp4_path"]:
        raise ValueError(f"no local video for {shortcode}")
    source = Path(row["mp4_path"])
    if not source.is_file():
        raise ValueError(f"missing local video for {shortcode}")
    poster = source.parent / f"{source.stem}-first-frame.jpg"
    if not poster.is_file() or poster.stat().st_size == 0:
        subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-q:v",
                "3",
                str(poster),
            ],
            check=True,
            capture_output=True,
        )
    conn.execute(
        "UPDATE media SET poster_path=? WHERE shortcode=?", (str(poster), shortcode)
    )
    conn.commit()
    return str(poster)


def make_entity_first_frames(conn: sqlite3.Connection) -> dict[str, int]:
    """Create missing first frames only for reels that illustrate catalogue entities."""
    rows = conn.execute(
        "SELECT DISTINCT er.shortcode FROM entity_reel er JOIN media m ON m.shortcode=er.shortcode WHERE m.mp4_path IS NOT NULL"
    ).fetchall()
    stats = {"done": 0, "failed": 0}
    for row in rows:
        try:
            make_first_frame(conn, row["shortcode"])
            stats["done"] += 1
        except (OSError, ValueError, subprocess.SubprocessError):
            stats["failed"] += 1
    return stats


def make_viewing_assets(
    conn: sqlite3.Connection, shortcode: str, *, max_height: int = 720
) -> dict:
    """Create a poster and H.264 proxy next to the source, without replacing it."""
    row = conn.execute(
        "SELECT mp4_path, duration_s FROM media WHERE shortcode = ?", (shortcode,)
    ).fetchone()
    if not row or not row["mp4_path"]:
        raise ValueError(f"no local video for {shortcode}")
    source = Path(row["mp4_path"])
    info = validate(conn, shortcode)
    float(row["duration_s"] or info["duration_s"])
    target_dir = source.parent
    poster = target_dir / f"{source.stem}-first-frame.jpg"
    proxy = target_dir / f"{source.stem}-proxy.mp4"
    timestamp = 0.0  # Stable entity-card illustration; source media remains unchanged.
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-ss",
            f"{timestamp:.2f}",
            "-i",
            str(source),
            "-frames:v",
            "1",
            "-q:v",
            "3",
            str(poster),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        _proxy_command(source, proxy, max_height), check=True, capture_output=True
    )
    conn.execute(
        "UPDATE media SET poster_path=?, proxy_path=? WHERE shortcode=?",
        (str(poster), str(proxy), shortcode),
    )
    conn.commit()
    return {
        "poster_path": str(poster),
        "proxy_path": str(proxy),
        "source_sha256": _sha256(source),
    }
