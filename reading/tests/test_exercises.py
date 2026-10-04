"""Tests for the practice content: questions, the writing task, and the cheap backfill.

Exercises are their own DeepSeek call, which is the whole point: an article that is
already simplified and paid for can be given questions without being rewritten.  So
these pin the parsing, the storage shape, the exported bundle, and the refresh path
that re-pays only for the versions whose exercises are missing or stale.
"""
from __future__ import annotations

import json
from dataclasses import replace

from reading.pipeline import prompts
from reading.pipeline.deepseek import (
    Exercises,
    QuizItem,
    WritingTask,
    _quiz_items,
    _writing,
)
from reading.pipeline.exercises import exercises_rows, order_options
from reading.pipeline.extract import bundle_for_version
from reading.pipeline.ingest import generate_exercise_rows, run_exercise_refresh, run_ingest
from reading.settings import settings
from reading.storage import db

from .conftest import StubSimplifier


def run(conn, raw_article, stub, dictionary, **kwargs):
    cfg = kwargs.pop("cfg", replace(settings, fetch_full_text=False, min_body_chars=0))
    return run_ingest(
        conn,
        cefr_level=kwargs.pop("cefr", "B1"),
        client=stub,
        dictionary=dictionary,
        cfg=cfg,
        articles=[raw_article],
        verbose=False,
        **kwargs,
    )


class TestOrdering:
    """The correct answer must not always be A.

    Measured over the first 40 generated questions, 34 had the answer at A — the model
    anchors on the first option, and the prompt's own example showed `"answer": 0`.
    The options are therefore put in a content-determined order at storage time.
    """

    def test_the_same_question_always_gets_the_same_order(self):
        options = ["a", "b", "c", "d"]
        first = order_options("Chuyện gì đã xảy ra?", options, 0)
        second = order_options("Chuyện gì đã xảy ra?", options, 0)
        assert first == second

    def test_the_correct_text_is_still_the_correct_index(self):
        options = ["right", "wrong1", "wrong2", "wrong3"]
        for question in [f"q{i}" for i in range(40)]:
            ordered, answer = order_options(question, options, 0)
            assert ordered[answer] == "right"
            assert sorted(ordered) == sorted(options)

    def test_a_position_biased_set_comes_out_balanced(self):
        options = ["right", "w1", "w2", "w3"]
        answers = [order_options(f"question {i}", options, 0)[1] for i in range(400)]
        # The point is not an exact quarter each; it is that no position is the
        # default, which is what the model produced.
        for position in range(4):
            assert 0.15 < answers.count(position) / len(answers) < 0.35, position

    def test_ordering_an_already_ordered_set_changes_nothing(self):
        # This is what makes the repair tool safe to run twice.  A fixed permutation
        # would shuffle a balanced set straight back to 50% at A.
        options = ["right", "w1", "w2", "w3"]
        ordered, answer = order_options("q", options, 0)
        assert order_options("q", ordered, answer) == (ordered, answer)

    def test_a_single_option_is_left_alone(self):
        assert order_options("q", ["only"], 0) == (["only"], 0)

    def test_an_index_out_of_range_is_left_alone(self):
        assert order_options("q", ["a", "b"], 5) == (["a", "b"], 5)

    def test_rows_go_through_the_ordering(self):
        # Ten questions the model would have answered "A" every time.
        exercises = Exercises(
            questions=[QuizItem(f"câu {i}", ["đúng", "sai 1", "sai 2", "sai 3"], 0) for i in range(10)]
        )
        rows = exercises_rows(exercises)
        positions = {row["answer"] for row in rows}
        assert len(positions) > 1, "every correct answer is still in the same place"
        for row in rows:
            assert row["options"][row["answer"]] == "đúng"


