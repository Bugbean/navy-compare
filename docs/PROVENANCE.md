# Provenance

Code in this repository that did not start here. It answers the vendor questionnaire's question about third-party code, and it tells you where to apply the same fix if you find a bug in a shared piece.

All of it was written by Chris Eade and comes from **nbl-platform**, commit `a34062b6bcf5e4afd1f9e154926254a546cdad3b` (22 Oct 2026). Files were exported with `git show a34062b:<path>`, so nothing uncommitted in that working copy came across. None of nbl-platform's git history is in this repository.

Every ported file was renamed and stripped of basketball-specific code. `tests/test_clean_source.py` fails if an NBL name, domain or concept turns up anywhere outside this file.

## Platform port (23 Oct 2026)

| This repo | From nbl-platform | Lines (from → now) | What changed |
| --- | --- | --- | --- |
| `app/auth/__init__.py` | `services/nbl_auth/__init__.py` | 822 → 792 | Cookie names `nc_at`/`nc_rt`. Shared-password ("break-glass") admin removed. `sign_up` removed (invite only). Activity log `service` is `web`. Logic otherwise unchanged: JWKS verification, refresh-once, session revocation, activity log, AI limits and cost. |
| `app/auth/mailer.py` | `services/nbl_auth/mailer.py` | 86 → 84 | Default sender and User-Agent renamed. |
| `app/auth/feedback.py` | `services/nbl_auth/feedback.py` | 135 → 127 | Table `feedback`; site `web`; no default recipient (`FEEDBACK_EMAIL`). |
| `app/auth/routes.py` | `services/dashboard/auth_routes.py` | 938 → 761 | Self-registration, the shared-password route, `_on_confirmed`/`ADMIN_EMAILS` bootstrap and the 15 Sep 2026 welcome-link repair removed. Invite route added. Emails rebranded. Package imports. |
| `app/auth/invite.py` | `scripts/create_accounts.py` (`create_one`) | new, about 90 | Invite-only account creation, by role (rep, lead, admin), plus a CLI for the first admin. |
| `app/admin/routes.py` | `services/dashboard/admin_routes.py` | 1303 → 475 | Kept: overview, activity log, AI usage, feedback. Removed: live monitor, game pipeline, game freshness, analyst log, reports. |
| `app/alerts.py` | `services/live/alert_state.py` | 198 → 197 | Docstrings only. The `AlertGate` state machine is unchanged. |
| `ai/client.py` | `services/analyst_v2/llm.py` | 65 → 67 | Models `claude-haiku-5-5` by default; `WRITER_MODEL` switches the writer. Vertex switch unchanged. |
| `app/__init__.py` | `services/dashboard/app.py` lines 27–311, 940–1025, 2520–2544 | 6968 → about 200 | Only the access-control hooks (`_authorise`, session persistence, page-view log), `/healthz` and `/api/feedback`, as a `create_app()` factory. Navy route-to-module map. |
| `app/db.py` | `services/dashboard/app.py` `get_sb` | about 30 | Unchanged. |
| `app/templates/auth/*` | `services/dashboard/templates/auth/*` | | Rebranded and restyled light. `register.html` and `break_glass.html` not brought across. Login tab script moved to `static/login.js` (CSP). |
| `app/templates/admin/*` | `services/dashboard/templates/admin/` (`_styles`, `_tabs`, `activity`, `feedback`, `overview`, `usage`, `user`, `users`) | | Shared-password and NBL site references removed. Module groups are rep and lead. Invite form added to Users. Inline script moved to `static/admin_user.js`. |
| `app/templates/forbidden.html` | same path | | Title. |
| `migrations/versions/0001_platform_baseline.py` | `database/migrations/039, 040, 041, 048, 049, 062, 066` | about 7 files → 1 | Consolidated into one Alembic baseline. Navy modules. `pipeline_runs` dropped. Foreign keys `ON DELETE SET NULL` from the start (049). "Public read" policies dropped; anon and authenticated get no grants. 12-month `fn_purge_activity_log` dropped (the plan keeps 7 years). |
| `deploy/deploy.sh` | `deploy/gcp/deploy.sh` | 539 → 185 | One web service and its jobs per environment, in place of 12 services. Secrets suffixed per environment. Migrate job runs on every deploy. |
| `deploy/cloudbuild.yaml`, `cloudbuild-deploy.yaml`, `smoke-check.sh` | `deploy/gcp/` same names | | One Dockerfile. Migrate step added before deploy. `APP_BUILD` replaces the old build variable. |
| `deploy/setup-triggers.sh` | `deploy/gcp/setup-triggers.sh` | | Two triggers: main to staging, `v*` tag to prod. |
| `deploy/setup-monitoring.sh` | `deploy/gcp/setup-monitoring.sh`, plus the failure-alert block of `deploy.sh` | | Uptime check on `/healthz`, ERROR log alert, failed-job alert. The live-feed heartbeat was removed. |
| `deploy/email-templates/magic-link.html`, `reset-password.html` | `deploy/gcp/email-templates/` | | Rebranded. `confirm-signup.html` not needed (invite only). |
| `tests/conftest.py` | same path | 42 → 34 | Also refuses a non-local `DATABASE_URL` (plan, Test data rule). |
| `tests/test_auth_core.py` | `tests/test_nbl_auth.py` | 96 → 89 | Shared-password case removed; Navy route examples. |
| `tests/test_session_persistence.py`, `test_session_revocation.py`, `test_owner_protection.py`, `test_feedback.py` | same paths | | Imports. Shared-password and NBL site cases removed. |
| `tests/test_alert_gate.py` | `tests/test_alerts.py` lines 1–190 | 484 → 189 | The pure `AlertGate` tests only. The live watcher tests were not brought across. |

