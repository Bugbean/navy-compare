"""From how the rep describes the member's household in plain words to a usage profile the estimator
(engine/estimate.py) can price: which billed items, how many times a year, for how many people.

One tool-forced model call. The model only reads the words; it never sees or states a benefit, limit
or dollar figure, and anything it returns that is not an allowed item is dropped. The rep sees the
profile and can change every line before (and after) it is priced.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config.reference import EXTRAS_ITEMS
from engine.estimate import MAX_PEOPLE, MAX_TIMES, Use, clean_uses

USAGE_PROMPT = Path(__file__).resolve().parent / "prompts" / "usage_v1.md"

USAGE_TOOL = {
    "name": "usage_profile",
    "description": "The extras the member's household will claim in a typical year.",
    "input_schema": {
        "type": "object",
        "properties": {
            "uses": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "item": {"type": "string", "enum": list(EXTRAS_ITEMS)},
                        "times": {"type": "integer", "minimum": 1, "maximum": MAX_TIMES,
                                  "description": "How many times a year, for each person"},
                        "people": {"type": "integer", "minimum": 1, "maximum": MAX_PEOPLE,
                                   "description": "How many people in the household claim it"},
                    },
                    "required": ["item", "times", "people"],
                },
            },
            "unclear": {"type": "array", "items": {"type": "string"},
                        "description": "What the rep said that was left out because it is not an extras "
                                       "claim, in the rep's own few words. Never an assumption or a note."},
        },
        "required": ["uses"],
    },
}


@dataclass
class UsageRead:
    uses: list[Use] = field(default_factory=list)
    unclear: list[str] = field(default_factory=list)
    usage: Any = None


def item_menu() -> str:
    """The items the model may choose from, one per line, with the words a rep would use."""
    return "\n".join(f"{k}: {label}" for k, (_svc, label) in EXTRAS_ITEMS.items())


def read_usage(text: str, *, client: Any, model: str, household: str = "") -> UsageRead:
    system = USAGE_PROMPT.read_text(encoding="utf-8").replace("{items}", item_menu())
    content = (f"Household: {household}\n" if household else "") + f"The rep says: {text}"
    response = client.messages.create(
        model=model, max_tokens=800,
        system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        tools=[USAGE_TOOL], tool_choice={"type": "tool", "name": "usage_profile"},
        messages=[{"role": "user", "content": content}])
    usage = getattr(response, "usage", None)
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == "usage_profile":
            d = dict(block.input)
            unclear = [u.strip()[:80] for u in d.get("unclear") or [] if isinstance(u, str) and u.strip()][:5]
            return UsageRead(uses=clean_uses(d.get("uses")), unclear=unclear, usage=usage)
    return UsageRead(usage=usage)
