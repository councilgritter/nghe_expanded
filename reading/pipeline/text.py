"""Text model shared by the segmenter, the reconciler and the token-map builder.

Vietnamese is written with syllables separated by spaces, so "words" are runs of
consecutive syllables.  Everything downstream works in two coordinate systems and
this module is the only place that converts between them:

* **character offsets** into the stored ``simplified_text`` — what the frontend
  needs, because the page wraps ranges of the rendered string; and
* **syllable indices** — what segmentation actually talks about, because both the
  LLM (underscores) and underthesea express their answer as a grouping of the
  syllable sequence.

The invariant that keeps them in step: every grouping must cover the *same*
syllable sequence, so a grouping is just a list of counts.  :func:`parse_grouping`
and :func:`grouping_from_underscores` return those counts, and both are checked
against the syllable list so a mismatch is detected rather than silently
mis-aligned.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Iterable, Sequence

UNDERSCORE = "_"
_WS_RE = re.compile(r"\s+")
_WORD_RE = re.compile(r"\w+", re.UNICODE)


class GroupingError(ValueError):
    """A grouping did not cover the syllable sequence it claims to describe."""


def nfc(text: str) -> str:
    """NFC-normalise.

    Required before any offset work: in NFD a syllable is a base letter plus
    combining marks, and ``\\w`` does not match combining marks, so offsets would
    silently land mid-character.
    """
    return unicodedata.normalize("NFC", text)


def squeeze_whitespace(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


@dataclass(frozen=True)
class Syllable:
    """One space-delimited unit (syllable, number or acronym) with its offsets."""

    index: int
    start: int
    end: int
    text: str

    @property
    def norm(self) -> str:
        return self.text.casefold()


def syllables_with_offsets(text: str) -> list[Syllable]:
    """Every ``\\w+`` run in ``text``, with character offsets.

    Punctuation and whitespace are deliberately excluded, so a span that ends at
    ``syllables[i].end`` never swallows the comma after it.
    """
    return [
        Syllable(index=i, start=m.start(), end=m.end(), text=m.group(0))
        for i, m in enumerate(_WORD_RE.finditer(text))
    ]


def words_in_chunk(chunk: str) -> int:
    """Number of syllable units inside one whitespace-delimited chunk.

    Underscores are treated as separators here, so ``chúng_tôi,`` is two units.
    """
    return len(_WORD_RE.findall(chunk.replace(UNDERSCORE, " ")))


def grouping_from_underscores(segmented_text: str) -> tuple[str, list[int]]:
    """Split LLM underscore output into (plain text, group sizes).

    The plain text is what gets stored and rendered; the group sizes are the LLM's
    claim about which syllables form one word.
    """
    squeezed = squeeze_whitespace(nfc(segmented_text))
    # Underscores become spaces, then whitespace is squeezed again: an underscore
    # next to a space would otherwise leave a double space in the stored text.
    # This cannot change the syllable sequence, so the counts still hold.
    plain = squeeze_whitespace(squeezed.replace(UNDERSCORE, " "))
    counts = [
        n for n in (words_in_chunk(chunk) for chunk in squeezed.split(" ")) if n
    ]
    return plain, counts


def grouping_from_tokens(tokens: Iterable[str]) -> list[int]:
    """Group sizes from a token list (underthesea's non-text output)."""
    return [n for n in (words_in_chunk(t) for t in tokens) if n]


def counts_to_spans(counts: Sequence[int]) -> list[tuple[int, int]]:
    """Convert group sizes to ``(start_syllable, end_syllable_exclusive)`` spans."""
    spans: list[tuple[int, int]] = []
    cursor = 0
    for count in counts:
        spans.append((cursor, cursor + count))
        cursor += count
    return spans


def spans_to_counts(spans: Sequence[tuple[int, int]]) -> list[int]:
    return [end - start for start, end in spans]


def check_grouping(counts: Sequence[int], syllables: Sequence[Syllable], label: str) -> None:
    """Raise if a grouping does not exactly cover the syllable sequence."""
    total = sum(counts)
    if total != len(syllables):
        raise GroupingError(
            f"{label} grouping covers {total} syllables but the text has "
            f"{len(syllables)}; the segmentations cannot be aligned"
        )
    if any(c <= 0 for c in counts):
        raise GroupingError(f"{label} grouping contains an empty group")


def slice_text(text: str, syllables: Sequence[Syllable], start: int, end: int) -> str:
    """The exact substring spanning syllables ``[start, end)``."""
    return text[syllables[start].start : syllables[end - 1].end]
