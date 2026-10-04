"""SQLite connection, migration runner and the reading module's queries.

The schema is owned by ``storage/migrations/*.sql``.  :func:`migrate` applies any
file that has not run yet and records it in ``schema_migrations``; nothing here
ever drops or recreates a table.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Sequence

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def now_iso() -> str:
    """Current UTC time as an ISO-8601 string with a ``Z`` suffix."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def connect(db_path: Path | str) -> sqlite3.Connection:
    """Open (creating if needed) the reading database."""
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def pending_migrations(conn: sqlite3.Connection, migrations_dir: Path = MIGRATIONS_DIR) -> list[Path]:
    """Migration files that have not been applied yet, in filename order."""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    applied = {row["version"] for row in conn.execute("SELECT version FROM schema_migrations")}
    return [
        p for p in sorted(migrations_dir.glob("*.sql")) if p.name not in applied
    ]


def migrate(conn: sqlite3.Connection, migrations_dir: Path = MIGRATIONS_DIR) -> list[str]:
    """Apply every pending migration.  Returns the versions applied."""
    applied: list[str] = []
    for path in pending_migrations(conn, migrations_dir):
        conn.executescript(path.read_text(encoding="utf-8"))
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (path.name, now_iso()),
        )
        conn.commit()
        applied.append(path.name)
    return applied


# ---------------------------------------------------------------------------
# Articles
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ArticleRecord:
    """A fetched article before it has been simplified."""

    source: str
    guid: str
    source_url: str
    title: str
    original_text: str
    published_at: str | None = None
    author: str | None = None
    body_source: str = "rss"
    attribution: str = ""


def article_exists(conn: sqlite3.Connection, source: str, guid: str) -> bool:
    """Deduplication check used by the ingester."""
    row = conn.execute(
        "SELECT 1 FROM articles WHERE source = ? AND guid = ? LIMIT 1", (source, guid)
    ).fetchone()
    return row is not None


def article_by_guid(conn: sqlite3.Connection, source: str, guid: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM articles WHERE source = ? AND guid = ?", (source, guid)
    ).fetchone()


def version_exists(
    conn: sqlite3.Connection, source: str, guid: str, cefr_level: str
) -> bool:
    """Has this article already been rendered at this level?

    The unit of deduplication is the *version*, not the article: one article is
    expected to exist at several levels, and each level is a separate simplification.
    """
    row = conn.execute(
        """SELECT 1 FROM article_versions v
             JOIN articles a ON a.id = v.article_id
            WHERE a.source = ? AND a.guid = ? AND v.cefr_level = ?
            LIMIT 1""",
        (source, guid, cefr_level.upper()),
    ).fetchone()
    return row is not None


def version_prompt_version(
    conn: sqlite3.Connection, source: str, guid: str, cefr_level: str
) -> str | None:
    """Which prompt produced the stored version, or ``None`` if there is none.

    Lets a refresh target only the versions a prompt change has made stale, instead
    of re-paying for every pair that was already generated with the current prompt.
    """
    row = conn.execute(
        """SELECT v.prompt_version FROM article_versions v
             JOIN articles a ON a.id = v.article_id
            WHERE a.source = ? AND a.guid = ? AND v.cefr_level = ?
            LIMIT 1""",
        (source, guid, cefr_level.upper()),
    ).fetchone()
    return row["prompt_version"] if row is not None else None


def exercises_prompt_version(conn: sqlite3.Connection, version_id: int) -> str:
    """Which exercises prompt produced this version's practice content.

    Empty when there is none, which is what makes ``--refresh-exercises`` able to
    find the versions that have no exercises yet as well as the stale ones.
    """
    row = conn.execute(
        "SELECT exercises_prompt_version FROM article_versions WHERE id = ?",
        (version_id,),
    ).fetchone()
    if row is None:
        return ""
    return row["exercises_prompt_version"] or ""


def set_exercises_prompt_version(
    conn: sqlite3.Connection, version_id: int, prompt_version: str
) -> None:
    conn.execute(
        "UPDATE article_versions SET exercises_prompt_version = ? WHERE id = ?",
        (prompt_version, version_id),
    )
    conn.commit()


