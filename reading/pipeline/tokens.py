"""Build the token map: the character-offset index the reading page resolves against.

A token map entry is everything the frontend needs for one tappable word, and
nothing else — the page walks ``simplified_text`` and wraps each
``[start_char, end_char)`` range in ``<span data-word-id="N">``.  Storing offsets
rather than pre-rendered HTML keeps the artifact small and lets the client decide
how to wrap.

Definitions are resolved here, in cost order, and the source is recorded so the
provenance is auditable:

1. the local Việt→Anh dictionary, for the **English** meaning (free, offline);
2. the pre-teach vocabulary the LLM already returned for this article, for the
   **Vietnamese** gloss (free, and already paid for as part of simplification);
3. nothing — leaving the definition columns NULL, which is what the frontend's
   fallback is for.  Ingest never makes a per-word LLM call; that would be one call
   per token, and the whole point of doing segmentation in batch is not to.

The two languages are kept in separate columns rather than merged, so the reader can
show English first with the Vietnamese gloss underneath, and so a later dictionary
rebuild changes one without disturbing the other.
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

# How many alternative English senses to carry into the bundle.  The tap sheet
# shows the primary gloss prominently and the rest as a short list.
MAX_EXTRA_SENSES = 4


@dataclass(frozen=True)
class Definition:
    """What is known about one form's meaning, in both languages.

    ``source`` defaults to empty meaning "unspecified", not to :data:`SRC_NONE`:
    callers that omit it are supplying a pre-teach gloss, and a truthy sentinel here
    would suppress that fallback.
    """

    text: str
    cefr: str | None = None
    source: str = ""
    # The English meaning from the local dictionary, when it has one.
    english: str | None = None
    # The Vietnamese gloss from the model's pre-teach list, when it gave one.
    vietnamese: str | None = None
    # Further English senses, for the sheet's secondary line.
    senses: tuple[str, ...] = ()


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
                "definition_en": (definition.english or None) if definition else None,
                "definition_vi": (definition.vietnamese or None) if definition else None,
                "senses_en": list(definition.senses) if definition and definition.senses else None,
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
    """The glosses available for this span, English first, or ``None`` to defer.

    A span can have one language, the other, both or neither; only when neither
    exists does this return ``None`` and hand the tap to the UI's fallback.
    """
    english = ""
    senses: tuple[str, ...] = ()
    if dictionary is not None:
        found = dictionary.glosses(form)
        if found:
            english = found[0]
            senses = tuple(found[1 : 1 + MAX_EXTRA_SENSES])

    vietnamese = ""
    cefr: str | None = None
    for key in _lookup_keys(form, syllables, span):
        known = definitions.get(key)
        if known is not None and (known.vietnamese or known.text):
            vietnamese = known.vietnamese or known.text
            cefr = known.cefr
            break

    if not english and not vietnamese:
        return None

    # English is the primary display string when the dictionary has it; the
    # Vietnamese gloss is the fallback and always kept alongside.
    return Definition(
        text=english or vietnamese,
        cefr=cefr,
        source=SRC_DICTIONARY if english else SRC_PRET_EACH,
        english=english or None,
        vietnamese=vietnamese or None,
        senses=senses,
    )


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
