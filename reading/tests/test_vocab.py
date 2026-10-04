"""Tests for pre-teach normalisation.

The lookup key and the displayed prose have opposite requirements: `term` must keep
the underscore form so the token map can find it, while glosses and examples are read
by a learner and must not carry the segmentation convention.
"""
from __future__ import annotations

from reading.pipeline.deepseek import PreTeachItem
from reading.pipeline.vocab import definitions_from_preteach, normalize_term, plain, preteach_rows


class TestNormalizeTerm:
    def test_keeps_an_underscore_form(self):
        assert normalize_term("tấn_công") == "tấn_công"

    def test_converts_spaces_to_underscores(self):
        assert normalize_term("tấn công") == "tấn_công"

    def test_is_case_insensitive(self):
        assert normalize_term("Tấn Công") == "tấn_công"


class TestPlain:
    def test_strips_the_underscore_convention(self):
        assert plain("hành_động dùng vũ_lực") == "hành động dùng vũ lực"

    def test_leaves_ordinary_prose_alone(self):
        assert plain("một nơi nào đó") == "một nơi nào đó"

    def test_squeezes_whitespace(self):
        assert plain("  a_b   c  ") == "a b c"


class TestPreteachRows:
    def test_term_keeps_underscores_but_the_gloss_does_not(self):
        rows = preteach_rows(
            [PreTeachItem(term="tấn_công", gloss="dùng vũ_lực để đánh", cefr="a2")],
            [],
        )
        assert rows[0]["term"] == "tấn_công"
        assert rows[0]["gloss"] == "dùng vũ lực để đánh"
        assert rows[0]["cefr_level"] == "A2"

    def test_examples_are_cleaned_too(self):
        rows = preteach_rows(
            [],
            [PreTeachItem(term="câu_bị_động", gloss="x", example="cây_cầu bị phá")],
        )
        assert rows[0]["kind"] == "grammar"
        assert rows[0]["example"] == "cây cầu bị phá"

    def test_duplicate_terms_are_dropped(self):
        rows = preteach_rows(
            [PreTeachItem(term="a_b", gloss="1"), PreTeachItem(term="a b", gloss="2")],
            [],
        )
        assert len(rows) == 1

    def test_blank_terms_are_skipped(self):
        assert preteach_rows([PreTeachItem(term="  ", gloss="x")], []) == []

    def test_ordinals_are_contiguous_per_kind(self):
        rows = preteach_rows(
            [PreTeachItem(term=f"t{i}") for i in range(3)],
            [PreTeachItem(term=f"g{i}") for i in range(2)],
        )
        vocab = [r["ordinal"] for r in rows if r["kind"] == "vocab"]
        grammar = [r["ordinal"] for r in rows if r["kind"] == "grammar"]
        assert vocab == [0, 1, 2]
        assert grammar == [0, 1]


class TestDefinitionsFromPreteach:
    def test_keyed_by_underscore_form_with_clean_prose(self):
        found = definitions_from_preteach(
            [PreTeachItem(term="tấn công", gloss="dùng vũ_lực", cefr="A2")]
        )
        assert "tấn_công" in found
        assert found["tấn_công"].text == "dùng vũ lực"

    def test_a_glossless_item_contributes_nothing(self):
        assert definitions_from_preteach([PreTeachItem(term="tấn_công", gloss="")]) == {}

    def test_the_first_gloss_wins(self):
        found = definitions_from_preteach(
            [PreTeachItem(term="a_b", gloss="first"), PreTeachItem(term="a_b", gloss="second")]
        )
        assert found["a_b"].text == "first"
