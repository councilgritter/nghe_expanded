"""Tests for the offline lookup index.

The index is what lets a reader highlight any run of words — including a run the
segmenter never produced a token for — and get a meaning without a network call.  So
these pin its contents (dictionary spans only, glosses attached), its ordering (the
reader searches it by syllable range) and its behaviour with no dictionary at all.
"""
from __future__ import annotations

from reading.pipeline.lookup import build_lookup_index
from reading.pipeline.text import syllables_with_offsets

from .conftest import make_dictionary


def index_for(text: str, dictionary):
    return build_lookup_index(syllables_with_offsets(text), dictionary)


def keyed(index):
    return {f"{entry['s']}:{entry['e']}": entry for entry in index}


class TestContents:
    def test_a_known_compound_is_indexed_with_its_gloss(self):
        dictionary = make_dictionary(["đại học"], glosses={"đại học": ("university", "college")})
        entries = keyed(index_for("ở trường đại học", dictionary))
        assert "2:4" in entries
        assert entries["2:4"]["f"] == "đại_học"
        assert entries["2:4"]["en"] == "university"
        assert entries["2:4"]["senses"] == ["college"]
        assert entries["2:4"]["n"] == 2

    def test_a_known_compound_without_a_gloss_is_still_indexed(self):
        # The reader can then say "the dictionary treats this as one word and has no
        # meaning for it", which is a different answer from "this is not a word".
        dictionary = make_dictionary(["Bắc Đại Tây Dương"])
        entries = keyed(index_for("phía Bắc Đại Tây Dương", dictionary))
        entry = next(e for e in entries.values() if e["f"].startswith("bắc"))
        assert "en" not in entry
        assert entry["n"] == 4

    def test_single_syllables_are_not_indexed(self):
        # They are already tokens; the index exists for spans that are not.
        dictionary = make_dictionary(["học"], glosses={"học": ("to study",)})
        assert index_for("đi học", dictionary) == []

    def test_a_glossed_form_wins_over_the_boundary_layer(self):
        # A word can be glossed without being a boundary headword, which is how a
        # word the segmenter produced still gets a meaning.
        dictionary = make_dictionary([], glosses={"thành phố": ("city",)})
        entries = keyed(index_for("ở thành phố", dictionary))
        assert entries["1:3"]["en"] == "city"

    def test_a_span_the_dictionary_does_not_know_is_absent(self):
        dictionary = make_dictionary(["đại học"], glosses={"đại học": ("university",)})
        entries = keyed(index_for("trời đẹp hôm nay", dictionary))
        assert entries == {}


class TestShape:
    def test_entries_are_sorted_by_syllable_range(self):
        dictionary = make_dictionary(
            ["đại học", "thành phố", "giáo sư"],
            glosses={"đại học": ("university",), "thành phố": ("city",), "giáo sư": ("professor",)},
        )
        index = index_for("giáo sư ở thành phố đại học", dictionary)
        ranges = [(e["s"], e["e"]) for e in index]
        assert ranges == sorted(ranges)

    def test_overlapping_spans_are_all_kept(self):
        # A 3-syllable word and its 2-syllable prefix can both be real; the reader
        # picks by the range that was highlighted, so both have to be there.
        dictionary = make_dictionary(
            ["đại học quốc gia", "đại học"],
            glosses={"đại học quốc gia": ("national university",), "đại học": ("university",)},
        )
        entries = keyed(index_for("đại học quốc gia", dictionary))
        assert entries["0:2"]["en"] == "university"
        assert entries["0:4"]["en"] == "national university"

    def test_no_dictionary_means_no_index(self):
        assert build_lookup_index(syllables_with_offsets("đại học quốc gia"), None) == []

    def test_a_single_syllable_article_is_empty(self):
        dictionary = make_dictionary(["đại học"], glosses={"đại học": ("university",)})
        assert index_for("học", dictionary) == []
