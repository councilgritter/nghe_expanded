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


@dataclass
class QuizItem:
    """One comprehension question: multiple choice, or open with a model answer."""

    question: str
    options: list[str] = field(default_factory=list)
    # 0-based index into ``options``; -1 when the model gave no usable answer.
    answer: int = -1
    why: str = ""
    sample: str = ""
    key_points: list[str] = field(default_factory=list)


@dataclass
class WritingTask:
    prompt: str = ""
    key_points: list[str] = field(default_factory=list)
    model_answer: str = ""
    min_words: int = 0


@dataclass
class Exercises:
    """The practice content for one (article, level): questions plus a writing task."""

    questions: list[QuizItem] = field(default_factory=list)
    short_answers: list[QuizItem] = field(default_factory=list)
    writing: WritingTask = field(default_factory=WritingTask)

    @property
    def count(self) -> int:
        """How many items there are, writing task included.  Zero means "no content"."""
        return (
            len(self.questions)
            + len(self.short_answers)
            + (1 if self.writing.prompt else 0)
        )


class Simplifier(Protocol):
    """What the pipeline needs from an LLM.  Tests implement this."""

    def simplify(self, article_text: str, cefr_level: str) -> SimplifyResult: ...

    def define(
        self, sentence: str, selection: str, cefr_level: str | None = ...
    ) -> DefinitionResult: ...

    def exercises(self, simplified_text: str, cefr_level: str) -> Exercises: ...


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


def _string_list(raw: Any) -> list[str]:
    """Coerce a model field into a list of non-empty strings."""
    if isinstance(raw, str):
        raw = [part for part in re.split(r"[;\n]", raw)]
    if not isinstance(raw, list):
        return []
    return [str(item).strip() for item in raw if str(item or "").strip()]


def _quiz_items(raw: Any, *, with_options: bool) -> list[QuizItem]:
    """Coerce the model's question arrays into :class:`QuizItem` objects.

    Tolerant on purpose: a question with fewer than two options, or an ``answer``
    that indexes nothing, is dropped rather than stored, because the reader would
    otherwise show a question that cannot be answered correctly.
    """
    if not isinstance(raw, list):
        return []
    out: list[QuizItem] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        question = str(entry.get("q") or entry.get("question") or "").strip()
        if not question:
            continue
        options = _string_list(entry.get("options"))
        if with_options:
            if len(options) < 2:
                continue
            try:
                answer = int(entry.get("answer", -1))
            except (TypeError, ValueError):
                continue
            if not 0 <= answer < len(options):
                continue
        else:
            options, answer = [], -1
        out.append(
            QuizItem(
                question=question,
                options=options,
                answer=answer,
                why=str(entry.get("why") or "").strip(),
                sample=str(entry.get("sample") or entry.get("model_answer") or "").strip(),
                key_points=_string_list(entry.get("key_points")),
            )
        )
    return out


def _writing(raw: Any) -> WritingTask:
    if not isinstance(raw, dict):
        return WritingTask()
    prompt = str(raw.get("prompt") or raw.get("q") or "").strip()
    try:
        min_words = int(raw.get("min_words") or 0)
    except (TypeError, ValueError):
        min_words = 0
    return WritingTask(
        prompt=prompt,
        key_points=_string_list(raw.get("key_points")),
        model_answer=str(raw.get("model_answer") or raw.get("sample") or "").strip(),
        min_words=max(0, min_words),
    )


def _read_stream(response) -> tuple[str, dict]:
    """Accumulate a streamed chat completion into ``(content, usage)``.

    Reasoning arrives as ``delta.reasoning_content``, separately from the answer in
    ``delta.content``, and is deliberately dropped here: it is billed as completion
    tokens but it is not part of the article.  Usage rides on the trailing chunks, so
    the cost is still reported exactly.

    Split out from the HTTP call so it can be tested against a canned stream.
    """
    parts: list[str] = []
    usage: dict = {}
    for line in response.iter_lines():
        if not line or not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            event = json.loads(payload)
        except ValueError:
            continue  # keep-alive or a partial frame; the next one carries on
        if isinstance(event, dict) and event.get("usage"):
            usage = event["usage"]
        for choice in (event.get("choices") or []) if isinstance(event, dict) else []:
            if not isinstance(choice, dict):
                continue
            piece = (choice.get("delta") or {}).get("content")
            if piece:
                parts.append(piece)
    return "".join(parts), usage


class DeepSeekClient:
    """HTTP client for the DeepSeek chat-completions API."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or default_settings

    # -- internals ----------------------------------------------------------

    def _post(self, system_prompt: str, user_message: str) -> tuple[dict, dict]:
        """Send one chat completion and return (parsed JSON, usage).

        The system prompt is the stable prefix; the user message carries all
        variation.  That ordering is deliberate — see the module docstring.

        The response is **streamed**.  The configured model reasons before it
        answers, so a single call can run to tens of thousands of tokens; a
        non-streamed request leaves the socket silent for minutes and the server
        drops it mid-generation ("incomplete chunked read"), which is what happened
        to the longest articles.  Streaming keeps data flowing and removes the
        failure without changing the result.
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
            "stream": True,
        }
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        last_error: Exception | None = None
        for attempt in range(max(1, self.settings.deepseek_max_retries)):
            try:
                with httpx.Client(timeout=self.settings.deepseek_timeout_s) as client:
                    with client.stream("POST", url, json=payload, headers=headers) as response:
                        if response.status_code >= 400:
                            response.read()  # the body must be drained before .text
                            raise DeepSeekError(
                                f"DeepSeek returned {response.status_code}: "
                                f"{response.text[:200]}"
                            )
                        content, usage = _read_stream(response)
                if not content.strip():
                    raise DeepSeekError("DeepSeek streamed an empty completion")
                return parse_json_object(content), usage
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

    def exercises(self, simplified_text: str, cefr_level: str) -> Exercises:
        """The comprehension questions and writing task for one simplified article.

        A second call rather than more output from :meth:`simplify`: the article is
        already paid for and stored, so exercises can be added to existing articles —
        or rewritten on their own — without re-simplifying anything.
        """
        level = (cefr_level or self.settings.default_cefr).upper()
        if level not in prompts.CEFR_LEVELS:
            raise ValueError(f"unknown CEFR level {cefr_level!r}")
        data, _ = self._post(
            prompts.EXERCISES_SYSTEM_PROMPT,
            prompts.exercises_user_message(simplified_text, level),
        )
        return Exercises(
            questions=_quiz_items(data.get("questions"), with_options=True),
            short_answers=_quiz_items(data.get("short_answers"), with_options=False),
            writing=_writing(data.get("writing")),
        )
