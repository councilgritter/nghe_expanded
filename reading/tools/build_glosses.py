"""Import English (Việt→Anh) glosses into the compound dictionary.

    python -m reading.tools.build_dictionary     # boundaries first
    python -m reading.tools.build_glosses        # then the English layer

The gloss source is the machine-readable **Vietnamese dictionary derived from
English Wiktionary** published by kaikki.org (wiktextract).  It is the practical
offline option: ~52k Vietnamese headwords, ~89% of them carrying at least one
English gloss, one small JSONL file, and a licence (CC BY-SA 4.0) that only asks
for attribution — which the reader page and the README both carry.

Why not just ask the model for English glosses?  Because a dictionary lookup costs
nothing per word and does not drift between runs.  The model's Vietnamese pre-teach
glosses are still stored (``tokens.definition_vi``); this layer is the English half,
and it is available for every word the segmenter produced rather than only the
handful the model chose to pre-teach.

The glosses are written to a ``glosses`` table *alongside* ``entries`` rather than
into ``entries.definition``.  They are keyed by the same normalised form but are
deliberately not restricted to the boundary headwords, so a word can still be
glossed when the underthesea word lists happen not to contain it.  Re-running this
tool is safe and idempotent: it replaces the ``glosses`` table wholesale.

Licence note: the source data is Wiktionary, CC BY-SA 4.0.  If you redistribute the
exported bundles, the attribution rendered on the reading page and in the README
must travel with them.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from reading.settings import settings  # noqa: E402
from reading.storage.dictionary import UNDERSCORE, normalize_syllable  # noqa: E402

# Where the kaikki.org Vietnamese extract lives, and where we cache it locally.
# The direct JSONL is marked DEPRECATED on kaikki.org (it will eventually give way
# to the raw wiktextract dumps), so the URL is a flag, not a constant to hardcode
# in a calling script.
DEFAULT_SOURCE_URL = (
    "https://kaikki.org/dictionary/Vietnamese/kaikki.org-dictionary-Vietnamese.jsonl"
)
DEFAULT_CACHE = Path(__file__).resolve().parents[1] / "data" / "_dict_src" / "kaikki-vi.jsonl"
SOURCE_LICENCE = "Wiktionary (CC BY-SA 4.0), via kaikki.org / wiktextract"

# Bound the stored senses: the reader shows at most a few, and an unbounded list
# would bloat the dictionary for no benefit.
MAX_SENSES_PER_WORD = 8
MAX_GLOSS_CHARS = 200

GLOSS_SCHEMA = """
CREATE TABLE IF NOT EXISTS glosses (
    headword        TEXT NOT NULL,      -- normalised underscore form
    pos             TEXT NOT NULL DEFAULT '',
    rank            INTEGER NOT NULL DEFAULT 0,  -- display order, most likely first
    gloss           TEXT NOT NULL,      -- English sense
    tags            TEXT NOT NULL DEFAULT '',
    source          TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_glosses_headword ON glosses (headword, rank);
"""

# Part-of-speech display order.  Content words first: a tap on "vào" should read
# "to enter", not a romanisation or a character entry.  Anything unlisted sorts
# after these, keeping its original order.
POS_ORDER = (
    "noun", "verb", "adj", "adv", "prep", "phrase", "pron", "classifier",
    "conj", "num", "det", "particle", "intj", "prefix", "suffix", "affix",
    "proverb", "name",
)
# Entries that describe another headword rather than a meaning of this one; they
# are kept (they can be genuinely useful) but never offered as the primary sense.
META_GLOSS = re.compile(
    r"^(?:short for|alternative (?:form|spelling)|misspelling|see |synonym of|"
    r"used to transliterate|romanization|clipping of|ellipsis of)\b",
    re.IGNORECASE,
)


def normalize_headword(word: str) -> str:
    """The key a token's ``form`` is compared against."""
    syllables = [s for s in (normalize_syllable(p) for p in word.split()) if s]
    return UNDERSCORE.join(syllables)


def _pos_rank(pos: str) -> int:
    try:
        return POS_ORDER.index(pos)
    except ValueError:
        return len(POS_ORDER)


def _clean(gloss: str) -> str:
    text = unicodedata.normalize("NFC", gloss)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > MAX_GLOSS_CHARS:
        text = text[: MAX_GLOSS_CHARS - 1].rstrip() + "…"
    return text


def entry_senses(record: dict) -> list[tuple[int, str, str, str]]:
    """``(rank, pos, gloss, tags)`` for one kaikki record, most likely first.

    Pure, so the ordering rules are testable without the 79 MB source file.
    """
    word = normalize_headword(str(record.get("word") or ""))
    if not word:
        return []

    ranked: list[tuple[int, int, str, str, str]] = []
    for sense_index, sense in enumerate(record.get("senses") or []):
        if not isinstance(sense, dict):
            continue
        pos = str(record.get("pos") or "")
        tags = ",".join(str(t) for t in (sense.get("tags") or []))
        for gloss in sense.get("glosses") or []:
            if not isinstance(gloss, str):
                continue
            cleaned = _clean(gloss)
            if not cleaned:
                continue
            # POS order dominates; then meta-ness; then the source's own order.
            meta = 1 if META_GLOSS.match(cleaned) else 0
            ranked.append((meta, _pos_rank(pos), f"{pos}\u0000{sense_index}", cleaned, tags))

    ranked.sort(key=lambda r: (r[1], r[0]))
    out: list[tuple[int, str, str, str]] = []
    seen: set[str] = set()
    for rank, (_, _, pos_and_sense, gloss, tags) in enumerate(ranked):
        key = gloss.casefold()
        if key in seen:
            continue
        seen.add(key)
        pos = pos_and_sense.split("\u0000", 1)[0]
        out.append((rank, pos, gloss, tags))
        if len(out) >= MAX_SENSES_PER_WORD:
            break
    return out


def read_source(path: Path, wanted: set[str] | None = None):
    """Yield ``(headword, [(rank, pos, gloss, tags), ...])`` from the JSONL source.

    ``wanted`` optionally restricts output to a set of headwords, which keeps a
    partial rebuild cheap.
    """
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("lang_code") != "vi":
                continue
            headword = normalize_headword(str(record.get("word") or ""))
            if not headword or (wanted is not None and headword not in wanted):
                continue
            senses = entry_senses(record)
            if senses:
                yield headword, senses


def download(url: str, dest: Path, *, force: bool = False) -> Path:
    """Fetch the gloss source, unless it is already cached."""
    if dest.is_file() and not force:
        print(f"Using cached source {dest}  ({dest.stat().st_size / 1_048_576:.1f} MB)")
        return dest
    import httpx

    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"Downloading {url}")
    with httpx.stream(
        "GET", url, timeout=300, follow_redirects=True,
        headers={"User-Agent": "nghe-reading/0.1"},
    ) as response:
        response.raise_for_status()
        with dest.open("wb") as handle:
            for chunk in response.iter_bytes(1 << 16):
                handle.write(chunk)
    print(f"  wrote {dest}  ({dest.stat().st_size / 1_048_576:.1f} MB)")
    return dest