New in this repo, not ported: `app/static/app.css`, `app/templates/base.html` (replaces NBL's base and its `nav.py` menu), `app/templates/home.html`, `wsgi.py`, `jobs/run_migrate.py`, `migrations/env.py`, `docker/initdb/00_supabase_shim.sql`, `docker-compose.yml`, `deploy/Dockerfile`, `tests/test_app.py`, `tests/test_clean_source.py`.

## AI layer and platform port (23 Oct 2026)

Everything else the plan reuses from nbl-platform, brought across ahead of its week so the two codebases can separate now. Same commit, `a34062b`.

| This repo | From nbl-platform | Lines (from → now) | What changed |
| --- | --- | --- | --- |
| `ai/facts.py` | `services/analyst_v2/schemas.py` (the `Fact`, `CoverageMeta`, `Insight`, `FactBundle`, `Claim`, `AnswerWriterOutput`, `VerificationResult` contracts) + `services/analyst_v3/facts.py` (fields, `EvidencePack`) | 668 + 409 → about 470 | Planner, dialogue, context and match-preview contracts not brought across. `Fact` gains `text`, `names` and `source` (plan, Fact objects). Each side of a `{navy, comp}` value is its own field in the fact's unit, so swapping sides fails verification. `CLAIMABLE_EVIDENCE_KEYS` and scope keys are Navy's (state, family type, excess, snapshot). Tool-call record becomes `QueryCall`; a season disclosure becomes a general `must_mention`. Season tokens are renamed year ranges. |
| `ai/verify.py` | `services/analyst_v3/verify.py` | 977 → about 640 | The binding verifier, `remap`, `rescue`, `quick_look` and `render_facts` (the plan's facts-only fallback). Removed: display blocks, shot-type, game-period and scoring-run token rules. Added: a banned-words check (`config/banned.yaml`). The answer schema is cut to answer, claims, unanswered and clarification. |
| `ai/verify_claims.py` | `services/analyst_v2/answer/verifier.py` | 711 → about 440 | The claim-level verifier (no bindings, cheaper output) kept as the alternative for the script route. Removed: match-preview rules (tactical consequence, "Overall read"), the competition-word check, surname-first names and shot-kind labels. The not-a-name words are insurance ones. Banned words added. |
| `ai/compose.py` | `services/analyst_v3/reasoner.py` (`_Client`, `_request`, `Usage`, streaming preview, draft parsing, verify → remap → repair → rescue) | 548 → about 330 | No tools and no rounds: facts are chosen in code before the call. Haiku 5.5 runs with thinking disabled at low effort for script speed. The refusal fallback is sent only for Sonnet 5.5 / Opus on the Claude API. One repair round, then rescue, then the facts as plain lines (fewer than 2 sentences surviving also falls back). Timeouts and API errors are outcomes, not exceptions. |
| `ai/calls.py` | `services/dashboard/app.py` `_ai_begin`, `_ai_end`, `_ai_alert`, `_ai_failed` | about 130 → 120 | Unchanged in function: per-person limit, activity log row, cost and latency, one alert per half hour. The backup-model retry (`_ai_message`) is replaced by compose's server-side fallback. |
| `ai/evidence_guard.py` | `services/analyst_v3/evidence_guard.py` | 64 → 60 | The same Requirement list pattern over an `EvidencePack`; the first requirement is the snapshot date, in place of a season. |
| `ai/prompts/writer_v1.md` | conduct rules of `services/analyst_v3/prompt.py` and `services/analyst_v2/answer/writer.py` | new | Written for a retention call: binding per figure, never calculate, digits only, no record words, never one side's figure as the other's. A draft for the sales lead to review. |
| `app/auth/__init__.py` `AI_PRICES` | same | | Haiku 5.5, Sonnet 5.5 and Opus 5.5 rates added from the Claude API reference (cached 2026-10-06). |
| `identity/name_match.py` | `services/analyst_v2/name_match.py` | 168 → 167 | Docstrings only: typo distance, safe auto-match, never pick between two close candidates. |
| `jobs/runner.py` | `services/orchestrator/run_pipeline.py` `record_job_run`, `_was_already_failing`, `_alert_job_failed`, `_alert_job_recovered` | about 90 → 150 | A `job_run()` context manager in place of the orchestrator; a `skipped` status for "no new release". One email when a job starts failing, one when it recovers. |
| `app/freshness.py` | the approach of `services/dashboard/freshness_register.py` and `admin_routes.pipeline_freshness` | 217 → about 130 | NBL's register was keyed by games. Kept: no stored status, asked on every page load. Navy's entries are the plan's (release check, live snapshot, rebuild), checked against `job_runs`. Shown on the admin overview. |
| `tests/certification/` (`harness.py`, `modes.py`, `test_corpus.py`, `corpus.yaml`) and `scripts/certification_report.py` | `scripts/certification_core_report.py`, `tests/regression/modes.py`, `tests/regression/test_certification_core.py` | | The structure: a reviewed corpus, static coverage tests, live runs only on opt-in, a classifying report. The corpus fields and scoring are the plan's (question type, slots, must-cite facts, plainness, PASS only if all 3 repeats pass, FLAKY if some). |

New with this port, not from nbl-platform: `ai/readability.py` (Flesch-Kincaid grade), `config/banned.yaml`, `config/question_types.yaml` (from the plan's question-type table), and the tests `test_verify.py`, `test_verify_claims.py`, `test_compose.py` and `test_platform_ports.py`. NBL's own tests for these modules are built on basketball facts throughout, so they were rewritten against Navy-shaped facts (`tests/ai_fixtures.py`), not ported.

## Deliberately left behind

The dashboard app (everything else in `app.py`), `nbl_auth/help.py` (help bot), `nbl_auth/nav.py` (platform menu), the v2 planner, router, validator, dialogue and query layer, the v3 tool registry, bootstrap and transcript store, `resolver.py` and the `engine/*_identity` modules (basketball identity; the plan's policy resolver is new, using `identity/name_match.py`), the FastAPI analyst service, trend alerts and the live watcher.

Nothing further from nbl-platform is planned. A fix to a ported file should be made here, and also in nbl-platform if the same bug exists there; the From column says where.
