"""Build the local compound dictionary from underthesea's bundled word lists.

    python -m reading.tools.build_dictionary

underthesea ships four Vietnamese word lists under ``underthesea/corpus/data``:

    Viet11K.txt    11,373 headwords
    Viet22K.txt    22,426
    Viet39K.txt    39,071
    Viet74K.txt    73,901

Each line is one headword with its syllables separated by spaces (``đại học``),
so the lists are a direct statement of compound boundaries — which is exactly the
"hard constraint" the segmentation reconciler needs.

They contain no definitions, so ``entries.definition`` is left NULL and the
definition step fills it from DeepSeek.  Re-running this script is safe: it
rebuilds the dictionary table from scratch (this file is a build artifact, not
user data).
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from reading.settings import settings  # noqa: E402
from reading.storage.dictionary import normalize_syllable, UNDERSCORE  # noqa: E402

WORDLIST_NAMES = ("Viet11K", "Viet22K", "Viet39K", "Viet74K")

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    headword        TEXT PRIMARY KEY,   -- underscore form, normalised
    display         TEXT NOT NULL,      -- original spelling from the word list
    syllable_count  INTEGER NOT NULL,
    syllables       TEXT NOT NULL,      -- underscore-joined normalised syllables
    definition      TEXT,               -- NULL: word lists carry no glosses
    sources         TEXT NOT NULL DEFAULT '',  -- which word lists contained it
    freq_rank       INTEGER             -- NULL: word lists are not frequency-ordered
);
CREATE INDEX IF NOT EXISTS idx_entries_syllable_count ON entries (syllable_count);
"""


def wordlist_dir() -> Path:
    """Locate the word lists inside the installed underthesea package."""
    try:
        import underthesea
    except ImportError as exc:  # pragma: no cover - environment problem
        raise SystemExit(
            "underthesea is not installed.  Install the reading module's "
            "dependencies first:\n    pip install -r reading/requirements.txt"
        ) from exc
    return Path(underthesea.__file__).resolve().parent / "corpus" / "data"


def read_wordlists(directory: Path) -> dict[str, dict]:
    """Merge the four word lists into ``{headword: {...}}``."""
    entries: dict[str, dict] = {}
    for name in WORDLIST_NAMES:
        path = directory / f"{name}.txt"
        if not path.is_file():
            print(f"  warning: {path} missing, skipping", file=sys.stderr)
            continue
        count = 0
        with path.open(encoding="utf-8") as handle:
            for raw in handle:
                display = raw.strip()
                if not display:
                    continue
                syllables = [normalize_syllable(s) for s in display.split()]
                syllables = [s for s in syllables if s]
                if not syllables:
                    continue
                headword = UNDERSCORE.join(syllables)
                record = entries.get(headword)
                if record is None:
                    entries[headword] = {
                        "headword": headword,
                        "display": display,
                        "syllable_count": len(syllables),
                        "syllables": UNDERSCORE.join(syllables),
                        "sources": name,
                    }
                elif name not in record["sources"].split(","):
                    record["sources"] = f"{record['sources']},{name}"
                count += 1
        print(f"  {name:<8} {count:>7,} lines")
    return entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(settings.dict_path), help="output SQLite path")
    args = parser.parse_args(argv)

    directory = wordlist_dir()
    print(f"Reading word lists from {directory}")
    entries = read_wordlists(directory)
    if not entries:
        print("error: no word lists found", file=sys.stderr)
        return 1

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(out))
    try:
        conn.executescript(SCHEMA)
        conn.execute("DELETE FROM entries")
        conn.executemany(
            """INSERT OR REPLACE INTO entries
                 (headword, display, syllable_count, syllables, sources)
               VALUES (:headword, :display, :syllable_count, :syllables, :sources)""",
            entries.values(),
        )
        conn.commit()
        total = conn.execute("SELECT COUNT(*) FROM entries").fetchone()[0]
        multi = conn.execute(
            "SELECT COUNT(*) FROM entries WHERE syllable_count > 1"
        ).fetchone()[0]
        max_syl = conn.execute("SELECT MAX(syllable_count) FROM entries").fetchone()[0]
    finally:
        conn.close()

    print(
        f"\n{total:,} headwords ({multi:,} multi-syllable, longest {max_syl} syllables)\n"
        f"wrote {out}  ({out.stat().st_size / 1024:.0f} KB)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
