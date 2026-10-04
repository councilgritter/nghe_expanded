"""Integration test: one fixture article through the whole ingest pipeline.

DeepSeek is stubbed, so this exercises every real component — RSS record handling,
underscore parsing, underthesea segmentation, dictionary-constrained
reconciliation, token-map building, definitions and persistence — without a network
call or an API key.  That is the point: the pipeline's own logic is under test, not
the model's.
"""
from __future__ import annotations

from dataclasses import replace

from reading.pipeline.ingest import QUALITY_OK, run_ingest
from reading.pipeline.resolve import SOURCE_CACHE, SOURCE_LLM, SOURCE_STORED, resolve_offset
from reading.settings import settings
from reading.storage import db


def run(conn, raw_article, stub_simplifier, test_dictionary, **kwargs):
    cfg = replace(settings, fetch_full_text=False)
    return run_ingest(
        conn,
        cefr_level=kwargs.pop("cefr", "B1"),
        client=stub_simplifier,
        dictionary=test_dictionary,
        cfg=cfg,
        articles=[raw_article],
        verbose=False,
        **kwargs,
    )

class TestHappyPath:
    def test_article_version_tokens_and_preteach_are_persisted(
        self, conn, raw_article, stub_simplifier, test_dictionary, fixture_json
    ):
        stats = run(conn, raw_article, stub_simplifier, test_dictionary)

        assert stats.ingested == 1
        assert stats.failed == 0
        result = stats.results[0]
        assert result.quality == QUALITY_OK
        assert result.version_id is not None
        assert result.tokens > 0

        article = db.get_article(conn, result.article_id)
        assert article["title"] == fixture_json["article"]["title"]
        assert article["body_source"] == "rss"
        # Attribution is a licence obligation, so it must survive to storage.
        assert "VOA" in article["attribution"]

        version = db.get_version(conn, result.article_id, "B1")
        assert version is not None
        assert version["model"] == "stub-model"
        assert version["prompt_version"]
        assert version["quality"] == QUALITY_OK

        preteach = db.get_preteach(conn, result.version_id)
        assert len(preteach["vocab"]) == 3
        assert len(preteach["grammar"]) == 2
        assert {row["term"] for row in preteach["vocab"]} == {
            "đại_học",
            "thành_phố",
            "kinh_tế",
        }

    def test_token_offsets_address_the_stored_text_exactly(
        self, conn, raw_article, stub_simplifier, test_dictionary
    ):
        stats = run(conn, raw_article, stub_simplifier, test_dictionary)
        result = stats.results[0]
        version = db.get_version(conn, result.article_id, "B1")
        text = version["simplified_text"]
        tokens = db.get_tokens(conn, result.version_id)

        assert tokens, "no tokens were stored"
        for token in tokens:
            assert token["surface"] == text[token["start_char"] : token["end_char"]]
            assert token["start_char"] < token["end_char"]

        # Ranges are ordered and non-overlapping.
        previous_end = -1
        for token in tokens:
            assert token["start_char"] >= previous_end
            previous_end = token["end_char"]

    def test_stored_text_has_no_leftover_underscores(
        self, conn, raw_article, stub_simplifier, test_dictionary
    ):
        stats = run(conn, raw_article, stub_simplifier, test_dictionary)
        version = db.get_version(conn, stats.results[0].article_id, "B1")
        assert "_" not in version["simplified_text"]

    def test_dictionary_compounds_win_and_are_tappable(
        self, conn, raw_article, stub_simplifier, test_dictionary
    ):
        stats = run(conn, raw_article, stub_simplifier, test_dictionary)
        tokens = db.get_tokens(conn, stats.results[0].version_id)
        by_form = {t["form"]: t for t in tokens}

        for form in ("đại_học", "thành_phố", "quốc_gia", "sinh_viên", "giáo_sư", "kinh_tế"):
            assert form in by_form, f"{form} was not kept as one word"
            assert by_form[form]["syllable_count"] == 2
            assert by_form[form]["ambiguous"] == 0

    def test_the_model_is_asked_once_per_article(
        self, conn, raw_article, stub_simplifier, test_dictionary
    ):
        run(conn, raw_article, stub_simplifier, test_dictionary)
        assert len(stub_simplifier.simplify_calls) == 1
        _, level = stub_simplifier.simplify_calls[0]
        assert level == "B1"


