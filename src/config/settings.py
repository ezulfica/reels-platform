"""Settings overridable through `.env`, in one place.

Two kinds of value used to live scattered across module constants: the model
choice (`extract.MODEL`, inherited by import in `verify.py`) and the processing
thresholds (`NUM_CTX`, `MAX_FRAMES_EXTRACT`, `MIN_FRAMES`, `FPS`).

Pulling them here answers two distinct needs:

- comparing two models without editing code, which was impossible while the model
  name was a constant;
- not freezing thresholds measured on 60 downloaded reels out of 1347. That is
  exactly what happened to `num_ctx=8192`: a number set once, never re-examined,
  which ended up crashing llama-server by reserving more than twice the KV cache
  it needed. `reels tuning` re-measures these thresholds against the current
  database; that only helps if they can be changed without touching code.

Resolution order: explicit argument (CLI) > `.env` > code default.

Inference is selected through `REELS_LLM_BACKEND`: `codex` (default), `hermes`,
`api` (OpenAI-compatible Chat Completions endpoint), or `ollama` (local model).
The extraction contracts and persistence do not depend on that choice.
"""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
ENV_PATH = PROJECT_ROOT / ".env"


def _env() -> dict[str, str]:
    """Read `.env` on every call rather than caching it: settings are consulted a
    handful of times per run, and a cache would mean remembering to invalidate it
    when the user edits the file between two commands."""
    values: dict[str, str] = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip().strip("'\"")
    return values


def _get(key: str, default: str) -> str:
    # The process environment wins over `.env`: that allows a one-off
    # `REELS_MODEL_EXTRACT=... reels extract` without editing any file.
    # The selected web profile is non-sensitive user configuration. It makes the
    # interactive chat and manual CLI runs agree with the weekly systemd service.
    # Explicit process variables still win for one-off evaluations and tests.
    try:
        from config import runtime
    except ImportError:
        profile_value = None
    else:
        profile_value = runtime.profile_env().get(key)
    return os.environ.get(key) or profile_value or _env().get(key) or default


def _get_int(key: str, default: int) -> int:
    raw = _get(key, str(default))
    try:
        return int(raw)
    except ValueError:
        return default


def _get_float(key: str, default: float) -> float:
    raw = _get(key, str(default))
    try:
        return float(raw)
    except ValueError:
        return default


# -------------------------------------------------------------------- models


def model(stage: str, override: str | None = None) -> str:
    """Which model a step should use. `verify` falls back to `extract`'s: the
    verification pass confronts an entity with the same source text the
    extraction saw, so running it on a different model would only make sense if
    we explicitly wanted a second opinion — which is not the case today."""
    from pipeline.extract.extract import DEFAULT_MODEL

    if override:
        return override
    if stage == "chat" and _get("REELS_MODEL_CHAT", ""):
        return _get("REELS_MODEL_CHAT", "")
    if llm_backend() == "hermes":
        return _get("REELS_MODEL_HERMES", "")
    if llm_backend() == "codex":
        return _get("REELS_MODEL_CODEX", "gpt-5.6-luna")
    if llm_backend() == "api":
        return _get("REELS_MODEL_API", "gpt-5.6-luna")
    if stage == "verify":
        return _get("REELS_MODEL_VERIFY", "") or model("extract")
    return _get("REELS_MODEL_EXTRACT", DEFAULT_MODEL)


SUPPORTED_LLM_BACKENDS = ("codex", "hermes", "api", "ollama")
SUPPORTED_ASR_DEVICES = ("cuda", "cpu")


def asr_device() -> str:
    """Execution device for local Whisper transcription.

    CUDA is the default on the workstation. Set ``REELS_ASR_DEVICE=cpu`` to run
    without the NVIDIA runtime, for example on another machine.
    """
    device = _get("REELS_ASR_DEVICE", "cuda").strip().casefold()
    if device not in SUPPORTED_ASR_DEVICES:
        raise ValueError(
            f"unsupported REELS_ASR_DEVICE={device!r}; "
            f"choose one of {', '.join(SUPPORTED_ASR_DEVICES)}"
        )
    return device


