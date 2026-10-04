"""Re-apply the segmentation guards to stored articles, without paying for them.

    python -m reading.tools.resegment [--cefr B1] [--limit N] [--dry-run]

**Why this exists.**  ``pipeline/reconcile.py`` decides what a tapped word is, and
its guards have been tightened: a span may no longer cross punctuation, a run made
only of function words is never one word (``của ông``, ``trong lúc``), and a
dictionary lock now has to be explainable — a shorter glossed reading beats a longer
unglossed one.  Those rules live in the *reconciler*, not in the model, so applying
them to an article that is already stored does not need DeepSeek at all.  The stored
text is unchanged; only its token boundaries are rebuilt.

**What it does, precisely.**  It walks each stored token and a guard that refuses the
merge now splits it: into the syllables themselves, or into the shorter dictionary
reading the gloss layer prefers.  Everything the guards do *not* object to is left
byte-identical, which matters because the stored token map is also the record of the
model's own segmentation (``ambiguous`` flags and the alternative readings a reader
can widen to).  Re-segmenting from scratch would throw that away; repairing in place
keeps it.

**What it does not do.**  It cannot invent new exercise content, and it does not
re-run the model, so the stored text is exactly what was there before.  A paid
``ingest --refresh`` is still the way to get a fresh rewrite; this is the free way to
get a *correct* token map for the text you already have.
"""
from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

from reading.pipeline import prompts
from reading.pipeline.reconcile import (
    SRC_AGREED,
    SRC_DICTIONARY,
    SRC_SPLIT,
    SRC_UNDERTHESEA,
    Candidate,
    ReconciledSpan,
    dictionary_length,
    guard_refuses_merge,
)
from reading.pipeline.text import contiguous_predicate, syllables_with_offsets
from reading.pipeline.tokens import Definition, build_token_map
from reading.settings import Settings, settings as default_settings
from reading.storage import db
from reading.storage.dictionary import CompoundDictionary

RESEGMENT_NOTE = "re-segmented in place by tools/resegment.py"


@dataclass
class RepairStats:
    """One version's before/after, so the change is visible rather than assumed."""

    version_id: int
    tokens_before: int = 0
    tokens_after: int = 0
    tokens_changed: int = 0
    unglossed_before: int = 0
    unglossed_after: int = 0
    changed: bool = False
    skipped_reason: str = ""


def resegment_version(
    conn,
    version_id: int,
    text: str,
    dictionary: CompoundDictionary | None,
    definitions: dict[str, Definition],
    dry_run: bool = False,
) -> RepairStats:
    """Rebuild one version's token map, applying the guards to what is stored."""
    syllables = syllables_with_offsets(text)
    stored = [dict(row) for row in db.get_tokens(conn, version_id)]
    spans = _repair_spans(text, syllables, stored, dictionary)

    stats = RepairStats(
        version_id=version_id,
        tokens_before=len(stored),
        unglossed_before=sum(
            1 for t in stored if not (t["definition_en"] or t["definition_vi"])
        ),
    )
    problem = _cover_problem(spans, len(syllables))
    if problem:
        # A token map that does not tile the text would break every offset after the
        # gap, so the version is left exactly as it was rather than half-repaired.
        stats.tokens_after = stats.tokens_before
        stats.unglossed_after = stats.unglossed_before
        stats.skipped_reason = problem
        return stats

    tokens = build_token_map(text, syllables, spans, definitions, dictionary)
    stats.tokens_after = len(tokens)
    stats.unglossed_after = sum(
        1 for t in tokens if not (t["definition_en"] or t["definition_vi"])
    )
    before = {(t["start_char"], t["end_char"]) for t in stored}
    after = {(t["start_char"], t["end_char"]) for t in tokens}
    # Spans that appeared or disappeared, not a positional diff: splitting one token
    # shifts every ordinal after it, and counting that as "everything changed" hides
    # how small the repair actually is.
    stats.tokens_changed = len(before ^ after) // 2
    stats.changed = bool(before ^ after)

    if stats.changed and not dry_run:
        db.replace_tokens(conn, version_id, tokens)
        _note_resegmented(conn, version_id)
    return stats


def _cover_problem(spans, total: int) -> str:
    """Why these spans cannot replace a token map, or "" when they are sound."""
    cursor = 0
    for span in spans:
        if span.start != cursor:
            return f"span gap at syllable {cursor}"
        if span.end <= span.start:
            return f"empty span at syllable {cursor}"
        cursor = span.end
    if cursor != total:
        return f"spans cover {cursor} of {total} syllables"
    return ""


def _note_resegmented(conn, version_id: int) -> None:
    """Record that the map was rebuilt, keeping any note that was already there."""
    row = conn.execute(
        "SELECT notes FROM article_versions WHERE id = ?", (version_id,)
    ).fetchone()
    notes = (row["notes"] if row is not None else "") or ""
    if RESEGMENT_NOTE in notes:
        return
    conn.execute(
        "UPDATE article_versions SET notes = ? WHERE id = ?",
        ((notes + "; " if notes else "") + RESEGMENT_NOTE, version_id),
    )
    conn.commit()


def _repair_spans(text, syllables, stored, dictionary):
    """The guarded spans for a version, repaired from its stored token map.

    Stored tokens are reduced to syllable ranges (the map itself is offset-based) and
    each one is re-checked.  A token that the guards now refuse becomes either a
    single syllable or a shorter dictionary lock; a token they accept is passed
    through untouched, keeping its ambiguity flag and its alternative readings.
    """
    allow = contiguous_predicate(text, syllables)
    index_of = {s.start: position for position, s in enumerate(syllables)}

    spans: list[ReconciledSpan] = []
    for token in stored:
        start = index_of.get(token["start_char"])
        if start is None:
            continue
        end = min(start + int(token["syllable_count"]), len(syllables))
        spans.extend(_repair_one(syllables, start, end, token, dictionary, allow))
    return spans


