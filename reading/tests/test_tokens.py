"""Unit tests for the token-map builder.

The token map is the contract between ingest and the reading page: if an offset is
off by one, the wrong word becomes tappable.  So these tests check the invariants
the frontend relies on — that ``text[start_char:end_char]`` really is the surface,
that ranges never overlap, and that the underscore form matches the span.
"""
from __future__ import annotations

import pytest

from reading.pipeline.reconcile import reconcile
from reading.pipeline.text import counts_to_spans, syllables_with_offsets
from reading.pipeline.tokens import (
    SRC_PRET_EACH,
    Definition,
    build_token_map,
    find_token,
    sentence_around,
)

from .conftest import make_dictionary

TEXT = "Hôm nay trời đẹp. Chúng tôi đi học ở trường đại học quốc gia."


def build(text: str = TEXT, counts=None, definitions=None, dictionary=None):
    """Reconcile an identical segmentation, then build the token map."""
    syllables = syllables_with_offsets(text)
    counts = counts or [1] * len(syllables)
    spans = counts_to_spans(counts)
    reconciled = reconcile(
        [s.text for s in syllables], spans, spans, dictionary=dictionary
    )
    return (
        text,
        syllables,
        build_token_map(
            text,
            syllables,
            reconciled,
            definitions=definitions,
            dictionary=dictionary,
        ),
    )


class TestOffsets:
    def test_surface_matches_its_character_range(self):
        text, _, tokens = build()
        for token in tokens:
            assert token["surface"] == text[token["start_char"] : token["end_char"]]

    def test_ranges_are_ordered_and_never_overlap(self):
        text, _, tokens = build(counts=[2, 2, 1, 2, 1, 1, 1, 2, 1, 2, 2])
        previous_end = -1
        for token in tokens:
            assert token["start_char"] >= previous_end
            previous_end = token["end_char"]

    def test_punctuation_is_never_swallowed(self):
        text, _, tokens = build()
        surfaces = {t["surface"] for t in tokens}
        assert "đẹp." not in surfaces
        assert "đẹp" in surfaces

    def test_spans_include_the_internal_space_of_a_compound(self):
        dictionary = make_dictionary(["đại học"])
        text, _, tokens = build(dictionary=dictionary)
        compound = next(t for t in tokens if t["form"] == "đại_học")
        assert compound["surface"] == "đại học"
        assert compound["syllable_count"] == 2
        assert text[compound["start_char"] : compound["end_char"]] == "đại học"

    def test_ordinals_are_contiguous_from_zero(self):
        _, _, tokens = build()
        assert [t["ordinal"] for t in tokens] == list(range(len(tokens)))


class TestForms:
    def test_form_is_underscore_joined_lowercase(self):
        _, _, tokens = build()
        assert all(t["form"] == t["form"].casefold() for t in tokens)
        assert all(" " not in t["form"] for t in tokens)

    def test_single_syllable_has_no_underscore(self):
        _, _, tokens = build(counts=[1] * 12)
        assert all("_" not in t["form"] for t in tokens)


class TestDefinitions:
    def test_definition_comes_from_the_preteach_map(self):
        definitions = {"đại_học": Definition(text="trường học bậc cao", cefr="A2")}
        dictionary = make_dictionary(["đại học"])
        _, _, tokens = build(definitions=definitions, dictionary=dictionary)
        compound = next(t for t in tokens if t["form"] == "đại_học")
        assert compound["definition"] == "trường học bậc cao"
        assert compound["cefr_level"] == "A2"
        assert compound["definition_src"] == SRC_PRET_EACH

    def test_definition_lookup_matches_an_unaccented_key(self):
        definitions = {"đại_học": Definition(text="trường học bậc cao", cefr="A2")}
        dictionary = make_dictionary(["đại học"])
        _, _, tokens = build(definitions=definitions, dictionary=dictionary)
        assert any(t["definition"] == "trường học bậc cao" for t in tokens)

    def test_undefined_tokens_are_left_null_for_the_runtime_fallback(self):
        _, _, tokens = build()
        assert all(t["definition"] is None for t in tokens)
        assert all(t["definition_src"] == "none" for t in tokens)


class TestAmbiguity:
    def test_ambiguous_flag_and_candidates_are_carried_through(self):
        text = "trí tuệ nhân tạo"
        syllables = syllables_with_offsets(text)
        reconciled = reconcile(
            [s.text for s in syllables], [(0, 2), (2, 4)], [(0, 3), (3, 4)]
        )
        tokens = build_token_map(text, syllables, reconciled)

        first = tokens[0]
        assert first["ambiguous"] is True
        assert first["candidates"] is not None
        assert {c["source"] for c in first["candidates"]} == {"underthesea", "llm"}

    def test_unambiguous_tokens_have_no_candidates(self):
        _, _, tokens = build()
        assert all(t["candidates"] is None for t in tokens)
        assert all(t["ambiguous"] is False for t in tokens)


class TestFindToken:
    def test_picks_the_longest_covering_token(self):
        tokens = [
            {"start_char": 0, "end_char": 3, "syllable_count": 1},
            {"start_char": 0, "end_char": 8, "syllable_count": 3},
        ]
        assert find_token(tokens, 2)["syllable_count"] == 3

    def test_returns_none_outside_every_token(self):
        tokens = [{"start_char": 5, "end_char": 9, "syllable_count": 1}]
        assert find_token(tokens, 20) is None

    def test_find_token_agrees_with_the_built_map(self):
        # A dictionary entry is what produces a multi-syllable token here.
        _, _, tokens = build(dictionary=make_dictionary(["đại học"]))
        compound = next(t for t in tokens if t["syllable_count"] > 1)
        offset = compound["start_char"] + 1
        assert find_token(tokens, offset)["ordinal"] == compound["ordinal"]


class TestSentenceAround:
    text = "Hôm nay trời đẹp. Sinh viên ở thành phố rất đông. Giáo sư giảng bài."

    def test_extracts_the_containing_sentence(self):
        offset = self.text.index("Sinh")
        assert sentence_around(self.text, offset) == "Sinh viên ở thành phố rất đông."

    def test_handles_the_first_and_last_sentences(self):
        assert sentence_around(self.text, 0) == "Hôm nay trời đẹp."
        assert sentence_around(self.text, len(self.text) - 2) == "Giáo sư giảng bài."

    def test_clamps_out_of_range_offsets(self):
        assert sentence_around(self.text, -5) == "Hôm nay trời đẹp."
        assert sentence_around(self.text, 10_000) == "Giáo sư giảng bài."

    def test_empty_text_is_empty(self):
        assert sentence_around("", 0) == ""

    @pytest.mark.parametrize("offset", [0, 5, 20, 40, 60])
    def test_never_returns_the_whole_article_for_a_mid_text_offset(self, offset):
        assert sentence_around(self.text, offset) != ""
