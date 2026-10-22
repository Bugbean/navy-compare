"""The certification set has two modes.

    static   tests/certification/test_corpus.py - the corpus is internally consistent and covers every
             question type. No model and no database; runs on every pytest.
    live     sending every question to the router and writer against the fixture snapshot, 3 repeats
             each. Explicit opt-in only, because it spends money:

                 LIVE_CERTIFICATION=1 python -m pytest tests/certification -m live

Nightly on staging, the live mode runs against the full snapshot (plan, Testing).
"""
import os

import pytest

LIVE = os.environ.get("LIVE_CERTIFICATION") == "1"

live_only = pytest.mark.skipif(not LIVE, reason="live certification is explicit opt-in (LIVE_CERTIFICATION=1)")
