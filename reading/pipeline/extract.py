"""Assemble the reading bundle that the static page consumes.

Kept separate from both ``storage`` (which knows SQL) and ``tools/build_site``
(which knows the filesystem) so the bundle shape can be unit-tested and reused by
the optional API layer without either of them depending on the exporter.
"""
from __future__ import annotations

import json

from reading.pipeline.lookup import build_lookup_index
from reading.pipeline.text import syllables_with_offsets
from reading.storage import db
from reading.storage.dictionary import CompoundDictionary


def list_versions(conn, cefr_level: str | None = None, source: str | None = None):
    """Stored (article, level) pairs, newest first."""
    sql = """
        SELECT v.id, v.article_id, v.cefr_level, v.quality, v.created_at,
               a.source, a.title, a.published_at
          FROM article_versions v
          JOIN articles a ON a.id = v.article_id
         WHERE 1 = 1
    """
    params: list[object] = []
    if cefr_level:
        sql += " AND v.cefr_level = ?"
        params.append(cefr_level.upper())
    if source:
        sql += " AND a.source = ?"
        params.append(source)
    sql += " ORDER BY COALESCE(a.published_at, v.created_at) DESC, v.id DESC"
    return list(conn.execute(sql, params))


def bundle_for_version(
    conn, version_id: int, dictionary: CompoundDictionary | None = None
) -> dict | None:
    """Everything the reading page needs for one (article, level) pair.

    ``dictionary`` is optional but strongly recommended: with it the bundle carries
    the offline lookup index, which is what lets the reader resolve a range of words
    the segmenter did not produce as a token (see ``pipeline/lookup.py``).
    """
    row = conn.execute(
        """SELECT v.*, a.source, a.guid, a.source_url, a.title, a.author,
                  a.published_at, a.fetched_at, a.attribution, a.body_source
             FROM article_versions v
             JOIN articles a ON a.id = v.article_id
            WHERE v.id = ?""",
        (version_id,),
    ).fetchone()
    if row is None:
        return None

    text = row["simplified_text"]
    syllables = syllables_with_offsets(text)
    tokens = [dict(token) for token in db.get_tokens(conn, version_id)]
    _attach_syllable_indices(tokens, syllables)
    preteach = db.get_preteach(conn, version_id)
    exercises = db.get_exercises(conn, version_id)

    return {
        "article": {
            "id": row["article_id"],
            "source": row["source"],
            "guid": row["guid"],
            "title": row["title"],
            "author": row["author"],
            "published_at": row["published_at"],
            "source_url": row["source_url"],
            "attribution": row["attribution"],
            "body_source": row["body_source"],
        },
        "version": {
            "id": row["id"],
            "cefr_level": row["cefr_level"],
            "quality": row["quality"],
            "notes": row["notes"],
            "created_at": row["created_at"],
        },
        "text": text,
        # The token map: offsets are authoritative, syllable indices are a
        # convenience for the UI's expand/shrink handles.
        "tokens": [
            {
                "id": token["ordinal"],
                "surface": token["surface"],
                "form": token["form"],
                "start": token["start_char"],
                "end": token["end_char"],
                "syl_start": token["syl_start"],
                "syl_end": token["syl_end"],
                "cefr": token["cefr_level"],
                # `definition` is the primary string to show (English when the
                # dictionary has it, Vietnamese otherwise); the two languages are
                # also carried separately so the sheet can show both.
                "definition": token["definition"],
                "definition_src": token["definition_src"],
                "definition_en": token["definition_en"],
                "definition_vi": token["definition_vi"],
                "senses_en": _load_candidates(token["senses_en"]),
                "ambiguous": bool(token["ambiguous"]),
                "candidates": _load_candidates(token["candidates"]),
            }
            for token in tokens
        ],
        "syllables": [{"s": s.start, "e": s.end, "t": s.text} for s in syllables],
        # Every dictionary span inside this article, so a highlighted range resolves
        # offline.  Empty when no dictionary was available at export time.
        "lookup": build_lookup_index(syllables, dictionary),
        "preteach": {
            "vocab": [_preteach_row(r) for r in preteach["vocab"]],
            "grammar": [_preteach_row(r) for r in preteach["grammar"]],
        },
        "exercises": _exercises(exercises),
    }


def _exercises(rows: dict) -> dict:
    """The practice content, in the shape the reader renders.

    An older bundle (or an article whose exercises have not been generated yet) has
    no rows at all, and the page simply shows no practice section.
    """
    mcq = [
        {
            "q": row["prompt"],
            "options": _load_candidates(row["options"]) or [],
            "answer": row["answer"],
            "why": row["why"] or "",
        }
        for row in rows.get("mcq", [])
    ]
    short = [
        {
            "q": row["prompt"],
            "sample": row["sample"] or "",
            "key_points": _load_candidates(row["key_points"]) or [],
        }
        for row in rows.get("short", [])
    ]
    writing_row = (rows.get("writing") or [None])[0]
    writing = None
    if writing_row is not None and writing_row["prompt"]:
        writing = {
            "prompt": writing_row["prompt"],
            "key_points": _load_candidates(writing_row["key_points"]) or [],
            "model_answer": writing_row["sample"] or "",
            "min_words": writing_row["min_words"] or 0,
        }
    if not mcq and not short and writing is None:
        return {}
    return {"mcq": mcq, "short": short, "writing": writing}


def _attach_syllable_indices(tokens: list[dict], syllables) -> None:
    """Add ``syl_start`` / ``syl_end`` to each token, in place.

    A token covers a run of consecutive syllables, and its ``start_char`` is the
    start of the first of them, so the index is just how many syllables begin
    before it.
    """
    starts = [s.start for s in syllables]
    for token in tokens:
        # bisect_left: the first syllable that begins at or after the token start.
        low, high = 0, len(starts)
        while low < high:
            mid = (low + high) // 2
            if starts[mid] < token["start_char"]:
                low = mid + 1
            else:
                high = mid
        token["syl_start"] = low
        token["syl_end"] = min(low + token["syllable_count"], len(syllables))


def _load_candidates(raw):
    """Parse a JSON-array column (candidate segmentations, English senses)."""
    if not raw:
        return None
    import json

    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, list) else None


def _preteach_row(row) -> dict:
    return {
        "term": row["term"],
        "gloss": row["gloss"],
        "gloss_en": row["gloss_en"],
        "cefr": row["cefr_level"],
        "example": row["example"],
    }
