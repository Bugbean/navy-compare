"""Versioned prompt files. The version is the file name, recorded on every AI call (ai_calls.prompt_version),
so a change to a prompt is a new file and a pull request that re-runs the certification set."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).resolve().parent


@lru_cache(maxsize=None)
def load(version: str) -> str:
    """The prompt text for a version such as "writer_v1". Cached: the text is the cached prefix of every
    call, so it must not change within a process."""
    return (HERE / f"{version}.md").read_text(encoding="utf-8").strip()
