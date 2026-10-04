"""The ingest pipeline: RSS -> simplify -> segment -> reconcile -> persist.

    python -m reading.pipeline.ingest --cefr B1 --limit 5

One article at a time, because a long article is one expensive call and a partial
run should leave the database in a usable state.  A failure on one article is
recorded and skipped, never fatal to the run.

Idempotency: the unit of work is an (article, level) pair.  A pair already present
is skipped, so a scheduled run costs nothing once it has caught up; an article that
exists at another level is reused rather than re-fetched, and only its new level is
generated.  Pass ``--refresh`` to deliberately re-run an existing pair (after a
prompt change, say), which replaces that version's simplification, token map and
pre-teach rows in place.
"""
from __future__ import annotations

import argparse
import sys
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from reading.pipeline import offpeak, prompts, sources
from reading.pipeline.deepseek import DeepSeekClient, DeepSeekError, Simplifier
from reading.pipeline.fetch_full import fetch_body, polite_delay
from reading.pipeline.reconcile import ReconcileStats, reconcile, summarize
from reading.pipeline.segment import segment_with_underthesea
from reading.pipeline.sources import RawArticle
from reading.pipeline.text import (
    GroupingError,
    check_grouping,
    counts_to_spans,
    grouping_from_underscores,
    syllables_with_offsets,
)
from reading.pipeline.tokens import build_token_map
from reading.pipeline.vocab import definitions_from_preteach, preteach_rows
from reading.settings import Settings, settings as default_settings
from reading.storage import db
from reading.storage.dictionary import CompoundDictionary

QUALITY_OK = "ok"
QUALITY_MISMATCH = "syllable_mismatch"


@dataclass
class ArticleResult:
    source: str
    title: str
    status: str                      # ingested | duplicate | failed | skipped
    article_id: int | None = None
    version_id: int | None = None
    quality: str = QUALITY_OK
    tokens: int = 0
    reconcile: ReconcileStats | None = None
    reason: str = ""
    # Token usage DeepSeek reported for this article's one call, so the real cost
    # is visible rather than estimated.
    usage: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "ingested"


@dataclass
class RunStats:
    results: list[ArticleResult] = field(default_factory=list)
    # Feed items dropped by the dedupe pass before any work was attempted.  Counted
    # rather than turned into ArticleResults, because a steady-state run skips
    # thousands of them and the objects would be pure noise.
    skipped_duplicates: int = 0

    @property
    def ingested(self) -> int:
        return sum(1 for r in self.results if r.status == "ingested")

    @property
    def duplicates(self) -> int:
        return self.skipped_duplicates + sum(
            1 for r in self.results if r.status == "duplicate"
        )

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status == "failed")


# ---------------------------------------------------------------------------
# One article
# ---------------------------------------------------------------------------

