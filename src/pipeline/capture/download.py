"""Video download through yt-dlp.

We go through the post URL, not through `video_versions` in the raw JSON: those
CDN URLs are signed and expire within days, so they would not survive a re-run.
"""

from __future__ import annotations

import random
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections.abc import Collection
from pathlib import Path

from storage.database import mark, now

from .session import build_cookies, load_dotenv

MEDIA_ROOT = Path(__file__).resolve().parent.parent.parent.parent / "media"

# yt-dlp does not distinguish causes by exit code: you have to read the message.
GONE = re.compile(
    r"(no longer available|video unavailable|content isn'?t available|"
    r"post (?:has been )?(?:deleted|removed)|page not found|HTTP Error 404)",
    re.IGNORECASE,
)
BLOCKED = re.compile(
    r"(login required|rate.?limit|checkpoint|challenge_required|"
    r"empty media response|HTTP Error 401|HTTP Error 403|HTTP Error 429)",
    re.IGNORECASE,
)


def write_cookie_file(path: Path) -> Path:
    """Cookies in Netscape format, the only file format yt-dlp accepts."""
    load_dotenv()
    jar = build_cookies()
    expiry = int(time.time()) + 365 * 24 * 3600
    lines = ["# Netscape HTTP Cookie File"]
    lines += [
        f".instagram.com\tTRUE\t/\tTRUE\t{expiry}\t{k}\t{v}" for k, v in jar.items()
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o600)
    return path


