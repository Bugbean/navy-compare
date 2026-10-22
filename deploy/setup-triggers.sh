#!/usr/bin/env bash
# Creates the Cloud Build triggers: a push to main deploys staging, a tag
# v* deploys prod (plan, Environments).
#
# PREREQUISITE (one-time, manual — it needs an OAuth handshake that can't be
# scripted): connect the GitHub repo to Cloud Build.
#   Console → Cloud Build → Triggers → Connect Repository
#   → GitHub (Cloud Build GitHub App) → authorise → pick the navy-compare repo
#
# Usage:
#   PROJECT_ID=navy-compare-au GITHUB_OWNER=<owner> ./deploy/setup-triggers.sh
#
# Re-running replaces existing triggers of the same name.

set -euo pipefail

PROJECT_ID="${PROJECT_ID:?Set PROJECT_ID, e.g. PROJECT_ID=navy-compare-au}"
GITHUB_OWNER="${GITHUB_OWNER:?Set GITHUB_OWNER, the GitHub account that owns the repo}"
GITHUB_REPO="${GITHUB_REPO:-navy-compare}"
REGION="${REGION:-australia-southeast1}"
REPO="${REPO:-navy-compare}"
IMAGE_BASE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/navy-compare"

# Required where an org policy rejects triggers that don't name a
# user-managed service account: without --service-account the API returns a
# bare "INVALID_ARGUMENT: Request contains an invalid argument" that says
# nothing about the actual cause.
DEPLOYER_SA="${DEPLOYER_SA:-projects/${PROJECT_ID}/serviceAccounts/nc-deployer@${PROJECT_ID}.iam.gserviceaccount.com}"

cd "$(dirname "$0")/.."

create() {   # name, env, then --branch-pattern or --tag-pattern and its value
    local name="$1" env="$2"; shift 2
    echo "-- ${name} --"
    gcloud builds triggers delete "${name}" \
        --project "${PROJECT_ID}" --region "${REGION}" --quiet >/dev/null 2>&1 || true
    gcloud builds triggers create github \
        --project "${PROJECT_ID}" --region "${REGION}" \
        --name "${name}" \
        --repo-owner "${GITHUB_OWNER}" --repo-name "${GITHUB_REPO}" \
        "$@" \
        --build-config deploy/cloudbuild-deploy.yaml \
        --service-account "${DEPLOYER_SA}" \
        --substitutions "_IMAGE=${IMAGE_BASE}:${env},_REGION=${REGION},_ENV=${env}" \
        >/dev/null
}

create deploy-staging staging --branch-pattern '^main$'
create deploy-prod prod --tag-pattern '^v.*$'

echo
gcloud builds triggers list --project "${PROJECT_ID}" --region "${REGION}" \
    --format="table(name,github.push.branch,github.push.tag,filename)"
