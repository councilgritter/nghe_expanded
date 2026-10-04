"""Pre-teach content: normalise what the model returned into storable rows.

The model's glosses are also the cheapest source of definitions for the token map,
so this module produces both: the rows for the ``preteach`` table and the
``{form: Definition}`` map the token-map builder consults before giving up.
"""
from __future__ import annotations

from typing import Iterable, Sequence

from reading.pipeline.deepseek import PreTeachItem
from reading.pipeline.text import UNDERSCORE, squeeze_whitespace
from reading.pipeline.tokens import Definition, SRC_PRET_EACH

VOCAB = "vocab"
GRAMMAR = "grammar"

# A grammar "example" is often a quoted clause; keep it to a sentence.
MAX_EXAMPLE_CHARS = 300


def normalize_term(term: str) -> str:
    """Underscore form, matching how spans are keyed in the token map."""
    squeezed = squeeze_whitespace(term.replace(UNDERSCORE, " ")).casefold()
    return UNDERSCORE.join(part for part in squeezed.split(" ") if part)


def plain(text: str) -> str:
    """Prose with the underscore convention stripped out.

    The model is told to join syllables with ``_`` in the *article*, and it sometimes
    carries that into the glosses and examples as well ("dùng vũ_lực để..."). Those
    fields are read, not segmented, so the underscores come out. ``term`` keeps them:
    it is the key the token map looks definitions up by.
    """
    return squeeze_whitespace(text.replace(UNDERSCORE, " "))


def preteach_rows(
    vocab: Sequence[PreTeachItem],
    grammar: Sequence[PreTeachItem],
    default_cefr: str = "",
) -> list[dict]:
    """Rows for the ``preteach`` table, deduplicated on (kind, term)."""
    rows: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for kind, items in ((VOCAB, vocab), (GRAMMAR, grammar)):
        ordinal = 0
        for item in items:
            term = normalize_term(item.term)
            if not term or (kind, term) in seen:
                continue
            seen.add((kind, term))
            rows.append(
                {
                    "kind": kind,
                    "term": term,
                    "gloss": plain(item.gloss),
                    "cefr_level": (item.cefr or default_cefr).upper() or None,
                    "example": plain(item.example)[:MAX_EXAMPLE_CHARS] or None,
                    "ordinal": ordinal,
                }
            )
            ordinal += 1
    return rows


def definitions_from_preteach(vocab: Iterable[PreTeachItem]) -> dict[str, Definition]:
    """Vocab glosses keyed by underscore form, for the token map.

    Only vocabulary contributes: a grammar point's term is a pattern name, not a
    word the reader can tap.
    """
    out: dict[str, Definition] = {}
    for item in vocab:
        term = normalize_term(item.term)
        gloss = plain(item.gloss)
        if not term or not gloss:
            continue
        out.setdefault(
            term,
            Definition(
                text=gloss,
                cefr=(item.cefr.upper() or None) if item.cefr else None,
                source=SRC_PRET_EACH,
            ),
        )
    return out
