"""Tests for the DeepSeek client's stream handling.

The client streams, because the configured model reasons before answering and a
non-streamed request for a long article gets dropped by the server mid-generation.
That makes :func:`_read_stream` the path every article takes, so it is tested against
canned events rather than only against the live API — including the two things that
would silently corrupt a bundle: reasoning text leaking into the article, and usage
going unreported.
"""
from __future__ import annotations

import json

from reading.pipeline.deepseek import _read_stream, parse_json_object


class FakeStream:
    """The subset of an httpx streaming response that _read_stream touches."""

    def __init__(self, lines):
        self._lines = lines

    def iter_lines(self):
        yield from self._lines


def frame(delta: dict) -> str:
    return "data: " + json.dumps({"choices": [{"delta": delta}]})


class TestReadStream:
    def test_content_deltas_are_concatenated(self):
        stream = FakeStream([frame({"content": '{"a":'}), frame({"content": "1}"}), "data: [DONE]"])
        content, _ = _read_stream(stream)
        assert content == '{"a":1}'

    def test_the_result_parses_as_the_json_it_was_built_to_be(self):
        payload = json.dumps({"segmented_text": "Chúng_tôi đi học."})
        stream = FakeStream([frame({"content": payload[i:i + 7]}) for i in range(0, len(payload), 7)])
        content, _ = _read_stream(stream)
        assert parse_json_object(content)["segmented_text"] == "Chúng_tôi đi học."

    def test_reasoning_content_is_not_part_of_the_answer(self):
        """It is billed, but it must never end up in the article."""
        stream = FakeStream([
            frame({"reasoning_content": "We need to simplify this article to A2..."}),
            frame({"content": '{"segmented_text": "xin chào"}'}),
            frame({"reasoning_content": "more private deliberation"}),
            "data: [DONE]",
        ])
        content, _ = _read_stream(stream)
        assert "simplify this article" not in content
        assert "private deliberation" not in content
        assert json.loads(content)["segmented_text"] == "xin chào"

    def test_usage_is_picked_up_from_a_trailing_frame(self):
        usage = {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30}
        stream = FakeStream([
            frame({"content": "{}"}),
            "data: " + json.dumps({"choices": [], "usage": usage}),
            "data: [DONE]",
        ])
        _, got = _read_stream(stream)
        assert got == usage

    def test_usage_defaults_to_empty_when_never_sent(self):
        stream = FakeStream([frame({"content": "{}"}), "data: [DONE]"])
        _, got = _read_stream(stream)
        assert got == {}

    def test_a_malformed_frame_does_not_abort_the_stream(self):
        stream = FakeStream([
            frame({"content": '{"a"'}),
            "data: {not json",
            "",
            ": keep-alive",
            frame({"content": ":1}"}),
            "data: [DONE]",
        ])
        content, _ = _read_stream(stream)
        assert content == '{"a":1}'

    def test_stopping_at_done_ignores_anything_after_it(self):
        stream = FakeStream([frame({"content": "kept"}), "data: [DONE]", frame({"content": "dropped"})])
        content, _ = _read_stream(stream)
        assert content == "kept"

    def test_an_empty_stream_yields_empty_content(self):
        assert _read_stream(FakeStream([])) == ("", {})

    def test_a_null_delta_is_tolerated(self):
        stream = FakeStream(['data: {"choices":[{"delta":{}}]}', frame({"content": "ok"}), "data: [DONE]"])
        content, _ = _read_stream(stream)
        assert content == "ok"
