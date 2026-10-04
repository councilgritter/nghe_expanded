"""Unit tests for the English (Việt→Anh) gloss layer.

The gloss import reads a 79 MB third-party file, so the ordering and filtering rules
are tested against hand-written records rather than the real source.  What matters is
that the *first* gloss — the one the reader shows on a tap — is the sensible one: a
content word's meaning, not a romanisation, and not a "short for something else"
pointer.
"""
from __future__ import annotations

import sqlite3

import pytest

from reading.storage.dictionary import CompoundDictionary
from reading.tools.build_glosses import (
    MAX_SENSES_PER_WORD,
    entry_senses,
    normalize_headword,
)

from .conftest import SCRATCH


class TestEntrySenses:
    def test_content_part_of_speech_beats_character_and_romanization(self):
        record = {
            "word": "vào",
            "senses": [
                {"glosses": ["romanisation of something"]},
            ],
            "pos": "character",
        }
        # A character entry with one sense, then the verb reading.
        verb = {
            "word": "vào",
            "pos": "verb",
            "senses": [{"glosses": ["to enter"]}],
        }
        assert entry_senses(record)[0][2] == "romanisation of something"
        assert entry_senses(verb)[0][2] == "to enter"

    def test_meta_glosses_are_ranked_after_real_ones(self):
        record = {
            "word": "nước",
            "pos": "noun",
            "senses": [
                {"glosses": ["short for đất nước (“country”)"]},
                {"glosses": ["water"]},
            ],
        }
        senses = entry_senses(record)
        assert senses[0][2] == "water"
        assert senses[1][2] == "short for đất nước (“country”)"

    def test_duplicate_glosses_are_collapsed(self):
        record = {
            "word": "đọc",
            "pos": "verb",
            "senses": [{"glosses": ["to read"]}, {"glosses": ["TO READ"]}],
        }
        assert [s[2] for s in entry_senses(record)] == ["to read"]

    def test_senses_are_capped(self):
        record = {
            "word": "nhà",
            "pos": "noun",
            "senses": [{"glosses": [f"sense {i}"]} for i in range(30)],
        }
        assert len(entry_senses(record)) == MAX_SENSES_PER_WORD

    def test_ranks_are_contiguous_from_zero(self):
        record = {
            "word": "nhà",
            "pos": "noun",
            "senses": [{"glosses": ["house", "home", "building"]}],
        }
        assert [s[0] for s in entry_senses(record)] == [0, 1, 2]

    def test_a_record_with_no_senses_yields_nothing(self):
        assert entry_senses({"word": "nhà", "pos": "noun", "senses": []}) == []
        assert entry_senses({"word": "", "pos": "noun", "senses": [{"glosses": ["x"]}]}) == []

    def test_multi_syllable_headword_is_underscored(self):
        record = {
            "word": "thông tin",
            "pos": "noun",
            "senses": [{"glosses": ["information"]}],
        }
        assert entry_senses(record)[0][2] == "information"

    def test_over_long_glosses_are_truncated(self):
        record = {
            "word": "dài",
            "pos": "adj",
            "senses": [{"glosses": ["x" * 500]}],
        }
        gloss = entry_senses(record)[0][2]
        assert len(gloss) <= 200
        assert gloss.endswith("…")

    def test_empty_gloss_strings_are_skipped(self):
        record = {
            "word": "nhà",
            "pos": "noun",
            "senses": [{"glosses": ["", "   ", "house"]}],
        }
        assert [s[2] for s in entry_senses(record)] == ["house"]


class TestNormalizeHeadword:
    def test_case_and_spacing_are_folded(self):
        assert normalize_headword("  Đại  Học ") == "đại_học"

    def test_already_normalised_is_stable(self):
        assert normalize_headword("đại_học") == "đại_học"

    def test_empty_is_empty(self):
        assert normalize_headword("   ") == ""


def make_dict_db(path, *, glosses: bool = True) -> None:
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE entries (
            headword TEXT PRIMARY KEY, display TEXT, syllable_count INTEGER,
            syllables TEXT, definition TEXT, sources TEXT, freq_rank INTEGER
        );
        """
    )
    conn.execute(
        "INSERT INTO entries VALUES ('đại_học','đại học',2,'đại_học',NULL,'Viet74K',NULL)"
    )
    if glosses:
        conn.executescript(
            """
            CREATE TABLE glosses (
                headword TEXT, pos TEXT, rank INTEGER, gloss TEXT,
                tags TEXT, source TEXT
            );
            INSERT INTO glosses VALUES ('đại_học','noun',0,'university','','test');
            INSERT INTO glosses VALUES ('vào','verb',0,'to enter','','test');
            """
        )
    conn.commit()
    conn.close()


@pytest.fixture
def dict_db(tmp_path=None):
    """A tiny dictionary file in the module's scratch dir, removed afterwards.

    Deliberately not pytest's ``tmp_path``: the suite keeps its scratch space inside
    the module so it also runs in sandboxes without a writable temp directory.
    """
    import uuid

    SCRATCH.mkdir(parents=True, exist_ok=True)
    path = SCRATCH / f"dict-{uuid.uuid4().hex}.sqlite3"
    try:
        yield path
    finally:
        path.unlink(missing_ok=True)


class TestLoadingTheDictionaryFile:
    def test_glosses_are_loaded_alongside_boundaries(self, dict_db):
        make_dict_db(dict_db, glosses=True)
        dictionary = CompoundDictionary.from_sqlite(dict_db)
        assert dictionary.primary_gloss("đại_học") == "university"
        assert dictionary.glosses("đại_học") == ("university",)
        assert dictionary.gloss_count == 2

    def test_a_word_can_be_glossed_without_being_a_boundary_headword(self, dict_db):
        """The gloss table is independent of the underthesea word lists."""
        make_dict_db(dict_db, glosses=True)
        dictionary = CompoundDictionary.from_sqlite(dict_db)
        assert dictionary.get("vào") is None
        assert dictionary.primary_gloss("vào") == "to enter"

    def test_an_older_dictionary_without_glosses_still_loads(self, dict_db):
        make_dict_db(dict_db, glosses=False)
        dictionary = CompoundDictionary.from_sqlite(dict_db)
        assert len(dictionary) == 1
        assert dictionary.glosses("đại_học") == ()

    def test_lookup_is_case_and_form_insensitive(self, dict_db):
        make_dict_db(dict_db, glosses=True)
        dictionary = CompoundDictionary.from_sqlite(dict_db)
        assert dictionary.primary_gloss("Đại Học") == "university"
