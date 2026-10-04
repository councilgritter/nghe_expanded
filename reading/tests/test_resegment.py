"""Tests for the free repair pass over stored token maps.

`tools/resegment.py` exists because the guards live in the reconciler, not in the
model: an article already in the database can have its token boundaries re-derived
without paying DeepSeek for a rewrite. What it must never do is *lose* something in the
process — the stored map is also the record of the model's own segmentation (ambiguity
flags and the alternative readings a reader can widen to), and a map that does not tile
the text would break every offset after the gap.
"""
from __future__ import annotations

import json

from reading.pipeline.text import syllables_with_offsets
from reading.storage import db
from reading.tools.resegment import resegment_version, run_resegment

from .conftest import make_dictionary

TEXT = "của ông nói. Chúng tôi đi học."
#  syllables: 0 của · 1 ông · 2 nói · 3 Chúng · 4 tôi · 5 đi · 6 học


def store(conn, text, tokens) -> int:
    """A version plus a hand-written token map, as an older run would have left it."""
    article_id = db.insert_article(
        conn,
        db.ArticleRecord(
            source="voa",
            guid=f"resegment-{abs(hash((text, len(tokens)))) % 10**6}",
            source_url="https://example.test/a",
            title="Bài",
            original_text=text,
            attribution="Nguồn: VOA",
        ),
    )
    version_id = db.upsert_version(
        conn, article_id=article_id, cefr_level="B1", simplified_text=text
    )
    db.replace_tokens(conn, version_id, tokens)
    return version_id


def spans(text, specs):
    """Token rows from ``(syl_start, syl_end, form[, extra])``.

    Spans are given in *syllables*, not characters: the offsets have to line up with
    the syllable sequence exactly, because that is the invariant the repair pass works
    from — a hand-written character range that starts inside a space would be a broken
    fixture rather than a finding.
    """
    syllables = syllables_with_offsets(text)
    rows = []
    for ordinal, spec in enumerate(specs):
        start_syl, end_syl, form = spec[0], spec[1], spec[2]
        extra = spec[3] if len(spec) > 3 else {}
        start = syllables[start_syl].start
        end = syllables[end_syl - 1].end
        rows.append(
            {
                "ordinal": ordinal,
                "surface": text[start:end],
                "form": form,
                "start_char": start,
                "end_char": end,
                "syllable_count": extra.get("syllable_count", end_syl - start_syl),
                "ambiguous": extra.get("ambiguous", False),
                "candidates": extra.get("candidates"),
                "definition_en": extra.get("definition_en"),
                "definition_vi": None,
                "definition": extra.get("definition_en"),
                "definition_src": "dictionary" if extra.get("definition_en") else "none",
                "cefr_level": None,
                "senses_en": None,
            }
        )
    return rows


def forms(conn, version_id):
    return [t["form"] for t in db.get_tokens(conn, version_id)]


def covers(conn, version_id, text):
    """Every token is inside the text, and together they tile it exactly."""
    tokens = db.get_tokens(conn, version_id)
    assert tokens
    cursor = 0
    for token in tokens:
        assert token["surface"] == text[token["start_char"] : token["end_char"]]
        assert token["start_char"] >= cursor, "overlapping or out-of-order tokens"
        cursor = token["end_char"]


