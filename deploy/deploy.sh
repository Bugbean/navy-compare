#!/usr/bin/env bash
# Deploys Navy Compare to Google Cloud (Cloud Run + Cloud Scheduler) in
# australia-southeast1 (Sydney). Run from anywhere; it cd's to the repo root.
#
# Shape, per environment (ENV=staging|prod):
#   nc-<env>-web          the Flask app, public ingress
#   nc-<env>-migrate      alembic upgrade head; run by every deploy
#   nc-<env>-<job>        one Cloud Run Job per entry in JOBS below
# All from one image. Cloud Scheduler triggers the scheduled jobs directly.
#
# Prerequisites:
#   - gcloud CLI installed and authenticated: gcloud auth login
#   - A GCP project created with billing enabled, separate from any other
#     client's project
#   - Secrets already created in Secret Manager — this script references
#     them but does not create them, since their values are credentials that
#     shouldn't be typed into a shared script:
#       SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, DATABASE_URL (direct, :5432),
#       ANTHROPIC_API_KEY, RESEND_API_KEY, FLASK_SECRET_KEY
#     each suffixed with the environment, e.g. SUPABASE_URL_staging
#
# Usage:
#   ENV=staging PROJECT_ID=navy-compare-au OWNER_EMAILS=you@example.com \
#     NOTIFICATION_EMAIL=you@example.com ./deploy/deploy.sh
#
# Idempotent: re-running updates existing services/jobs/scheduler entries in
# place rather than failing.

set -euo pipefail

ENV="${ENV:?Set ENV=staging or ENV=prod}"
PROJECT_ID="${PROJECT_ID:?Set PROJECT_ID, e.g. PROJECT_ID=navy-compare-au}"
NOTIFICATION_EMAIL="${NOTIFICATION_EMAIL:?Set NOTIFICATION_EMAIL=you@example.com for job-failure alerts}"
OWNER_EMAILS="${OWNER_EMAILS:?Set OWNER_EMAILS — the account(s) no other admin can lock out}"
REGION="${REGION:-australia-southeast1}"
REPO="${REPO:-navy-compare}"
PUBLIC_BASE_URL="${PUBLIC_BASE_URL:?Set PUBLIC_BASE_URL, e.g. https://compare.example.com.au}"
MAIL_FROM="${MAIL_FROM:?Set MAIL_FROM, e.g. Navy Compare <noreply@your-verified-domain>}"
FEEDBACK_EMAIL="${FEEDBACK_EMAIL:-${NOTIFICATION_EMAIL}}"

# Plan: min 1 instance 7am to 7pm AEST. This sets the floor; the business
# hours schedule is a pair of scheduler jobs added once the pilot starts.
MIN_INSTANCES="${MIN_INSTANCES:-0}"
MAX_INSTANCES="${MAX_INSTANCES:-4}"

AR_HOST="${REGION}-docker.pkg.dev"
IMAGE="${AR_HOST}/${PROJECT_ID}/${REPO}/navy-compare:${ENV}"
WEB="nc-${ENV}-web"

# name | module | schedule ("" = invoked, not scheduled) | task timeout
# The ingest, rebuild and release-check jobs join this list when their entry
# points exist (plan, week 1 days 3 to 5).
JOBS=(
  "migrate|jobs.run_migrate||10m"
)

cd "$(dirname "$0")/.."   # repo root

echo "== Project: ${PROJECT_ID}  Region: ${REGION}  Env: ${ENV} =="

echo "-- Enabling required APIs --"
gcloud services enable \
    run.googleapis.com \
    cloudscheduler.googleapis.com \
    artifactregistry.googleapis.com \
    secretmanager.googleapis.com \
    cloudbuild.googleapis.com \
    monitoring.googleapis.com \
    --project "${PROJECT_ID}"

echo "-- Ensuring Artifact Registry repo exists --"
gcloud artifacts repositories describe "${REPO}" \
    --location="${REGION}" --project "${PROJECT_ID}" >/dev/null 2>&1 || \
gcloud artifacts repositories create "${REPO}" \
    --repository-format=docker --location="${REGION}" \
    --project "${PROJECT_ID}" --description="navy-compare images"

# Cloud Build runs as the Compute Engine default service account, and on a
# new project that account has no permissions at all — builds fail at the
# source-upload step with "does not have storage.objects.get access" before
# they even start. These grants are what it needs.
PROJECT_NUMBER=$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')
CLOUDBUILD_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
for role in roles/cloudbuild.builds.builder roles/logging.logWriter roles/storage.objectAdmin; do
    gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
        --member="serviceAccount:${CLOUDBUILD_SA}" \
        --role="${role}" \
        --condition=None >/dev/null 2>&1 || true
done
gcloud artifacts repositories add-iam-policy-binding "${REPO}" \
    --location="${REGION}" --project "${PROJECT_ID}" \
    --member="serviceAccount:${CLOUDBUILD_SA}" \
    --role="roles/artifactregistry.writer" >/dev/null 2>&1 || true

# ── Dedicated service accounts ──────────────────────────────────────────────
# nc-runner: the web service and the jobs. Secret Manager access only.
# nc-scheduler: Cloud Scheduler's identity, invoker on the scheduled jobs.
RUNNER_SA="nc-runner@${PROJECT_ID}.iam.gserviceaccount.com"
SCHEDULER_SA="nc-scheduler@${PROJECT_ID}.iam.gserviceaccount.com"
gcloud iam service-accounts describe "${RUNNER_SA}" --project "${PROJECT_ID}" >/dev/null 2>&1 || \
gcloud iam service-accounts create nc-runner \
    --project "${PROJECT_ID}" --display-name="navy-compare Cloud Run runtime"