def llm_backend() -> str:
    """Select the inference adapter without changing extraction code."""
    backend = _get("REELS_LLM_BACKEND", "codex").strip().casefold()
    if backend not in SUPPORTED_LLM_BACKENDS:
        raise ValueError(
            f"unsupported REELS_LLM_BACKEND={backend!r}; "
            f"choose one of {', '.join(SUPPORTED_LLM_BACKENDS)}"
        )
    return backend


def hermes_executable() -> str:
    return _get("REELS_HERMES_EXECUTABLE", "hermes").strip() or "hermes"


def hermes_reasoning() -> str:
    return _get("REELS_HERMES_REASONING", "medium").strip() or "medium"


def hermes_provider() -> str | None:
    value = _get("REELS_HERMES_PROVIDER", "").strip()
    return value or None


def api_base_url() -> str:
    return _get("REELS_API_BASE_URL", "https://api.openai.com/v1").rstrip("/")


def api_key() -> str:
    return _get("REELS_API_KEY", "").strip()


def api_timeout() -> int:
    return _get_int("REELS_API_TIMEOUT", 1200)


def codex_reasoning() -> str:
    return _get("REELS_CODEX_REASONING", "low").strip() or "low"


def codex_workdir() -> str:
    return _get("REELS_CODEX_WORKDIR", "/tmp").strip() or "/tmp"


# ---------------------------------------------------------------- thresholds


def think() -> bool:
    """The model's reasoning mode. Off by default: the original setting
    (`think=False`) was chosen because qwen3 mixed its reasoning into the JSON.
    Ollama has since separated reasoning into its own field, and
    `format=<schema>` already constrains the output — so the original reason may
    no longer hold, hence the value of being able to test both.

    Expected cost: reasoning generates many more tokens. Budget a factor of 3 to
    5 on the duration of a run."""
    return _get("REELS_THINK", "0").lower() in ("1", "true", "yes", "oui")


def num_predict() -> int:
    """Ceiling on GENERATED tokens. Distinct from num_ctx, which bounds the
    input: without this, nothing bounds the output.

    That is not theoretical. Ollama runs with `--context-shift`, so the window
    slides instead of hitting a wall: a model that starts looping generates
    forever. Observed with deepseek-r1:7b, a reasoning model — a single request
    ran for 19 minutes at ~55 tokens/s, on the order of 60,000 reasoning tokens
    for one reel, card at 99% and 75 degrees. A smaller version of the same
    phenomenon produced a 253-second qwen3:8b request in thinking mode.

    Value measured over 950 real generations: median 422 tokens, p90 1150,
    p99 2560. At 4000 there is 56% headroom above p99 and only one generation in
    950 gets truncated — the leak drops from 61440 to 4000 tokens.

    That measurement aged badly, and 4000 was raised to 12000 because of it: it
    dated from the easyocr era, which rendered Chinese and Japanese as latin mush.
    RapidOCR makes them readable, so the model has more to say. Reel DZkXscZRD4j
    (a Taipei itinerary, CJK names) truncated on the very first run at 4000 — one
    reel in 60, not one in 950 — and needed 12000 to get through. A ceiling that
    cuts legitimate generations short is worse than no measurement at all here:
    the missing entities look like a prompt failure when judging the output, which
    is precisely the confound this project keeps tripping over.

    12000 still bounds a runaway at about 3.6 minutes at 55 tokens/s, against 19
    minutes unbounded. That is what the ceiling is for; being "just above p99" was
    never the point.

    A truncated generation yields incomplete JSON, hence a Pydantic validation
    error: the reel is marked failed and picked up again on the next run. A clean,
    resumable failure beats a card running hot for 19 minutes.

    NOT COVERED BY prompt_fingerprint(), and it should be understood: this value
    changes the output without changing the fingerprint, so raising it does not
    make the corpus stale. `extraction.code_sha` is what records the change."""
    return _get_int("REELS_NUM_PREDICT", 12000)