class TestRepair:
    def test_an_unglossed_function_word_run_is_split(self, conn):
        dictionary = make_dictionary(["của ông"])
        # "của ông" (0-7) was locked; "nói." then "Chúng tôi" then "đi học".
        version_id = store(
            conn,
            TEXT,
            spans(
                TEXT,
                [
                    (0, 2, "của_ông"),
                    (2, 3, "nói"),
                    (3, 5, "chúng_tôi"),
                    (5, 7, "đi_học"),
                ],
            ),
        )
        stats = resegment_version(conn, version_id, TEXT, dictionary, {})
        assert stats.changed
        assert "của" in forms(conn, version_id)
        assert "ông" in forms(conn, version_id)
        assert "của_ông" not in forms(conn, version_id)
        covers(conn, version_id, TEXT)

    def test_a_glossed_function_word_run_is_left_alone(self, conn):
        dictionary = make_dictionary(
            ["chúng tôi"], glosses={"chúng tôi": ("we",)}
        )
        version_id = store(
            conn,
            TEXT,
            spans(
                TEXT,
                [
                    (0, 2, "của_ông"),
                    (2, 3, "nói"),
                    (3, 5, "chúng_tôi", {"definition_en": "we"}),
                    (5, 7, "đi_học"),
                ],
            ),
        )
        resegment_version(conn, version_id, TEXT, dictionary, {})
        assert "chúng_tôi" in forms(conn, version_id)

    def test_a_token_crossing_punctuation_is_split(self, conn):
        # "nói. Chúng" as one token: the offset slice contains a full stop and a space.
        version_id = store(
            conn,
            TEXT,
            spans(
                TEXT,
                [
                    (0, 2, "của_ông"),
                    (2, 4, "nói_chúng"),
                    (4, 7, "tôi_đi_học"),
                ],
            ),
        )
        resegment_version(conn, version_id, TEXT, make_dictionary([]), {})
        stored = forms(conn, version_id)
        assert "nói_chúng" not in stored
        assert "nói" in stored and "chúng" in stored
        covers(conn, version_id, TEXT)

    def test_an_accepted_token_keeps_its_ambiguity_and_candidates(self, conn):
        candidates = [
            {"source": "underthesea", "form": "chúng_tôi", "syllables": 2},
            {"source": "llm", "form": "chúng_tôi_đi", "syllables": 3},
        ]
        version_id = store(
            conn,
            TEXT,
            spans(
                TEXT,
                [
                    (0, 2, "của_ông"),
                    (2, 3, "nói"),
                    (3, 5, "chúng_tôi", {"ambiguous": True, "candidates": candidates}),
                    (5, 7, "đi_học"),
                ],
            ),
        )
        resegment_version(
            conn,
            version_id,
            TEXT,
            make_dictionary(["chúng tôi"], glosses={"chúng tôi": ("we",)}),
            {},
        )
        token = next(t for t in db.get_tokens(conn, version_id) if t["form"] == "chúng_tôi")
        assert token["ambiguous"] == 1
        assert json.loads(token["candidates"]) == candidates

    def test_a_dry_run_writes_nothing(self, conn):
        dictionary = make_dictionary(["của ông"])
        version_id = store(
            conn,
            TEXT,
            spans(TEXT, [(0, 2, "của_ông"), (2, 7, "nói")]),
        )
        before = forms(conn, version_id)
        stats = resegment_version(conn, version_id, TEXT, dictionary, {}, dry_run=True)
        assert stats.changed
        assert forms(conn, version_id) == before

    def test_a_second_run_is_a_no_op(self, conn):
        dictionary = make_dictionary(["của ông"])
        version_id = store(
            conn,
            TEXT,
            spans(TEXT, [(0, 2, "của_ông"), (2, 3, "nói"), (3, 5, "chúng_tôi"), (5, 7, "đi_học")]),
        )
        first = resegment_version(conn, version_id, TEXT, dictionary, {})
        assert first.changed
        second = resegment_version(conn, version_id, TEXT, dictionary, {})
        assert not second.changed

    def test_a_corrupt_stored_map_is_left_alone_rather_than_half_repaired(self, conn):
        """A map that does not tile the text would break every offset after the gap."""
        dictionary = make_dictionary(["của ông"])
        # Syllables 2-4 are covered by no stored token at all.
        rows = spans(TEXT, [(0, 2, "của_ông"), (5, 7, "đi_học")])
        version_id = store(conn, TEXT, rows)
        before = forms(conn, version_id)
        stats = resegment_version(conn, version_id, TEXT, dictionary, {})
        assert stats.skipped_reason
        assert forms(conn, version_id) == before


class TestRun:
    def test_it_reports_the_versions_it_touched(self, conn):
        dictionary = make_dictionary(["của ông"])
        store(conn, TEXT, spans(TEXT, [(0, 2, "của_ông"), (2, 7, "nói")]))
        stats = run_resegment(conn, dictionary, verbose=False)
        assert len(stats) == 1
        assert stats[0].changed

    def test_it_can_be_limited_to_one_level(self, conn):
        dictionary = make_dictionary(["của ông"])
        version_id = store(conn, TEXT, spans(TEXT, [(0, 2, "của_ông"), (2, 7, "nói")]))
        conn.execute("UPDATE article_versions SET cefr_level = 'B2' WHERE id = ?", (version_id,))
        conn.commit()
        assert run_resegment(conn, dictionary, cefr_level="B1", verbose=False) == []
        assert len(run_resegment(conn, dictionary, cefr_level="B2", verbose=False)) == 1