class TestDeduplication:
    def test_a_second_run_skips_the_article(
        self, conn, raw_article, stub_simplifier, test_dictionary
    ):
        first = run(conn, raw_article, stub_simplifier, test_dictionary)
        second = run(conn, raw_article, stub_simplifier, test_dictionary)

        assert first.ingested == 1
        assert second.ingested == 0
        assert second.duplicates == 1
        # The article was not re-simplified, so no second model call.
        assert len(stub_simplifier.simplify_calls) == 1
        assert len(db.list_articles(conn)) == 1

    def test_the_same_article_can_be_generated_at_several_levels(
        self, conn, raw_article, test_dictionary, fixture_json
    ):
        """Deduplication is per (article, level), not per article."""
        from .conftest import StubSimplifier

        run(conn, raw_article, StubSimplifier.from_fixture(fixture_json), test_dictionary, cefr="A2")
        run(conn, raw_article, StubSimplifier.from_fixture(fixture_json), test_dictionary, cefr="B1")

        assert len(db.list_articles(conn)) == 1, "the article was stored twice"
        article_id = db.list_articles(conn)[0]["id"]
        assert db.get_version(conn, article_id, "A2") is not None
        assert db.get_version(conn, article_id, "B1") is not None

    def test_a_second_run_at_the_same_level_is_the_only_duplicate(
        self, conn, raw_article, stub_simplifier, test_dictionary
    ):
        run(conn, raw_article, stub_simplifier, test_dictionary, cefr="B1")
        again = run(conn, raw_article, stub_simplifier, test_dictionary, cefr="B1")
        assert again.ingested == 0
        assert again.duplicates == 1
        assert len(db.list_articles(conn)) == 1

    def test_refresh_regenerates_the_version_in_place(
        self, conn, raw_article, stub_simplifier, test_dictionary
    ):
        first = run(conn, raw_article, stub_simplifier, test_dictionary, cefr="B1")
        version_id = first.results[0].version_id
        tokens_before = len(db.get_tokens(conn, version_id))

        refreshed = run(conn, raw_article, stub_simplifier, test_dictionary, cefr="B1", refresh=True)

        assert refreshed.ingested == 1
        assert refreshed.results[0].version_id == version_id, "refresh created a new version"
        assert len(db.get_tokens(conn, version_id)) == tokens_before
        assert len(db.list_articles(conn)) == 1

    def test_refresh_leaves_never_ingested_articles_alone(
        self, conn, raw_article, stub_simplifier, test_dictionary, fixture_json
    ):
        """A refresh must stay bounded by what was already paid for.

        Treating the whole feed as new would let `--refresh` with no `--limit` spend on
        every feed item, which is the opposite of what the flag means.
        """
        from .conftest import StubSimplifier

        run(conn, raw_article, stub_simplifier, test_dictionary, cefr="B1")
        never_ingested = replace(raw_article, guid="fixture-never", title="Chưa từng nhập")

        refreshed = run_ingest(
            conn,
            cefr_level="B1",
            client=StubSimplifier.from_fixture(fixture_json),
            dictionary=test_dictionary,
            cfg=replace(settings, fetch_full_text=False),
            articles=[raw_article, never_ingested],
            verbose=False,
            refresh=True,
        )

        assert refreshed.ingested == 1, "refresh touched an article that was never ingested"
        assert len(db.list_articles(conn)) == 1
        assert db.list_articles(conn)[0]["guid"] == raw_article.guid


