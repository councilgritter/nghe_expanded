"""Unit tests for the article-body extractor.

This file exists because the extractor shipped without one and was quietly wrong:
it grouped paragraphs by their *immediate* parent, so on a page that wraps each
``<p>`` in its own ``<div>`` — which is what BBC does — every parent held exactly
one paragraph, the scores tied, and ``max()`` returned whichever came first in the
document.  Live BBC articles were stored as a single 315-character paragraph from an
8,000-character article, and nothing failed loudly.

So the cases below are the shapes that actually occur on the two sources: the
per-paragraph-wrapper layout that used to break, prose mixed with navigation and
related-links furniture, and pages where full text simply is not available.
"""
from __future__ import annotations

from reading.pipeline.fetch_full import (
    MIN_PARAGRAPH_CHARS,
    extract_body,
)

PROSE = (
    "Thủ tướng Mark Carney tuần trước thừa nhận rằng ông đã chuẩn bị cho một khả "
    "năng rất khó xảy ra, nhưng không thể loại trừ hoàn toàn."
)


def paragraphs(*texts: str) -> str:
    return "".join(f"<p>{t}</p>" for t in texts)


class TestPerParagraphWrapper:
    """The BBC layout: each paragraph sits in its own container."""

    def build(self, count: int = 6) -> str:
        # 48 distinct parents in the real page; the shape is what matters.
        body = "".join(
            f'<div class="paragraph"><p>{PROSE} Đoạn số {i}.</p></div>'
            for i in range(count)
        )
        return (
            "<html><body>"
            "<nav><p>" + ("Điều hướng " * 20) + "</p></nav>"
            '<main><div class="article-body">' + body + "</div></main>"
            "</body></html>"
        )

    def test_returns_every_paragraph_not_just_the_first(self):
        html = self.build(count=6)
        # Six prose paragraphs plus nav; the article prose is what must come back.
        assert extract_body(html).count("Đoạn số") == 6

    def test_is_not_dominated_by_the_first_paragraph(self):
        """The regression: one paragraph in, one paragraph out."""
        html = self.build(count=6)
        body = extract_body(html)
        assert len(body) > 3 * len(PROSE)

    def test_paragraph_breaks_are_preserved(self):
        """The reader rebuilds paragraphs from blank lines, so they must survive."""
        body = extract_body(self.build(count=4))
        assert body.count("\n\n") == 3


class TestFurniture:
    def test_navigation_and_related_links_are_dropped(self):
        html = (
            "<html><body>"
            "<nav>" + paragraphs("Trang chủ " * 30, "Thế giới " * 30) + "</nav>"
            "<article>" + paragraphs(PROSE, PROSE + " Hai.") + "</article>"
            "<aside>" + paragraphs("Tin liên quan " * 30) + "</aside>"
            "<footer>" + paragraphs("Bản quyền " * 30) + "</footer>"
            "</body></html>"
        )
        body = extract_body(html)
        assert "Tin liên quan" not in body
        assert "Bản quyền" not in body

    def test_link_farm_paragraphs_are_dropped(self):
        """A paragraph that is mostly anchor text is a teaser, not prose."""
        links = "".join(f'<a href="/x{i}">Bài viết liên quan số {i} khác</a>' for i in range(6))
        assert len(links) >= MIN_PARAGRAPH_CHARS
        html = (
            "<html><body><article>"
            + paragraphs(PROSE, PROSE + " Hai.", PROSE + " Ba.")
            + f"<p>{links}</p>"
            + "</article></body></html>"
        )
        assert "Bài viết liên quan số 0" not in extract_body(html)

    def test_short_captions_are_not_prose(self):
        html = (
            "<html><body><article>"
            + paragraphs(PROSE, PROSE + " Hai.")
            + "<p>Ảnh: VOA</p>"
            + "</article></body></html>"
        )
        assert "Ảnh: VOA" not in extract_body(html)


class TestDegeneratePages:
    def test_page_with_no_usable_paragraph_returns_empty(self):
        assert extract_body("<html><body><div>Ngắn.</div></body></html>") == ""

    def test_single_paragraph_page_still_returns_it(self):
        html = f"<html><body><article><p>{PROSE}</p></article></body></html>"
        assert extract_body(html) == PROSE

    def test_body_is_plain_text_without_markup(self):
        html = (
            "<html><body><article>"
            + paragraphs(f"{PROSE} <b>Nhấn mạnh</b> và <i>nghiêng</i>.")
            + "</article></body></html>"
        )
        body = extract_body(html)
        assert "<" not in body and ">" not in body
        assert "Nhấn mạnh" in body

    def test_paragraph_inside_a_stripped_element_is_ignored(self):
        html = (
            "<html><body>"
            "<article>" + paragraphs(PROSE, PROSE + " Hai.") + "</article>"
            "<script><p>" + ("Mã " * 60) + "</p></script>"
            "</body></html>"
        )
        assert "Mã Mã" not in extract_body(html)
