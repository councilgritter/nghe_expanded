-- 0001_init.sql — reading module initial schema.
--
-- Additive only: every future change is a new numbered file in this directory.
-- Tables are never dropped or recreated by the ingest pipeline.
--
-- Layout note: an article's *original* text is immutable, but the same article is
-- rewritten once per CEFR level.  So simplification output, its token map and its
-- pre-teach list all hang off an article_version, not off the article.

PRAGMA foreign_keys = ON;

-- --------------------------------------------------------------------------
-- Articles: one row per source article, deduplicated on (source, guid).
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS articles (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    source          TEXT    NOT NULL,              -- 'voa' | 'bbc'
    guid            TEXT    NOT NULL,              -- stable feed identifier
    source_url      TEXT    NOT NULL,
    title           TEXT    NOT NULL,
    author          TEXT,
    published_at    TEXT,                          -- ISO-8601 UTC
    fetched_at      TEXT    NOT NULL,              -- ISO-8601 UTC
    -- Body text as scraped (RSS summary if full-text fetch is off/unavailable).
    original_text   TEXT    NOT NULL,
    body_source     TEXT    NOT NULL DEFAULT 'rss',-- 'rss' | 'full'
    -- Attribution is a licence obligation for both sources, so it is stored with
    -- the article rather than reconstructed at render time.
    attribution     TEXT    NOT NULL DEFAULT '',
    UNIQUE (source, guid)
);

CREATE INDEX IF NOT EXISTS idx_articles_published ON articles (published_at DESC);

-- --------------------------------------------------------------------------
-- Article versions: one simplified rendering of an article at one CEFR level.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS article_versions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    article_id      INTEGER NOT NULL REFERENCES articles (id) ON DELETE CASCADE,
    cefr_level      TEXT    NOT NULL,              -- A1..C2
    simplified_text TEXT    NOT NULL,
    model           TEXT    NOT NULL DEFAULT '',   -- model that produced it
    prompt_version  TEXT    NOT NULL DEFAULT '',   -- prompts.PROMPT_VERSION
    -- 'ok' | 'syllable_mismatch' — the latter means the LLM altered the syllable
    -- sequence, so the token map covers only the reconcilable part.
    quality         TEXT    NOT NULL DEFAULT 'ok',
    notes           TEXT    NOT NULL DEFAULT '',
    created_at      TEXT    NOT NULL,              -- ISO-8601 UTC
    UNIQUE (article_id, cefr_level)
);

-- --------------------------------------------------------------------------
-- Token map: every tappable word in a version, with its character range in
-- article_versions.simplified_text.  This is what the frontend resolves against
-- on long-press, so it must be readable with a single indexed range scan.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS tokens (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id      INTEGER NOT NULL REFERENCES article_versions (id) ON DELETE CASCADE,
    ordinal         INTEGER NOT NULL,              -- word id used by the frontend
    surface         TEXT    NOT NULL,              -- literal text incl. spaces
    form            TEXT    NOT NULL,              -- underscore form: chúng_tôi
    start_char      INTEGER NOT NULL,              -- inclusive, into simplified_text
    end_char        INTEGER NOT NULL,              -- exclusive
    syllable_count  INTEGER NOT NULL DEFAULT 1,
    cefr_level      TEXT,                          -- NULL when unknown
    definition      TEXT,
    -- 'dictionary' | 'llm' | 'cache' | 'none'
    definition_src  TEXT    NOT NULL DEFAULT 'none',
    -- 1 when the LLM segmenter and underthesea disagreed on this span.
    ambiguous       INTEGER NOT NULL DEFAULT 0
                    CHECK (ambiguous IN (0, 1)),
    -- JSON array of the competing segmentations, when ambiguous.
    candidates      TEXT,
    UNIQUE (version_id, ordinal)
);

CREATE INDEX IF NOT EXISTS idx_tokens_range ON tokens (version_id, start_char, end_char);
CREATE INDEX IF NOT EXISTS idx_tokens_form  ON tokens (form);

-- --------------------------------------------------------------------------
-- Pre-teach content: vocabulary and grammar points surfaced before reading.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS preteach (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    version_id      INTEGER NOT NULL REFERENCES article_versions (id) ON DELETE CASCADE,
    kind            TEXT    NOT NULL CHECK (kind IN ('vocab', 'grammar')),
    term            TEXT    NOT NULL,
    gloss           TEXT    NOT NULL DEFAULT '',
    cefr_level      TEXT,
    example         TEXT,
    ordinal         INTEGER NOT NULL DEFAULT 0,
    UNIQUE (version_id, kind, term)
);

CREATE INDEX IF NOT EXISTS idx_preteach_version ON preteach (version_id, kind, ordinal);

-- --------------------------------------------------------------------------
-- Runtime fallback cache: a tapped offset with no token sends one sentence of
-- context to DeepSeek.  Results are cached here so the same context is never
-- paid for twice.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS lookup_cache (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    context_key     TEXT    NOT NULL UNIQUE,       -- hash of (sentence, selection)
    sentence        TEXT    NOT NULL,
    selection       TEXT    NOT NULL,
    form            TEXT    NOT NULL,
    definition      TEXT    NOT NULL DEFAULT '',
    cefr_level      TEXT,
    created_at      TEXT    NOT NULL
);

-- --------------------------------------------------------------------------
-- Schema bookkeeping.  db.py records which migration files have run.
-- --------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS schema_migrations (
    version         TEXT PRIMARY KEY,
    applied_at      TEXT NOT NULL
);
