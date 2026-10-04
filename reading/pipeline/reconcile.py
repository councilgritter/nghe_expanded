"""Reconcile the LLM's segmentation with underthesea's, under the dictionary.

Two segmenters look at the same simplified text and disagree about where words
begin and end.  This module decides, in a fixed and testable order:

1. **The dictionary wins — if it can explain the word.**  If the local compound
   dictionary knows a word starting here, that span is locked.  This is the "hard
   constraint": the word lists are a statement of record about Vietnamese compound
   boundaries, so neither model gets to override them.  Three guards narrow it,
   because the word lists are corpus-derived and contain things that are not words
   (see *Guards* below).
2. **Agreement is locked.**  When the LLM and underthesea propose the same span,
   take it.
3. **Disagreement is preserved, not resolved.**  underthesea's span becomes the
   primary (it is a deterministic, trained Vietnamese segmenter, and biasing to
   the shorter span is the safe failure — it can never swallow a whole clause),
   the token is flagged ``ambiguous``, and *both* readings are stored as
   candidates.  The reader-facing remedy is the expand/shrink handle in the UI,
   which is why the alternatives are kept rather than thrown away.

## Guards

The bundled word lists come from a corpus, so they contain sequences that are not
lexical units: measured over the stored articles, 183 distinct multi-syllable
*locked* spans have no English gloss, and the list includes ``của ông``,
``không phải``, ``lúc nào``.  Both guards below were written against that sample and
then narrowed by what it did to the glossed compounds:

* **contiguous** — no full stop, quote, newline or paragraph break inside the span
  (digits keep their separators, so ``300.000`` stays one token).  The syllables
  carry offsets but not punctuation, so this has to be checked by the caller through
  ``allow_span``; see :func:`reading.pipeline.text.span_is_contiguous`.
* **a run of function words is not a word — unless the dictionary glosses it.**  The
  gloss condition is not decoration: ``trước đây`` (before), ``chúng tôi`` (we),
  ``tháng Một`` (January), ``vì vậy`` (therefore) and ``ngày nay`` (nowadays) are all
  runs of function words and all real words, and the word lists gloss them.  Splitting
  them produced worse readings than the compound — ``tôi`` leads with "slave", ``là``
  with "fine silk" — so the guard only splits what nothing can explain.  A span the
  guards leave alone is still reachable: the reader can widen a selection and the
  exported lookup index answers for any range the dictionary knows.

The reconciler is deterministic and free of I/O, which is what makes it directly
unit-testable.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Sequence

from reading.storage.dictionary import CompoundDictionary, normalize_syllable
from reading.pipeline.text import UNDERSCORE

# Span provenance values, also surfaced in the token map.
SRC_DICTIONARY = "dictionary"
SRC_AGREED = "agreed"
SRC_UNDERTHESEA = "underthesea"
SRC_LLM = "llm"
# A span the guards refused to merge: a phrase made only of function words, or a
# span that crossed punctuation.  It is split into its syllables deliberately.
SRC_SPLIT = "split"

# ---------------------------------------------------------------------------
# The function-word guard.
#
# Kept deliberately conservative: it is only consulted when *every* syllable of a
# candidate span is in the set, so a real compound containing one function word
# (``không khí``, ``trong nước``, ``một nước``) is untouched.  It exists because a
# corpus word list records frequent *sequences*, and a sequence of function words
# is a phrase a learner needs split, not one word to memorise.
# ---------------------------------------------------------------------------
FUNCTION_WORDS = frozenset(
    """
    của và với cho các những một mọi này đó kia ấy nào ai gì thì mà nên nhưng hoặc
    nếu vì do bởi để đã đang sẽ vừa mới cũng đều chỉ còn rất quá lắm hơn không
    chẳng chưa phải được bị có là ở tại từ đến tới về ra vào lên xuống rồi trước
    sau trong ngoài trên dưới giữa khi lúc nay ông bà anh chị em nó họ ta tôi
    mình chúng người tháng ngày năm cũng lại đây thế vậy
    """.split()
)


def _all_function_words(syllables: Sequence[str]) -> bool:
    return bool(syllables) and all(
        normalize_syllable(s) in FUNCTION_WORDS for s in syllables
    )


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


def _allowed(allow_span: Callable[[int, int], bool] | None, start: int, end: int) -> bool:
    return allow_span is None or allow_span(start, end)


def guard_refuses_merge(
    syllables: Sequence[str],
    start: int,
    end: int,
    dictionary: CompoundDictionary | None = None,
) -> bool:
    """True when the merge guard refuses to emit ``[start, end)`` as one word.

    The guard is the function-word rule, applied to *any* multi-syllable span and
    not just to dictionary matches, because a model or a segmenter can propose the
    same phrase.  It fires only when nothing glosses the span: a run of function
    words the dictionary cannot explain is a phrase, and splitting it is what makes
    ``của`` and ``ông`` tappable; a run the dictionary *does* gloss is a word
    (``trước đây``, ``chúng tôi``, ``vì vậy``) and is left alone.
    """
    if end - start < 2 or not _all_function_words(syllables[start:end]):
        return False
    if dictionary is None:
        return True
    return not dictionary.primary_gloss(_underscore(syllables, start, end))


def _split_span(start: int) -> ReconciledSpan:
    """The single syllable emitted where the guards refused a merge."""
    return ReconciledSpan(start=start, end=start + 1, source=SRC_SPLIT, locked=True)


def dictionary_length(
    dictionary: CompoundDictionary | None,
    syllables: Sequence[str],
    cursor: int,
    allow_span: Callable[[int, int], bool] | None = None,
) -> int:
    """How many syllables the dictionary should lock at ``cursor`` (0 = none).

    The longest known compound starting here that survives the contiguity guard.  A
    bare syllable is always a valid span, so "nothing to lock" is 0 rather than 1.
    """
    if dictionary is None:
        return 0

    limit = min(dictionary.max_syllables, len(syllables) - cursor)
    for length in range(limit, 1, -1):
        end = cursor + length
        if not _allowed(allow_span, cursor, end):
            continue
        if dictionary.is_known_compound(list(syllables[cursor:end])):
            return length
    return 0
    return 0 if has_glosses else fallback


def reconcile(
    syllables: Sequence[str],
    underthesea_spans: Sequence[tuple[int, int]],
    llm_spans: Sequence[tuple[int, int]],
    dictionary: CompoundDictionary | None = None,
    allow_span: Callable[[int, int], bool] | None = None,
) -> list[ReconciledSpan]:
    """Merge two segmentations into one partition, marking every disagreement.

    ``syllables`` is the syllable sequence of the simplified text; both span lists
    must partition it.  ``allow_span`` is the punctuation guard (see
    :func:`reading.pipeline.text.span_is_contiguous`): a span it rejects is never
    emitted, whatever either segmenter or the dictionary proposed.
    """
    total = len(syllables)
    uts = _span_map(underthesea_spans)
    llm = _span_map(llm_spans)

    result: list[ReconciledSpan] = []
    cursor = 0
    while cursor < total:
        # 1. the dictionary, as a hard constraint narrowed by the guards
        length = dictionary_length(dictionary, syllables, cursor, allow_span)
        if length >= 2:
            end = min(cursor + length, total)
            if guard_refuses_merge(syllables, cursor, end, dictionary):
                result.append(_split_span(cursor))
                cursor += 1
                continue
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

        # A span that crosses punctuation is not a word even if both segmenters
        # say it is: both readings collapse to the single syllable, which is the
        # only reading the text supports.  The same applies to a span that is only
        # function words.
        if (
            not _allowed(allow_span, cursor, uts_end)
            or not _allowed(allow_span, cursor, llm_end)
            or guard_refuses_merge(syllables, cursor, uts_end, dictionary)
        ):
            result.append(_split_span(cursor))
            cursor += 1
            continue

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
    # Spans the guards refused to merge: function-word phrases and spans that
    # crossed punctuation.  Printed, because a jump here means the guards are
    # doing real work on this article and the split is worth eyeballing.
    split_by_guard: int = 0

    @property
    def ambiguous_pct(self) -> float:
        return 100.0 * self.ambiguous / self.total_spans if self.total_spans else 0.0


def summarize(spans: Sequence[ReconciledSpan]) -> ReconcileStats:
    stats = ReconcileStats(total_spans=len(spans))
    for span in spans:
        if span.source == SRC_DICTIONARY:
            stats.from_dictionary += 1
        elif span.source == SRC_AGREED:
            stats.from_agreement += 1
        elif span.source == SRC_SPLIT:
            stats.split_by_guard += 1
        if span.ambiguous:
            stats.ambiguous += 1
        else:
            stats.locked += 1
    return stats
