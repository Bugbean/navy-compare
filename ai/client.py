"""The one place the app builds a model client, and the plain call shape: a
system prompt, one user message, no tools.

The writer is deliberately given no tool list. It may not query anything; the
facts it is given are the only data it sees.
"""
from __future__ import annotations

import os
from typing import Any

# Haiku for everything in the pilot. WRITER_MODEL=claude-sonnet-5-5 moves the
# free text writer to Sonnet if Haiku falls short in certification.
ROUTER_MODEL = os.environ.get("ROUTER_MODEL", "claude-haiku-5-5")
WRITER_MODEL = os.environ.get("WRITER_MODEL", "claude-haiku-5-5")

_client: Any = None


class MissingCredentialError(RuntimeError):
    """The environment lacks a variable the chosen backend needs. Its message names the variable
    and the backend, so a log line shows the cause and not a bare KeyError."""


def _require(name: str, backend: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise MissingCredentialError(
            f"{name} is not set in this process's environment (needed by the {backend} backend). "
            f"This module does not read .env itself: load it first (dotenv.load_dotenv()) "
            f"or export the variable.")
    return value


def get_client() -> Any:
    """Vertex when CLAUDE_BACKEND=vertex (keeps AI calls in australia-southeast1), else the API key."""
    global _client
    if _client is None:
        import anthropic
        if os.environ.get("CLAUDE_BACKEND") == "vertex":
            _client = anthropic.AnthropicVertex(
                region=os.environ.get("VERTEX_REGION", "australia-southeast1"),
                project_id=_require("GCP_PROJECT_ID", "vertex"))
        else:
            _client = anthropic.Anthropic(api_key=_require("ANTHROPIC_API_KEY", "anthropic-api"))
    return _client


def complete(client: Any, *, model: str, system: str, user: str, max_tokens: int) -> str:
    """One tool-less completion; the reply's text. `tools` is never passed."""
    response = client.messages.create(
        model=model, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": user}])
    return "".join(b.text for b in response.content if getattr(b, "type", None) == "text")


def extract_json(text: str) -> Any:
    """The JSON object in a model reply, which may be wrapped in a code fence or
    a sentence. ValueError if there is none."""
    import json
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("the reply holds no JSON object")
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise ValueError(f"the reply's JSON does not parse: {exc}") from None
