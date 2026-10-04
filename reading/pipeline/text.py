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

import difflib
import re
import unicodedata
from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

UNDERSCORE = "_"
_WS_RE = re.compile(r"\s+")
_WORD_RE = re.compile(r"\w+", re.UNICODE)
# A blank line: the only whitespace run that carries meaning.  "\n\n", "\r\n\r\n",
# "  \n \n  " and a run of three newlines are all one paragraph break.
_PARA_RE = re.compile(r"(?:\r?\n)[ \t]*(?:(?:\r?\n)[ \t]*)+")


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
    """Collapse every whitespace run to one space.  For single-line prose only."""
    return _WS_RE.sub(" ", text).strip()


def squeeze_whitespace_preserving_paragraphs(text: str) -> str:
    """Collapse whitespace *within* paragraphs, keeping blank lines between them.

    :func:`squeeze_whitespace` treats a paragraph break as just more whitespace, so
    applying it to the model's article silently runs every paragraph together and
    the reader — which rebuilds paragraphs from blank lines — renders an 8,000
    character article as one wall of text.  Newlines are the one kind of whitespace
    here that carries meaning, so they survive; spaces, tabs and runs of three
    newlines do not.

    The syllable sequence is unaffected, so token offsets and grouping counts stay
    valid across this transform.
    """
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    paragraphs = (
        squeeze_whitespace(part) for part in _PARA_RE.split(normalized)
    )
    return "\n\n".join(part for part in paragraphs if part)


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


def word_change_ratio(original: str, rewritten: str) -> float:
    """Share of the source's words that the rewrite replaced, added or dropped.

    Zero means the "simplification" returned the article unchanged apart from
    underscores — which is exactly the failure this pipeline had, and which nothing
    downstream could see: the bundle looked perfect, the token map was sound, and an
    A2 article was in fact unmodified B2 prose.  Measuring it at ingest is the only
    cheap way to notice.

    Compared on whitespace-separated words, so reordering counts as change (as it
    should — a reordered sentence is a rewritten one).
    """
    before = squeeze_whitespace(original.replace(UNDERSCORE, " ")).split()
    after = squeeze_whitespace(rewritten.replace(UNDERSCORE, " ")).split()
    if not before:
        return 0.0
    matcher = difflib.SequenceMatcher(None, before, after, autojunk=False)
    changed = sum(
        max(i2 - i1, j2 - j1)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes()
        if tag != "equal"
    )
    return changed / len(before)


def grouping_from_underscores(segmented_text: str) -> tuple[str, list[int]]:
    """Split LLM underscore output into (plain text, group sizes).

    The plain text is what gets stored and rendered; the group sizes are the LLM's
    claim about which syllables form one word.

    Paragraph breaks are preserved: they are the only structure the reader uses to
    rebuild the article's shape from the stored string.
    """
    squeezed = squeeze_whitespace_preserving_paragraphs(nfc(segmented_text))
    # Underscores become spaces, then whitespace is squeezed again: an underscore
    # next to a space would otherwise leave a double space in the stored text.
    # This cannot change the syllable sequence, so the counts still hold.
    plain = squeeze_whitespace_preserving_paragraphs(squeezed.replace(UNDERSCORE, " "))
    # Split on *any* whitespace run, so a paragraph break separates chunks too.  A
    # chunk must never straddle a newline: words_in_chunk would then count two
    # separate words as one multi-syllable group.
    counts = [
        n for n in (words_in_chunk(chunk) for chunk in _WS_RE.split(squeezed)) if n
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


# A gap that is part of a *number*, not punctuation between two words: 300.000,
# 1/7/2025, 3-4.  The syllable sequence splits these into digit runs, so the guard
# has to let them back through or a price gets tappable as two tokens.
_NUMBER_SEPARATORS = ".,/:-–"


def span_is_contiguous(
    text: str, syllables: Sequence[Syllable], start: int, end: int
) -> bool:
    """True when syllables ``[start, end)`` are adjacent words in a single sentence.

    :func:`syllables_with_offsets` keeps only ``\\w+`` runs, so punctuation,
    paragraph breaks and double spaces are *not* in the syllable sequence at all.
    That means a span of two or more syllables can silently straddle a full stop or
    a blank line, and the token map then slices ``text[start_char:end_char]`` and
    produces a "word" like ``BBC.\\n\\nÔng`` — tapped as one unit, defined as
    nothing, and rendered as a broken span.

    Every multi-syllable span therefore has to be contiguous: exactly one space
    between consecutive syllables, nothing else — except a separator *inside a
    number*, where both sides are digits.  This is the guard that makes that
    checkable, and it is the reason the reconciler takes an ``allow_span`` predicate
    rather than assuming the two coordinate systems line up.
    """
    if end - start < 2:
        return True
    for i in range(start, end - 1):
        left, right = syllables[i], syllables[i + 1]
        gap = text[left.end : right.start]
        if gap == " ":
            continue
        if (
            len(gap) == 1
            and gap in _NUMBER_SEPARATORS
            and left.text.isdigit()
            and right.text.isdigit()
        ):
            continue
        return False
    return True


def contiguous_predicate(
    text: str, syllables: Sequence[Syllable]
) -> "Callable[[int, int], bool]":
    """An ``allow_span`` predicate bound to one text and syllable sequence."""
    return lambda start, end: span_is_contiguous(text, syllables, start, end)
