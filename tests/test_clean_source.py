"""Nothing in this repo exists by accident.

Code reused from an earlier Bugbean platform (docs/PROVENANCE.md) arrived
renamed and stripped of that platform's sports domain. This fails if any of its
names, domains or concepts turns up in a file, so a careless copy is caught in
CI rather than by Navy Health's security review.

docs/PROVENANCE.md is exempt: naming the source is its job.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXEMPT = {ROOT / "docs" / "PROVENANCE.md", Path(__file__).resolve()}
SCANNED = {".py", ".html", ".js", ".css", ".sql", ".yaml", ".yml", ".sh", ".toml", ".ini",
           ".md", ".txt", ".example", ".cfg", ".mako"}
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache",
             ".mypy_cache"}

FORBIDDEN = re.compile(
    r"nbl|icoach|basketball|commentar|broadcast|sportradar|datacore|rosetta|"
    r"\bplayers?\b|\bgames?\b|\bseasons?\b|\bclubs?\b|\bcoach|\bcourt\b|roster|"
    r"possession|lineup|\brebound|\bdunk|layup|three-?point|\bppg\b|\bladder\b|\bleague\b|playoff|"
    r"scraper|play-by-play|\bpbp\b|break.?glass|the platform this came from",
    re.IGNORECASE)


def _files():
    for p in ROOT.rglob("*"):
        parts = p.relative_to(ROOT).parts
        if p.is_file() and p.suffix in SCANNED and p not in EXEMPT \
                and not (set(parts) & SKIP_DIRS) and not any(x.endswith(".egg-info") for x in parts):
            yield p


def test_no_references_to_the_source_platform():
    hits = []
    for p in _files():
        for n, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            m = FORBIDDEN.search(line)
            if m:
                hits.append(f"{p.relative_to(ROOT)}:{n}: {m.group(0)!r}: {line.strip()[:100]}")
    assert not hits, "source-platform references found:\n" + "\n".join(hits)
