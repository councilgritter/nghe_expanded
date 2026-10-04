"""Tests for the shared text model: syllable offsets and grouping parsing.

Both coordinate systems in the pipeline meet here, so the invariants are worth
pinning: offsets must address the exact substring, and a grouping must always cover
every syllable or be rejected rather than silently mis-aligned.
"""
from __future__ import annotations

import pytest

from reading.pipeline.text import (
    GroupingError,
    check_grouping,
    contiguous_predicate,
    counts_to_spans,
    grouping_from_underscores,
    nfc,
    slice_text,
    span_is_contiguous,
    spans_to_counts,
    squeeze_whitespace,
    squeeze_whitespace_preserving_paragraphs,
    syllables_with_offsets,
    word_change_ratio,
    words_in_chunk,
)

TEXT = "Chúng tôi đi học ở trường đại học quốc gia."


class TestSyllables:
    def test_offsets_address_the_exact_substring(self):
        for syllable in syllables_with_offsets(TEXT):
            assert TEXT[syllable.start : syllable.end] == syllable.text

    def test_offsets_are_ordered_and_non_overlapping(self):
        previous = -1
        for syllable in syllables_with_offsets(TEXT):
            assert syllable.start >= previous
            previous = syllable.end

    def test_indexes_are_contiguous(self):
        syllables = syllables_with_offsets(TEXT)
        assert [s.index for s in syllables] == list(range(len(syllables)))

    def test_punctuation_is_excluded(self):
        syllables = syllables_with_offsets(TEXT)
        assert "." not in {s.text for s in syllables}

    def test_vietnamese_diacritics_are_single_units(self):
        assert [s.text for s in syllables_with_offsets("đại học")] == ["đại", "học"]

    def test_nfd_input_is_normalised_before_counting(self):
        # "học" written as NFD: base letters plus combining marks.
        nfd = "ho\u0323c"
        assert len(nfd) > 3
        assert [s.text for s in syllables_with_offsets(nfc(nfd))] == ["học"]

    def test_slice_text_spans_a_range(self):
        syllables = syllables_with_offsets(TEXT)
        assert slice_text(TEXT, syllables, 0, 2) == "Chúng tôi"
        assert slice_text(TEXT, syllables, 3, 5) == "học ở"


class TestContiguity:
    """The guard that keeps a "word" inside one sentence.

    Syllables are ``\\w+`` runs, so punctuation and paragraph breaks are invisible to
    the segmentation: without this check a span can be sliced across a full stop and
    the token map emits ``Lâm.\\n\\nChính`` as one tappable word.
    """

    def test_adjacent_syllables_with_one_space_are_contiguous(self):
        text = "trường đại học quốc gia"
        syllables = syllables_with_offsets(text)
        assert span_is_contiguous(text, syllables, 0, 4)

    def test_a_full_stop_inside_the_span_breaks_contiguity(self):
        text = "Lâm. Chính phủ"
        syllables = syllables_with_offsets(text)
        assert [s.text for s in syllables] == ["Lâm", "Chính", "phủ"]
        assert not span_is_contiguous(text, syllables, 0, 2)
        assert span_is_contiguous(text, syllables, 1, 3)

    def test_a_paragraph_break_breaks_contiguity(self):
        text = "một hai.\n\nba bốn"
        syllables = syllables_with_offsets(text)
        assert [s.text for s in syllables] == ["một", "hai", "ba", "bốn"]
        # "hai" then a full stop, a blank line, then "ba": never one word.
        assert not span_is_contiguous(text, syllables, 1, 3)
        assert span_is_contiguous(text, syllables, 2, 4)

    def test_single_syllables_are_always_contiguous(self):
        text = "Lâm. Chính"
        syllables = syllables_with_offsets(text)
        assert span_is_contiguous(text, syllables, 0, 1)
        assert span_is_contiguous(text, syllables, 1, 2)

    def test_the_predicate_binds_text_and_syllables(self):
        text = "Lâm. Chính"
        syllables = syllables_with_offsets(text)
        allow = contiguous_predicate(text, syllables)
        assert allow(0, 1)
        assert not allow(0, 2)

    def test_a_number_keeps_its_separators(self):
        # "300.000" and "1/7/2025" are one token each: the syllable splitter gives
        # digit runs, so the guard has to let a separator between digits through.
        text = "khoảng 300.000 người, ngày 1/7/2025"
        syllables = syllables_with_offsets(text)
        texts = [s.text for s in syllables]
        start = texts.index("300")
        assert span_is_contiguous(text, syllables, start, start + 2)
        start = texts.index("1")
        assert span_is_contiguous(text, syllables, start, start + 3)

    def test_a_separator_between_words_is_still_punctuation(self):
        text = "một-hai"
        syllables = syllables_with_offsets(text)
        assert not span_is_contiguous(text, syllables, 0, 2)


class TestGroupingFromUnderscores:
    def test_plain_text_has_no_underscores(self):
        plain, _ = grouping_from_underscores("Chúng_tôi đi học .")
        assert "_" not in plain
        assert plain == "Chúng tôi đi học ."

    def test_counts_reflect_the_underscore_groups(self):
        _, counts = grouping_from_underscores("Chúng_tôi đi học .")
        assert counts == [2, 1, 1]

    def test_counts_always_cover_the_plain_text(self):
        for segmented in (
            "a b c",
            "a_b c",
            "a_b_c",
            "a , b .",
            "  a_b   c  ",
            "a__b c",
        ):
            plain, counts = grouping_from_underscores(segmented)
            assert sum(counts) == len(syllables_with_offsets(plain)), segmented

    def test_punctuation_only_chunks_contribute_no_group(self):
        _, counts = grouping_from_underscores("a , b")
        assert counts == [1, 1]

    def test_underscore_adjacent_to_space_does_not_double_space(self):
        plain, counts = grouping_from_underscores("Chúng_ tôi")
        assert "  " not in plain
        assert sum(counts) == len(syllables_with_offsets(plain))