class TestReshuffleTool:
    """The free repair for questions stored before the ordering existed."""

    def _store_biased(self, conn, count=20):
        version_id = db.upsert_version(
            conn, article_id=_article(conn), cefr_level="B1", simplified_text="Trời mưa."
        )
        db.replace_exercises(
            conn,
            version_id,
            [
                {
                    "kind": "mcq",
                    "ordinal": i,
                    "prompt": f"câu {i}",
                    "options": ["đúng", "sai 1", "sai 2", "sai 3"],
                    "answer": 0,
                    "why": "vì",
                }
                for i in range(count)
            ],
        )
        return version_id

    def test_it_moves_the_answers_without_re_generating_anything(self, conn):
        from reading.tools.reshuffle import distribution, reshuffle

        version_id = self._store_biased(conn)
        assert distribution(conn) == {0: 20}

        report = reshuffle(conn, verbose=False)

        # A question whose content-determined order happens to match what was stored
        # is skipped, so "changed" is at most the count.
        assert 10 <= report["changed"] <= 20
        assert len(distribution(conn)) >= 3, "the answers are still bunched in one place"
        # The same texts, and the correct text still at the recorded index.
        for row in conn.execute("SELECT options, answer FROM exercises WHERE kind='mcq'"):
            options = json.loads(row["options"])
            assert sorted(options) == sorted(["đúng", "sai 1", "sai 2", "sai 3"])
            assert options[row["answer"]] == "đúng"
        assert len(db.get_exercises(conn, version_id)["mcq"]) == 20

    def test_a_dry_run_writes_nothing(self, conn):
        from reading.tools.reshuffle import distribution, reshuffle

        self._store_biased(conn, count=8)
        reshuffle(conn, dry_run=True, verbose=False)
        assert distribution(conn) == {0: 8}

    def test_running_it_twice_is_a_no_op(self, conn):
        """The tool must not drift the distribution back when run again."""
        from reading.tools.reshuffle import distribution, reshuffle

        self._store_biased(conn)
        reshuffle(conn, verbose=False)
        balanced = distribution(conn)
        second = reshuffle(conn, verbose=False)
        assert second["changed"] == 0
        assert distribution(conn) == balanced


class TestParsing:
    def test_multiple_choice_questions_keep_their_answer_index(self):
        items = _quiz_items(
            [{"q": "Chuyện gì?", "options": ["a", "b", "c", "d"], "answer": 2, "why": "vì"}],
            with_options=True,
        )
        assert len(items) == 1
        assert items[0].answer == 2
        assert items[0].why == "vì"

    def test_a_question_whose_answer_indexes_nothing_is_dropped(self):
        # Better no question than one that cannot be answered correctly.
        assert _quiz_items([{"q": "x", "options": ["a", "b"], "answer": 5}], with_options=True) == []
        assert _quiz_items([{"q": "x", "options": ["a"]}], with_options=True) == []

    def test_open_questions_accept_a_sample_and_key_points(self):
        items = _quiz_items(
            [{"q": "Vì sao?", "sample": "Vì mưa.", "key_points": "mưa; ướt"}],
            with_options=False,
        )
        assert items[0].sample == "Vì mưa."
        assert items[0].key_points == ["mưa", "ướt"]

    def test_a_writing_task_is_read_from_either_key(self):
        task = _writing({"prompt": "Tóm tắt.", "model_answer": "Bài mẫu.", "min_words": "60"})
        assert (task.prompt, task.model_answer, task.min_words) == ("Tóm tắt.", "Bài mẫu.", 60)
        assert _writing({"q": "Viết đi", "sample": "x"}).model_answer == "x"
        assert _writing(None).prompt == ""


class TestRows:
    def test_rows_are_ordered_and_typed(self):
        rows = exercises_rows(
            Exercises(
                questions=[QuizItem("q1", ["a", "b"], 1), QuizItem("q2", ["a", "b"], 0)],
                short_answers=[QuizItem("s1", sample="mẫu", key_points=["ý"])],
                writing=WritingTask(prompt="viết", key_points=["kp"], model_answer="mẫu", min_words=40),
            )
        )
        assert [r["kind"] for r in rows] == ["mcq", "mcq", "short", "writing"]
        assert [r["ordinal"] for r in rows[:3]] == [0, 1, 0]
        # The options are reordered by content, so the assertion is on the set and on
        # which one is marked correct.
        assert sorted(rows[0]["options"]) == ["a", "b"]
        assert rows[0]["options"][rows[0]["answer"]] == "b"
        assert rows[3]["sample"] == "mẫu" and rows[3]["min_words"] == 40

    def test_an_empty_result_produces_no_rows(self):
        assert exercises_rows(Exercises()) == []

    def test_a_writing_task_without_a_prompt_is_not_a_row(self):
        assert exercises_rows(Exercises(writing=WritingTask(model_answer="mẫu"))) == []


