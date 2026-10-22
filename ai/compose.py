"""One verified answer from a pack of facts: write, check, repair once, rescue, or fall back.

    facts -> WRITER model -> AnswerDraft -> verify
                                  BLOCKED -> remap (no model call) -> verify
                                          -> WRITER model, one repair round with the verifier's reasons -> verify
                                          -> rescue (drop the sentences that fail) -> verify
                                          -> fallback: the facts as plain lines (render_facts)

No tool loop and no retrieval: the facts are chosen in code before the model is called (the card's
differences, or a question type's fixed query), so the model only ever writes. Determinism comes from
structure - a JSON schema on the answer and a verifier that names what to fix - not from sampling.

The model call streams, sends the server-side refusal fallback where the model takes it, and retries
as a plain call in an environment that rejects the output format.
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Literal, Optional

from .client import WRITER_MODEL
from .facts import EvidencePack, compact_fact, compact_insight
from .verify import ANSWER_SCHEMA, AliasIndex, AnswerDraft, NameIndex, Verdict, remap, render_facts, rescue, verify

# Speed first (plan, AI layer): the script has a 3 second budget. Haiku 5.5 thinks by default; at
# effort high or below it accepts thinking disabled, which is what a short, fully-specified write needs.
# Sonnet 5.5 rejects disabled thinking, so there it is left adaptive and effort carries the cost.
EFFORT = os.environ.get("WRITER_EFFORT", "low")
THINKING = os.environ.get("WRITER_THINKING", "off")      # off | adaptive
MAX_TOKENS = int(os.environ.get("WRITER_MAX_TOKENS", "4096"))
# The server-side refusal fallback: Claude Sonnet 5.5 and the Opus line on the Claude API only. Haiku
# 5.5 has no server-side fallback, and Vertex does not take the beta.
_FALLBACKS_BETA = "server-side-fallback-2026-07-01"
_FALLBACK_MODELS = ("claude-sonnet-5-5", "claude-opus-5-5")
MIN_SENTENCES = 2

Event = Callable[[dict], None]


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    calls: int = 0

    def add(self, usage: Any) -> None:
        self.calls += 1
        self.input_tokens += getattr(usage, "input_tokens", 0) or 0
        self.output_tokens += getattr(usage, "output_tokens", 0) or 0
        self.cache_read += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.cache_write += getattr(usage, "cache_creation_input_tokens", 0) or 0


Outcome = Literal["pass", "partial", "fallback", "timeout", "error"]


@dataclass
class ComposeResult:
    """What the rep is shown (`text`), how it got there (`outcome`, as ai_calls.verifier_result), and the
    record the audit trail keeps: the draft, what the verifier said, what was dropped and why."""
    text: str
    outcome: Outcome
    draft: Optional[AnswerDraft] = None
    verdict: Optional[Verdict] = None
    raw_output: Optional[str] = None
    repaired: bool = False
    remapped: bool = False
    rescued: bool = False
    first_failures: list = field(default_factory=list)
    dropped: list = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    model: str = ""
    error: Optional[str] = None
    timings: dict = field(default_factory=dict)


class _Client:
    """The Messages API call, with the server-side refusal fallback where the model and platform take it
    and a plain-call fallback for an environment that rejects the beta or the output format."""

    def __init__(self, client: Any, model: str):
        self.client = client
        vertex = type(client).__name__.startswith("AnthropicVertex")
        self.use_fallbacks = (os.environ.get("AI_FALLBACKS", "1") != "0" and not vertex
                              and model.startswith(_FALLBACK_MODELS))
        self.use_format = True

    def create(self, *, on_text: Optional[Callable[[str], None]] = None, **kw: Any) -> Any:
        """One streamed request, returning the final message. `on_text`, when given, sees each text delta
        as it arrives - the hook that lets the script box fill while the model is still writing."""
        import anthropic

        def drain(s: Any) -> Any:
            if on_text is not None:
                for delta in s.text_stream:
                    on_text(delta)
            return s.get_final_message()

        if not self.use_format:
            kw["output_config"] = {k: v for k, v in kw.get("output_config", {}).items() if k != "format"}
        if self.use_fallbacks:
            try:
                with self.client.beta.messages.stream(betas=[_FALLBACKS_BETA], fallbacks="default", **kw) as s:
                    return drain(s)
            except (anthropic.BadRequestError, TypeError, AttributeError):
                self.use_fallbacks = False
        try:
            with self.client.messages.stream(**kw) as s:
                return drain(s)
        except anthropic.BadRequestError as exc:
            text = str(exc)
            if self.use_format and ("format" in text or "output_config" in text):
                self.use_format = False
                return self.create(on_text=on_text, **kw)
            raise


def _request(messages: list, *, model: str, system: str, schema: dict = ANSWER_SCHEMA) -> dict:
    kw: dict = {
        "model": model, "max_tokens": MAX_TOKENS,
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        # Top-level cache_control also caches the conversation so far, so the repair round re-reads the
        # facts from cache instead of in full.
        "cache_control": {"type": "ephemeral"},
        "messages": messages,
        "output_config": {"effort": EFFORT, "format": {"type": "json_schema", "schema": schema}},
    }
    if THINKING == "off" and model.startswith("claude-haiku"):
        kw["thinking"] = {"type": "disabled"}
    return kw


def _text_of(message: Any) -> str:
    # By type, never by position: a reply can open with thinking blocks.
    return "".join(getattr(b, "text", "") for b in message.content if getattr(b, "type", None) == "text")


def _extract_json(text: str) -> Any:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("the reply holds no JSON object")
    return json.loads(text[start:end + 1])


def _parse_draft(text: str) -> AnswerDraft:
    return AnswerDraft.model_validate(_extract_json(text))


_ANSWER_KEY = re.compile(r'"answer_markdown"\s*:\s*"')
_UNESCAPED_QUOTE = re.compile(r'(?<!\\)(?:\\\\)*"')


def _answer_so_far(raw: str) -> Optional[str]:
    """The answer_markdown value decoded out of a partially generated draft, or None if it has not begun.
    It is the first key of the schema, so it is written long before the claims and bindings after it."""
    m = _ANSWER_KEY.search(raw)
    if m is None:
        return None
    body = raw[m.end():]
    end = _UNESCAPED_QUOTE.search(body)
    fragment = body[:end.start()] if end else body
    try:
        return json.loads('"' + fragment + '"')
    except ValueError:
        return None


def _streamer(on_event: Optional[Event], min_growth: int = 24) -> Optional[Callable[[str], None]]:
    """An on_text callback that emits answer_preview events - the draft's prose so far - as it grows.
    None when nobody is listening. A preview is unverified: the page must replace it with the verified
    text (or the fallback) when the result arrives."""
    if on_event is None:
        return None
    raw: list[str] = []
    shown = [0]

    def on_text(delta: str) -> None:
        raw.append(delta)
        text = _answer_so_far("".join(raw))
        if text is not None and len(text) - shown[0] >= min_growth:
            shown[0] = len(text)
            _emit(on_event, type="answer_preview", text=text)

    return on_text


def _emit(on_event: Optional[Event], **event: Any) -> None:
    if on_event is not None:
        try:
            on_event(event)
        except Exception:                               # noqa: BLE001
            pass


def facts_message(pack: EvidencePack, *, question: str = "", context: Optional[dict] = None) -> str:
    """The user turn: the facts as the model may see them, the context (chips, state, family type) and
    the question when there is one."""
    payload: dict = {"facts": [compact_fact(f, id=pack.display_id.get(f.fact_id)) for f in pack.facts.values()]}
    if pack.insights:
        payload["insights"] = [compact_insight(i) for i in pack.insights.values()]
    if pack.warnings:
        payload["caveats"] = list(pack.warnings)
    if context:
        payload["context"] = context
    if question:
        payload["question"] = question
    return json.dumps(payload, ensure_ascii=False, default=str)


def _sentences(text: str) -> int:
    return len([s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if re.search(r"[A-Za-z]", s)])


def compose(pack: EvidencePack, *, client: Any, system: str, question: str = "",
            context: Optional[dict] = None, model: str = WRITER_MODEL, on_event: Optional[Event] = None,
            known_names: Optional[NameIndex] = None, aliases: Optional[AliasIndex] = None,
            banned: Iterable[str] = (), timeout_s: Optional[float] = None,
            min_sentences: int = MIN_SENTENCES) -> ComposeResult:
    """Write from `pack` and return what the rep may be shown. Never raises for a model failure: a
    timeout or an API error is an outcome, and the text is then the facts themselves."""
    import anthropic
    banned = list(banned)
    started = time.monotonic()
    result = ComposeResult(text="", outcome="fallback", model=model)
    if not pack.facts:
        result.text, result.outcome = "", "fallback"
        result.error = "no facts to write from"
        return result

    api_client = client.with_options(timeout=timeout_s) if timeout_s and hasattr(client, "with_options") else client
    api = _Client(api_client, model)
    messages: list = [{"role": "user", "content": facts_message(pack, question=question, context=context)}]
    checks = dict(known_names=known_names, aliases=aliases, question=question, banned=banned)

    def finish(outcome: Outcome) -> ComposeResult:
        result.outcome = outcome
        if outcome in ("fallback", "timeout", "error"):
            cited = [f for c in (result.draft.claims if result.draft else []) for f in c.fact_ids]
            result.text = render_facts(pack, prefer=cited)
        result.timings["total_ms"] = int((time.monotonic() - started) * 1000)
        return result

    try:
        t0 = time.monotonic()
        _emit(on_event, type="model_start", stage="write", model=model)
        message = api.create(on_text=_streamer(on_event), **_request(messages, model=model, system=system))
        result.timings["write_ms"] = int((time.monotonic() - t0) * 1000)
        result.usage.add(message.usage)
        if message.stop_reason == "refusal":
            result.error = "the model declined to write (refusal)"
            return finish("fallback")
        if message.stop_reason == "max_tokens":
            result.error = "the answer was cut off (max_tokens)"
            return finish("fallback")
        result.raw_output = _text_of(message)
        try:
            draft = _parse_draft(result.raw_output)
        except Exception as exc:                        # noqa: BLE001
            result.error = f"the answer was not the expected JSON: {str(exc)[:200]}"
            return finish("fallback")

        verdict = verify(draft, pack, **checks)
        result.draft, result.verdict = draft, verdict
        result.first_failures = list(verdict.failures)
        _emit(on_event, type="verify", status=verdict.status, failures=verdict.failures)
        if verdict.status != "BLOCKED":
            result.text = draft.answer_markdown
            return finish("pass")

        fixed = remap(draft, verdict, pack, **checks)
        if fixed is not None:
            result.draft, result.verdict = fixed
            result.remapped = True
            result.text = result.draft.answer_markdown
            return finish("pass")

        # One repair round (plan: "the writer is retried once with the verifier's reasons appended").
        result.repaired = True
        messages.append({"role": "assistant", "content": message.content})
        messages.append({"role": "user", "content": "The checker rejected the draft:\n- " + "\n- ".join(verdict.failures)
                         + "\n\nReturn the corrected JSON. Bind every figure to the fact field that holds it, "
                           "remove any figure or name you cannot bind, and keep the rest as it was."})
        t0 = time.monotonic()
        _emit(on_event, type="model_start", stage="repair", model=model)
        message = api.create(on_text=_streamer(on_event), **_request(messages, model=model, system=system))
        result.timings["repair_ms"] = int((time.monotonic() - t0) * 1000)
        result.usage.add(message.usage)
        if message.stop_reason in ("end_turn", "stop_sequence"):
            try:
                draft2 = _parse_draft(_text_of(message))
                verdict2 = verify(draft2, pack, **checks)
                if verdict2.status != "BLOCKED" or len(verdict2.failures) <= len(verdict.failures):
                    result.draft, result.verdict = draft2, verdict2
                    result.raw_output = _text_of(message)
            except Exception as exc:                    # noqa: BLE001
                _emit(on_event, type="repair", ok=False, reason=str(exc)[:120])

        if result.verdict.status == "BLOCKED":
            result.dropped = list(result.verdict.failures)
            saved = rescue(result.draft, result.verdict, pack, **checks)
            if saved is None:
                return finish("fallback")
            result.draft, result.verdict = saved
            result.rescued = True
            if _sentences(result.draft.answer_markdown) < min_sentences:
                return finish("fallback")
            result.text = result.draft.answer_markdown
            return finish("partial")
        result.text = result.draft.answer_markdown
        return finish("pass")
    except anthropic.APITimeoutError as exc:
        result.error = f"timed out: {str(exc)[:200]}"
        return finish("timeout")
    except anthropic.APIError as exc:
        result.error = f"model call failed: {type(exc).__name__}: {str(exc)[:200]}"
        return finish("error")
