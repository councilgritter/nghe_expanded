"""Normalise the model's practice content into storable rows.

The counterpart of ``vocab.preteach_rows``: the exercises call returns objects, the
``exercises`` table stores rows, and this is the only place that maps one to the
other.  Kept separate from ``deepseek`` (which only parses) and from ``storage``
(which only writes SQL) so the shape can be unit-tested without either.

## Why the options are reordered

Measured over the first 40 questions this pipeline generated, **34 had the correct
answer at A**, five at B, one at D and none at C.  The model anchors on the first
option — the prompt's own example showed `"answer": 0` — and a reader who notices
learns to press A, which measures nothing.

Instruction alone does not fix a position bias, so the options are put in a
**content-determined order** here, at storage time: each option is sorted by a hash of
the question plus its own text.  The correct answer's *text* never changes, only where
it sits, and because the order is a pure function of the content it is stable — the
same question always lands in the same order, and re-running the repair cannot drift
it back towards the bias (applying a permutation twice would).

This is also why the prompt forbids options that refer to each other ("cả A và B",
"tất cả các ý trên"): such an option would not survive any reordering.
"""
from __future__ import annotations

import hashlib

from reading.pipeline.deepseek import Exercises, QuizItem

MCQ = "mcq"
SHORT = "short"
WRITING = "writing"


def _order_key(question: str, option: str) -> str:
    """A stable, content-only sort key: no position, no randomness, no clock."""
    digest = hashlib.sha256(f"{question}\x1f{option}".encode("utf-8"))
    return digest.hexdigest()


def order_options(question: str, options: list[str], answer: int) -> tuple[list[str], int]:
    """Options in a content-determined order, with ``answer`` remapped.

    Deterministic and **idempotent**: the result depends only on the set of option
    texts and the question, so re-running it (on a stored row, say) cannot shuffle a
    set that is already balanced back towards one position.  O(n log n) on four
    options, and a stable sort, so duplicate texts keep their relative order.
    """
    if len(options) < 2 or not 0 <= answer < len(options):
        return list(options), answer
    correct = options[answer]
    ordered = sorted(options, key=lambda option: _order_key(question, option))
    return ordered, ordered.index(correct)


def exercises_rows(exercises: Exercises) -> list[dict]:
    """Rows for the ``exercises`` table, in display order."""
    rows: list[dict] = []
    for ordinal, item in enumerate(exercises.questions):
        rows.append(_mcq_row(item, ordinal))
    for ordinal, item in enumerate(exercises.short_answers):
        rows.append(_short_row(item, ordinal))
    if exercises.writing.prompt:
        rows.append(
            {
                "kind": WRITING,
                "ordinal": 0,
                "prompt": exercises.writing.prompt,
                "key_points": exercises.writing.key_points,
                "sample": exercises.writing.model_answer,
                "min_words": exercises.writing.min_words,
            }
        )
    return rows


def _mcq_row(item: QuizItem, ordinal: int) -> dict:
    options, answer = order_options(item.question, item.options, item.answer)
    return {
        "kind": MCQ,
        "ordinal": ordinal,
        "prompt": item.question,
        "options": options,
        "answer": answer,
        "why": item.why,
    }


def _short_row(item: QuizItem, ordinal: int) -> dict:
    return {
        "kind": SHORT,
        "ordinal": ordinal,
        "prompt": item.question,
        "key_points": item.key_points,
        "sample": item.sample,
    }
