"""The Supabase client for the web app.

The platform pieces (accounts, activity log, feedback) talk to Supabase
through PostgREST with the service role key. The market data and comparison
tables the plan adds are read with psycopg and plain SQL instead; that
connection lives with the engine when it arrives.
"""
from __future__ import annotations

import os
import threading

from supabase import Client, create_client

_SB_LOCK = threading.Lock()
_SB: "Client | None" = None


def get_sb() -> Client:
    """The Supabase client for this worker, built once.

    Building one per call was measured at about 215ms, and a query through a
    fresh client at ~535ms against ~50ms through a warm one, because each new
    client opens a new TLS connection and throws it away after one question.

    Shared across the worker's threads deliberately. The underlying httpx
    client is safe to use from several threads and pools its connections;
    PostgREST query builders are created fresh per .table() call, so nothing
    mutable is shared between two requests.
    """
    global _SB
    if _SB is None:
        with _SB_LOCK:
            if _SB is None:      # another thread may have won the race
                _SB = create_client(os.environ["SUPABASE_URL"],
                                    os.environ["SUPABASE_SERVICE_ROLE_KEY"])
    return _SB