def download_one(
    shortcode: str, url: str, cookies: Path, timeout: int = 180
) -> tuple[int, Path | None, str]:
    """(status, path, message). 200 ok, 404 gone, 401 blocked, 0 technical failure."""
    target = MEDIA_ROOT / shortcode
    target.mkdir(parents=True, exist_ok=True)

    try:
        proc = subprocess.run(
            [
                "yt-dlp",
                "--cookies",
                str(cookies),
                "--no-playlist",
                "--no-warnings",
                "--no-progress",
                "--retries",
                "2",
                "--socket-timeout",
                "30",
                "--format",
                "mp4/best",
                "--output",
                str(target / f"{shortcode}.%(ext)s"),
                url,
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return 0, None, "timeout"

    media = next(
        (
            f
            for f in sorted(target.glob(f"{shortcode}.*"))
            if f.suffix in (".mp4", ".webm", ".mkv")
        ),
        None,
    )
    if proc.returncode == 0 and media and media.stat().st_size > 0:
        return 200, media, ""

    output = (proc.stderr or "") + (proc.stdout or "")
    if GONE.search(output):
        return 404, None, "deleted on Instagram"
    if BLOCKED.search(output):
        return 401, None, "auth / rate-limit"
    last = (
        output.strip().splitlines()[-1][:150] if output.strip() else "unknown failure"
    )
    return 0, None, last


def download(
    conn: sqlite3.Connection,
    limit: int | None = None,
    delay: tuple[float, float] = (1.5, 4.0),
    abort_after_blocked: int = 5,
    sample: int | None = None,
    seed: int = 0,
    shortcodes: Collection[str] | None = None,
) -> dict[str, int]:
    """Download the missing videos. Resumable: the state lives in stage_state.

    Two ways to pick what to fetch:

    - `limit`: the head of the queue, in feed order. Simple, and it is how the
      first 80 reels were downloaded — which is also its flaw: they are the 80
      most recently saved, hence one period of the user's life and whichever
      themes dominated it.
    - `sample` with `seed`: a deterministic random draw over the whole eligible
      corpus, which stops after `sample` SUCCESSFUL downloads. Reels saved long
      ago are more likely to have been deleted on Instagram since, so counting
      attempts rather than successes would quietly return fewer videos than
      asked for. The seed goes in the commit message: the draw has to be
      replayable, otherwise "we added 20 random reels" is not a fact anyone can
      check.
    """
    if not shutil.which("yt-dlp"):
        raise RuntimeError("yt-dlp not found in PATH")
    if shortcodes and (limit or sample):
        raise ValueError(
            "targeted download cannot be combined with --limit or --sample"
        )

    requested = set(shortcodes or ())
    # A completed stage is trusted only while its referenced file still exists.
    # This keeps the normal path idempotent while recovering from a moved or
    # deleted local MP4 after a later Instagram scan.
    todo = []
    rows = conn.execute(
        """
        SELECT r.shortcode, s.status, s.attempts, m.mp4_path
        FROM reel r
        LEFT JOIN stage_state s
               ON s.shortcode = r.shortcode AND s.stage = 'download'
        LEFT JOIN media m ON m.shortcode = r.shortcode
        WHERE r.unsaved_at IS NULL
          AND r.kind IN ('reel', 'video')
          AND (s.status IS NULL OR s.status = 'skipped'
               OR (s.status = 'failed' AND s.attempts < 3)
               OR s.status = 'done')
        ORDER BY r.rowid
        """
    )
    for row in rows:
        if requested and row["shortcode"] not in requested:
            continue
        if row["status"] == "done":
            path = Path(row["mp4_path"] or "")
            try:
                if path.is_file() and path.stat().st_size > 0:
                    continue
            except OSError:
                pass
        if row["status"] == "skipped":
            # `gone` is terminal; a later full sync can explicitly clear it if
            # Instagram makes the content available again.
            continue
        todo.append(row["shortcode"])
    if sample:
        random.Random(seed).shuffle(todo)
    elif limit:
        todo = todo[:limit]

    stats = {"ok": 0, "gone": 0, "blocked": 0, "failed": 0, "bytes": 0}
    tmp = Path(tempfile.mkdtemp(prefix="reels-"))

    try:
        cookies = write_cookie_file(tmp / "cookies.txt")
        for i, shortcode in enumerate(todo, 1):
            url = conn.execute(
                "SELECT url FROM reel WHERE shortcode = ?", (shortcode,)
            ).fetchone()["url"]

            status, path, message = download_one(shortcode, url, cookies)
            size = path.stat().st_size if path else None

            conn.execute(
                "INSERT OR REPLACE INTO media"
                "(shortcode, mp4_path, bytes, downloaded_at, http_status)"
                " VALUES (?, ?, ?, ?, ?)",
                (shortcode, str(path) if path else None, size, now(), status),
            )

            if status == 200:
                stats["ok"] += 1
                stats["bytes"] += size or 0
                mark(conn, shortcode, "download", "done")
            elif status == 404:
                stats["gone"] += 1
                # Nothing left to download: do not put it back in the queue.
                mark(conn, shortcode, "download", "skipped", message)
            elif status == 401:
                stats["blocked"] += 1
                mark(conn, shortcode, "download", "failed", message)
            else:
                stats["failed"] += 1
                mark(conn, shortcode, "download", "failed", message)
            conn.commit()

            flag = {200: "ok", 404: "gone", 401: "BLOCKED"}.get(status, "failed")
            detail = f" — {message}" if message else ""
            mo = f" {size / 1e6:.1f} MB" if size else ""
            # With `sample` the queue is the whole shuffled corpus, so "i of
            # len(todo)" would read [3/1214] for a draw of 20. Count what we are
            # actually after: successful downloads.
            progress = f"{stats['ok']}/{sample}" if sample else f"{i}/{len(todo)}"
            print(f"  [{progress}] {shortcode} {flag}{mo}{detail}", file=sys.stderr)

            if sample and stats["ok"] >= sample:
                break

            # A burst of blocks signals a rate limit: better to stop than to burn
            # the session on a thousand refused requests.
            if stats["blocked"] >= abort_after_blocked:
                print(
                    f"\n  {abort_after_blocked} blocks -> stopping. "
                    "Session expired or Instagram rate limit.",
                    file=sys.stderr,
                )
                break

            if i < len(todo):
                time.sleep(random.uniform(*delay))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    return stats
