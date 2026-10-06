"""OCR over keyframes extracted by ffmpeg.

0.5 frames per second: measured over 20 real videos this session. On-screen text
stays up for 2 to 4 s, so denser sampling only re-reads the same word in several
variants (`taipei`, `tafpef`, `\'faipei` — those examples date from easyocr,
RapidOCR is markedly more stable) without learning anything new. Perceptual-hash
deduplication was abandoned (measured at only 26% savings): so it is not frame
sampling that filters the noise, but the de-duplication of consecutive identical
text lines done below.

The fps is not fixed, though: applied as-is to a short video (~8 s), 0.5 fps
yields only 4 frames — not enough for an itinerary-style reel showing 7 different
places (case DZkXscZRD4j, seen during review). So the fps is derived from the
duration to hold the NUMBER of frames inside a [MIN_FRAMES, MAX_FRAMES] band
rather than a fixed rate: short videos are sampled more densely, long ones capped
so compute time does not explode.

OCR runs on EVERY reel, with no arbitration, unlike the old vision step. That is
measured: 45 of the corpus's 296 verified entities (15%) are attested by it alone,
and gating it on "the extraction returned few entities" would lose 62% of them —
a reel rich in caption can perfectly well have a screen covered in names that no
sentence pronounces.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

from config import settings as config
from storage.database import mark, now

TOOL_VERSION = "rapidocr-ppocrv6-adaptive-fps"

# These three thresholds are measured, not guessed — and they are overridable
# through `.env` (cf. config/settings.py) because they were calibrated on 60 reels
# downloaded out of 1347. `reels tuning` re-measures them against the current
# database; hard-coding them would replay the num_ctx=8192 mistake, set once and
# never re-examined.
FPS = config.ocr_fps()  # reference rate, "average" video
MIN_FRAMES = config.ocr_min_frames()  # floor: a short video stays covered
MAX_FRAMES_EXTRACT = config.ocr_max_frames()  # cap: the long tail is expensive


def _reader():
    """RapidOCR (PP-OCRv6 models through onnxruntime), replacing easyocr.

    Three reasons, measured on the corpus reels whose OCR was degraded:

    1. It reads non-latin scripts. easyocr was configured as ["fr", "en"] and its
       CJK models only combine with English — ["fr","en","ja"] was impossible, it
       would have taken one pass per script family. So all Chinese or Japanese
       on-screen text came out as latin approximations: "Zi#t# SCALLION PANCAKE"
       for 葱油饼, "FKAFFETJ" for 有KAFFE, "Onncta b*08363H474" for perfectly
       legible Japanese. RapidOCR returns 蘭芳麵食館 / LAN FANG,
       東區粉圓 / EASTERN ICE STORE, and fixes the latin along the way
       ("Ay-Clung" -> "Ay-Chung", the real name).
    2. It runs on CPU through onnxruntime, with no accelerator dependency.
    3. easyocr has had no release since September 2024.

    Cost: it is quick to load (0.2s in the measured corpus) and stays suitable
    for small batches as well as a whole-corpus pass."""
    from rapidocr import RapidOCR

    return RapidOCR()


def probe_duration(mp4_path: Path) -> float | None:
    try:
        out = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "csv=p=0",
                str(mp4_path),
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=True,
        )
        return float(out.stdout.strip())
    except Exception:  # noqa: BLE001 - no readable duration: fall back to fixed FPS
        return None


def _adaptive_fps(duration_s: float | None) -> float:
    """fps derived from the duration to aim for a frame COUNT inside
    [MIN_FRAMES, MAX_FRAMES_EXTRACT] rather than a fixed rate (cf. the module
    docstring)."""
    if not duration_s or duration_s <= 0:
        return FPS
    target = duration_s * FPS
    if target < MIN_FRAMES:
        return MIN_FRAMES / duration_s
    if target > MAX_FRAMES_EXTRACT:
        return MAX_FRAMES_EXTRACT / duration_s
    return FPS


def extract_frames(mp4_path: Path, out_dir: Path) -> list[Path]:
    fps = _adaptive_fps(probe_duration(mp4_path))
    subprocess.run(
        [
            "ffmpeg",
            "-loglevel",
            "error",
            "-y",
            "-i",
            str(mp4_path),
            "-vf",
            f"fps={fps}",
            str(out_dir / "f%04d.jpg"),
        ],
        check=True,
        timeout=120,
    )
    return sorted(out_dir.glob("f*.jpg"))


def ocr_observations(reader, frames: list[Path]) -> list[dict[str, object]]:
    observations: list[dict[str, object]] = []
    previous = None
    for frame in frames:
        result = reader(str(frame))
        # RapidOCR returns an object whose `txts` carries the recognised segments,
        # and None when the frame contains no text at all.
        segments = list(getattr(result, "txts", None) or ())
        joined = " ".join(t.strip() for t in segments if t and t.strip())
        # The same on-screen text stays visible across consecutive frames: keeping
        # only the changes avoids repeating the same line six times.
        if joined and joined != previous:
            observations.append(
                {
                    "frame": frame.stem,
                    "text": joined,
                }
            )
        previous = joined
    return observations


def ocr_frames(reader, frames: list[Path]) -> str:
    """Return the legacy aggregate representation used by existing callers."""
    return "\n".join(item["text"] for item in ocr_observations(reader, frames))


def _todo(conn: sqlite3.Connection, limit: int | None) -> list[tuple[str, str]]:
    """Same reason as asr._todo: a direct query over the pending mp4s, not
    `pending()`, which orders by taken_at while downloading advances by rowid — on
    a small-batch flow, a `--limit` based on `pending()` ends up targeting only
    reels with no mp4."""
    sql = """
        SELECT bm.shortcode, bm.mp4_path
        FROM media bm
        JOIN reel r ON r.shortcode = bm.shortcode
        LEFT JOIN screen_text so ON so.shortcode = bm.shortcode
                                AND so.tool_version = ?
        LEFT JOIN stage_state s ON s.shortcode = bm.shortcode AND s.stage = 'ocr'
        WHERE bm.mp4_path IS NOT NULL AND r.unsaved_at IS NULL AND so.shortcode IS NULL
          AND (s.status IS NULL OR s.status != 'failed' OR s.attempts < 3)
        -- Neither 'done' nor 'skipped' is terminal here, and that is deliberate:
        -- what counts is the presence of a screen_text row FOR THE CURRENT
        -- TOOL_VERSION, exactly as extract._todo trusts (prompt_version, model)
        -- rather than stage_state. Without the tool_version predicate, moving from
        -- easyocr to RapidOCR replayed no reel at all: the 60 easyocr rows were
        -- enough to drop every one of them out of the queue, and the new reader
        -- never ran. A 'skipped: no mp4' status is stale anyway for a row we find
        -- here, mp4_path IS NOT NULL being imposed above.
        --
        -- Accepted limit: `attempts` has no tool_version dimension. A reel that
        -- failed 3 times under the old reader stays out of the queue under the new
        -- one. Three failures in a row point in practice at the video (unreadable
        -- mp4), not the reader; the day that stops being true, tool_version will
        -- have to go into stage_state.
        ORDER BY r.rowid
    """
    params: list = [TOOL_VERSION]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return [(r["shortcode"], r["mp4_path"]) for r in conn.execute(sql, params)]


def run(conn: sqlite3.Connection, limit: int | None = None) -> dict[str, int]:
    todo = _todo(conn, limit)
    stats = {"ok": 0, "no_text": 0, "failed": 0}
    if not todo:
        return stats

    reader = _reader()
    tmp_root = Path(tempfile.mkdtemp(prefix="reels-ocr-"))

    try:
        for i, (shortcode, mp4_path) in enumerate(todo, 1):
            frame_dir = tmp_root / shortcode
            frame_dir.mkdir(exist_ok=True)
            try:
                frames = extract_frames(Path(mp4_path), frame_dir)
                observations = ocr_observations(reader, frames)
                text = "\n".join(item["text"] for item in observations)
            except Exception as error:  # noqa: BLE001
                stats["failed"] += 1
                mark(conn, shortcode, "ocr", "failed", str(error)[:300])
                print(
                    f"  [{i}/{len(todo)}] {shortcode} FAILED — {error}", file=sys.stderr
                )
                conn.commit()
                continue
            finally:
                shutil.rmtree(frame_dir, ignore_errors=True)

            conn.execute(
                "INSERT OR REPLACE INTO screen_text"
                "(shortcode, tool_version, text, frames_used, observations_json, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    shortcode,
                    TOOL_VERSION,
                    text,
                    len(frames),
                    json.dumps(observations, ensure_ascii=False),
                    now(),
                ),
            )
            mark(conn, shortcode, "ocr", "done")
            conn.commit()

            if text:
                stats["ok"] += 1
                preview = text.replace("\n", " / ")[:80]
                print(
                    f"  [{i}/{len(todo)}] {shortcode} ok ({len(frames)} frames) — {preview}",
                    file=sys.stderr,
                )
            else:
                stats["no_text"] += 1
                print(
                    f"  [{i}/{len(todo)}] {shortcode} no text ({len(frames)} frames)",
                    file=sys.stderr,
                )
    finally:
        shutil.rmtree(tmp_root, ignore_errors=True)

    return stats
