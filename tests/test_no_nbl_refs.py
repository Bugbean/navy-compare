"""Nothing in this repo exists by accident.

Code ported from nbl-platform (docs/PROVENANCE.md) must arrive renamed and
stripped of basketball. This fails if any NBL name, domain or concept turns up
in a source file, so a careless copy is caught in CI rather than by Navy
Health's security review.

docs/PROVENANCE.md and CLAUDE.md are exempt: naming the source is their job.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXEMPT = {ROOT / "docs" / "PROVENANCE.md", ROOT / "CLAUDE.md", Path(__file__).resolve()}
SCANNED = {".py", ".html", ".js", ".css", ".sql", ".yaml", ".yml", ".sh", ".toml", ".ini",
           ".md", ".txt", ".example", ".cfg", ".mako"}
SKIP_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".ruff_cache",
             ".mypy_cache"}

FORBIDDEN = re.compile(
    r"nbl|icoach|basketball|commentary|broadcast|sportradar|datacore|"
    r"\bplayers?\b|\bgames?\b|\bseasons?\b|possession|"
    r"scraper|play-by-play|\bpbp\b|break.?glass",
    re.IGNORECASE)


def _files():
    for p in ROOT.rglob("*"):
        parts = p.relative_to(ROOT).parts
        if p.is_file() and p.suffix in SCANNED and p not in EXEMPT \
                and not (set(parts) & SKIP_DIRS) and not any(x.endswith(".egg-info") for x in parts):
            yield p


def test_no_nbl_references_in_source():
    hits = []
    for p in _files():
        for n, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            m = FORBIDDEN.search(line)
            if m:
                hits.append(f"{p.relative_to(ROOT)}:{n}: {m.group(0)!r}: {line.strip()[:100]}")
    assert not hits, "NBL references found:\n" + "\n".join(hits)
