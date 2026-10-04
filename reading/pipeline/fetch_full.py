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

# A paragraph shorter than this is a caption, a byline or a teaser, not prose.
MIN_PARAGRAPH_CHARS = 40

# A paragraph whose text is mostly anchor text is navigation or a related-links
# block rather than article prose.
MAX_LINK_DENSITY = 0.5

# The chosen container must hold at least this share of the page's qualifying
# prose, so a container that captured only part of the article is not accepted.
CONTAINER_COVERAGE = 0.95


@dataclass(frozen=True)
class FullText:
    text: str
    ok: bool
    reason: str = ""


def _link_density(node) -> float:
    """Share of a paragraph's text that sits inside anchors (1.0 = all of it)."""
    text = node.get_text(" ", strip=True)
    if not text:
        return 1.0
    linked = sum(len(a.get_text(" ", strip=True)) for a in node.find_all("a"))
    return min(1.0, linked / len(text))


def _depth(node) -> int:
    """How many ancestors a node has — used to prefer the tightest container."""
    depth = 0
    for _ in node.parents:
        depth += 1
    return depth


def _qualifying_paragraphs(soup) -> list:
    """Paragraphs that look like article prose, in document order."""
    out = []
    for node in soup.find_all("p"):
        if len(node.get_text(" ", strip=True)) < MIN_PARAGRAPH_CHARS:
            continue
        if _link_density(node) > MAX_LINK_DENSITY:
            continue
        out.append(node)
    return out


def extract_body(html: str) -> str:
    """Pull the main prose out of an article page.  Pure, so it is testable offline.

    Scoring *ancestors* rather than immediate parents is the whole point.  Many
    news sites (BBC among them) wrap every paragraph in its own ``<div>``, so each
    immediate parent holds exactly one paragraph and a max() over them returns
    whichever came first in the document — a single paragraph, silently presented
    as the article.  Scoring every ancestor and then keeping the deepest one that
    still holds essentially all of the page's prose finds the real body container
    without also reaching for ``<body>`` and its furniture.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "lxml")
    for selector in STRIP_SELECTORS:
        for node in soup.find_all(selector):
            node.decompose()

    paragraphs = _qualifying_paragraphs(soup)
    if not paragraphs:
        return ""

    lengths = {id(node): len(node.get_text(" ", strip=True)) for node in paragraphs}
    total = sum(lengths.values())

    scores: dict[int, int] = {}
    owners: dict[int, object] = {}
    for node in paragraphs:
        for ancestor in node.parents:
            key = id(ancestor)
            scores[key] = scores.get(key, 0) + lengths[id(node)]
            owners[key] = ancestor

    if not scores:
        return ""

    # Deepest container still covering ~all of the prose: the article body, not
    # <body> (which drags in furniture) and not a per-paragraph wrapper.
    threshold = total * CONTAINER_COVERAGE
    candidates = [key for key, score in scores.items() if score >= threshold]
    container = owners[max(candidates, key=lambda key: _depth(owners[key]))]

    wanted = {id(node) for node in paragraphs}
    chunks: list[str] = []
    for node in container.find_all("p"):
        if id(node) not in wanted:
            continue
        text = node.get_text(" ", strip=True)
        if text:
            chunks.append(text)
    if not chunks:
        # The container narrowed past the prose (a malformed tree, say); the
        # qualifying paragraphs are still the best answer available.
        chunks = [node.get_text(" ", strip=True) for node in paragraphs]
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