def structured() -> bool:
    """Constrain the output with the JSON schema (default), or let the model run
    free and extract the JSON afterwards.

    Constrained mode guarantees parsable JSON, but it forces generation token by
    token inside the schema's grammar: the model fills the fields in the imposed
    order, with no chance to deliberate over the whole before answering. Free mode
    gives that latitude back, at the risk of malformed output (cf.
    llm.extract_json).

    MEASURED, and the answer is no. `v8+libre` against `v8`, 60 reels, same model,
    same OCR:

    - quality: no gain. Cities 39/36 over the 57 common reels, sign test 6 to 2,
      and above all tuning folds 1 and 2 at -1 and -1. The +5 on fold 0 is a
      single-fold fluctuation the tuning folds do not confirm — commenting on it
      would be exactly the overfitting the fold split exists to prevent.
    - cost: 3 reels out of 60 lost to malformed output, and NOT just any three —
      the three richest in the corpus (Db_Y5A2KZcY 26 entities, DZkXscZRD4j 10,
      DcBIpd9qhD4 8), so 44 entities, 33% of the total. Free mode fails where the
      task is longest, which is where it pays most.

    Keep constrained mode. The flag stays so the measurement can be redone if the
    model changes.

    Worth knowing before retesting: free mode is not just a flag. The schema
    reached the model ONLY through `format=`; without it the prompt ended with
    "Reply with the JSON required by the schema" while referring to a schema the
    model had never read, and all 60 reels failed validation. See llm.chat_json,
    which re-injects the schema into the prompt in that mode."""
    return _get("REELS_STRUCTURED", "1").lower() not in ("0", "false", "no", "non")


def num_ctx() -> int:
    """Measured over the 1347 reels: median context 187 tokens, p90 505, maximum
    3459 — none exceeds 4096. With the system prompt (~700 tokens) the worst case
    fits in 4200. 6144 leaves ~45% headroom while staying well clear of Ollama's
    4096 default, which would truncate the prompt without raising anything."""
    return _get_int("REELS_NUM_CTX", 6144)


def ocr_max_frames() -> int:
    """OCR cost is concentrated, not spread out: reels above 15 frames account for
    66% of the work, and the 26-40 band yields no entity that OCR alone attests.
    Conversely, the 9 reels that do carry such entities average 11.9 frames.
    Capping at 25 saves 14% without touching a single carrying reel."""
    return _get_int("REELS_OCR_MAX_FRAMES", 25)


def ocr_min_frames() -> int:
    """Floor: a short video stays properly covered."""
    return _get_int("REELS_OCR_MIN_FRAMES", 8)


def ocr_fps() -> float:
    """Reference rate for an 'average' video (~20-30 s)."""
    return _get_float("REELS_OCR_FPS", 0.5)


# -------------------------------------------------------------- video archive


def archive_backend() -> str:
    """Selected archive API; remote storage stays disabled unless opted in."""
    value = _get("REELS_ARCHIVE_BACKEND", "disabled").lower()
    if value not in {"disabled", "s3"}:
        raise ValueError("REELS_ARCHIVE_BACKEND must be 'disabled' or 's3'")
    return value


def archive_bucket() -> str:
    return _get("REELS_ARCHIVE_BUCKET", "")


def archive_endpoint() -> str:
    """Optional custom S3 endpoint for a compatible service; no credential read."""
    return _get("REELS_ARCHIVE_ENDPOINT", "")


def archive_region() -> str:
    """Optional region; an empty value defers to the AWS SDK/profile defaults."""
    return _get("REELS_ARCHIVE_REGION", "")


def archive_profile() -> str:
    """Optional AWS shared-config profile name, never an access key or secret."""
    return _get("REELS_ARCHIVE_PROFILE", "")


def archive_prefix() -> str:
    return _get("REELS_ARCHIVE_PREFIX", "reels-originals").strip("/")
