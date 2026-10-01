"""The local compound dictionary — the hard constraint on segmentation.

Built by ``tools/build_dictionary.py`` from the word lists bundled with
underthesea (``Viet11K`` / ``Viet22K`` / ``Viet39K`` / ``Viet74K``).  Those lists
are *boundary* data: they say which syllable runs form one word, and they carry no
glosses.  So this dictionary decides compounds, and definitions come from DeepSeek.

The reconciler treats a dictionary hit as authoritative: if the dictionary knows
``đại_học``, no amount of model disagreement will split it.
"""
from __future__ import annotations

import sqlite3
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

MAX_COMPOUND_SYLLABLES = 4
UNDERSCORE = "_"


def normalize_syllable(text: str) -> str:
    """NFC-normalise, strip, and casefold a syllable for matching.

    Vietnamese is diacritic-heavy but the word lists are already correctly
    accented, so diacritics are *kept* — only case and Unicode form are folded.
    """
    return unicodedata.normalize("NFC", text).strip().casefold()


def normalize_form(form: str) -> str:
    """Normalise an underscore form (``Đại_học`` -> ``đại_học``)."""
    parts = form.replace(" ", UNDERSCORE).split(UNDERSCORE)
    return UNDERSCORE.join(p for p in (normalize_syllable(x) for x in parts) if p)


@dataclass(frozen=True)
class DictEntry:
    headword: str          # underscore form, normalised
    display: str           # original spelling from the word list
    syllable_count: int
    syllables: tuple[str, ...]
    sources: str = ""      # which word lists contained it
    # NULL for the bundled word lists, which carry boundaries but no glosses.
    # Present so an enriched dictionary can supply definitions without a schema change.
    definition: str | None = None


class CompoundDictionary:
    """In-memory view over the dictionary SQLite file.

    Loaded once per ingest run; ~74k headwords is a few MB, so a set lookup beats
    querying SQLite per syllable.
    """

    def __init__(self, entries: dict[str, DictEntry], max_syllables: int = MAX_COMPOUND_SYLLABLES):
        self._entries = entries
        self.max_syllables = max_syllables

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, form: str) -> bool:
        return normalize_form(form) in self._entries

    def get(self, form: str) -> DictEntry | None:
        return self._entries.get(normalize_form(form))

    @classmethod
    def from_sqlite(
        cls, path: Path | str, max_syllables: int = MAX_COMPOUND_SYLLABLES
    ) -> "CompoundDictionary":
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(
                f"dictionary not found at {path}.  Build it first:\n"
                f"    python -m reading.tools.build_dictionary"
            )
        conn = sqlite3.connect(str(path))
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """SELECT headword, display, syllable_count, syllables, sources,
                          definition
                     FROM entries WHERE syllable_count <= ?""",
                (max_syllables,),
            ).fetchall()
        finally:
            conn.close()
        entries = {
            row["headword"]: DictEntry(
                headword=row["headword"],
                display=row["display"],
                syllable_count=int(row["syllable_count"]),
                syllables=tuple(row["syllables"].split(UNDERSCORE)),
                sources=row["sources"] or "",
                definition=row["definition"],
            )
            for row in rows
        }
        return cls(entries, max_syllables=max_syllables)

    # -- the constraint the reconciler uses ---------------------------------

    def longest_match(self, syllables: list[str], start: int) -> int:
        """Length in syllables of the longest dictionary word at ``start``.

        Returns 1 when nothing matches (a bare syllable is always a valid span).
        """
        limit = min(self.max_syllables, len(syllables) - start)
        for length in range(limit, 1, -1):
            candidate = UNDERSCORE.join(
                normalize_syllable(s) for s in syllables[start : start + length]
            )
            if candidate in self._entries:
                return length
        return 1

    def is_known_compound(self, syllables: list[str]) -> bool:
        """True when the whole run is a single dictionary word."""
        if len(syllables) < 2 or len(syllables) > self.max_syllables:
            return False
        form = UNDERSCORE.join(normalize_syllable(s) for s in syllables)
        return form in self._entries


@lru_cache(maxsize=4)
def load_dictionary(path: str, max_syllables: int = MAX_COMPOUND_SYLLABLES) -> CompoundDictionary:
    """Cached loader so repeated pipeline calls share one in-memory copy."""
    return CompoundDictionary.from_sqlite(path, max_syllables)
