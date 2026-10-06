"""Audio transcription through faster-whisper.

An empty result is not a failure: many reels have no voice-over (music only,
on-screen text only). `has_speech=0` records that and avoids retrying forever a
reel that will never speak.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

from faster_whisper import WhisperModel

from config import settings
from storage.database import mark, now

TOOL_VERSION = "faster-whisper-large-v3"

# Segments whose average confidence is too low are noise (music, breath) that
# Whisper hallucinates into words: we drop them rather than surface them as if
# they were speech.
MIN_AVG_LOGPROB = -1.0


def _configure_cuda_runtime() -> None:
    """Expose CUDA wheels to CTranslate2 before the first GPU inference.

    NVIDIA's pip packages place shared libraries under ``site-packages`` rather
    than a system linker path. CTranslate2 loads them lazily, so adding those
    directories here keeps ``reels asr`` self-contained.
    """
    try:
        import nvidia.cublas
        import nvidia.cudnn
    except ImportError as error:
        raise RuntimeError(
            "GPU ASR requires nvidia-cublas-cu12 and nvidia-cudnn-cu12; run uv sync"
        ) from error

    roots = (*nvidia.cublas.__path__, *nvidia.cudnn.__path__)
    library_paths = [str(Path(root) / "lib") for root in roots]
    existing = os.environ.get("LD_LIBRARY_PATH", "")
    paths = [*library_paths, *(path for path in existing.split(os.pathsep) if path)]
    os.environ["LD_LIBRARY_PATH"] = os.pathsep.join(dict.fromkeys(paths))


def _model():
    """Load Whisper on CUDA by default, with an explicit portable CPU fallback."""
    device = settings.asr_device()
    if device == "cuda":
        _configure_cuda_runtime()
        return WhisperModel("large-v3", device="cuda", compute_type="float16")
    return WhisperModel("large-v3", device="cpu", compute_type="int8")


def has_audio_stream(mp4_path: Path) -> bool:
    """Does the file carry an audio track at all?

    Asked only after a failure, so the probe costs nothing on the normal path.

    Reason it exists: three reels of the older corpus are VIDEO ONLY — yt-dlp fell
    back to `best` and returned VP9 with no audio stream — and faster-whisper dies
    on them with `tuple index out of range`, an error that says nothing about the
    cause. They were counted as failures, so they carried no `transcript` row, and
    extract._todo requires one: the reels would have been silently dropped from
    the corpus rather than treated as what they are, reels without speech.

    0 of 100 in the recent corpus, 3 of 100 in the older one. The format is what
    changed, not the code."""
    try:
        out = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "stream=index",
                "-of",
                "csv=p=0",
                str(mp4_path),
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
        return bool(out.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        # Cannot tell: treat it as a real failure rather than invent a silence.
        return True


def transcribe_one(model, mp4_path: Path) -> dict:
    segments, info = model.transcribe(str(mp4_path), vad_filter=True)
    kept = [s for s in segments if s.avg_logprob >= MIN_AVG_LOGPROB]

    text = " ".join(s.text.strip() for s in kept).strip()

    # No reel-level gate, deliberately. A sung or off-topic transcription
    # (cf. DZkXscZRD4j, a Japanese song over an English-language reel about
    # Taipei) is a real problem, but dropping it here would need a confidence
    # threshold to calibrate, fallible in both directions, inside a step whose job
    # is to transcribe faithfully. Deciding what is relevant belongs to extraction:
    # it reads the caption, the OCR and the audio together, so it is the only one
    # able to judge that a soundtrack is not about the subject. Steps 1 and 2 stay
    # faithful to the source, step 3 decides. avg_logprob is kept below all the
    # same: it costs nothing and makes diagnosis possible.
    return {
        "lang": info.language,
        "text": text,
        # avg_logprob is kept, not merely used to filter. Whisper transcribes
        # SINGING with markedly lower confidence than speech, and a reel whose
        # soundtrack is a song gets its lyrics injected into the context dossier as
        # if they described the subject. Observed on DZkXscZRD4j: a Japanese song
        # ("the sounds of the city echo softly...") over an English-language reel
        # about a Taipei district, where none of the places is spoken. Coherent but
        # off-topic content, so more harmful than plain noise.
        #
        # The MIN_AVG_LOGPROB threshold is not enough to catch this case: the
        # lyrics pass the filter segment by segment. It is the reel's AVERAGE
        # confidence that separates them, and it was being thrown away.
        "segments_json": json.dumps(
            [
                {
                    "start": s.start,
                    "end": s.end,
                    "text": s.text.strip(),
                    "avg_logprob": round(s.avg_logprob, 3),
                }
                for s in kept
            ],
            ensure_ascii=False,
        ),
        "has_speech": 1 if text else 0,
    }


def _todo(conn: sqlite3.Connection, limit: int | None) -> list[tuple[str, str]]:
    """Reels with an mp4 but no ASR yet. A direct query rather than the generic
    `pending()`: that one orders by taken_at while downloading advances by rowid —
    on a small-batch flow, a `--limit` based on `pending()` ends up targeting only
    reels with no mp4 (hence the `stage_state` polluted with "skipped" that this
    workaround avoids)."""
    sql = """
        SELECT bm.shortcode, bm.mp4_path
        FROM media bm
        JOIN reel r ON r.shortcode = bm.shortcode
        LEFT JOIN transcript sa ON sa.shortcode = bm.shortcode
                                AND sa.tool_version = ?
        LEFT JOIN stage_state s ON s.shortcode = bm.shortcode AND s.stage = 'asr'
        WHERE bm.mp4_path IS NOT NULL AND r.unsaved_at IS NULL AND sa.shortcode IS NULL
          AND (s.status IS NULL OR s.status = 'skipped'
               OR (s.status = 'failed' AND s.attempts < 3))
        ORDER BY r.rowid
    """
    # 'skipped' is not terminal here: bm.mp4_path IS NOT NULL is already imposed
    # above, so a "skipped: no mp4" status on a row we nevertheless find in this
    # result is necessarily stale (the mp4 arrived afterwards) — first batch versus
    # second batch is exactly that case.
    if limit:
        sql += " LIMIT ?"
        return [
            (r["shortcode"], r["mp4_path"])
            for r in conn.execute(sql, (TOOL_VERSION, limit))
        ]
    return [(r["shortcode"], r["mp4_path"]) for r in conn.execute(sql, (TOOL_VERSION,))]


def run(conn: sqlite3.Connection, limit: int | None = None) -> dict[str, int]:
    todo = _todo(conn, limit)
    stats = {"ok": 0, "no_speech": 0, "failed": 0}
    if not todo:
        return stats

    model = _model()
    try:
        for i, (shortcode, mp4_path) in enumerate(todo, 1):
            try:
                result = transcribe_one(model, Path(mp4_path))
            except Exception as error:  # noqa: BLE001 - one failure must not break the batch
                if not has_audio_stream(Path(mp4_path)):
                    # A video with no audio track is not a failure, it is a reel
                    # without speech — the case the pipeline already knows how to
                    # carry (has_speech=0). Recorded as such so the reel keeps a
                    # transcript row and stays in the corpus.
                    result = {
                        "lang": None,
                        "text": "",
                        "segments_json": "[]",
                        "has_speech": 0,
                    }
                else:
                    stats["failed"] += 1
                    mark(conn, shortcode, "asr", "failed", str(error)[:300])
                    print(
                        f"  [{i}/{len(todo)}] {shortcode} FAILED — {error}",
                        file=sys.stderr,
                    )
                    conn.commit()
                    continue

            conn.execute(
                "INSERT OR REPLACE INTO transcript"
                "(shortcode, tool_version, lang, text, segments_json, has_speech, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    shortcode,
                    TOOL_VERSION,
                    result["lang"],
                    result["text"],
                    result["segments_json"],
                    result["has_speech"],
                    now(),
                ),
            )
            mark(conn, shortcode, "asr", "done")
            conn.commit()

            if result["has_speech"]:
                stats["ok"] += 1
                preview = result["text"][:80].replace("\n", " ")
                print(
                    f"  [{i}/{len(todo)}] {shortcode} ok ({result['lang']}) — {preview}",
                    file=sys.stderr,
                )
            else:
                stats["no_speech"] += 1
                print(f"  [{i}/{len(todo)}] {shortcode} no voice-over", file=sys.stderr)

    finally:
        del model

    return stats
