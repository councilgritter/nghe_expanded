"""Runtime word resolution — the tap-time path.

The reader's long-press is served from the stored token map with **no** API call.
This module is the exception path, and it is deliberately narrow:

* a token with a stored definition resolves locally;
* a token with no definition, or an offset that falls in no token at all, sends
  **one sentence** of context to DeepSeek and caches the answer;
* the same context is never paid for twice.

It is written as a library function rather than a route so it can be unit-tested
without a server, and so the optional API layer (phase 2) is a thin wrapper over
it rather than a second implementation.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from reading.pipeline.deepseek import Simplifier
from reading.pipeline.tokens import sentence_around
from reading.storage import db

SOURCE_STORED = "stored"
SOURCE_CACHE = "cache"
SOURCE_LLM = "llm"
SOURCE_NONE = "none"


@dataclass
class Resolution:
    """What the reader should see after tapping ``offset``."""

    surface: str
    form: str
    definition: str
    cefr: str | None
    source: str
    token_start: int | None = None
    token_end: int | None = None
    ambiguous: bool = False
    candidates: list[str] | None = None


def context_key(sentence: str, selection: str) -> str:
    """Stable cache key for one (context, selection) pair."""
    payload = f"{sentence}\x1f{selection}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def resolve_offset(
    conn,
    version_id: int,
    offset: int,
    simplified_text: str,
    client: Simplifier | None = None,
    cefr_level: str | None = None,
    allow_llm: bool = True,
) -> Resolution:
    """Resolve a tapped character offset.

    ``allow_llm=False`` makes this purely local: stored definitions and the cache
    only.  The frontend calls that mode first and only escalates when it comes back
    empty, which is what keeps per-tap cost at zero for the overwhelming majority
    of taps.
    """
    token = db.find_token_at(conn, version_id, offset)

    if token is not None and token["definition"]:
        return Resolution(
            surface=token["surface"],
            form=token["form"],
            definition=token["definition"],
            cefr=token["cefr_level"],
            source=SOURCE_STORED,
            token_start=token["start_char"],
            token_end=token["end_char"],
            ambiguous=bool(token["ambiguous"]),
            candidates=_candidate_forms(token["candidates"]),
        )

    # No stored definition: the word is either known-but-undefined or not in the
    # map at all.  Both need one sentence of context.
    if token is not None:
        selection = token["surface"]
        start, end = token["start_char"], token["end_char"]
        ambiguous = bool(token["ambiguous"])
        candidates = _candidate_forms(token["candidates"])
    else:
        selection = _word_at(simplified_text, offset)
        start = end = None
        ambiguous = False
        candidates = None

    if not selection:
        return Resolution(
            surface="", form="", definition="", cefr=None, source=SOURCE_NONE
        )

    sentence = sentence_around(simplified_text, offset)
    key = context_key(sentence, selection)

    cached = db.get_cached_lookup(conn, key)
    if cached is not None:
        return Resolution(
            surface=selection,
            form=cached["form"],
            definition=cached["definition"],
            cefr=cached["cefr_level"],
            source=SOURCE_CACHE,
            token_start=start,
            token_end=end,
            ambiguous=ambiguous,
            candidates=candidates,
        )

    if not allow_llm or client is None:
        return Resolution(
            surface=selection,
            form=selection.casefold().replace(" ", "_"),
            definition="",
            cefr=None,
            source=SOURCE_NONE,
            token_start=start,
            token_end=end,
            ambiguous=ambiguous,
            candidates=candidates,
        )

    # The one paid path: a single sentence, then cached.
    result = client.define(sentence, selection, cefr_level)
    db.put_cached_lookup(
        conn,
        context_key=key,
        sentence=sentence,
        selection=selection,
        form=result.form,
        definition=result.definition,
        cefr_level=result.cefr or None,
    )
    return Resolution(
        surface=selection,
        form=result.form,
        definition=result.definition,
        cefr=result.cefr or None,
        source=SOURCE_LLM,
        token_start=start,
        token_end=end,
        ambiguous=ambiguous,
        candidates=candidates,
    )


def _word_at(text: str, offset: int) -> str:
    """The whitespace-delimited chunk under ``offset``.

    Used only when the token map has a hole — the segmentation missed this
    position — so a rough "which word did you touch" is all that is needed; the
    model is told to correct the span from context.
    """
    if not text:
        return ""
    offset = max(0, min(offset, len(text) - 1))
    if text[offset].isspace():
        return ""
    start = offset
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    end = offset
    while end < len(text) and not text[end].isspace():
        end += 1
    return text[start:end].strip()


def _candidate_forms(raw) -> list[str] | None:
    """Render stored JSON candidates as plain underscore forms."""
    if not raw:
        return None
    import json

    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, list):
        return None
    forms = [str(c.get("form")) for c in parsed if isinstance(c, dict) and c.get("form")]
    return forms or None
