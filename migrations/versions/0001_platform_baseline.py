"""Platform baseline: accounts, modules, activity log, feedback, job runs.

One starting point for the platform tables; docs/PROVENANCE.md lists where they came from. In short:

  * modules are Navy Compare's (call screen, review queue, overrides, pilot
    reports) in two layers, rep and lead
  * the decided_by / granted_by foreign keys are ON DELETE SET NULL from the
    start, so an admin account can be deleted
  * no "Public read" policies: RLS is on everywhere and anon / authenticated
    have no grants, so the auto-generated Supabase API exposes nothing
  * no 12-month activity_log purge: the plan keeps the audit trail 7 years

Supabase provides the auth schema, auth.users, auth.uid() and the anon,
authenticated and service_role roles. docker/initdb/ creates stand-ins for
local Postgres.

Revision ID: 0001
Revises:
"""
from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


UPGRADE = r"""
-- ── Profiles ────────────────────────────────────────────────────────────────
-- Lifecycle: invited -> 'approved'. 'pending' remains for an account created
-- any other way (the Supabase dashboard, say), which then needs an admin.
CREATE TABLE user_profiles (
    id            UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    email         TEXT NOT NULL,
    full_name     TEXT,
    phone         TEXT,
    status        TEXT NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending', 'approved', 'rejected', 'suspended')),
    is_admin      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    confirmed_at  TIMESTAMPTZ,            -- mirrors auth.users.email_confirmed_at
    notified_at   TIMESTAMPTZ,
    decided_at    TIMESTAMPTZ,            -- last approve / reject / suspend
    decided_by    UUID REFERENCES auth.users(id) ON DELETE SET NULL,
    -- Per-person AI limits; NULL = use the default
    ai_limit_minute INTEGER CHECK (ai_limit_minute >= 0),
    ai_limit_hour   INTEGER CHECK (ai_limit_hour   >= 0),
    ai_limit_day    INTEGER CHECK (ai_limit_day    >= 0),
    -- Tokens issued before this moment are refused (app.auth.resolve compares
    -- the JWT's iat against it). Set when an admin changes someone's password
    -- or sign-in address. NULL means nothing has ever been revoked.
    sessions_valid_from TIMESTAMPTZ
);
CREATE INDEX idx_user_profiles_status ON user_profiles(status);
CREATE INDEX idx_user_profiles_email  ON user_profiles(lower(email));

-- ── Modules: the unit of permission ─────────────────────────────────────────
CREATE TABLE modules (
    key         TEXT PRIMARY KEY,
    label       TEXT NOT NULL,
    layer       TEXT NOT NULL CHECK (layer IN ('rep', 'lead')),
    sort_order  INTEGER NOT NULL DEFAULT 0
);
INSERT INTO modules (key, label, layer, sort_order) VALUES
    ('call_screen',   'Call screen',          'rep',  10),
    ('review_queue',  'Identity review queue', 'lead', 110),
    ('overrides',     'Product overrides',    'lead', 120),
    ('pilot_reports', 'Pilot reports',        'lead', 130);

CREATE TABLE user_module_access (
    user_id     UUID NOT NULL REFERENCES user_profiles(id) ON DELETE CASCADE,
    module_key  TEXT NOT NULL REFERENCES modules(key) ON DELETE CASCADE,
    granted_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    granted_by  UUID REFERENCES auth.users(id) ON DELETE SET NULL,
    PRIMARY KEY (user_id, module_key)
);

-- ── Profile creation: every auth.users row gets a profile ───────────────────
CREATE FUNCTION public.handle_new_user()
RETURNS TRIGGER LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
    INSERT INTO public.user_profiles (id, email, full_name, phone, confirmed_at)
    VALUES (NEW.id, NEW.email,
            NEW.raw_user_meta_data ->> 'full_name',
            NEW.raw_user_meta_data ->> 'phone',
            NEW.email_confirmed_at)
    ON CONFLICT (id) DO NOTHING;
    RETURN NEW;
END;
$$;
CREATE TRIGGER on_auth_user_created
    AFTER INSERT ON auth.users
    FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();

CREATE FUNCTION public.handle_user_confirmed()
RETURNS TRIGGER LANGUAGE plpgsql SECURITY DEFINER SET search_path = public AS $$
BEGIN
    IF NEW.email_confirmed_at IS DISTINCT FROM OLD.email_confirmed_at THEN
        UPDATE public.user_profiles SET confirmed_at = NEW.email_confirmed_at WHERE id = NEW.id;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TRIGGER on_auth_user_confirmed
    AFTER UPDATE OF email_confirmed_at ON auth.users
    FOR EACH ROW EXECUTE FUNCTION public.handle_user_confirmed();

-- ── Activity log: the audit trail and the rate-limit source ─────────────────
-- The limiter counts from the same rows the admin sees, so what's enforced
-- and what's reported can't drift apart. Kept 7 years (plan, Audit trail).
CREATE TABLE activity_log (
    id             BIGSERIAL PRIMARY KEY,
    occurred_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    service        TEXT NOT NULL CHECK (service IN ('web', 'jobs', 'system')),
    -- email is denormalised: rows stay readable if the user is deleted, and a
    -- failed sign-in (no user id yet) still records the address attempted
    user_id        UUID REFERENCES auth.users(id) ON DELETE SET NULL,
    user_email     TEXT,
    category       TEXT NOT NULL
                   CHECK (category IN ('auth', 'admin', 'access', 'ai', 'page', 'feedback')),
    event          TEXT NOT NULL,
    path           TEXT,
    method         TEXT,
    status         INTEGER,
    ip             TEXT,
    user_agent     TEXT,
    detail         JSONB,
    -- AI events only. ai_tokens_in is the TOTAL prompt (uncached + cache
    -- write + cache read); the split is kept in detail.usage.
    ai_model       TEXT,
    ai_tokens_in   INTEGER,
    ai_tokens_out  INTEGER,
    ai_cost_usd    NUMERIC(12, 6),
    latency_ms     INTEGER
);
CREATE INDEX idx_activity_time      ON activity_log (occurred_at DESC);
CREATE INDEX idx_activity_user      ON activity_log (user_id, occurred_at DESC);
CREATE INDEX idx_activity_category  ON activity_log (category, occurred_at DESC);
CREATE INDEX idx_activity_event     ON activity_log (event, occurred_at DESC);
CREATE INDEX idx_activity_ai_quota  ON activity_log (user_id, occurred_at DESC)
    WHERE category = 'ai' AND event <> 'ai_rate_limited';
CREATE INDEX idx_activity_user_time ON activity_log (user_id, occurred_at DESC)
    WHERE user_id IS NOT NULL;

-- Everything in 'ai' counts EXCEPT ai_rate_limited — otherwise being refused
-- would itself consume quota and extend the lockout forever.
CREATE FUNCTION fn_ai_usage_counts(p_user_id UUID)
RETURNS TABLE (per_minute BIGINT, per_hour BIGINT, per_day BIGINT)
LANGUAGE sql STABLE AS $$
    SELECT
        COUNT(*) FILTER (WHERE occurred_at > NOW() - INTERVAL '1 minute'),
        COUNT(*) FILTER (WHERE occurred_at > NOW() - INTERVAL '1 hour'),
        COUNT(*)
    FROM activity_log
    WHERE user_id = p_user_id AND category = 'ai' AND event <> 'ai_rate_limited'
      AND occurred_at > NOW() - INTERVAL '1 day';
$$;

-- "Today" and "this month" are Sydney days, not UTC ones.
CREATE FUNCTION fn_admin_overview()
RETURNS JSONB LANGUAGE sql STABLE AS $$
    WITH b AS (
        SELECT
            (date_trunc('day',   NOW() AT TIME ZONE 'Australia/Sydney') AT TIME ZONE 'Australia/Sydney') AS today,
            (date_trunc('month', NOW() AT TIME ZONE 'Australia/Sydney') AT TIME ZONE 'Australia/Sydney') AS month,
            NOW() - INTERVAL '24 hours' AS day_ago
    ),
    failed_by_email AS (
        SELECT user_email, COUNT(*) AS n FROM activity_log, b
        WHERE event = 'sign_in_failed' AND occurred_at > b.day_ago
        GROUP BY user_email ORDER BY n DESC LIMIT 1
    ),
    denied_by_user AS (
        SELECT user_email, COUNT(*) AS n FROM activity_log, b
        WHERE event = 'access_denied' AND occurred_at > b.day_ago
        GROUP BY user_email ORDER BY n DESC LIMIT 1
    )
    SELECT jsonb_build_object(
        'pending_users',     (SELECT COUNT(*) FROM user_profiles WHERE status = 'pending' AND confirmed_at IS NOT NULL),
        'unconfirmed_users', (SELECT COUNT(*) FROM user_profiles WHERE status = 'pending' AND confirmed_at IS NULL),
        'approved_users',    (SELECT COUNT(*) FROM user_profiles WHERE status = 'approved'),
        'sign_ins_today',    (SELECT COUNT(*) FROM activity_log, b WHERE event = 'sign_in' AND occurred_at >= b.today),
        'failed_24h',        (SELECT COUNT(*) FROM activity_log, b WHERE event = 'sign_in_failed' AND occurred_at > b.day_ago),
        'ai_today',          (SELECT COUNT(*) FROM activity_log, b WHERE category = 'ai' AND event <> 'ai_rate_limited' AND occurred_at >= b.today),
        'cost_today',        (SELECT COALESCE(SUM(ai_cost_usd), 0) FROM activity_log, b WHERE category = 'ai' AND occurred_at >= b.today),
        'cost_month',        (SELECT COALESCE(SUM(ai_cost_usd), 0) FROM activity_log, b WHERE category = 'ai' AND occurred_at >= b.month),
        'rate_limited_24h',  (SELECT COUNT(*) FROM activity_log, b WHERE event = 'ai_rate_limited' AND occurred_at > b.day_ago),
        'denied_24h',        (SELECT COUNT(*) FROM activity_log, b WHERE event = 'access_denied' AND occurred_at > b.day_ago),
        'offtopic_24h',      (SELECT COUNT(*) FROM activity_log, b WHERE event = 'ai_rejected_offtopic' AND occurred_at > b.day_ago),
        'top_failed_email',  (SELECT user_email FROM failed_by_email),
        'top_failed_count',  (SELECT n FROM failed_by_email),
        'top_denied_email',  (SELECT user_email FROM denied_by_user),
        'top_denied_count',  (SELECT n FROM denied_by_user)
    );
$$;

CREATE FUNCTION fn_ai_usage_by_user(p_since TIMESTAMPTZ)
RETURNS TABLE (
    user_id UUID, user_email TEXT, full_name TEXT,
    questions BIGINT, offtopic BIGINT, rate_limited BIGINT, errors BIGINT,
    tokens_in BIGINT, tokens_out BIGINT, cost_usd NUMERIC, last_at TIMESTAMPTZ
)
LANGUAGE sql STABLE AS $$
    SELECT
        a.user_id, MAX(a.user_email), MAX(p.full_name),
        COUNT(*) FILTER (WHERE a.event = 'ai_question'),
        COUNT(*) FILTER (WHERE a.event = 'ai_rejected_offtopic'),
        COUNT(*) FILTER (WHERE a.event = 'ai_rate_limited'),
        COUNT(*) FILTER (WHERE a.event = 'ai_error'),
        COALESCE(SUM(a.ai_tokens_in), 0), COALESCE(SUM(a.ai_tokens_out), 0),
        COALESCE(SUM(a.ai_cost_usd), 0), MAX(a.occurred_at)
    FROM activity_log a
    LEFT JOIN user_profiles p ON p.id = a.user_id
    WHERE a.category = 'ai' AND a.occurred_at >= p_since
    GROUP BY a.user_id;
$$;

-- When each account was last doing something. Derived rather than stored: a
-- last_seen_at column would be a write on every request for a figure nobody
-- reads more than once a day.
CREATE VIEW v_user_last_activity AS
SELECT
    user_id,
    MAX(occurred_at)                                  AS last_activity,
    MAX(occurred_at) FILTER (WHERE category = 'page') AS last_page_view,
    MAX(occurred_at) FILTER (WHERE event = 'sign_in') AS last_sign_in,
    MAX(occurred_at) FILTER (WHERE category = 'ai')   AS last_ai_question,
    COUNT(*)                                          AS events,
    COUNT(*) FILTER (WHERE category = 'ai')           AS ai_questions
FROM activity_log
WHERE user_id IS NOT NULL
GROUP BY user_id;

-- ── Feedback: requests and bug reports, with the context captured server side
-- user_email and user_name are copied rather than joined, so a report stays
-- readable after the account is closed.
CREATE TABLE feedback (
    id           BIGSERIAL PRIMARY KEY,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    kind         TEXT NOT NULL CHECK (kind IN ('request', 'bug')),
    message      TEXT NOT NULL CHECK (length(btrim(message)) > 0),
    site         TEXT CHECK (site IN ('web')),
    page_key     TEXT,
    page_url     TEXT,
    user_agent   TEXT,
    user_id      UUID REFERENCES user_profiles(id) ON DELETE SET NULL,
    user_email   TEXT,
    user_name    TEXT,
    -- false with a row present: the report is safe but nobody was told
    emailed      BOOLEAN NOT NULL DEFAULT FALSE,
    status       TEXT NOT NULL DEFAULT 'new'
                 CHECK (status IN ('new', 'triaged', 'done', 'declined')),
    notes        TEXT
);
CREATE INDEX idx_feedback_recent ON feedback(created_at DESC);
CREATE INDEX idx_feedback_open   ON feedback(status, created_at DESC) WHERE status = 'new';
CREATE INDEX idx_feedback_page   ON feedback(page_key, created_at DESC);
CREATE INDEX idx_feedback_unsent ON feedback(created_at DESC) WHERE emailed = false;

-- ── Job runs: one row per scheduled job execution ───────────────────────────
-- Written by jobs/ (ingest, rebuild, release check); read by the admin
-- overview's "Scheduled jobs" tile and the freshness register.
CREATE TABLE job_runs (
    id             BIGSERIAL PRIMARY KEY,
    job_name       TEXT NOT NULL,
    started_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at    TIMESTAMPTZ,
    status         TEXT NOT NULL DEFAULT 'running'
                   CHECK (status IN ('running', 'success', 'failed', 'skipped')),
    detail         JSONB,
    error_message  TEXT
);
CREATE INDEX idx_job_runs_job_name ON job_runs(job_name, started_at DESC);

-- ── Access: service role only ───────────────────────────────────────────────
-- RLS on with no policies, and no grants to anon / authenticated, so neither
-- Supabase's public key nor a signed-in user's JWT can read these through
-- PostgREST. The app holds the service role key, which bypasses RLS.
ALTER TABLE user_profiles      ENABLE ROW LEVEL SECURITY;
ALTER TABLE modules            ENABLE ROW LEVEL SECURITY;
ALTER TABLE user_module_access ENABLE ROW LEVEL SECURITY;
ALTER TABLE activity_log       ENABLE ROW LEVEL SECURITY;
ALTER TABLE feedback           ENABLE ROW LEVEL SECURITY;
ALTER TABLE job_runs           ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON user_profiles, modules, user_module_access, activity_log, feedback, job_runs,
              v_user_last_activity
    FROM anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE
    ON user_profiles, modules, user_module_access, activity_log, feedback, job_runs
    TO service_role;
GRANT SELECT ON v_user_last_activity TO service_role;
GRANT USAGE, SELECT ON SEQUENCE activity_log_id_seq, feedback_id_seq, job_runs_id_seq TO service_role;

-- Postgres grants EXECUTE to PUBLIC by default; without the revoke anyone
-- holding the anon key could call the admin summaries through PostgREST.
REVOKE ALL ON FUNCTION fn_ai_usage_counts(UUID)         FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION fn_admin_overview()              FROM PUBLIC, anon, authenticated;
REVOKE ALL ON FUNCTION fn_ai_usage_by_user(TIMESTAMPTZ) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION fn_ai_usage_counts(UUID)         TO service_role;
GRANT EXECUTE ON FUNCTION fn_admin_overview()              TO service_role;
GRANT EXECUTE ON FUNCTION fn_ai_usage_by_user(TIMESTAMPTZ) TO service_role;
"""

DOWNGRADE = r"""
DROP TABLE IF EXISTS job_runs, feedback CASCADE;
DROP VIEW IF EXISTS v_user_last_activity;
DROP FUNCTION IF EXISTS fn_ai_usage_by_user(TIMESTAMPTZ), fn_admin_overview(), fn_ai_usage_counts(UUID);
DROP TABLE IF EXISTS activity_log CASCADE;
DROP TRIGGER IF EXISTS on_auth_user_confirmed ON auth.users;
DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
DROP FUNCTION IF EXISTS public.handle_user_confirmed(), public.handle_new_user();
DROP TABLE IF EXISTS user_module_access, modules, user_profiles CASCADE;
"""


def upgrade() -> None:
    op.execute(UPGRADE)


def downgrade() -> None:
    op.execute(DOWNGRADE)
