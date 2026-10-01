"""Tests for the real underthesea segmenter.

These exercise the installed model rather than a stub, which is the point: the
pipeline's correctness depends on the segmenter running offline and on its output
describing exactly the same syllable sequence as the text it was given.
"""
from __future__ import annotations

from reading.pipeline.segment import segment_with_underthesea, underscore_form
from reading.pipeline.text import syllables_with_offsets

SENTENCE = "Chúng tôi đi học ở trường đại học quốc gia."
EXTRA = "Hôm nay trời rất đẹp."


class TestSegmentation:
    def test_runs_offline_and_returns_groups(self):
        counts = segment_with_underthesea(SENTENCE)
        assert counts
        assert all(c >= 1 for c in counts)

    def test_groups_cover_the_whole_syllable_sequence(self):
        """The invariant reconciliation depends on."""
        for text in (SENTENCE, EXTRA):
            counts = segment_with_underthesea(text)
            assert sum(counts) == len(syllables_with_offsets(text)), text

    def test_finds_multi_syllable_words(self):
        assert any(c > 1 for c in segment_with_underthesea(SENTENCE))

    def test_is_deterministic(self):
        assert segment_with_underthesea(SENTENCE) == segment_with_underthesea(SENTENCE)

    def test_empty_input_yields_no_groups(self):
        assert segment_with_underthesea("") == []

    def test_punctuation_does_not_create_groups(self):
        counts = segment_with_underthesea("xin chào , bạn .")
        assert len(counts) == len(syllables_with_offsets("xin chào , bạn ."))


class TestUnderscoreForm:
    def test_joins_compounds_with_underscores(self):
        assert "đại_học" in underscore_form(SENTENCE)

    def test_format_text_respaces_punctuation(self):
        """Documents why the pipeline does not segment from this string.

        ``format='text'`` emits punctuation as its own space-delimited token, so the
        character offsets no longer match the source text.  The pipeline therefore
        segments via the token list and counts groups, leaving offsets intact.
        """
        rendered = underscore_form(SENTENCE)
        assert rendered.endswith("gia .")
        assert "gia." not in rendered

    def test_the_syllable_sequence_is_unchanged(self):
        """Punctuation spacing aside, no syllable is added, lost or reordered.

        Underscores are stripped first: ``_`` is a word character, so ``\\w+`` would
        otherwise read a joined compound as a single unit.
        """
        rendered = underscore_form(SENTENCE).replace("_", " ")
        assert [s.text for s in syllables_with_offsets(rendered)] == [
            s.text for s in syllables_with_offsets(SENTENCE)
        ]