def import_glosses(db_path: Path, source: Path) -> dict:
    """Replace the ``glosses`` table with what ``source`` contains."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.executescript(GLOSS_SCHEMA)
        conn.execute("DELETE FROM glosses")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_glosses_headword ON glosses (headword, rank)"
        )

        words: set[str] = set()
        senses = 0
        batch: list[tuple] = []
        for headword, entries in read_source(source):
            words.add(headword)
            for rank, pos, gloss, tags in entries:
                batch.append((headword, pos, rank, gloss, tags, SOURCE_LICENCE))
                senses += 1
            if len(batch) >= 5000:
                conn.executemany(
                    """INSERT INTO glosses (headword, pos, rank, gloss, tags, source)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    batch,
                )
                batch.clear()
        if batch:
            conn.executemany(
                """INSERT INTO glosses (headword, pos, rank, gloss, tags, source)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                batch,
            )
        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM glosses").fetchone()[0]
    finally:
        conn.close()
    return {"words": len(words), "senses": senses, "rows": total}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default=str(settings.dict_path), help="dictionary SQLite path")
    parser.add_argument("--source", default=str(DEFAULT_CACHE),
                        help="local JSONL path, or an http(s) URL")
    parser.add_argument("--force-download", action="store_true",
                        help="re-download even when the cache file exists")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.is_file():
        print(f"error: no dictionary at {db_path}\n"
              f"Build the boundaries first:\n"
              f"    python -m reading.tools.build_dictionary", file=sys.stderr)
        return 1

    source = args.source
    if source.startswith(("http://", "https://")):
        source_path = download(source, DEFAULT_CACHE, force=args.force_download)
    else:
        source_path = Path(source)
        if not source_path.is_file():
            print(f"error: gloss source {source_path} not found.\n"
                  f"Download it with:\n"
                  f"    python -m reading.tools.build_glosses --source {DEFAULT_SOURCE_URL}",
                  file=sys.stderr)
            return 1

    print(f"Importing English glosses from {source_path.name}")
    summary = import_glosses(db_path, source_path)
    print(
        f"  {summary['words']:,} headwords, {summary['senses']:,} senses "
        f"({summary['rows']:,} rows)\n"
        f"  source: {SOURCE_LICENCE}\n"
        f"wrote {db_path}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
