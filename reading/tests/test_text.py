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
    counts_to_spans,
    grouping_from_underscores,
    nfc,
    slice_text,
    spans_to_counts,
    squeeze_whitespace,
    syllables_with_offsets,
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

    def test_words_in_chunk_treats_underscore_as_separator(self):
        assert words_in_chunk("chúng_tôi,") == 2
        assert words_in_chunk(",") == 0
        assert words_in_chunk("đẹp") == 1
