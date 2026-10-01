"""DeepSeek client for the reading pipeline.

Two calls exist, and both keep the system prompt fixed and put the variable text
after it, so DeepSeek's context cache can hit on the prefix:

* :meth:`DeepSeekClient.simplify` — batch, at ingest, one call per article/level.
* :meth:`DeepSeekClient.define`   — the *rare* runtime fallback, one sentence.

Both are exposed through the :class:`Simplifier` protocol so the pipeline can be
driven by a stub in tests without monkeypatching HTTP.
"""
from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from reading.pipeline import prompts
from reading.settings import Settings, settings as default_settings

_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class DeepSeekError(RuntimeError):
    """DeepSeek was unreachable or returned something unusable."""


@dataclass
class PreTeachItem:
    term: str
    gloss: str = ""
    cefr: str = ""
    example: str = ""


@dataclass
class SimplifyResult:
    """One article rewritten at one CEFR level."""

    segmented_text: str
    vocab: list[PreTeachItem] = field(default_factory=list)
    grammar: list[PreTeachItem] = field(default_factory=list)
    model: str = ""
    prompt_version: str = prompts.PROMPT_VERSION
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass
class DefinitionResult:
    form: str
    definition: str
    cefr: str = ""


class Simplifier(Protocol):
    """What the pipeline needs from an LLM.  Tests implement this."""

    def simplify(self, article_text: str, cefr_level: str) -> SimplifyResult: ...

    def define(
        self, sentence: str, selection: str, cefr_level: str | None = ...
    ) -> DefinitionResult: ...


def parse_json_object(raw: str) -> dict:
    """Parse a JSON object from a model response, tolerating code fences."""
    text = _FENCE_RE.sub("", raw.strip()).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        # Last resort: grab the outermost braces.
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise DeepSeekError(f"model did not return JSON: {raw[:200]!r}") from exc
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError as inner:
            raise DeepSeekError(f"model returned malformed JSON: {inner}") from inner
    if not isinstance(parsed, dict):
        raise DeepSeekError(f"expected a JSON object, got {type(parsed).__name__}")
    return parsed


def _items(raw: Any) -> list[PreTeachItem]:
    """Coerce the model's pre-teach array into :class:`PreTeachItem` objects."""
    if not isinstance(raw, list):
        return []
    out: list[PreTeachItem] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        term = str(entry.get("term") or entry.get("point") or "").strip()
        if not term:
            continue
        out.append(
            PreTeachItem(
                term=term,
                gloss=str(entry.get("gloss") or entry.get("definition") or "").strip(),
                cefr=str(entry.get("cefr") or entry.get("level") or "").strip().upper(),
                example=str(entry.get("example") or "").strip(),
            )
        )
    return out


class DeepSeekClient:
    """HTTP client for the DeepSeek chat-completions API."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or default_settings

    # -- internals ----------------------------------------------------------

    def _post(self, system_prompt: str, user_message: str) -> tuple[dict, dict]:
        """Send one chat completion and return (parsed JSON, usage).

        The system prompt is the stable prefix; the user message carries all
        variation.  That ordering is deliberate — see the module docstring.
        """
        import httpx

        api_key = self.settings.require_api_key()
        url = f"{self.settings.deepseek_base_url}/chat/completions"
        payload = {
            "model": self.settings.deepseek_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_message},
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
            "stream": False,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        last_error: Exception | None = None
        for attempt in range(max(1, self.settings.deepseek_max_retries)):
            try:
                with httpx.Client(timeout=self.settings.deepseek_timeout_s) as client:
                    response = client.post(url, json=payload, headers=headers)
                if response.status_code in (429, 500, 502, 503, 504):
                    raise DeepSeekError(
                        f"DeepSeek returned {response.status_code}: {response.text[:200]}"
                    )
                response.raise_for_status()
                body = response.json()
                content = body["choices"][0]["message"]["content"]
                return parse_json_object(content), body.get("usage", {}) or {}
            except Exception as exc:  # noqa: BLE001 - retried below, re-raised after
                last_error = exc
                if attempt + 1 >= max(1, self.settings.deepseek_max_retries):
                    break
                # Exponential backoff with jitter; ingest is batch, so waiting is fine.
                time.sleep(min(30.0, (2**attempt) + random.random()))
        raise DeepSeekError(f"DeepSeek request failed after retries: {last_error}")

    # -- the public surface -------------------------------------------------

    def simplify(self, article_text: str, cefr_level: str) -> SimplifyResult:
        level = (cefr_level or self.settings.default_cefr).upper()
        if level not in prompts.CEFR_LEVELS:
            raise ValueError(f"unknown CEFR level {cefr_level!r}")
        data, usage = self._post(
            prompts.SIMPLIFY_SYSTEM_PROMPT,
            prompts.simplify_user_message(article_text, level),
        )
        segmented = data.get("segmented_text") or data.get("text") or ""
        if not str(segmented).strip():
            raise DeepSeekError("model returned empty segmented_text")
        preteach = data.get("preteach") or {}
        if not isinstance(preteach, dict):
            preteach = {}
        return SimplifyResult(
            segmented_text=str(segmented),
            vocab=_items(preteach.get("vocab")),
            grammar=_items(preteach.get("grammar")),
            model=self.settings.deepseek_model,
            usage=usage,
        )

    def define(
        self, sentence: str, selection: str, cefr_level: str | None = None
    ) -> DefinitionResult:
        data, _ = self._post(
            prompts.DEFINITION_SYSTEM_PROMPT,
            prompts.definition_user_message(sentence, selection, cefr_level),
        )
        return DefinitionResult(
            form=str(data.get("form") or selection).strip() or selection,
            definition=str(data.get("definition") or "").strip(),
            cefr=str(data.get("cefr") or "").strip().upper(),
        )