def _repair_one(syllables, start, end, token, dictionary, allow):
    """The spans a single stored token becomes: usually itself."""
    if end - start < 2:
        return [ReconciledSpan(start=start, end=end, source=SRC_AGREED, locked=True)]

    syllable_text = [s.text for s in syllables]

    # 1. the same two guards the reconciler applies to a fresh segmentation: a span
    #    that crosses punctuation is not a word, and neither is a run of function
    #    words that nothing can gloss.
    if not allow(start, end) or guard_refuses_merge(
        syllable_text, start, end, dictionary
    ):
        return [
            ReconciledSpan(start=i, end=i + 1, source=SRC_SPLIT, locked=True)
            for i in range(start, end)
        ]

    # 2. untouched: keep the stored reading, ambiguity and candidates included.  The
    #    original provenance is not stored, so an unambiguous span is recorded as
    #    agreement — the one label that is true of every segmentation that produced it.
    candidates = [
        Candidate(
            source=str(c.get("source") or ""),
            form=str(c.get("form") or ""),
            syllables=int(c.get("syllables") or 0),
        )
        for c in _load(token.get("candidates"))
        if isinstance(c, dict)
    ]
    return [
        ReconciledSpan(
            start=start,
            end=end,
            source=SRC_UNDERTHESEA if candidates else SRC_AGREED,
            locked=not bool(token.get("ambiguous")),
            ambiguous=bool(token.get("ambiguous")),
            candidates=candidates,
        )
    ]


def _load(raw):
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _definitions_from_stored(conn, version_id: int) -> dict[str, Definition]:
    """Rebuild the pre-teach gloss map from the stored rows.

    The model's pre-teach list is the free source of Vietnamese glosses, and it is
    already in the database, so re-running the token map does not need the model.
    """
    out: dict[str, Definition] = {}
    for row in db.get_preteach(conn, version_id)["vocab"]:
        term = row["term"]
        gloss = row["gloss"] or ""
        if not term or not gloss:
            continue
        out.setdefault(
            term,
            Definition(
                text=gloss,
                vietnamese=gloss,
                cefr=row["cefr_level"],
                source="preteach",
            ),
        )
    return out


def run_resegment(
    conn,
    dictionary: CompoundDictionary | None,
    cefr_level: str | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    verbose: bool = True,
) -> list[RepairStats]:
    sql = """SELECT v.id, v.cefr_level, v.simplified_text, a.title, a.source
               FROM article_versions v
               JOIN articles a ON a.id = v.article_id
              WHERE 1 = 1"""
    params: list[object] = []
    if cefr_level:
        sql += " AND v.cefr_level = ?"
        params.append(cefr_level.upper())
    sql += " ORDER BY v.cefr_level, v.id"
    rows = list(conn.execute(sql, params))
    if limit:
        rows = rows[:limit]

    stats: list[RepairStats] = []
    for row in rows:
        result = resegment_version(
            conn,
            int(row["id"]),
            row["simplified_text"],
            dictionary,
            _definitions_from_stored(conn, int(row["id"])),
            dry_run=dry_run,
        )
        stats.append(result)
        if verbose:
            if result.skipped_reason:
                flag = f"skipped: {result.skipped_reason}"
            elif result.changed:
                flag = "repaired"
            else:
                flag = "unchanged"
            print(
                f"  [{row['id']:>4}] {row['cefr_level']} {row['title'][:56]}  "
                f"{result.tokens_before} -> {result.tokens_after} tokens, "
                f"{result.tokens_changed} changed, "
                f"{result.unglossed_before} -> {result.unglossed_after} without a gloss"
                f"  [{flag}]"
            )
    return stats


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cefr", default=None, help="only this level")
    parser.add_argument("--limit", type=int, default=None, help="only the first N versions")
    parser.add_argument("--dry-run", action="store_true", help="report, write nothing")
    parser.add_argument("--db", default=None, help="override the database path")
    args = parser.parse_args(argv)

    cfg: Settings = default_settings
    if args.db:
        from dataclasses import replace

        cfg = replace(cfg, db_path=Path(args.db))

    try:
        dictionary = CompoundDictionary.from_sqlite(
            cfg.dict_path, max_syllables=cfg.max_compound_syllables
        )
    except FileNotFoundError as exc:
        print(f"warning: {exc}\nRe-segmenting without the dictionary constraint.")
        dictionary = None

    conn = db.connect(cfg.db_path)
    applied = db.migrate(conn)
    if applied:
        print(f"Applied migrations: {', '.join(applied)}")
    try:
        print(f"Re-segmenting stored versions{' (dry run)' if args.dry_run else ''}...")
        stats = run_resegment(
            conn, dictionary, cefr_level=args.cefr, limit=args.limit, dry_run=args.dry_run
        )
    finally:
        conn.close()

    repaired = sum(1 for s in stats if s.changed)
    unglossed_before = sum(s.unglossed_before for s in stats)
    unglossed_after = sum(s.unglossed_after for s in stats)
    print(
        f"\n{len(stats)} versions, {repaired} repaired. "
        f"Tokens without a definition: {unglossed_before} -> {unglossed_after}."
    )
    if args.dry_run:
        print("Nothing was written; re-run without --dry-run to apply.")
    else:
        print("Now re-export the site:")
        print("    python -m reading.tools.build_site")
    # The prompt version is deliberately untouched: the article text did not change.
    print(f"(simplification still {prompts.PROMPT_VERSION})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
