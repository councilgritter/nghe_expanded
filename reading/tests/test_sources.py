"""Tests for RSS parsing and the full-text extractor.

Both run against saved fixtures rather than the live sites: the tests must not
depend on today's headlines, and neither source should be polled by CI.
"""
from __future__ import annotations

from reading.pipeline.fetch_full import extract_body
from reading.pipeline.sources import (
    ATTRIBUTION,
    BBC,
    VOA,
    parse_feed,
    strip_html,
)
from reading.tests.conftest import FIXTURES


def voa_feed():
    return (FIXTURES / "rss_voa_fixture.xml").read_bytes()


class TestFeedParsing:
    def test_reads_every_item(self):
        articles = parse_feed(voa_feed(), VOA)
        assert len(articles) == 2

    def test_captures_the_fields_ingest_needs(self):
        first = parse_feed(voa_feed(), VOA)[0]
        assert first.source == VOA
        assert first.title == "Trời đẹp ở thành phố lớn"
        assert first.url.startswith("https://www.voatiengviet.com/a/")
        assert first.guid
        assert first.published_at is not None

    def test_published_date_is_iso_utc(self):
        first = parse_feed(voa_feed(), VOA)[0]
        assert first.published_at.endswith("Z")
        assert first.published_at.startswith("2025-03-")

    def test_html_entities_are_decoded_in_the_summary(self):
        first = parse_feed(voa_feed(), VOA)[0]
        assert "&amp;" not in first.summary
        assert "&" in first.summary

    def test_markup_is_stripped_from_a_cdata_summary(self):
        second = parse_feed(voa_feed(), VOA)[1]
        assert "<p>" not in second.summary
        assert "<b>" not in second.summary
        assert "kinh tế" in second.summary

    def test_guid_is_stable_across_parses(self):
        assert parse_feed(voa_feed(), VOA)[0].guid == parse_feed(voa_feed(), VOA)[0].guid

    def test_items_without_a_link_are_skipped(self):
        xml = """<?xml version="1.0"?><rss version="2.0"><channel>
            <item><title>Không có link</title></item>
            <item><title>Có link</title><link>https://example.test/a</link></item>
        </channel></rss>"""
        assert len(parse_feed(xml, VOA)) == 1

    def test_an_empty_feed_yields_nothing(self):
        assert parse_feed('<?xml version="1.0"?><rss version="2.0"><channel/></rss>', BBC) == []


class TestAttribution:
    def test_both_sources_carry_attribution(self):
        for source in (VOA, BBC):
            assert ATTRIBUTION[source].strip()

    def test_attribution_names_the_publisher(self):
        assert "VOA" in ATTRIBUTION[VOA]
        assert "BBC" in ATTRIBUTION[BBC]

    def test_attribution_is_exposed_on_parsed_articles(self):
        assert "VOA" in parse_feed(voa_feed(), VOA)[0].attribution


class TestStripHtml:
    def test_plain_text_passes_through(self):
        assert strip_html("xin chào") == "xin chào"

    def test_tags_are_removed(self):
        assert strip_html("<p>xin <b>chào</b></p>") == "xin chào"

    def test_empty_input(self):
        assert strip_html("") == ""


class TestExtractBody:
    def body(self):
        return extract_body((FIXTURES / "bbc_page_fixture.html").read_text(encoding="utf-8"))

    def test_keeps_the_article_paragraphs(self):
        body = self.body()
        assert "Hôm nay trời rất đẹp" in body
        assert "Giáo sư giảng bài về kinh tế" in body

    def test_drops_script_and_style_contents(self):
        body = self.body()
        assert "tracker" not in body
        assert "display: none" not in body

    def test_drops_navigation_and_footer(self):
        body = self.body()
        assert "Trang chủ" not in body
        assert "Mọi chi tiết xin liên hệ" not in body

    def test_drops_figcaption_boilerplate(self):
        assert "Ảnh minh hoạ" not in self.body()

    def test_drops_the_related_sidebar(self):
        assert "Bài viết liên quan" not in self.body()

    def test_paragraphs_are_preserved(self):
        assert self.body().count("\n\n") >= 2

    def test_a_page_with_no_paragraphs_yields_nothing(self):
        assert extract_body("<html><body><div>ngắn</div></body></html>") == ""
