"""Normalise the model's practice content into storable rows.

The counterpart of ``vocab.preteach_rows``: the exercises call returns objects, the
``exercises`` table stores rows, and this is the only place that maps one to the
other.  Kept separate from ``deepseek`` (which only parses) and from ``storage``
(which only writes SQL) so the shape can be unit-tested without either.
"""
from __future__ import annotations

from reading.pipeline.deepseek import Exercises, QuizItem

MCQ = "mcq"
SHORT = "short"
WRITING = "writing"


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
    return {
        "kind": MCQ,
        "ordinal": ordinal,
        "prompt": item.question,
        "options": item.options,
        "answer": item.answer,
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
