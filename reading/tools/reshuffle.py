"""Reorder stored multiple-choice options — no model call, no re-generation.

    python -m reading.tools.reshuffle [--dry-run] [--cefr B1]

**Why this exists.**  The questions DeepSeek wrote put the correct answer first 34
times out of 40.  The text of an option carries no position, so the fix is a
reordering, not a new call: `pipeline/exercises.py` already orders the options when it
stores a new question, and this applies the same content-determined order to the rows
that were stored before that existed.  Nothing is asked of the API, nothing is
re-simplified, and the correct *answer* is still the same sentence — it just moves.

**It is idempotent.**  The order is a pure function of the question and the option
texts, so running it again is a no-op.  That matters: an earlier version applied a
fixed permutation, and a second run shuffled the balanced set straight back to 50% A.

`--dry-run` prints the distribution it found and the distribution it would write.
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

from reading.pipeline.exercises import MCQ, order_options
from reading.settings import Settings, settings as default_settings
from reading.storage import db


def distribution(conn) -> dict[int, int]:
    """How many stored questions have the correct answer at each index."""
    counts: Counter = Counter()
    for row in conn.execute("SELECT answer FROM exercises WHERE kind = ?", (MCQ,)):
        counts[row["answer"]] += 1
    return dict(sorted(counts.items()))


def reshuffle(conn, cefr_level: str | None = None, dry_run: bool = False, verbose: bool = True) -> dict:
    """Put every stored MCQ row into its content-determined order, remapping answers.

    Returns the before/after distributions so the change is visible rather than
    assumed, which matters here because a wrong answer index is silent.
    """
    sql = """
        SELECT e.id, e.prompt, e.options, e.answer, v.cefr_level
          FROM exercises e
          JOIN article_versions v ON v.id = e.version_id
         WHERE e.kind = ?
    """
    params: list[object] = [MCQ]
    if cefr_level:
        sql += " AND v.cefr_level = ?"
        params.append(cefr_level.upper())
    sql += " ORDER BY e.id"

    before = distribution(conn)
    changed = 0
    for row in conn.execute(sql, params):
        options = json.loads(row["options"] or "[]")
        answer = row["answer"] if row["answer"] is not None else -1
        if len(options) < 2 or not 0 <= answer < len(options):
            continue
        new_options, new_answer = order_options(row["prompt"], options, answer)
        if new_options == options and new_answer == answer:
            continue
        changed += 1
        if not dry_run:
            conn.execute(
                "UPDATE exercises SET options = ?, answer = ? WHERE id = ?",
                (
                    json.dumps(new_options, ensure_ascii=False),
                    new_answer,
                    row["id"],
                ),
            )
    if not dry_run and changed:
        conn.commit()

    after = distribution(conn)
    if verbose:
        if dry_run:
            print(f"{changed} questions would move; nothing was written")
        else:
            print(f"{changed} questions reordered")
        print(f"  before: {_render(before)}")
        print(f"  after:  {_render(after)}")
    return {"changed": changed, "before": before, "after": after, "dry_run": dry_run}


def _render(counts: dict[int, int]) -> str:
    letters = "ABCDEFGH"
    total = sum(counts.values()) or 1
    return ", ".join(
        f"{letters[index] if 0 <= index < len(letters) else index}: "
        f"{count} ({100 * count // total}%)"
        for index, count in counts.items()
    ) or "none"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--cefr", default=None, help="only this level")
    parser.add_argument("--dry-run", action="store_true", help="report, write nothing")
    parser.add_argument("--db", default=None, help="override the database path")
    args = parser.parse_args(argv)

    cfg: Settings = default_settings
    if args.db:
        from dataclasses import replace

        cfg = replace(cfg, db_path=Path(args.db))

    conn = db.connect(cfg.db_path)
    try:
        applied = db.migrate(conn)
        if applied:
            print(f"Applied migrations: {', '.join(applied)}")
        reshuffle(conn, cefr_level=args.cefr, dry_run=args.dry_run)
    finally:
        conn.close()

    if args.dry_run:
        print("\nNothing was written; re-run without --dry-run, then re-export:")
    else:
        print("\nRe-export the bundles so the pages pick up the new order:")
    print("    python -m reading.tools.build_site")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
