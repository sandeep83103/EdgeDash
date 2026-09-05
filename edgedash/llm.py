"""LLM gateway — the ONLY module that imports an LLM SDK.

Public API:
    complete_json(prompt, schema, *, max_retries=1) -> dict

Provider is selected from config (llm_provider / llm_model).
Adding a new provider: implement _Provider, add to _PROVIDERS dict.
No other file may import google.generativeai or any LLM library.

CLI check:
    python -m edgedash.llm --check
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import deque
from typing import Any, Protocol

from edgedash.config import Config, load_config

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

class LLMError(Exception):
    """Raised when the model fails after all retries or config is invalid."""


# ---------------------------------------------------------------------------
# Rate limiter  (rule 15: min 1 s between calls, max 15 calls per minute)
# ---------------------------------------------------------------------------

class _RateLimiter:
    def __init__(self, min_gap_s: float = 1.0, max_per_minute: int = 15) -> None:
        self._min_gap = min_gap_s
        self._max_per_minute = max_per_minute
        self._call_times: deque[float] = deque()
        self._last_call: float = 0.0

    def wait(self) -> None:
        now = time.monotonic()

        # Enforce minimum gap between consecutive calls.
        gap = now - self._last_call
        if gap < self._min_gap:
            time.sleep(self._min_gap - gap)
            now = time.monotonic()

        # Enforce rolling 60-second window cap.
        cutoff = now - 60.0
        while self._call_times and self._call_times[0] < cutoff:
            self._call_times.popleft()

        if len(self._call_times) >= self._max_per_minute:
            sleep_until = self._call_times[0] + 60.0
            time.sleep(max(0.0, sleep_until - now))
            now = time.monotonic()

        self._call_times.append(now)
        self._last_call = now


_limiter = _RateLimiter()


# ---------------------------------------------------------------------------
# Provider protocol + implementations
# ---------------------------------------------------------------------------

class _Provider(Protocol):
    def call(self, prompt: str) -> str: ...


class _GeminiProvider:
    def __init__(self, model: str, api_key: str) -> None:
        import google.generativeai as genai  # only import site: here
        genai.configure(api_key=api_key)
        self._client = genai.GenerativeModel(model)

    def call(self, prompt: str) -> str:
        import google.generativeai.types as gtypes
        _MAX_BACKOFF_ATTEMPTS = 3
        last_exc: Exception | None = None
        for attempt in range(_MAX_BACKOFF_ATTEMPTS):
            try:
                response = self._client.generate_content(prompt)
                return response.text
            except Exception as exc:
                msg = str(exc).lower()
                if "429" in msg or "quota" in msg or "resource" in msg:
                    time.sleep(2.0 ** (attempt + 1))
                    last_exc = exc
                else:
                    raise LLMError(f"Gemini error: {exc}") from exc
        raise LLMError(
            f"Gemini quota/rate error after {_MAX_BACKOFF_ATTEMPTS} attempts: {last_exc}"
        )


class _OllamaProvider:
    def __init__(self, model: str) -> None:
        self._model = model
        self._url = "http://localhost:11434/api/generate"

    def call(self, prompt: str) -> str:
        import requests  # already a project dependency
        try:
            resp = requests.post(
                self._url,
                json={"model": self._model, "prompt": prompt, "stream": False},
                timeout=120,
            )
            resp.raise_for_status()
            return resp.json()["response"]
        except Exception as exc:
            raise LLMError(f"Ollama error: {exc}") from exc


def _build_provider(config: Config) -> _Provider:
    provider = config.llm_provider.lower()
    model = config.llm_model

    if provider == "gemini":
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not api_key:
            raise LLMError(
                "GEMINI_API_KEY is not set. "
                "Add it to your .env file (see .env.example) or export it in your shell."
            )
        return _GeminiProvider(model, api_key)

    if provider == "ollama":
        return _OllamaProvider(model)

    raise LLMError(
        f"Unknown llm_provider {provider!r}. "
        "Supported values: 'gemini', 'ollama'. Check config.yaml."
    )


# ---------------------------------------------------------------------------
# JSON cleaning + validation
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def _extract_json(text: str) -> str:
    """Strip markdown fences and leading/trailing prose; return raw JSON."""
    fence_match = _FENCE_RE.search(text)
    if fence_match:
        return fence_match.group(1).strip()
    # No fence — find the first { or [ and take everything from there.
    for start_char, end_char in (("{", "}"), ("[", "]")):
        start = text.find(start_char)
        end = text.rfind(end_char)
        if start != -1 and end != -1 and end > start:
            return text[start : end + 1]
    return text.strip()


def _validate(data: dict, schema: dict) -> list[str]:
    """Return a list of validation error messages, empty if valid.

    Supports: required (list), properties with 'type' hints.
    Intentionally simple — avoids jsonschema dependency.
    """
    errors: list[str] = []
    required = schema.get("required", [])
    properties = schema.get("properties", {})

    for key in required:
        if key not in data:
            errors.append(f"missing required field '{key}'")

    _TYPE_MAP: dict[str, type] = {
        "string": str, "number": (int, float),
        "integer": int, "boolean": bool,
        "array": list, "object": dict,
    }
    for key, prop in properties.items():
        if key not in data:
            continue
        expected = prop.get("type")
        if expected and expected in _TYPE_MAP:
            if not isinstance(data[key], _TYPE_MAP[expected]):  # type: ignore[arg-type]
                errors.append(
                    f"field '{key}' should be {expected}, "
                    f"got {type(data[key]).__name__}"
                )
    return errors


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def complete_json(
    prompt: str,
    schema: dict,
    config: Config | None = None,
    *,
    max_retries: int = 1,
) -> dict:
    """Send prompt to the configured LLM; parse and validate JSON response.

    Retries once on parse/validation failure with the error appended.
    Raises LLMError if the response is still invalid after all retries.
    """
    cfg = config or load_config()
    provider = _build_provider(cfg)

    last_error: str = ""
    active_prompt = prompt

    for attempt in range(max_retries + 1):
        if attempt > 0:
            active_prompt = (
                f"{prompt}\n\n"
                f"Your previous response failed validation: {last_error}\n"
                "Reply with ONLY a valid JSON object. "
                "No prose, no markdown fences, no explanation."
            )

        _limiter.wait()
        raw = provider.call(active_prompt)

        try:
            cleaned = _extract_json(raw)
            data = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            last_error = f"JSON parse error: {exc}"
            continue

        if not isinstance(data, dict):
            last_error = f"expected a JSON object, got {type(data).__name__}"
            continue

        errors = _validate(data, schema)
        if errors:
            last_error = "; ".join(errors)
            continue

        return data

    raise LLMError(
        f"Model response failed validation after {max_retries + 1} attempt(s): "
        f"{last_error}"
    )


# ---------------------------------------------------------------------------
# CLI check:  python -m edgedash.llm --check
# ---------------------------------------------------------------------------

def _cli_check() -> None:
    cfg = load_config()
    print(f"  provider : {cfg.llm_provider}")
    print(f"  model    : {cfg.llm_model}")
    print("  sending test prompt …")

    schema = {
        "required": ["ok"],
        "properties": {"ok": {"type": "boolean"}},
    }
    prompt = 'Reply with exactly: {"ok": true}'

    try:
        result = complete_json(prompt, schema, cfg)
        if result.get("ok") is True:
            print("  result   : OK ✓")
        else:
            print(f"  result   : unexpected value — {result}")
    except LLMError as exc:
        print(f"  result   : FAILED — {exc}")


if __name__ == "__main__":
    import sys
    if "--check" in sys.argv:
        _cli_check()
    else:
        print("Usage: python -m edgedash.llm --check")
