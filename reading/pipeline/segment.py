"""underthesea word segmentation.

underthesea ships its word-segmentation CRF model inside the package
(``pipeline/word_tokenize/models/ws_crf_vlsp2013_20230727/``), so this runs fully
offline with no first-use model download — which is what makes it safe to call in
the ingest loop and in tests.
"""
from __future__ import annotations

from typing import Sequence

from reading.pipeline.text import grouping_from_tokens

SEGMENTER_NAME = "underthesea"


def segment_with_underthesea(text: str) -> list[int]:
    """Segment ``text`` and return group sizes over its syllable sequence.

    ``word_tokenize`` returns tokens that may themselves contain spaces, so the
    token list is converted to sizes rather than to strings — that keeps this
    directly comparable with the LLM's grouping.
    """
    from underthesea import word_tokenize

    tokens: Sequence[str] = word_tokenize(text)
    return grouping_from_tokens(tokens)


def underscore_form(text: str) -> str:
    """Convenience: underthesea's own ``_``-joined rendering, for debugging."""
    from underthesea import word_tokenize

    return word_tokenize(text, format="text")