class TestStorage:
    def test_ingest_stores_and_exports_the_practice_content(
        self, conn, raw_article, stub_simplifier, test_dictionary
    ):
        stats = run(conn, raw_article, stub_simplifier, test_dictionary)
        result = stats.results[0]
        assert result.exercises == 3

        stored = db.get_exercises(conn, result.version_id)
        assert len(stored["mcq"]) == 1
        assert len(stored["short"]) == 1
        assert len(stored["writing"]) == 1

        bundle = bundle_for_version(conn, result.version_id, dictionary=test_dictionary)
        exercises = bundle["exercises"]
        mcq = exercises["mcq"][0]
        # The order is permuted at storage time, so the assertion is about the text
        # that is marked correct, not about where it sits.
        assert mcq["options"][mcq["answer"]] == "B"
        assert sorted(mcq["options"]) == ["A", "B", "C", "D"]
        assert exercises["short"][0]["key_points"] == ["trời mưa"]
        assert exercises["writing"]["min_words"] == 40

    def test_the_bundle_carries_the_offline_lookup_index(
        self, conn, raw_article, stub_simplifier, test_dictionary
    ):
        stats = run(conn, raw_article, stub_simplifier, test_dictionary)
        bundle = bundle_for_version(conn, stats.results[0].version_id, dictionary=test_dictionary)
        assert bundle["lookup"], "no lookup index was exported"
        assert all("f" in entry and "s" in entry and "e" in entry for entry in bundle["lookup"])

    def test_exercises_can_be_skipped(self, conn, raw_article, stub_simplifier, test_dictionary):
        stats = run(conn, raw_article, stub_simplifier, test_dictionary, generate_exercises=False)
        result = stats.results[0]
        assert result.exercises == 0
        assert stub_simplifier.exercise_calls == []
        assert db.get_exercises(conn, result.version_id)["mcq"] == []
        # The article itself is unaffected: the practice call is optional.
        assert result.tokens > 0

    def test_a_client_that_cannot_write_exercises_is_not_an_error(
        self, conn, raw_article, test_dictionary, fixture_json
    ):
        class NoExercises(StubSimplifier):
            exercises = None

        stub = NoExercises.from_fixture(fixture_json)
        stats = run(conn, raw_article, stub, test_dictionary)
        assert stats.ingested == 1
        assert stats.results[0].exercises == 0

    def test_an_empty_response_is_recorded_rather_than_stored_as_nothing(
        self, conn, raw_article, stub_simplifier, test_dictionary
    ):
        stub_simplifier.exercise_result = Exercises()
        stats = run(conn, raw_article, stub_simplifier, test_dictionary)
        result = stats.results[0]
        assert stats.ingested == 1
        assert result.exercises == 0
        assert "empty" in result.reason


class TestRefreshExercises:
    """The cheap path: questions for an article that is already paid for."""

    def test_a_current_version_is_skipped(self, conn, raw_article, stub_simplifier, test_dictionary):
        run(conn, raw_article, stub_simplifier, test_dictionary)
        before = len(stub_simplifier.exercise_calls)

        stats = run_exercise_refresh(conn, stub_simplifier, verbose=False)

        assert stats.refreshed == 0
        assert stats.skipped == 1
        assert len(stub_simplifier.exercise_calls) == before

    def test_a_stale_version_is_regenerated_without_re_simplifying(
        self, conn, raw_article, stub_simplifier, test_dictionary
    ):
        run(conn, raw_article, stub_simplifier, test_dictionary)
        conn.execute("UPDATE article_versions SET exercises_prompt_version = 'exercises-v0'")
        conn.commit()
        simplify_calls = len(stub_simplifier.simplify_calls)
        exercise_calls = len(stub_simplifier.exercise_calls)

        stats = run_exercise_refresh(conn, stub_simplifier, verbose=False)

        assert stats.refreshed == 1
        assert stats.rows == 3
        assert len(stub_simplifier.exercise_calls) == exercise_calls + 1
        assert len(stub_simplifier.simplify_calls) == simplify_calls, "exercises re-simplified the article"
        version_id = db.list_articles(conn)[0]["id"]
        row = conn.execute(
            "SELECT exercises_prompt_version FROM article_versions WHERE article_id = ?",
            (version_id,),
        ).fetchone()
        assert row["exercises_prompt_version"] == prompts.EXERCISES_PROMPT_VERSION

    def test_force_regenerates_even_a_current_version(
        self, conn, raw_article, stub_simplifier, test_dictionary
    ):
        run(conn, raw_article, stub_simplifier, test_dictionary)
        stats = run_exercise_refresh(conn, stub_simplifier, force=True, verbose=False)
        assert stats.refreshed == 1

    def test_an_article_with_no_version_is_not_touched(
        self, conn, stub_simplifier, test_dictionary
    ):
        stats = run_exercise_refresh(conn, stub_simplifier, verbose=False)
        assert (stats.refreshed, stats.skipped, stats.failed) == (0, 0, 0)

    def test_a_failure_leaves_the_previous_content_alone(
        self, conn, raw_article, stub_simplifier, test_dictionary
    ):
        run(conn, raw_article, stub_simplifier, test_dictionary)
        version_id = db.list_articles(conn)[0]["id"]
        before = len(db.get_exercises(conn, version_id)["mcq"])

        def boom(*_args, **_kwargs):
            raise RuntimeError("model down")

        stub_simplifier.exercises = boom
        stats = run_exercise_refresh(conn, stub_simplifier, force=True, verbose=False)

        assert stats.failed == 1
        assert len(db.get_exercises(conn, version_id)["mcq"]) == before


