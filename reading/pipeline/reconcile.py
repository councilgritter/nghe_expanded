"""Reconcile the LLM's segmentation with underthesea's, under the dictionary.

Two segmenters look at the same simplified text and disagree about where words
begin and end.  This module decides, in a fixed and testable order:

1. **The dictionary wins outright.**  If the local compound dictionary knows a
   word starting here, that span is locked.  This is the "hard constraint": the
   word lists are a statement of record about Vietnamese compound boundaries, so
   neither model gets to override them.
2. **Agreement is locked.**  When the LLM and underthesea propose the same span,
   take it.
3. **Disagreement is preserved, not resolved.**  underthesea's span becomes the
   primary (it is a deterministic, trained Vietnamese segmenter, and biasing to
   the shorter span is the safe failure — it can never swallow a whole clause),
   the token is flagged ``ambiguous``, and *both* readings are stored as
   candidates.  The reader-facing remedy is the expand/shrink handle in the UI,
   which is why the alternatives are kept rather than thrown away.

The reconciler is deterministic and free of I/O, which is what makes it directly
unit-testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from reading.storage.dictionary import CompoundDictionary, normalize_syllable
from reading.pipeline.text import UNDERSCORE

# Span provenance values, also surfaced in the token map.
SRC_DICTIONARY = "dictionary"
SRC_AGREED = "agreed"
SRC_UNDERTHESEA = "underthesea"
SRC_LLM = "llm"


@dataclass(frozen=True)
class Candidate:
    """One reading of a span, kept so the UI can offer the alternative."""

    source: str
    form: str          # underscore form, for display/compare
    syllables: int

    def as_dict(self) -> dict:
        return {"source": self.source, "form": self.form, "syllables": self.syllables}


@dataclass(frozen=True)
class ReconciledSpan:
    """A span of syllables ``[start, end)`` in the simplified text."""

    start: int
    end: int
    source: str
    locked: bool
    ambiguous: bool = False
    candidates: list[Candidate] = field(default_factory=list)

    @property
    def length(self) -> int:
        return self.end - self.start


def _span_map(spans: Sequence[tuple[int, int]]) -> dict[int, int]:
    """``{start_syllable: end_syllable}`` for a partition."""
    return {start: end for start, end in spans}


def _underscore(syllables: Sequence[str], start: int, end: int) -> str:
    return UNDERSCORE.join(normalize_syllable(s) for s in syllables[start:end])


def reconcile(
    syllables: Sequence[str],
    underthesea_spans: Sequence[tuple[int, int]],
    llm_spans: Sequence[tuple[int, int]],
    dictionary: CompoundDictionary | None = None,
) -> list[ReconciledSpan]:
    """Merge two segmentations into one partition, marking every disagreement.

    ``syllables`` is the syllable sequence of the simplified text; both span lists
    must partition it.
    """
    total = len(syllables)
    uts = _span_map(underthesea_spans)
    llm = _span_map(llm_spans)

    result: list[ReconciledSpan] = []
    cursor = 0
    while cursor < total:
        # 1. the dictionary, as a hard constraint
        if dictionary is not None:
            length = dictionary.longest_match(list(syllables), cursor)
            if length >= 2:
                end = min(cursor + length, total)
                result.append(
                    ReconciledSpan(
                        start=cursor, end=end, source=SRC_DICTIONARY, locked=True
                    )
                )
                cursor = end
                continue

        # 2/3. compare the two segmenters
        uts_end = uts.get(cursor, cursor + 1)
        llm_end = llm.get(cursor, cursor + 1)
        uts_end = max(cursor + 1, min(uts_end, total))
        llm_end = max(cursor + 1, min(llm_end, total))

        if uts_end == llm_end:
            result.append(
                ReconciledSpan(
                    start=cursor, end=uts_end, source=SRC_AGREED, locked=True
                )
            )
            cursor = uts_end
            continue

        # Disagreement: keep underthesea primary, remember both readings.
        candidates = [
            Candidate(
                source=SRC_UNDERTHESEA,
                form=_underscore(syllables, cursor, uts_end),
                syllables=uts_end - cursor,
            ),
            Candidate(
                source=SRC_LLM,
                form=_underscore(syllables, cursor, llm_end),
                syllables=llm_end - cursor,
            ),
        ]
        result.append(
            ReconciledSpan(
                start=cursor,
                end=uts_end,
                source=SRC_UNDERTHESEA,
                locked=False,
                ambiguous=True,
                candidates=candidates,
            )
        )
        cursor = uts_end

    return result


@dataclass
class ReconcileStats:
    """Counts for the ingest log — the cheap way to notice a bad prompt."""

    total_spans: int = 0
    locked: int = 0
    ambiguous: int = 0
    from_dictionary: int = 0
    from_agreement: int = 0

    @property
    def ambiguous_pct(self) -> float:
        return 100.0 * self.ambiguous / self.total_spans if self.total_spans else 0.0


def summarize(spans: Sequence[ReconciledSpan]) -> ReconcileStats:
    stats = ReconcileStats(total_spans=len(spans))
    for span in spans:
        if span.source == SRC_DICTIONARY:
            stats.from_dictionary += 1
        elif span.locked:
            stats.from_agreement += 1
        if span.ambiguous:
            stats.ambiguous += 1
        else:
            stats.locked += 1
    return stats
