"""
Unit tests for the auth/limits/cost rules in app.auth.
Pure functions only: no database, no network.

    python -m pytest tests/
"""

from app import auth
from app.auth import LimitResult, ai_usage, client_ip, module_for_path, sum_usage

# ── client_ip ─────────────────────────────────────────────────────────────────

def test_client_ip_through_load_balancer():
    # "<client>, <load balancer>" — the client is second from the right
    assert client_ip("203.0.113.9, 34.102.232.19") == "203.0.113.9"


def test_client_ip_ignores_forged_prefix():
    # A caller can put anything on the left; only the LB-appended entry counts
    assert client_ip("1.1.1.1, 203.0.113.9, 34.102.232.19") == "203.0.113.9"


def test_client_ip_direct_and_missing():
    assert client_ip("203.0.113.9") == "203.0.113.9"
    assert client_ip(None, "10.0.0.1") == "10.0.0.1"
    assert client_ip("", None) is None


# ── Cost ──────────────────────────────────────────────────────────────────────

def test_sonnet_cost():
    cols, split = ai_usage("claude-sonnet-4-6", input_tokens=1000, output_tokens=500)
    assert cols["ai_cost_usd"] == 0.0105          # 1000×$3 + 500×$15 per million
    assert cols["ai_tokens_in"] == 1000 and cols["ai_tokens_out"] == 500
    assert split == {"input": 1000, "cache_write": 0, "cache_read": 0, "output": 500}


def test_dated_model_id_prices_like_its_family():
    cols, _ = ai_usage("claude-haiku-4-5-20251001", input_tokens=800, output_tokens=150)
    assert cols["ai_cost_usd"] == 0.00155


def test_cache_tokens_priced_separately_and_counted_in_prompt_size():
    cols, _ = ai_usage("claude-sonnet-4-6", input_tokens=100, output_tokens=0,
                       cache_write=1000, cache_read=10000)
    # 100×3 + 1000×3×1.25 + 10000×3×0.1 = 300 + 3750 + 3000 = 7050 per million
    assert cols["ai_cost_usd"] == 0.00705
    assert cols["ai_tokens_in"] == 11100


def test_unknown_model_records_tokens_but_no_guessed_cost():
    cols, _ = ai_usage("some-other-model", input_tokens=10, output_tokens=10)
    assert cols["ai_cost_usd"] is None and cols["ai_tokens_in"] == 10


def test_sum_usage_accepts_objects_dicts_and_none():
    class U:
        input_tokens, output_tokens = 5, 7
        cache_creation_input_tokens = cache_read_input_tokens = None
    total = sum_usage([U(), {"input_tokens": 1, "output_tokens": 2, "cache_read_input_tokens": 3}, None])
    assert total == {"input_tokens": 6, "output_tokens": 9,
                     "cache_creation_input_tokens": 0, "cache_read_input_tokens": 3}


# ── Limits ────────────────────────────────────────────────────────────────────

def test_limit_messages_never_promise_a_midnight_reset():
    msg = LimitResult(allowed=False, window="day", limit=300, used=300, retry_after=86400).message
    assert "24 hours" in msg and "tomorrow" not in msg and "midnight" not in msg


def test_no_user_is_refused():
    assert not auth.check_ai_limit(None).allowed


# ── Route → module mapping (fails closed) ────────────────────────────────────

ROUTES = [("/review/merge", "review_merge"), ("/review", "review_queue"), ("/call", "call_screen")]


def test_most_specific_route_wins():
    assert module_for_path("/review/merge", ROUTES) == "review_merge"
    assert module_for_path("/review/123", ROUTES) == "review_queue"


def test_prefix_needs_a_path_boundary():
    # /callXYZ must not inherit /call's module
    assert module_for_path("/callXYZ", ROUTES) is None
    assert module_for_path("/unmapped", ROUTES) is None