def ingest_article(
    conn,
    raw: RawArticle,
    cefr_level: str,
    client: Simplifier,
    dictionary: CompoundDictionary | None,
    cfg: Settings | None = None,
    refresh: bool = False,
) -> ArticleResult:
    """Run one article end to end and persist it.

    ``refresh=True`` re-generates an (article, level) pair that already exists,
    replacing its version, token map and pre-teach rows in place.
    """
    cfg = cfg or default_settings
    result = ArticleResult(source=raw.source, title=raw.title, status="failed")
    level = cefr_level.upper()

    # Dedupe at the (article, level) level.  An article that already exists at
    # another level is reused rather than re-fetched: the body is immutable, and
    # only the simplification differs per level.
    existing = db.article_by_guid(conn, raw.source, raw.guid)
    if existing is not None:
        if not refresh and db.version_exists(conn, raw.source, raw.guid, level):
            result.status = "duplicate"
            result.reason = f"already ingested at {level}"
            return result
        article_id = int(existing["id"])
        body = existing["original_text"]
        body_source = existing["body_source"]
    else:
        article_id = None
        # 1. Body: full text when available, the RSS summary otherwise.
        body, body_source = raw.summary, "rss"
        if cfg.fetch_full_text:
            fetched = fetch_body(raw.url, cfg)
            polite_delay(cfg)
            if fetched.ok:
                body, body_source = fetched.text, "full"
            elif fetched.reason:
                result.reason = f"body fallback: {fetched.reason}; "

    if not body.strip():
        result.reason += "no article text available"
        return result

    # 2. Simplify at the target level.
    try:
        simplification = client.simplify(body, cefr_level)
    except (DeepSeekError, Exception) as exc:  # noqa: BLE001 - recorded, not fatal
        result.reason += f"simplify failed: {exc}"
        return result

    # 3. Parse the model's segmentation, then segment the same text ourselves.
    plain, llm_counts = grouping_from_underscores(simplification.segmented_text)
    syllables = syllables_with_offsets(plain)
    if not syllables:
        result.reason += "simplified text had no readable syllables"
        return result

    quality = QUALITY_OK
    notes = ""
    try:
        check_grouping(llm_counts, syllables, "llm")
        llm_spans = counts_to_spans(llm_counts)
    except GroupingError as exc:
        # The model's underscores did not line up. Trust underthesea alone and mark
        # the version so a human can see it, rather than emitting a mangled map.
        quality = QUALITY_MISMATCH
        notes = f"llm grouping discarded: {exc}"
        llm_spans = []

    uts_counts = segment_with_underthesea(plain)
    try:
        check_grouping(uts_counts, syllables, "underthesea")
        uts_spans = counts_to_spans(uts_counts)
    except GroupingError as exc:
        # Should not happen; a single-syllable partition is always valid.
        notes = (notes + "; " if notes else "") + f"underthesea grouping rejected: {exc}"
        uts_spans = counts_to_spans([1] * len(syllables))
        quality = QUALITY_MISMATCH

    if not llm_spans:
        llm_spans = uts_spans

    # 4. Reconcile: dictionary first, then agreement, else keep both readings.
    spans = reconcile(
        [s.text for s in syllables], uts_spans, llm_spans, dictionary=dictionary
    )
    stats = summarize(spans)

    # 5. Definitions come from the pre-teach glosses; everything else is deferred
    #    to the reader's tap-time fallback.
    definitions = definitions_from_preteach(simplification.vocab)
    tokens = build_token_map(plain, syllables, spans, definitions, dictionary)

    # 6. Persist.  A reused article is not re-inserted — only its new version is.
    if article_id is None:
        article_id = db.insert_article(
            conn,
            db.ArticleRecord(
                source=raw.source,
                guid=raw.guid,
                source_url=raw.url,
                title=raw.title,
                original_text=body,
                published_at=raw.published_at,
                author=raw.author,
                body_source=body_source,
                attribution=raw.attribution,
            ),
        )
        if article_id is None:  # lost a race with another writer
            result.status = "duplicate"
            result.reason = "inserted concurrently"
            return result

    version_id = db.upsert_version(
        conn,
        article_id=article_id,
        cefr_level=level,
        simplified_text=plain,
        model=simplification.model or "",
        prompt_version=simplification.prompt_version,
        quality=quality,
        notes=notes,
    )
    row_count = db.replace_tokens(conn, version_id, tokens)
    db.replace_preteach(
        conn,
        version_id,
        preteach_rows(
            simplification.vocab, simplification.grammar, default_cefr=level
        ),
    )

    result.status = "ingested"
    result.article_id = article_id
    result.version_id = version_id
    result.quality = quality
    result.tokens = row_count
    result.reconcile = stats
    result.usage = simplification.usage or {}
    return result


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------

def run_ingest(
    conn,
    cefr_level: str = "B1",
    client: Simplifier | None = None,
    dictionary: CompoundDictionary | None = None,
    cfg: Settings | None = None,
    articles: list[RawArticle] | None = None,
    limit: int | None = None,
    verbose: bool = True,
    refresh: bool = False,
) -> RunStats:
    """Fetch feeds (unless ``articles`` is given) and ingest what is new."""
    import httpx

    cfg = cfg or default_settings
    stats = RunStats()

    if articles is None:
        if verbose:
            print(f"Fetching feeds ({len(cfg.voa_rss_urls)} VOA, {len(cfg.bbc_rss_urls)} BBC)...")
        articles = sources.fetch_all(cfg)

    cap = cfg.ingest_limit if limit is None else limit
    # Dedupe before applying any cap.  The unit of work is the (article, level) pair:
    # an article already stored at *another* level still needs work here.
    #
    # `refresh` means "re-generate what is already there", NOT "treat everything as
    # new" — so it is bounded by the pairs already paid for.  Without that split,
    # --refresh with no --limit would re-ingest the entire feed and spend on all of it.
    fresh = [
        a
        for a in articles
        if db.version_exists(conn, a.source, a.guid, cefr_level) == refresh
    ]
    stats.skipped_duplicates = len(articles) - len(fresh)
    if cap:
        fresh = fresh[:cap]

    if verbose:
        print(f"{len(articles)} feed items, {len(fresh)} to process")

    for index, raw in enumerate(fresh, start=1):
        if verbose:
            print(f"[{index}/{len(fresh)}] {raw.source}: {raw.title[:70]}")
        try:
            result = ingest_article(
                conn, raw, cefr_level, client, dictionary, cfg, refresh=refresh
            )
        except Exception as exc:  # noqa: BLE001 - one article never kills the run
            result = ArticleResult(
                source=raw.source,
                title=raw.title,
                status="failed",
                reason=f"unhandled: {exc}",
            )
            if verbose:
                traceback.print_exc()
        stats.results.append(result)
        if verbose:
            _print_result(result)

    return stats


def _print_result(result: ArticleResult) -> None:
    if result.status == "ingested":
        stats = result.reconcile
        detail = (
            f"{result.tokens} tokens, {stats.ambiguous} ambiguous "
            f"({stats.ambiguous_pct:.0f}%), {stats.from_dictionary} from dictionary"
            if stats
            else f"{result.tokens} tokens"
        )
        flag = "" if result.quality == QUALITY_OK else f"  [{result.quality}]"
        print(f"    ingested: {detail}{flag}")
        spent = usage_line(result.usage)
        if spent:
            print(f"    cost: {spent}")
        if result.reason:
            print(f"    note: {result.reason}")
    else:
        print(f"    {result.status}: {result.reason}")


