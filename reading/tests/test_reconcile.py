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
    SRC_SPLIT,
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


class TestGuards:
    """The word lists are corpus-derived, so a lock has to be earned.

    These pin the three guards that stop ``của ông`` being one word: a span made
    only of function words is split, an unexplainable span loses to one the app can
    gloss, and a span that crosses punctuation is never emitted at all.
    """

    def test_a_glossed_run_of_function_words_is_a_word(self):
        # "trước đây" (before), "chúng tôi" (we) and "tháng Một" (January) are all
        # runs of function words and all real words.  Splitting them produced worse
        # readings than the compound — "tôi" leads with "slave" — so the guard only
        # splits a run that *nothing* can gloss.
        dictionary = make_dictionary(
            ["trước đây", "chúng tôi"],
            glosses={"trước đây": ("before",), "chúng tôi": ("we",)},
        )
        syllables = syllables_of("trước đây chúng tôi")
        spans = [(0, 2), (2, 4)]
        result = reconcile(syllables, spans, spans, dictionary=dictionary)
        assert [(s.start, s.end) for s in result] == [(0, 2), (2, 4)]
        assert all(s.source == SRC_DICTIONARY for s in result)

    def test_an_unglossed_run_of_function_words_is_not_a_word(self):
        # "của ông" is in the corpus word list and nothing glosses it, so the merge
        # is refused and each word behind the phrase becomes tappable.
        dictionary = make_dictionary(["của ông"])
        syllables = syllables_of("của ông nói")
        spans = [(0, 2), (2, 3)]
        result = reconcile(syllables, spans, spans, dictionary=dictionary)
        assert (result[0].start, result[0].end) == (0, 1)
        assert result[0].source == SRC_SPLIT

    def test_a_real_compound_containing_a_function_word_is_untouched(self):
        # The guard only fires when *every* syllable is a function word, so words
        # like "không khí" (air) and "trong nước" (domestic) still lock.
        dictionary = make_dictionary(["không khí"])
        syllables = syllables_of("không khí đây")
        spans = [(0, 2), (2, 3)]
        result = reconcile(syllables, spans, spans, dictionary=dictionary)
        assert (result[0].start, result[0].end) == (0, 2)
        assert result[0].source == SRC_DICTIONARY

    def test_without_a_dictionary_a_function_word_run_is_split(self):
        # A boundaries-only or absent dictionary cannot gloss anything, so the
        # conservative reading is the split.
        syllables = syllables_of("của ông")
        result = reconcile(syllables, [(0, 2)], [(0, 2)])
        assert [(s.start, s.end) for s in result] == [(0, 1), (1, 2)]

    def test_a_span_crossing_punctuation_is_never_emitted(self):
        # Two syllables the segmenters call one word, with a paragraph break between
        # them.  The syllable sequence carries no punctuation, so only the caller's
        # predicate can see it.
        syllables = syllables_of("Lâm Chính")
        spans = [(0, 2)]
        result = reconcile(
            syllables, spans, spans, allow_span=lambda start, end: end - start < 2
        )
        assert [(s.start, s.end) for s in result] == [(0, 1), (1, 2)]
        # The forced split is visible; the second syllable is a legal word on its own.
        assert result[0].source == SRC_SPLIT
        assert result[1].source == SRC_AGREED

    def test_the_guard_also_applies_to_a_dictionary_lock(self):
        dictionary = make_dictionary(["đại học"], glosses={"đại học": ("university",)})
        syllables = syllables_of("đại học")
        result = reconcile(
            syllables,
            [(0, 1), (1, 2)],
            [(0, 1), (1, 2)],
            dictionary=dictionary,
            allow_span=lambda start, end: end - start < 2,
        )
        assert [(s.start, s.end) for s in result] == [(0, 1), (1, 2)]

    def test_guard_splits_are_counted(self):
        dictionary = make_dictionary(["của ông"])
        syllables = syllables_of("của ông")
        result = reconcile(syllables, [(0, 2)], [(0, 2)], dictionary=dictionary)
        stats = summarize(result)
        assert stats.split_by_guard == 1
        assert stats.from_dictionary == 0


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