def insert_article(conn: sqlite3.Connection, article: ArticleRecord) -> int | None:
    """Insert an article, or return ``None`` if it was already present."""
    if article_exists(conn, article.source, article.guid):
        return None
    cur = conn.execute(
        """INSERT INTO articles
             (source, guid, source_url, title, author, published_at, fetched_at,
              original_text, body_source, attribution)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            article.source,
            article.guid,
            article.source_url,
            article.title,
            article.author,
            article.published_at,
            now_iso(),
            article.original_text,
            article.body_source,
            article.attribution,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def get_article(conn: sqlite3.Connection, article_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM articles WHERE id = ?", (article_id,)).fetchone()


def update_article_body(
    conn: sqlite3.Connection,
    article_id: int,
    original_text: str,
    body_source: str,
) -> None:
    """Replace an article's stored body.

    Used by ``ingest --refetch-body``: the body is normally immutable, but the
    extractor itself can be wrong (it once kept a single paragraph of a BBC page),
    and when that happens the stored text has to be replaceable or the fix cannot
    reach articles already in the database.
    """
    conn.execute(
        "UPDATE articles SET original_text = ?, body_source = ?, fetched_at = ? WHERE id = ?",
        (original_text, body_source, now_iso(), article_id),
    )
    conn.commit()


def list_articles(
    conn: sqlite3.Connection, source: str | None = None, limit: int | None = None
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM articles"
    params: list[object] = []
    if source:
        sql += " WHERE source = ?"
        params.append(source)
    sql += " ORDER BY COALESCE(published_at, fetched_at) DESC, id DESC"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return list(conn.execute(sql, params))


# ---------------------------------------------------------------------------
# Versions, token map and pre-teach
# ---------------------------------------------------------------------------

def upsert_version(
    conn: sqlite3.Connection,
    article_id: int,
    cefr_level: str,
    simplified_text: str,
    model: str = "",
    prompt_version: str = "",
    quality: str = "ok",
    notes: str = "",
) -> int:
    """Insert or replace the rendering of one article at one CEFR level.

    Re-ingesting the same (article, level) replaces the simplification and its
    dependent rows, which keeps ingest idempotent without touching the article.
    """
    row = conn.execute(
        "SELECT id FROM article_versions WHERE article_id = ? AND cefr_level = ?",
        (article_id, cefr_level),
    ).fetchone()
    if row is not None:
        version_id = int(row["id"])
        conn.execute(
            """UPDATE article_versions
                  SET simplified_text = ?, model = ?, prompt_version = ?,
                      quality = ?, notes = ?, created_at = ?
                WHERE id = ?""",
            (simplified_text, model, prompt_version, quality, notes, now_iso(), version_id),
        )
        conn.execute("DELETE FROM tokens WHERE version_id = ?", (version_id,))
        conn.execute("DELETE FROM preteach WHERE version_id = ?", (version_id,))
    else:
        cur = conn.execute(
            """INSERT INTO article_versions
                 (article_id, cefr_level, simplified_text, model, prompt_version,
                  quality, notes, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                article_id,
                cefr_level,
                simplified_text,
                model,
                prompt_version,
                quality,
                notes,
                now_iso(),
            ),
        )
        version_id = int(cur.lastrowid)
    conn.commit()
    return version_id


def get_version(
    conn: sqlite3.Connection, article_id: int, cefr_level: str
) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM article_versions WHERE article_id = ? AND cefr_level = ?",
        (article_id, cefr_level),
    ).fetchone()


