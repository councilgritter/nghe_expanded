"""Build the token map: the character-offset index the reading page resolves against.

A token map entry is everything the frontend needs for one tappable word, and
nothing else — the page walks ``simplified_text`` and wraps each
``[start_char, end_char)`` range in ``<span data-word-id="N">``.  Storing offsets
rather than pre-rendered HTML keeps the artifact small and lets the client decide
how to wrap.

Definitions are resolved here, in cost order, and the source is recorded so the
provenance is auditable:

1. the pre-teach vocabulary the LLM already returned for this article (free);
2. the local dictionary (free, but the bundled word lists carry no glosses, so in
   practice this yields boundaries rather than definitions);
3. nothing — leaving ``definition`` NULL, which is what the frontend's fallback
   is for.  Ingest never makes a per-word LLM call; that would be one call per
   token, and the whole point of doing segmentation in batch is not to.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from reading.pipeline.reconcile import ReconciledSpan
from reading.pipeline.text import Syllable, UNDERSCORE
from reading.storage.dictionary import CompoundDictionary

SRC_PRET_EACH = "preteach"
SRC_DICTIONARY = "dictionary"
SRC_NONE = "none"


@dataclass(frozen=True)
class Definition:
    """A gloss for one form, plus where it came from.

    ``source`` defaults to empty meaning "unspecified", not to :data:`SRC_NONE`:
    callers that omit it are supplying a pre-teach gloss, and a truthy sentinel here
    would suppress that fallback.
    """

    text: str
    cefr: str | None = None
    source: str = ""


def build_token_map(
    text: str,
    syllables: Sequence[Syllable],
    spans: Sequence[ReconciledSpan],
    definitions: Mapping[str, Definition] | None = None,
    dictionary: CompoundDictionary | None = None,
) -> list[dict]:
    """Turn reconciled spans into storable token rows.

    ``definitions`` is keyed by underscore form; ``text`` must be the exact string
    the offsets refer to.
    """
    definitions = definitions or {}
    tokens: list[dict] = []
    for ordinal, span in enumerate(spans):
        start_char = syllables[span.start].start
        end_char = syllables[span.end - 1].end
        surface = text[start_char:end_char]
        form = UNDERSCORE.join(s.text.casefold() for s in syllables[span.start : span.end])

        definition = _resolve_definition(form, syllables, span, definitions, dictionary)

        tokens.append(
            {
                "ordinal": ordinal,
                "surface": surface,
                "form": form,
                "start_char": start_char,
                "end_char": end_char,
                "syllable_count": span.length,
                "cefr_level": definition.cefr if definition else None,
                "definition": definition.text if definition else None,
                "definition_src": definition.source if definition else SRC_NONE,
                "ambiguous": span.ambiguous,
                "candidates": [c.as_dict() for c in span.candidates] or None,
            }
        )
    return tokens


def _resolve_definition(
    form: str,
    syllables: Sequence[Syllable],
    span: ReconciledSpan,
    definitions: Mapping[str, Definition],
    dictionary: CompoundDictionary | None,
) -> Definition | None:
    """Cheapest reliable gloss for this span, or ``None`` to defer to the UI."""
    for key in _lookup_keys(form, syllables, span):
        found = definitions.get(key)
        if found is not None and found.text:
            return Definition(
                text=found.text, cefr=found.cefr, source=found.source or SRC_PRET_EACH
            )

    if dictionary is not None:
        entry = dictionary.get(form)
        # The bundled word lists have no glosses; if a dictionary is ever enriched,
        # this is where its definition would surface.
        if entry is not None and entry.definition:
            return Definition(text=entry.definition, source=SRC_DICTIONARY)

    return None


def _lookup_keys(
    form: str, syllables: Sequence[Syllable], span: ReconciledSpan
) -> Iterable[str]:
    """Forms to try, most specific first.

    The LLM writes ``term`` in underscore form, which usually matches ``form``
    exactly, but it may use the un-accented or differently-cased spelling, so the
    span's raw text is tried too.
    """
    yield form
    raw = UNDERSCORE.join(s.text for s in syllables[span.start : span.end])
    if raw != form:
        yield raw
    if span.length == 1:
        yield syllables[span.start].text


def sentence_around(text: str, offset: int) -> str:
    """The sentence containing ``offset``.

    This is the entire payload of the runtime fallback: one sentence, not the
    article, so a mis-tap costs one short call at most (and usually zero, since
    the result is cached).
    """
    if not text:
        return ""
    offset = max(0, min(offset, len(text) - 1))
    terminators = ".!?…\n"
    start = 0
    for i in range(offset - 1, -1, -1):
        if text[i] in terminators:
            start = i + 1
            break
    end = len(text)
    for i in range(offset, len(text)):
        if text[i] in terminators:
            end = i + 1
            break
    return text[start:end].strip()


def find_token(tokens: Sequence[dict], offset: int) -> dict | None:
    """The token covering ``offset``, preferring the longest span.

    Mirrors :func:`reading.storage.db.find_token_at` for callers that already hold
    the token list (the artifact builder and the tests).
    """
    best: dict | None = None
    for token in tokens:
        if token["start_char"] <= offset < token["end_char"]:
            if best is None or token["syllable_count"] > best["syllable_count"]:
                best = token
    return best