class TestCli:
    """The command line, because two flags there decide what gets paid for.

    `--refresh-exercises` with no level has to mean *every* level: a parser default of
    the configured level would silently restrict the run to one, which is the sort of
    bug that only shows up as "why did half the articles get questions".
    """

    def _stub(self, monkeypatch, stub):
        from reading.pipeline import ingest as ingest_module

        monkeypatch.setattr(ingest_module, "DeepSeekClient", lambda cfg: stub)
        return stub

    def test_refresh_exercises_without_a_level_covers_every_level(
        self, conn, raw_article, stub_simplifier, test_dictionary, fixture_json, monkeypatch
    ):
        from .conftest import StubSimplifier
        from reading.pipeline.ingest import main

        run(conn, raw_article, StubSimplifier.from_fixture(fixture_json), test_dictionary, cefr="A2")
        run(conn, raw_article, StubSimplifier.from_fixture(fixture_json), test_dictionary, cefr="B1")
        conn.execute("UPDATE article_versions SET exercises_prompt_version = 'exercises-v0'")
        conn.commit()

        stub = self._stub(monkeypatch, stub_simplifier)
        db_path = conn.execute("PRAGMA database_list").fetchone()[2]
        assert main(["--refresh-exercises", "--db", db_path]) == 0

        assert len(stub.exercise_calls) == 2, "a level was skipped"
        assert all(
            row["exercises_prompt_version"] == prompts.EXERCISES_PROMPT_VERSION
            for row in conn.execute("SELECT exercises_prompt_version FROM article_versions")
        )

    def test_an_explicit_level_is_still_honoured(
        self, conn, raw_article, stub_simplifier, test_dictionary, fixture_json, monkeypatch
    ):
        from .conftest import StubSimplifier
        from reading.pipeline.ingest import main

        run(conn, raw_article, StubSimplifier.from_fixture(fixture_json), test_dictionary, cefr="A2")
        run(conn, raw_article, StubSimplifier.from_fixture(fixture_json), test_dictionary, cefr="B1")
        conn.execute("UPDATE article_versions SET exercises_prompt_version = 'exercises-v0'")
        conn.commit()

        stub = self._stub(monkeypatch, stub_simplifier)
        db_path = conn.execute("PRAGMA database_list").fetchone()[2]
        main(["--refresh-exercises", "--cefr", "A2", "--db", db_path])

        assert len(stub.exercise_calls) == 1
        levels = [
            row["cefr_level"]
            for row in conn.execute(
                "SELECT cefr_level FROM article_versions WHERE exercises_prompt_version = ?",
                (prompts.EXERCISES_PROMPT_VERSION,),
            )
        ]
        assert levels == ["A2"]


class TestGenerateDirect:
    def test_rows_are_written_for_a_stored_text(self, conn, stub_simplifier):
        """The helper the refresh path uses, exercised on its own."""
        version_id = db.upsert_version(
            conn, article_id=_article(conn), cefr_level="B1", simplified_text="Trời mưa."
        )
        written = generate_exercise_rows(conn, version_id, "Trời mưa.", "B1", stub_simplifier)
        assert written == 3
        assert db.exercises_prompt_version(conn, version_id) == prompts.EXERCISES_PROMPT_VERSION


def _article(conn) -> int:
    return db.insert_article(
        conn,
        db.ArticleRecord(
            source="voa",
            guid="exercises-direct",
            source_url="https://example.test/a",
            title="Bài",
            original_text="Trời mưa.",
            attribution="Nguồn: VOA",
        ),
    )