def usage_line(usage: dict) -> str:
    """Human summary of one call's token usage, from what the API reported."""
    if not usage:
        return ""
    prompt = usage.get("prompt_tokens", 0)
    completion = usage.get("completion_tokens", 0)
    cached = usage.get("prompt_cache_hit_tokens", 0)
    text = f"{prompt:,} in / {completion:,} out / {usage.get('total_tokens', 0):,} total"
    if cached:
        text += f" ({cached:,} cached)"
    return text


def run_totals(results: list[ArticleResult]) -> dict:
    """Sum the reported usage across a run, for the closing summary."""
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
              "prompt_cache_hit_tokens": 0}
    for result in results:
        for key in totals:
            totals[key] += int(result.usage.get(key, 0) or 0)
    return totals


def _open_dictionary(cfg: Settings, required: bool) -> CompoundDictionary | None:
    try:
        dictionary = CompoundDictionary.from_sqlite(
            cfg.dict_path, max_syllables=cfg.max_compound_syllables
        )
        return dictionary
    except FileNotFoundError as exc:
        if required:
            raise SystemExit(
                f"{exc}\nDictionary is required; build it with:\n"
                f"    python -m reading.tools.build_dictionary"
            )
        print(f"warning: {exc}\nContinuing without the dictionary constraint.")
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m reading.pipeline.ingest",
        description="Ingest Vietnamese news articles for reading practice.",
    )
    parser.add_argument("--cefr", default=default_settings.default_cefr,
                        choices=list(prompts.CEFR_LEVELS), help="target level")
    parser.add_argument("--limit", type=int, default=None,
                        help="max new articles this run (0 or unset = config default)")
    parser.add_argument("--source", choices=[sources.VOA, sources.BBC],
                        help="restrict to one source")
    parser.add_argument("--db", default=None, help="override database path")
    parser.add_argument("--no-fetch-full", action="store_true",
                        help="use RSS summaries only, no article pages")
    parser.add_argument("--no-dictionary", action="store_true",
                        help="skip the compound-dictionary hard constraint")
    parser.add_argument("--refresh", action="store_true",
                        help="re-generate (article, level) pairs that already exist")
    parser.add_argument("--allow-peak", action="store_true",
                        help="run even during DeepSeek's peak hours (costs double)")
    parser.add_argument("--list", action="store_true", help="list stored articles and exit")
    args = parser.parse_args(argv)

    cfg = default_settings
    if args.db:
        from dataclasses import replace

        cfg = replace(cfg, db_path=Path(args.db))
    if args.no_fetch_full:
        from dataclasses import replace

        cfg = replace(cfg, fetch_full_text=False)

    conn = db.connect(cfg.db_path)
    applied = db.migrate(conn)
    if applied:
        print(f"Applied migrations: {', '.join(applied)}")

    if args.list:
        rows = db.list_articles(conn, source=args.source)
        if not rows:
            print("No articles stored yet.")
            return 0
        for row in rows:
            print(f"  [{row['id']:>4}] {row['source']:<4} {row['title'][:70]}")
        print(f"\n{len(rows)} articles")
        return 0

    dictionary = None if args.no_dictionary else _open_dictionary(cfg, required=False)
    if dictionary is not None:
        print(f"Dictionary: {len(dictionary):,} headwords (<= {dictionary.max_syllables} syllables)")

    # Off-peak guard.  Ingest spends money, so it will not start during DeepSeek's
    # peak hours without explicit approval.  Reading the feeds is cheap but it is
    # gated too, so a scheduled run is either wholly inside the window or a no-op.
    policy = cfg.peak_policy()
    now = datetime.now(timezone.utc)
    allowed, refusal = offpeak.check(
        policy, now, allow_peak=args.allow_peak or cfg.allow_peak
    )
    print(f"Pricing: {offpeak.status_line(policy, now)}")
    if not allowed:
        print("\n" + refusal)
        return 2

    articles = None
    if args.source:
        articles = [a for a in sources.fetch_all(cfg) if a.source == args.source]

    client = DeepSeekClient(cfg)
    stats = run_ingest(
        conn,
        cefr_level=args.cefr,
        client=client,
        dictionary=dictionary,
        cfg=cfg,
        articles=articles,
        limit=args.limit,
        refresh=args.refresh,
    )
    print(
        f"\n{stats.ingested} ingested, {stats.duplicates} duplicates, "
        f"{stats.failed} failed"
    )
    spent = usage_line(run_totals(stats.results))
    if spent:
        print(f"DeepSeek usage this run: {spent}")
    return 1 if stats.failed and not stats.ingested else 0


if __name__ == "__main__":
    sys.exit(main())
