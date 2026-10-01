"""Fetch an article's full body and strip the page furniture around it.

The RSS summary is a usable fallback, so this step is allowed to fail: the ingester
keeps whatever it got and records ``body_source`` as ``rss`` or ``full``.

The extraction is a readability-style heuristic rather than a full port of
readability-lxml: score each ``<p>`` by text length, then keep the container whose
paragraphs hold the most text.  That is enough for VOA and BBC article pages, both
of which put the body in ordinary paragraph tags, and it keeps the dependency
surface small.  If it ever picks the wrong container, the ``body_source`` column and
the stored text make it visible rather than silent.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from reading.settings import Settings, settings as default_settings

# Elements that never contain article prose.
STRIP_SELECTORS = (
    "script",
    "style",
    "noscript",
    "nav",
    "header",
    "footer",
    "aside",
    "form",
    "figure",
    "figcaption",
    "iframe",
    "button",
    "svg",
)

# Don't bother with pages that yielded almost nothing.
MIN_BODY_CHARS = 240


@dataclass(frozen=True)
class FullText:
    text: str
    ok: bool
    reason: str = ""


def extract_body(html: str) -> str:
    """Pull the main prose out of an article page.  Pure, so it is testable offline."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    for selector in STRIP_SELECTORS:
        for node in soup.find_all(selector):
            node.decompose()

    # Score every paragraph, then let each candidate container inherit its children.
    paragraphs = []
    for node in soup.find_all("p"):
        text = node.get_text(" ", strip=True)
        if len(text) >= 40:
            paragraphs.append((node, text))
    if not paragraphs:
        return ""

    scores: dict[int, int] = {}
    owners: dict[int, object] = {}
    for node, text in paragraphs:
        parent = node.parent
        if parent is None:
            continue
        key = id(parent)
        scores[key] = scores.get(key, 0) + len(text)
        owners[key] = parent

    if not scores:
        return ""
    best_key = max(scores, key=lambda k: scores[k])
    container = owners[best_key]

    chunks: list[str] = []
    for node in container.find_all("p"):
        text = node.get_text(" ", strip=True)
        if text:
            chunks.append(text)
    return "\n\n".join(chunks).strip()


def fetch_body(url: str, settings: Settings | None = None) -> FullText:
    """Fetch one article page and extract its body."""
    cfg = settings or default_settings
    try:
        import httpx

        headers = {
            "User-Agent": cfg.user_agent,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
        }
        with httpx.Client(timeout=cfg.http_timeout_s, follow_redirects=True) as client:
            response = client.get(url, headers=headers)
            response.raise_for_status()
            html = response.text
    except Exception as exc:  # noqa: BLE001 - caller falls back to the RSS summary
        return FullText(text="", ok=False, reason=f"fetch failed: {exc}")

    try:
        body = extract_body(html)
    except Exception as exc:  # noqa: BLE001
        return FullText(text="", ok=False, reason=f"extract failed: {exc}")

    if len(body) < MIN_BODY_CHARS:
        return FullText(text=body, ok=False, reason=f"body too short ({len(body)} chars)")
    return FullText(text=body, ok=True)


def polite_delay(settings: Settings | None = None) -> None:
    """Space out requests to the news sites."""
    cfg = settings or default_settings
    if cfg.fetch_delay_s > 0:
        time.sleep(cfg.fetch_delay_s)