class TestIngestIsResilient:
    def test_an_empty_module_response_is_a_failure_not_a_crash(
        self, conn, raw_article, test_dictionary, fixture_json
    ):
        from .conftest import StubSimplifier

        empty = StubSimplifier(segmented_text="")
        stats = run(conn, raw_article, empty, test_dictionary)
        assert stats.ingested == 0
        assert stats.failed == 1
        assert "no readable syllables" in stats.results[0].reason
        assert db.list_articles(conn) == []

    def test_runs_without_a_dictionary(
        self, conn, raw_article, stub_simplifier
    ):
        stats = run(conn, raw_article, stub_simplifier, None)
        assert stats.ingested == 1
        assert stats.results[0].tokens > 0


class TestRuntimeResolution:
    def _ingest(self, conn, raw_article, stub_simplifier, test_dictionary):
        stats = run(conn, raw_article, stub_simplifier, test_dictionary)
        result = stats.results[0]
        version = db.get_version(conn, result.article_id, "B1")
        return result, version

    def test_a_stored_definition_resolves_with_no_model_call(
        self, conn, raw_article, stub_simplifier, test_dictionary
    ):
        result, version = self._ingest(conn, raw_article, stub_simplifier, test_dictionary)
        tokens = db.get_tokens(conn, result.version_id)
        defined = next(t for t in tokens if t["definition"])
        offset = defined["start_char"]

        resolution = resolve_offset(
            conn,
            result.version_id,
            offset,
            version["simplified_text"],
            client=stub_simplifier,
            cefr_level="B1",
        )
        assert resolution.source == SOURCE_STORED
        assert resolution.definition
        assert stub_simplifier.define_calls == []

    def test_an_undefined_word_calls_the_model_once_then_caches(
        self, conn, raw_article, stub_simplifier, test_dictionary
    ):
        result, version = self._ingest(conn, raw_article, stub_simplifier, test_dictionary)
        tokens = db.get_tokens(conn, result.version_id)
        # "Hôm" / "nay" are single syllables with no pre-teach gloss.
        undefined = next(t for t in tokens if not t["definition"])
        offset = undefined["start_char"]

        first = resolve_offset(
            conn,
            result.version_id,
            offset,
            version["simplified_text"],
            client=stub_simplifier,
            cefr_level="B1",
        )
        assert first.source == SOURCE_LLM
        assert len(stub_simplifier.define_calls) == 1

        # The context that was sent must be a sentence, not the whole article.
        sentence, selection, _ = stub_simplifier.define_calls[0]
        assert selection
        assert len(sentence) <= len(version["simplified_text"])

        second = resolve_offset(
            conn,
            result.version_id,
            offset,
            version["simplified_text"],
            client=stub_simplifier,
            cefr_level="B1",
        )
        assert second.source == SOURCE_CACHE
        assert second.definition == first.definition
        assert len(stub_simplifier.define_calls) == 1, "cache miss on the second lookup"

    def test_local_only_mode_never_calls_the_model(
        self, conn, raw_article, stub_simplifier, test_dictionary
    ):
        result, version = self._ingest(conn, raw_article, stub_simplifier, test_dictionary)
        tokens = db.get_tokens(conn, result.version_id)
        undefined = next(t for t in tokens if not t["definition"])

        resolution = resolve_offset(
            conn,
            result.version_id,
            undefined["start_char"],
            version["simplified_text"],
            client=stub_simplifier,
            cefr_level="B1",
            allow_llm=False,
        )
        assert resolution.source == "none"
        assert stub_simplifier.define_calls == []

    def test_offset_with_no_token_falls_back_to_a_sentence_window(
        self, conn, raw_article, stub_simplifier, test_dictionary
    ):
        result, version = self._ingest(conn, raw_article, stub_simplifier, test_dictionary)
        text = version["simplified_text"]
        # A space is inside no token but inside a sentence.
        offset = text.index(" ")
        resolution = resolve_offset(
            conn, result.version_id, offset, text, client=stub_simplifier, cefr_level="B1"
        )
        assert resolution.surface  # the chunk under the offset was recovered
        assert resolution.definition
