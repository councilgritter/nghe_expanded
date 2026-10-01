"""Shared fixtures for the reading module's tests.

The dictionary is built from a literal list of headwords rather than the real
73k-entry file, so segmentation expectations are exact and the tests do not
depend on a build artifact being present.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_DIR = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from reading.pipeline.deepseek import DefinitionResult, PreTeachItem, SimplifyResult  # noqa: E402
from reading.pipeline.sources import RawArticle  # noqa: E402
from reading.storage import db  # noqa: E402
from reading.storage.dictionary import CompoundDictionary, DictEntry, normalize_syllable  # noqa: E402

FIXTURES = MODULE_DIR / "fixtures"

# Scratch space for test databases.  Deliberately inside the module rather than the
# system temp dir so the suite works in sandboxes that do not grant writable temp
# directories; it is gitignored and each fixture removes its own file.
SCRATCH = MODULE_DIR / "data" / "_test"



def make_dictionary(phrases, max_syllables: int = 4) -> CompoundDictionary:
    """A :class:`CompoundDictionary` over the given space-separated headwords."""
    entries: dict[str, DictEntry] = {}
    for phrase in phrases:
        syllables = tuple(s for s in (normalize_syllable(p) for p in phrase.split()) if s)
        if not syllables:
            continue
        headword = "_".join(syllables)
        entries[headword] = DictEntry(
            headword=headword,
            display=phrase,
            syllable_count=len(syllables),
            syllables=syllables,
            sources="test",
        )
    return CompoundDictionary(entries, max_syllables=max_syllables)


@pytest.fixture
def fixture_json() -> dict:
    return json.loads((FIXTURES / "article_fixture.json").read_text(encoding="utf-8"))


@pytest.fixture
def test_dictionary(fixture_json) -> CompoundDictionary:
    return make_dictionary(fixture_json["dictionary_entries"])


@pytest.fixture
def conn():
    """A migrated database in a fresh scratch file."""
    import uuid

    SCRATCH.mkdir(parents=True, exist_ok=True)
    path = SCRATCH / f"reading-{uuid.uuid4().hex}.sqlite3"
    connection = db.connect(path)
    db.migrate(connection)
    try:
        yield connection
    finally:
        connection.close()
        for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
            candidate.unlink(missing_ok=True)


@pytest.fixture
def raw_article(fixture_json) -> RawArticle:
    article = fixture_json["article"]
    return RawArticle(
        source=article["source"],
        guid=article["guid"],
        url=article["url"],
        title=article["title"],
        summary=article["original_text"],
        published_at=article["published_at"],
        author=article["author"],
    )


class StubSimplifier:
    """A :class:`reading.pipeline.deepseek.Simplifier` that never touches the network.

    Records its calls so tests can assert that the expensive runtime path was (or
    was not) taken.
    """

    def __init__(self, segmented_text: str, vocab=(), grammar=(), definition: str = ""):
        self.segmented_text = segmented_text
        self.vocab = list(vocab)
        self.grammar = list(grammar)
        self.definition = definition
        self.simplify_calls: list[tuple[str, str]] = []
        self.define_calls: list[tuple[str, str, str | None]] = []

    @classmethod
    def from_fixture(cls, fixture_json: dict, **kwargs) -> "StubSimplifier":
        llm = fixture_json["llm"]
        return cls(
            segmented_text=llm["segmented_text"],
            vocab=[PreTeachItem(**item) for item in llm["vocab"]],
            grammar=[PreTeachItem(**item) for item in llm["grammar"]],
            **kwargs,
        )

    def simplify(self, article_text: str, cefr_level: str) -> SimplifyResult:
        self.simplify_calls.append((article_text, cefr_level))
        return SimplifyResult(
            segmented_text=self.segmented_text,
            vocab=self.vocab,
            grammar=self.grammar,
            model="stub-model",
        )

    def define(self, sentence: str, selection: str, cefr_level: str | None = None) -> DefinitionResult:
        self.define_calls.append((sentence, selection, cefr_level))
        return DefinitionResult(
            form=selection.casefold().replace(" ", "_"),
            definition=self.definition or f"nghĩa của {selection}",
            cefr="A2",
        )


@pytest.fixture
def stub_simplifier(fixture_json) -> StubSimplifier:
    return StubSimplifier.from_fixture(fixture_json)