class TestParagraphPreservation:
    """Blank lines are the only whitespace that carries meaning.

    The reader rebuilds the article's paragraph shape from ``\\n\\n`` in the stored
    text.  Squeezing those to spaces rendered an 8,000-character article as a single
    wall of text, with no other symptom.
    """

    def test_a_blank_line_survives(self):
        plain, _ = grouping_from_underscores("Đoạn một.\n\nĐoạn hai.")
        assert plain == "Đoạn một.\n\nĐoạn hai."

    def test_whitespace_within_a_paragraph_is_still_collapsed(self):
        plain, _ = grouping_from_underscores("Đoạn   một.\n\nĐoạn \t hai.")
        assert plain == "Đoạn một.\n\nĐoạn hai."

    def test_bare_newlines_and_windows_line_endings_are_paragraph_breaks(self):
        assert "\n\n" in grouping_from_underscores("a\r\n\r\nb")[0]
        assert "\n\n" in grouping_from_underscores("a\n\n\n\nb")[0]

    def test_leading_and_trailing_blank_lines_are_trimmed(self):
        plain, _ = grouping_from_underscores("\n\n  Đoạn một.  \n\n")
        assert plain == "Đoạn một."

    def test_paragraph_breaks_survive_underscore_expansion(self):
        plain, _ = grouping_from_underscores("Chúng_tôi đi học.\n\nHôm_nay trời đẹp.")
        assert plain == "Chúng tôi đi học.\n\nHôm nay trời đẹp."

    def test_counts_still_cover_the_text_across_a_paragraph_break(self):
        plain, counts = grouping_from_underscores("Chúng_tôi đi.\n\nHôm_nay trời đẹp.")
        assert sum(counts) == len(syllables_with_offsets(plain))
        # The last word of one paragraph must not merge with the first of the next.
        assert counts == [2, 1, 2, 1, 1]

    def test_a_newline_never_joins_two_words_into_one_group(self):
        _, counts = grouping_from_underscores("một\nhai")
        assert counts == [1, 1]


class TestWordChangeRatio:
    """The guard against a "simplification" that transcribed the source.

    Nothing downstream could see it: the bundle, token map and segmentation were all
    correct, and an A2 article was really unmodified B2 prose.
    """

    def test_an_identical_text_scores_zero(self):
        text = "Chúng tôi đi học ở trường đại học quốc gia."
        assert word_change_ratio(text, text) == 0.0

    def test_underscores_alone_do_not_count_as_change(self):
        original = "Chúng tôi đi học."
        segmented = "Chúng_tôi đi học."
        assert word_change_ratio(original, segmented) == 0.0

    def test_reordering_counts_as_change(self):
        assert word_change_ratio("một hai ba bốn", "bốn ba hai một") > 0.5

    def test_a_fully_rewritten_text_scores_high(self):
        assert word_change_ratio("một hai ba bốn", "năm sáu bảy tám") == 1.0

    def test_a_partial_rewrite_lands_in_between(self):
        ratio = word_change_ratio("một hai ba bốn", "một hai ba chín")
        assert 0.0 < ratio <= 0.5

    def test_dropped_words_count_as_change(self):
        assert word_change_ratio("một hai ba bốn", "một hai") > 0.0

    def test_empty_source_is_not_a_division_error(self):
        assert word_change_ratio("", "bất kỳ") == 0.0


class TestGroupingArithmetic:
    def test_counts_and_spans_round_trip(self):
        counts = [2, 1, 3, 1]
        assert spans_to_counts(counts_to_spans(counts)) == counts

    def test_spans_are_contiguous(self):
        spans = counts_to_spans([2, 1, 3])
        assert spans == [(0, 2), (2, 3), (3, 6)]


class TestCheckGrouping:
    def test_accepts_an_exact_cover(self):
        syllables = syllables_with_offsets("a b c")
        check_grouping([2, 1], syllables, "test")

    def test_rejects_an_undercount(self):
        syllables = syllables_with_offsets("a b c")
        with pytest.raises(GroupingError):
            check_grouping([1, 1], syllables, "test")

    def test_rejects_an_empty_group(self):
        syllables = syllables_with_offsets("a b")
        with pytest.raises(GroupingError):
            check_grouping([1, 0, 1], syllables, "test")


class TestHelpers:
    def test_squeeze_whitespace_collapses_and_trims(self):
        assert squeeze_whitespace("  a \n b\t c  ") == "a b c"

    def test_paragraph_preserving_squeeze_keeps_the_blank_line(self):
        squeezed = squeeze_whitespace_preserving_paragraphs("  a \n b \n\n c \t d  ")
        assert squeezed == "a b\n\nc d"

    def test_words_in_chunk_treats_underscore_as_separator(self):
        assert words_in_chunk("chúng_tôi,") == 2
        assert words_in_chunk(",") == 0
        assert words_in_chunk("đẹp") == 1
