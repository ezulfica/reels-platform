"""The single call site for the local LLM.

`extract.py` and `verify.py` used to duplicate the same `client.chat(...)` call
with the same three precautions, all measured on this machine:

- `num_ctx` passed explicitly, never Ollama's 4096 default, which truncates the
  prompt without raising;
- `think` off by default, otherwise the model polluted the JSON with its
  reasoning (cf. config.think(), which allows retesting);
- `format=<Pydantic schema>`, which turns malformed JSON from an error case to
  catch into a structural impossibility.

Grouping them here means a fix to one cannot forget the other. Each backend
implements the same small chat seam: Codex, Hermes, a dedicated API endpoint,
or a local Ollama model.
"""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import requests

from config import settings as config

_USAGE = {
    "requests": 0,
    "prompt_eval_count": 0,
    "eval_count": 0,
    "total_duration_ns": 0,
    "eval_duration_ns": 0,
    "output_chars": 0,
    "input_chars": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "reasoning_output_tokens": 0,
    "cached_input_tokens": 0,
}


def reset_usage() -> None:
    for key in _USAGE:
        _USAGE[key] = 0


def usage() -> dict[str, int | None]:
    """Return usage accumulated by the current staged extraction.

    Ollama supplies exact token counters. Hermes supplies no counters through
    its CLI, so those fields stay zero while request count and output size remain
    available; callers must not label those estimates as exact tokens.
    """
    return dict(_USAGE)


model = config.model


class HermesClient:
    """Small JSON-compatible adapter around the installed Hermes CLI.

    Hermes has no Ollama ``format=<schema>`` equivalent in this invocation
    mode, so the shared caller adds the schema as prompt text and validates the
    returned object with Pydantic. It is intentionally an opt-in backend.
    """

    supports_structured = False

    def generate(self, *, model: str, prompt: str, keep_alive: int = 0) -> None:
        """Match Ollama's unload seam; Hermes owns its own process lifecycle."""
        return

    def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        think: bool,
        options: dict[str, object],
        **_: object,
    ):
        system = next((m["content"] for m in messages if m["role"] == "system"), "")
        user = next((m["content"] for m in messages if m["role"] == "user"), "")
        prompt = f"{system}\n\nUSER DOSSIER:\n{user}"
        command = [
            config.hermes_executable(),
            "chat",
            "-q",
            prompt,
            "-Q",
            "--reasoning",
            config.hermes_reasoning(),
        ]
        if model:
            command.extend(["--model", model])
        provider = config.hermes_provider()
        if provider:
            command.extend(["--provider", provider])
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=900, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError(f"Hermes inference failed: {error}") from error
        if result.returncode:
            detail = (result.stderr or result.stdout or "unknown Hermes error").strip()
            raise RuntimeError(f"Hermes inference failed: {detail[:500]}")
        return SimpleNamespace(message=SimpleNamespace(content=result.stdout.strip()))