gcloud iam service-accounts describe "${SCHEDULER_SA}" --project "${PROJECT_ID}" >/dev/null 2>&1 || \
gcloud iam service-accounts create nc-scheduler \
    --project "${PROJECT_ID}" --display-name="navy-compare Cloud Scheduler invoker"

SECRETS_LIST="SUPABASE_URL SUPABASE_SERVICE_ROLE_KEY DATABASE_URL ANTHROPIC_API_KEY RESEND_API_KEY FLASK_SECRET_KEY"
for secret in ${SECRETS_LIST}; do
    gcloud secrets describe "${secret}_${ENV}" --project "${PROJECT_ID}" >/dev/null 2>&1 || {
        echo "   missing secret ${secret}_${ENV} — create it first"; exit 1; }
    gcloud secrets add-iam-policy-binding "${secret}_${ENV}" \
        --project "${PROJECT_ID}" \
        --member="serviceAccount:${RUNNER_SA}" \
        --role="roles/secretmanager.secretAccessor" >/dev/null
done

secrets_for() {   # NAME=NAME_env:latest,... for the names given
    local out=""
    for s in "$@"; do out="${out:+${out},}${s}=${s}_${ENV}:latest"; done
    echo "${out}"
}
WEB_SECRETS=$(secrets_for SUPABASE_URL SUPABASE_SERVICE_ROLE_KEY ANTHROPIC_API_KEY RESEND_API_KEY FLASK_SECRET_KEY)
JOB_SECRETS=$(secrets_for SUPABASE_URL SUPABASE_SERVICE_ROLE_KEY DATABASE_URL ANTHROPIC_API_KEY RESEND_API_KEY)

# ── Build + push the image ──────────────────────────────────────────────────
echo "-- Building ${IMAGE} --"
gcloud builds submit \
    --project "${PROJECT_ID}" --region "${REGION}" \
    --config deploy/cloudbuild.yaml \
    --substitutions=_IMAGE="${IMAGE}" \
    .

# ── Jobs ────────────────────────────────────────────────────────────────────
for entry in "${JOBS[@]}"; do
    IFS='|' read -r name module schedule timeout <<< "${entry}"
    job="nc-${ENV}-${name}"
    echo "-- Job ${job} --"
    verb=create
    gcloud run jobs describe "${job}" --project "${PROJECT_ID}" --region "${REGION}" >/dev/null 2>&1 && verb=update
    gcloud run jobs "${verb}" "${job}" \
        --project "${PROJECT_ID}" --region "${REGION}" \
        --image "${IMAGE}" \
        --service-account "${RUNNER_SA}" \
        --command python --args "-m,${module}" \
        --set-env-vars "APP_ENV=${ENV}" \
        --set-secrets "${JOB_SECRETS}" \
        --max-retries=0 --task-timeout="${timeout}"

    if [ -n "${schedule}" ]; then
        gcloud run jobs add-iam-policy-binding "${job}" \
            --project "${PROJECT_ID}" --region "${REGION}" \
            --member="serviceAccount:${SCHEDULER_SA}" \
            --role="roles/run.invoker" >/dev/null
        gcloud scheduler jobs describe "${job}" --project "${PROJECT_ID}" --location "${REGION}" >/dev/null 2>&1 && \
            gcloud scheduler jobs delete "${job}" --project "${PROJECT_ID}" --location "${REGION}" --quiet
        gcloud scheduler jobs create http "${job}" \
            --project "${PROJECT_ID}" --location "${REGION}" \
            --schedule "${schedule}" \
            --time-zone "Australia/Sydney" \
            --uri "https://${REGION}-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/${PROJECT_ID}/jobs/${job}:run" \
            --http-method POST \
            --oauth-service-account-email "${SCHEDULER_SA}"
    fi
done

echo "-- Running migrations --"
gcloud run jobs execute "nc-${ENV}-migrate" --project "${PROJECT_ID}" --region "${REGION}" --wait

# ── Web service ─────────────────────────────────────────────────────────────
# ^|^ switches gcloud's list separator from "," to "|", because OWNER_EMAILS
# and MAIL_FROM can contain commas.
# --concurrency 8 matches gunicorn's 2 workers x 4 threads (deploy/Dockerfile).
gcloud run deploy "${WEB}" \
    --project "${PROJECT_ID}" --region "${REGION}" \
    --image "${IMAGE}" \
    --service-account "${RUNNER_SA}" \
    --allow-unauthenticated \
    --min-instances="${MIN_INSTANCES}" --max-instances="${MAX_INSTANCES}" \
    --concurrency=8 \
    --set-env-vars "^|^APP_ENV=${ENV}|PUBLIC_BASE_URL=${PUBLIC_BASE_URL}|OWNER_EMAILS=${OWNER_EMAILS}|MAIL_FROM=${MAIL_FROM}|FEEDBACK_EMAIL=${FEEDBACK_EMAIL}" \
    --set-secrets "${WEB_SECRETS}"

echo "== Done. =="
gcloud run services describe "${WEB}" --project "${PROJECT_ID}" --region "${REGION}" --format='value(status.url)'
echo "Next: ./deploy/setup-monitoring.sh for the uptime check and error alerts."
