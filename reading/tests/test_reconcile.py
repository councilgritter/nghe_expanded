"""Unit tests for the segmentation reconciler.

This is the piece that decides what a tapped word is, so the tests pin the
decision order (dictionary > agreement > keep-both) and the two invariants that
make the token map usable: the spans must exactly tile the syllable sequence, and
every disagreement must carry both readings.
"""
from __future__ import annotations

import pytest

from reading.pipeline.reconcile import (
    SRC_AGREED,
    SRC_DICTIONARY,
    SRC_LLM,
    SRC_UNDERTHESEA,
    reconcile,
    summarize,
)

from .conftest import make_dictionary


def syllables_of(text: str) -> list[str]:
    return text.split()


class TestAgreement:
    def test_identical_segmentations_are_all_locked(self):
        syllables = syllables_of("chúng tôi đi học")
        spans = [(0, 1), (1, 3), (3, 4)]
        result = reconcile(syllables, spans, spans)
        assert [s.source for s in result] == [SRC_AGREED] * 3
        assert all(s.locked for s in result)
        assert not any(s.ambiguous for s in result)

    def test_agreement_holds_without_a_dictionary(self):
        syllables = syllables_of("a b c")
        spans = [(0, 2), (2, 3)]
        assert reconcile(syllables, spans, spans, dictionary=None) == reconcile(
            syllables, spans, spans
        )


class TestDisagreement:
    def test_disagreement_keeps_both_readings(self):
        # underthesea says 2 syllables, the LLM says 3.
        syllables = syllables_of("trí tuệ nhân tạo")
        uts = [(0, 2), (2, 4)]
        llm = [(0, 3), (3, 4)]
        result = reconcile(syllables, uts, llm)

        first = result[0]
        assert first.ambiguous is True
        assert first.locked is False
        assert first.source == SRC_UNDERTHESEA          # underthesea is primary
        assert first.end == 2                            # ...so the shorter span wins
        assert {c.source for c in first.candidates} == {SRC_UNDERTHESEA, SRC_LLM}
        assert {c.syllables for c in first.candidates} == {2, 3}

    def test_candidate_forms_are_underscore_forms(self):
        syllables = syllables_of("trí tuệ nhân tạo")
        result = reconcile(syllables, [(0, 2), (2, 4)], [(0, 3), (3, 4)])
        forms = {c.form for c in result[0].candidates}
        assert "trí_tuệ" in forms
        assert "trí_tuệ_nhân" in forms


class TestDictionaryConstraint:
    def test_dictionary_overrides_both_segmenters(self):
        # Both segmenters split it; the dictionary says it is one word.
        dictionary = make_dictionary(["đại học"])
        syllables = syllables_of("trường đại học quốc gia")
        split = [(0, 1), (1, 2), (2, 3), (3, 5)]
        result = reconcile(syllables, split, split, dictionary=dictionary)

        assert result[1].source == SRC_DICTIONARY
        assert result[1].locked is True
        assert result[1].ambiguous is False
        assert (result[1].start, result[1].end) == (1, 3)

    def test_dictionary_beats_a_longer_llm_reading(self):
        # The LLM proposes a 4-syllable chunk; the dictionary knows a 3-syllable word
        # starting at the same place and must win.
        dictionary = make_dictionary(["quốc gia"])
        syllables = syllables_of("a quốc gia b")
        uts = [(0, 1), (1, 2), (2, 3), (3, 4)]
        llm = [(0, 4)]
        result = reconcile(syllables, uts, llm, dictionary=dictionary)
        assert (result[1].start, result[1].end) == (1, 3)
        assert result[1].source == SRC_DICTIONARY

    def test_dictionary_respects_max_syllables(self):
        # A 4-syllable entry is invisible to a dictionary limited to 3.
        dictionary = make_dictionary(["trí tuệ nhân tạo"], max_syllables=3)
        syllables = syllables_of("trí tuệ nhân tạo")
        split = [(0, 1), (1, 2), (2, 3), (3, 4)]
        result = reconcile(syllables, split, split, dictionary=dictionary)
        assert all(s.source == SRC_AGREED for s in result)

    def test_single_syllable_dictionary_hits_do_not_lock_anything(self):
        dictionary = make_dictionary(["học"])
        syllables = syllables_of("đi học")
        spans = [(0, 1), (1, 2)]
        result = reconcile(syllables, spans, spans, dictionary=dictionary)
        assert result[0].source == SRC_AGREED


class TestInvariants:
    @pytest.mark.parametrize(
        "uts, llm",
        [
            ([(0, 1)], [(0, 1)]),
            ([(0, 3)], [(0, 1)]),
            ([(0, 1), (1, 3)], [(0, 2), (2, 3)]),
            ([(0, 2), (2, 5)], [(0, 5)]),
            ([(0, 1), (1, 2), (2, 3), (3, 4)], [(0, 4)]),
        ],
    )
    def test_spans_tile_the_syllables_with_no_gap_or_overlap(self, uts, llm):
        count = max(end for _, end in uts + llm)
        syllables = [f"s{i}" for i in range(count)]
        result = reconcile(syllables, uts, llm)

        cursor = 0
        for span in result:
            assert span.start == cursor, "span does not start where the previous ended"
            assert span.end > span.start, "empty span"
            cursor = span.end
        assert cursor == len(syllables), "spans do not cover every syllable"

    def test_locked_is_the_negation_of_ambiguous(self):
        syllables = syllables_of("a b c d")
        result = reconcile(syllables, [(0, 3), (3, 4)], [(0, 2), (2, 4)])
        for span in result:
            assert span.locked is not span.ambiguous

    def test_result_is_deterministic(self):
        syllables = syllables_of("a b c d e")
        args = (syllables, [(0, 2), (2, 5)], [(0, 3), (3, 5)])
        assert reconcile(*args) == reconcile(*args)


class TestSummarize:
    def test_counts_by_provenance(self):
        syllables = syllables_of("a b c d e")
        spans = reconcile(syllables, [(0, 2), (2, 5)], [(0, 3), (3, 5)])
        stats = summarize(spans)
        assert stats.total_spans == len(spans)
        assert stats.locked + stats.ambiguous == stats.total_spans

    def test_ambiguous_percentage_is_zero_safe(self):
        assert summarize([]).ambiguous_pct == 0.0