class CodexClient:
    """Use the signed-in Codex CLI as an opt-in structured inference backend.

    This consumes the user's ChatGPT/Codex allowance, not an API key. Rules and
    repository context are disabled for the subprocess: only the bounded dossier
    supplied by reels-platform is sent to the agent.
    """

    supports_structured = False

    def generate(self, *, model: str, prompt: str, keep_alive: int = 0) -> None:
        """Match Ollama's unload seam; Codex subprocesses are already ephemeral."""
        return

    def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        think: bool,
        options: dict[str, object],
        **_: object,
    ):
        prompt = "\n\n".join(m["content"] for m in messages)
        command = [
            "codex",
            "exec",
            "--ephemeral",
            "--json",
            "--sandbox",
            "read-only",
            "--ignore-rules",
            "--skip-git-repo-check",
            "-C",
            config.codex_workdir(),
            "-m",
            model or "gpt-5.6-luna",
            "-c",
            f'model_reasoning_effort="{config.codex_reasoning()}"',
            prompt,
        ]
        try:
            result = subprocess.run(
                command, capture_output=True, text=True, timeout=1200, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise RuntimeError(f"Codex inference failed: {error}") from error
        if result.returncode:
            detail = (result.stderr or result.stdout or "unknown Codex error").strip()
            raise RuntimeError(f"Codex inference failed: {detail[:500]}")
        text = ""
        usage_payload: dict[str, int] = {}
        for line in result.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            item = event.get("item", {})
            if item.get("type") == "agent_message":
                text = str(item.get("text", ""))
            if event.get("type") == "turn.completed":
                usage_payload = event.get("usage", {}) or {}
        if not text:
            raise RuntimeError("Codex inference returned no agent message")
        return SimpleNamespace(message=SimpleNamespace(content=text), **usage_payload)


class ApiClient:
    """Adapter for an OpenAI-compatible Chat Completions endpoint.

    The endpoint is deliberately configured by URL and environment variable, so
    using a dedicated provider does not add a provider SDK or credentials to the
    project. The response is reduced to the same small shape as the other
    adapters.
    """

    supports_structured = False

    def generate(self, *, model: str, prompt: str, keep_alive: int = 0) -> None:
        return None

    def chat(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        think: bool,
        options: dict[str, object],
        **_: object,
    ):
        headers = {"Content-Type": "application/json"}
        if config.api_key():
            headers["Authorization"] = f"Bearer {config.api_key()}"
        body = {
            "model": model,
            "messages": messages,
            "temperature": options.get("temperature", 0.2),
            "max_tokens": options.get("num_predict", 12000),
        }
        try:
            response = requests.post(
                config.api_base_url() + "/chat/completions",
                headers=headers,
                json=body,
                timeout=config.api_timeout(),
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as error:
            raise RuntimeError(f"API inference failed: {error}") from error
        try:
            message = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise RuntimeError("API inference returned no assistant message") from error
        usage_payload = payload.get("usage") or {}
        return SimpleNamespace(
            message=SimpleNamespace(content=str(message)),
            prompt_eval_count=usage_payload.get("prompt_tokens"),
            eval_count=usage_payload.get("completion_tokens"),
            input_tokens=usage_payload.get("prompt_tokens"),
            output_tokens=usage_payload.get("completion_tokens"),
        )


def client():
    backend = config.llm_backend()
    if backend == "codex":
        return CodexClient()
    if backend == "hermes":
        return HermesClient()
    if backend == "api":
        return ApiClient()
    import ollama

    return ollama.Client()


def generation_settings(*, temperature: float) -> dict[str, object]:
    """The effective knobs that can change a completion.

    They are persisted with an extraction attempt.  A model name alone is not
    enough to reproduce a run when an environment override changed its context
    budget, output ceiling or reasoning/structured mode.
    """
    return {
        "temperature": temperature,
        "num_ctx": config.num_ctx(),
        "num_predict": config.num_predict(),
        "think": config.think(),
        "structured": config.structured(),
    }


def extract_json(text: str) -> str:
    r"""Isolate the first complete JSON object from a free-text response.

    Salvaged from the old vision.py, where it was written after a precise
    failure: a greedy `\{.*\}` regex captures up to the LAST brace in the text, so
    as soon as the model adds a comment after its JSON, `json.loads` fails with
    "Extra data" — 13 reels out of 38 on the first full run. We scan balanced
    braces to delimit exactly the first object.

    Only used in unconstrained mode (config.structured() == False): with
    `format=<schema>`, malformed output is structurally impossible.
    """
    start = text.find("{")
    if start == -1:
        raise ValueError("no JSON object in the response")
    depth = 0
    in_string = False
    escaped = False
    for i, ch in enumerate(text[start:], start):
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    raise ValueError("unterminated JSON object in the response")


def chat_json(
    cli,
    model_name: str,
    system: str,
    user: str,
    schema: dict,
    *,
    temperature: float = 0.2,
) -> str:
    """Return the raw (JSON) response, for the caller to validate with its own
    Pydantic model — the caller is the one that knows which schema it asked for.

    Two regimes, per config.structured():

    - constrained (default): `format=<schema>` imposes the grammar token by token.
      Malformed JSON becomes structurally impossible.
    - free: no constraint, we isolate the JSON from the response afterwards.
      Riskier, but the constrained grammar forces the model to fill the schema
      character by character, which may stop it reasoning about the content before
      answering. To be tested rather than assumed — exactly the kind of trade-off
      `reels compare` can settle.
    """
    settings = generation_settings(temperature=temperature)
    structured = settings["structured"] and getattr(cli, "supports_structured", True)
    kwargs = {"format": schema} if structured else {}
    # In free mode the schema has to travel through the PROMPT: `format=` was the
    # only place the model ever saw it. Removing that without handing it back here
    # makes free mode structurally untestable — the prompt ends with "Reply with
    # the JSON required by the schema" while referring to a schema the model has
    # never read. Measured: 60 reels out of 60 failing validation, the model
    # inventing a plausible shape (entities with no `name`, `topic`/`tags`
    # missing). That is what made the earlier run "crash".
    #
    # Added here and deliberately not in SYSTEM_PROMPT: the system prompt feeds
    # prompt_fingerprint(), so putting it there would force a version bump and
    # make constrained mode no longer comparable with itself. Here, constrained
    # mode stays byte for byte what it was.
    #
    # ACKNOWLEDGED CONFOUND, and an unavoidable one: `v8+libre` differs from `v8`
    # by TWO things, the absence of a decoding constraint and the presence of the
    # schema as text in the prompt. They cannot be separated — without the schema,
    # free mode produces nothing usable at all.
    if not structured:
        system += (
            "\n\nThe JSON object must match this schema exactly — same keys, "
            "same nesting, no extra keys:\n"
            + json.dumps(schema, ensure_ascii=False, sort_keys=True)
        )
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    response = cli.chat(
        model=model_name,
        messages=messages,
        think=settings["think"],
        options={
            "num_ctx": settings["num_ctx"],
            "temperature": temperature,
            # bounds the OUTPUT: num_ctx only bounds the input, and
            # `--context-shift` on the Ollama side slides the window instead
            # of hitting a wall. Without this ceiling, a looping model
            # generates endlessly (cf. config.num_predict).
            "num_predict": settings["num_predict"],
        },
        **kwargs,
    )
    content = response.message.content
    _USAGE["requests"] += 1
    _USAGE["input_chars"] += sum(len(m.get("content", "")) for m in messages)
    _USAGE["output_chars"] += len(content or "")
    for field in (
        "prompt_eval_count",
        "eval_count",
        "total_duration",
        "eval_duration",
        "input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "cached_input_tokens",
    ):
        value = getattr(response, field, None)
        if isinstance(value, int):
            key = {
                "total_duration": "total_duration_ns",
                "eval_duration": "eval_duration_ns",
            }.get(field, field)
            _USAGE[key] += value
    return content if structured else extract_json(content)


def chat_text(
    cli, model_name: str, system: str, user: str, *, temperature: float = 0.2
) -> str:
    """Return a bounded plain-text answer for the read-only web assistant."""
    settings = generation_settings(temperature=temperature)
    response = cli.chat(
        model=model_name,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        think=settings["think"],
        options={
            "num_ctx": settings["num_ctx"],
            "temperature": temperature,
            "num_predict": 1200,
        },
    )
    content = (response.message.content or "").strip()
    if not content:
        raise RuntimeError("le modèle n'a pas renvoyé de réponse")
    _USAGE["requests"] += 1
    _USAGE["input_chars"] += len(system) + len(user)
    _USAGE["output_chars"] += len(content)
    for field, key in (
        ("input_tokens", "input_tokens"),
        ("output_tokens", "output_tokens"),
        ("prompt_eval_count", "prompt_eval_count"),
        ("eval_count", "eval_count"),
    ):
        value = getattr(response, field, None)
        if isinstance(value, int):
            _USAGE[key] += value
    return content


def unload(cli, model_name: str) -> None:
    """Release an optional local Ollama model after a batch."""
    cli.generate(model=model_name, prompt="", keep_alive=0)
