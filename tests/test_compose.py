"""Write, check, repair once, rescue, or fall back - with a fake model, so every path runs offline."""
import json
from types import SimpleNamespace

import anthropic
import httpx
import pytest

from ai import compose as cm
from tests import ai_fixtures as fx

GOOD = {"answer_markdown": "Navy Health pays up to $1,000 on a crown. Bupa Silver Plus pays up to $600.",
        "claims": [{"claim_id": "c1", "text": "Navy Health pays up to $1,000 on a crown.", "kind": "plain",
                    "fact_ids": ["F3"], "insight_ids": [], "bindings": ["1,000|F3|value.navy"]},
                   {"claim_id": "c2", "text": "Bupa Silver Plus pays up to $600.", "kind": "plain",
                    "fact_ids": ["F3"], "insight_ids": [], "bindings": ["600|F3|value.comp"]}],
        "unanswered": [], "needs_clarification": False}
# Wrong in a way remap cannot fix: $750 is held nowhere.
BAD = {**GOOD, "answer_markdown": "Navy Health pays up to $750 on a crown.",
       "claims": [{**GOOD["claims"][0], "text": "Navy Health pays up to $750 on a crown.", "bindings": []}]}


def message(body, stop="end_turn"):
    text = body if isinstance(body, str) else json.dumps(body)
    return SimpleNamespace(content=[SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)],
                           stop_reason=stop, usage=SimpleNamespace(input_tokens=900, output_tokens=120,
                                                                   cache_read_input_tokens=800,
                                                                   cache_creation_input_tokens=0))


class Stream:
    def __init__(self, msg):
        self.msg = msg
        self.text_stream = [b.text for b in msg.content if b.type == "text"]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return self.msg


class FakeClient:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.requests = []
        self.messages = SimpleNamespace(stream=self._stream)

    def _stream(self, **kw):
        self.requests.append(kw)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return Stream(reply)


def run(*replies, **kw):
    client = FakeClient(*replies)
    return cm.compose(fx.pack(), client=client, system="sys", **kw), client


def test_a_verified_draft_is_shown_as_written():
    r, client = run(message(GOOD))
    assert r.outcome == "pass" and r.text == GOOD["answer_markdown"]
    assert len(client.requests) == 1 and r.usage.calls == 1


def test_the_request_is_tool_free_cached_and_fast():
    _, client = run(message(GOOD))
    kw = client.requests[0]
    assert "tools" not in kw
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"} and kw["cache_control"]
    assert kw["output_config"]["effort"] == "low" and kw["output_config"]["format"]["type"] == "json_schema"
    assert kw["thinking"] == {"type": "disabled"}        # Haiku, script speed


def test_sonnet_keeps_adaptive_thinking():
    client = FakeClient(message(GOOD))
    client.beta = SimpleNamespace(messages=SimpleNamespace(stream=client._stream))
    cm.compose(fx.pack(), client=client, system="sys", model="claude-sonnet-5-5")
    assert "thinking" not in client.requests[0]
    assert client.requests[0]["fallbacks"] == "default"   # refusal fallback on Sonnet 5.5


def test_a_blocked_draft_gets_one_repair_with_the_reasons():
    r, client = run(message(BAD), message(GOOD))
    assert r.outcome == "pass" and r.repaired and r.first_failures
    assert "rejected the draft" in client.requests[1]["messages"][-1]["content"]


def test_a_repair_that_fails_falls_back_to_the_facts():
    r, _ = run(message(BAD), message(BAD))
    assert r.outcome == "fallback"
    assert r.text.startswith("- Navy Health pays up to $1,000 on a crown")


def test_not_json_falls_back():
    r, _ = run(message("sure, here you go"))
    assert r.outcome == "fallback" and "JSON" in r.error


def test_a_timeout_is_an_outcome_not_an_exception():
    r, _ = run(anthropic.APITimeoutError(request=httpx.Request("POST", "https://x")))
    assert r.outcome == "timeout" and r.text


def test_the_preview_streams_the_answer_as_it_is_written():
    events = []
    cm.compose(fx.pack(), client=FakeClient(message(GOOD)), system="sys", on_event=events.append)
    assert any(e["type"] == "answer_preview" for e in events)


@pytest.mark.parametrize("raw, expected", [
    ('{"answer_markdown": "Navy Health pa', "Navy Health pa"),
    ('{"claims": []', None),
])
def test_answer_so_far(raw, expected):
    assert cm._answer_so_far(raw) == expected