def replace_tokens(conn: sqlite3.Connection, version_id: int, tokens: Sequence[dict]) -> int:
    """Write the token map for a version.  ``tokens`` are dicts of column values."""
    conn.execute("DELETE FROM tokens WHERE version_id = ?", (version_id,))
    rows = [
        (
            version_id,
            t["ordinal"],
            t["surface"],
            t["form"],
            t["start_char"],
            t["end_char"],
            t.get("syllable_count", 1),
            t.get("cefr_level"),
            t.get("definition"),
            t.get("definition_src", "none"),
            t.get("definition_en"),
            t.get("definition_vi"),
            json.dumps(t["senses_en"], ensure_ascii=False) if t.get("senses_en") else None,
            1 if t.get("ambiguous") else 0,
            json.dumps(t["candidates"], ensure_ascii=False) if t.get("candidates") else None,
        )
        for t in tokens
    ]
    conn.executemany(
        """INSERT INTO tokens
             (version_id, ordinal, surface, form, start_char, end_char,
              syllable_count, cefr_level, definition, definition_src,
              definition_en, definition_vi, senses_en, ambiguous, candidates)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()
    return len(rows)


def replace_preteach(conn: sqlite3.Connection, version_id: int, items: Iterable[dict]) -> int:
    conn.execute("DELETE FROM preteach WHERE version_id = ?", (version_id,))
    rows = [
        (
            version_id,
            item["kind"],
            item["term"],
            item.get("gloss", ""),
            item.get("gloss_en"),
            item.get("cefr_level"),
            item.get("example"),
            item.get("ordinal", 0),
        )
        for item in items
    ]
    conn.executemany(
        """INSERT OR REPLACE INTO preteach
             (version_id, kind, term, gloss, gloss_en, cefr_level, example, ordinal)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()
    return len(rows)


def get_tokens(conn: sqlite3.Connection, version_id: int) -> list[sqlite3.Row]:
    return list(
        conn.execute(
            "SELECT * FROM tokens WHERE version_id = ? ORDER BY ordinal", (version_id,)
        )
    )


def find_token_at(conn: sqlite3.Connection, version_id: int, offset: int) -> sqlite3.Row | None:
    """The token covering ``offset``, if any.

    This is the long-press hot path, so it reads the covering range directly
    instead of loading and scanning the whole token map.
    """
    return conn.execute(
        """SELECT * FROM tokens
            WHERE version_id = ? AND start_char <= ? AND end_char > ?
            ORDER BY syllable_count DESC
            LIMIT 1""",
        (version_id, offset, offset),
    ).fetchone()


def get_preteach(conn: sqlite3.Connection, version_id: int) -> dict[str, list[sqlite3.Row]]:
    rows = conn.execute(
        "SELECT * FROM preteach WHERE version_id = ? ORDER BY kind, ordinal", (version_id,)
    )
    out: dict[str, list[sqlite3.Row]] = {"vocab": [], "grammar": []}
    for row in rows:
        out[row["kind"]].append(row)
    return out


# ---------------------------------------------------------------------------
# Practice content: comprehension questions and the writing task
# ---------------------------------------------------------------------------

def replace_exercises(conn: sqlite3.Connection, version_id: int, items: Iterable[dict]) -> int:
    """Write the practice content for a version, replacing whatever was there.

    Regenerating exercises must never leave a half-updated mix of two prompts'
    questions, so the delete and the insert are one unit of work.
    """
    conn.execute("DELETE FROM exercises WHERE version_id = ?", (version_id,))
    rows = [
        (
            version_id,
            item["kind"],
            item.get("ordinal", 0),
            item["prompt"],
            json.dumps(item["options"], ensure_ascii=False) if item.get("options") else None,
            item.get("answer"),
            item.get("why", "") or "",
            json.dumps(item["key_points"], ensure_ascii=False) if item.get("key_points") else None,
            item.get("sample", "") or "",
            item.get("min_words"),
        )
        for item in items
    ]
    conn.executemany(
        """INSERT OR REPLACE INTO exercises
             (version_id, kind, ordinal, prompt, options, answer, why, key_points,
              sample, min_words)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()
    return len(rows)


def get_exercises(conn: sqlite3.Connection, version_id: int) -> dict[str, list[sqlite3.Row]]:
    """Practice rows grouped by kind, in stored order."""
    rows = conn.execute(
        "SELECT * FROM exercises WHERE version_id = ? ORDER BY kind, ordinal",
        (version_id,),
    )
    out: dict[str, list[sqlite3.Row]] = {"mcq": [], "short": [], "writing": []}
    for row in rows:
        out.setdefault(row["kind"], []).append(row)
    return out


def get_article_bundle(
    conn: sqlite3.Connection, article_id: int, cefr_level: str
) -> dict | None:
    """Everything the reading page needs for one article at one level."""
    article = get_article(conn, article_id)
    if article is None:
        return None
    version = get_version(conn, article_id, cefr_level)
    if version is None:
        return None
    return {
        "article": dict(article),
        "version": dict(version),
        "tokens": [dict(t) for t in get_tokens(conn, version["id"])],
        "preteach": {
            kind: [dict(r) for r in rows] for kind, rows in get_preteach(conn, version["id"]).items()
        },
    }


# ---------------------------------------------------------------------------
# Runtime fallback cache
# ---------------------------------------------------------------------------

def get_cached_lookup(conn: sqlite3.Connection, context_key: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM lookup_cache WHERE context_key = ?", (context_key,)
    ).fetchone()


def put_cached_lookup(
    conn: sqlite3.Connection,
    context_key: str,
    sentence: str,
    selection: str,
    form: str,
    definition: str,
    cefr_level: str | None,
) -> None:
    conn.execute(
        """INSERT OR REPLACE INTO lookup_cache
             (context_key, sentence, selection, form, definition, cefr_level, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (context_key, sentence, selection, form, definition, cefr_level, now_iso()),
    )
    conn.commit()
