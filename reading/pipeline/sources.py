"""RSS ingest for VOA Tiếng Việt and BBC News Tiếng Việt.

Feed URLs live in the environment (``VOA_RSS_URLS`` / ``BBC_RSS_URLS``) because
VOA's are opaque ``/api/<token>`` paths that have changed before.

Both sources are used under their own terms, and both require attribution wherever
the text is shown, so each article carries the notice in
:data:`ATTRIBUTION`, stored alongside it rather than reconstructed at render time.
"""
from __future__ import annotations

import calendar
import time
from dataclasses import dataclass
from datetime import datetime, timezone

from reading.settings import Settings, settings as default_settings

VOA = "voa"
BBC = "bbc"

ATTRIBUTION = {
    VOA: (
        "Nguồn: VOA Tiếng Việt (voatiengviet.com). "
        "Bản quyền VOA. Nội dung được đơn giản hoá cho mục đích học tập."
    ),
    BBC: (
        "Nguồn: BBC News Tiếng Việt (bbc.com/vietnamese). "
        "© British Broadcasting Corporation. "
        "Nội dung được đơn giản hoá cho mục đích học tập."
    ),
}


@dataclass(frozen=True)
class RawArticle:
    """One feed item, before the body is fetched or simplified."""

    source: str
    guid: str
    url: str
    title: str
    summary: str
    published_at: str | None = None
    author: str | None = None

    @property
    def attribution(self) -> str:
        return ATTRIBUTION.get(self.source, "")


def _iso(value) -> str | None:
    """Convert feedparser's ``*_parsed`` struct_time (UTC) to an ISO-8601 string."""
    if not value:
        return None
    try:
        stamp = calendar.timegm(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return (
        datetime.fromtimestamp(stamp, tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def strip_html(raw: str) -> str:
    """Feed summaries occasionally carry HTML; keep the text."""
    if not raw:
        return ""
    if "<" not in raw:
        return raw.strip()
    from bs4 import BeautifulSoup

    return BeautifulSoup(raw, "lxml").get_text(" ", strip=True)


def parse_feed(content: bytes | str, source: str) -> list[RawArticle]:
    """Parse one feed document into articles.  Pure, so it is testable offline."""
    import feedparser

    feed = feedparser.parse(content)
    articles: list[RawArticle] = []
    for entry in feed.entries:
        link = (entry.get("link") or "").strip()
        title = strip_html(entry.get("title") or "")
        if not link or not title:
            continue
        guid = (entry.get("id") or link).strip()
        articles.append(
            RawArticle(
                source=source,
                guid=guid,
                url=link,
                title=title,
                summary=strip_html(entry.get("summary") or entry.get("description") or ""),
                published_at=_iso(entry.get("published_parsed") or entry.get("updated_parsed")),
                author=strip_html(entry.get("author") or "") or None,
            )
        )
    return articles


def fetch_feed(url: str, source: str, settings: Settings | None = None) -> list[RawArticle]:
    """Fetch and parse one live feed."""
    import httpx

    cfg = settings or default_settings
    headers = {"User-Agent": cfg.user_agent, "Accept": "application/rss+xml, application/xml;q=0.9, */*;q=0.8"}
    with httpx.Client(timeout=cfg.http_timeout_s, follow_redirects=True) as client:
        response = client.get(url, headers=headers)
        response.raise_for_status()
        return parse_feed(response.content, source)


def fetch_all(settings: Settings | None = None) -> list[RawArticle]:
    """Every configured feed, newest first.  A dead feed does not abort the run."""
    cfg = settings or default_settings
    collected: list[RawArticle] = []
    for url in cfg.voa_rss_urls:
        collected.extend(_safe_fetch(url, VOA, cfg))
    for url in cfg.bbc_rss_urls:
        collected.extend(_safe_fetch(url, BBC, cfg))

    # Feeds overlap (VOA's channels cross-post), so collapse on (source, guid).
    seen: set[tuple[str, str]] = set()
    unique: list[RawArticle] = []
    for article in collected:
        key = (article.source, article.guid)
        if key not in seen:
            seen.add(key)
            unique.append(article)

    unique.sort(key=lambda a: (a.published_at or "", a.guid), reverse=True)
    return unique


def _safe_fetch(url: str, source: str, settings: Settings) -> list[RawArticle]:
    try:
        return fetch_feed(url, source, settings)
    except Exception as exc:  # noqa: BLE001 - one bad feed must not stop ingest
        print(f"  warning: feed failed ({source} {url}): {exc}")
        return []


__all__ = [
    "RawArticle",
    "VOA",
    "BBC",
    "ATTRIBUTION",
    "parse_feed",
    "fetch_feed",
    "fetch_all",
    "strip_html",
]
