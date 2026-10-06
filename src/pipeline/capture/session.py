"""Authenticated HTTP session against Instagram's private web API.

Ported from scrap-saved-instagram/ig_saved.py, validated over 65 pages /
1345 reels. No password involved: we reuse the cookies of a session already open
in the browser.
"""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

import requests

API = "https://www.instagram.com/api/v1"

# Without this header the API answers 400/403 even with a valid sessionid. It is
# the most common cause of a scraper that "used to work".
IG_APP_ID = "936619743392459"

UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent


class IGError(RuntimeError):
    pass


def load_dotenv(path: Path | None = None) -> None:
    path = path or PROJECT_ROOT / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def parse_cookie_header(raw: str) -> dict[str, str]:
    jar: dict[str, str] = {}
    for chunk in raw.split(";"):
        name, sep, value = chunk.strip().partition("=")
        if sep and name:
            jar[name.strip()] = value.strip()
    return jar


def build_cookies() -> dict[str, str]:
    raw = os.environ.get("IG_COOKIE", "").strip()
    jar = parse_cookie_header(raw) if raw else {}

    for env_key, cookie_key in (
        ("IG_SESSIONID", "sessionid"),
        ("IG_CSRFTOKEN", "csrftoken"),
        ("IG_DS_USER_ID", "ds_user_id"),
        ("IG_MID", "mid"),
        ("IG_IG_DID", "ig_did"),
        ("IG_RUR", "rur"),
    ):
        value = os.environ.get(env_key, "").strip()
        if value:
            jar[cookie_key] = value

    if not jar.get("sessionid"):
        raise IGError(
            "Cookie 'sessionid' not found.\n"
            "  -> create .env at the project root with IG_SESSIONID=...\n"
            "     (DevTools -> Application -> Cookies -> instagram.com)"
        )
    jar.setdefault("csrftoken", "missing")
    if not jar.get("ds_user_id"):
        match = re.match(r"^(\d+)(?:%3A|:)", jar["sessionid"])
        if match:
            jar["ds_user_id"] = match.group(1)
    return jar


def sessionid_variants(sessionid: str) -> list[str]:
    """Instagram stores ':' encoded as '%3A'; some inspectors show it decoded."""
    variants = [sessionid]
    if "%3A" in sessionid:
        variants.append(sessionid.replace("%3A", ":"))
    elif ":" in sessionid:
        variants.append(sessionid.replace(":", "%3A"))
    return variants


def make_session() -> requests.Session:
    load_dotenv()
    cookies = build_cookies()

    session = requests.Session()
    session.cookies.update(cookies)
    session.headers.update(
        {
            "User-Agent": UA,
            "Accept": "*/*",
            "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
            "X-IG-App-ID": IG_APP_ID,
            "X-CSRFToken": cookies.get("csrftoken", ""),
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://www.instagram.com/",
            "Origin": "https://www.instagram.com",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-origin",
        }
    )

    # Pick whichever sessionid encoding is accepted, before doing any work.
    attempts: list[str] = []
    for variant in sessionid_variants(cookies["sessionid"]):
        session.cookies.set("sessionid", variant)
        response = session.get(f"{API}/feed/saved/posts/", timeout=30)
        if response.status_code == 200:
            return session
        attempts.append(f"HTTP {response.status_code}")

    raise IGError(
        "Session refused by Instagram (" + ", ".join(attempts) + ").\n"
        "  The sessionid cookie has expired. Log in again and copy it across."
    )


def get_json(session: requests.Session, url: str, params: dict | None = None) -> dict:
    """GET plus 401/429/5xx handling. Instagram's errors are not talkative."""
    for attempt in range(4):
        response = session.get(url, params=params, timeout=30)

        if response.status_code == 200:
            try:
                return response.json()
            except ValueError:
                raise IGError(
                    f"Non-JSON response from {url} (login page or challenge?)"
                )

        if response.status_code in (401, 403):
            raise IGError(f"HTTP {response.status_code}: session expired or invalid.")

        if response.status_code == 429 or "wait a few minutes" in response.text.lower():
            wait = 60 * (attempt + 1)
            print(f"  rate-limit -> waiting {wait}s", file=sys.stderr)
            time.sleep(wait)
            continue

        if response.status_code >= 500:
            time.sleep(5 * (attempt + 1))
            continue

        raise IGError(f"HTTP {response.status_code} on {url}: {response.text[:200]}")

    raise IGError(f"Failed after several attempts on {url}")
