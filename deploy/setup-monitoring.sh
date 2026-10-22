#!/usr/bin/env bash
# Monitoring that lives outside the processes it watches.
#
# A service that has crashed cannot tell you it has crashed. So this adds
# the checks that run in Google's infrastructure rather than ours:
#
#   1. an uptime check on /healthz (public, no secrets in its output)
#   2. an alert when that check fails
#   3. an alert on any ERROR the web service or a job logs, which is the
#      backstop for when the app is too broken to send its own email
#   4. an alert on any failed Cloud Run Job execution
#
# The app's own alerts (stale snapshot, verifier fallback rate, AI latency)
# go through app/alerts.py and email; these are the layer underneath.
#
# Usage:
#   ENV=staging PROJECT_ID=navy-compare-au NOTIFICATION_EMAIL=you@example.com \
#     HOST=compare-staging.example.com.au ./deploy/setup-monitoring.sh
#
# Re-running is safe: everything is created only if it is missing.

set -euo pipefail

ENV="${ENV:?Set ENV=staging or ENV=prod}"
PROJECT_ID="${PROJECT_ID:?Set PROJECT_ID, e.g. PROJECT_ID=navy-compare-au}"
NOTIFICATION_EMAIL="${NOTIFICATION_EMAIL:?Set NOTIFICATION_EMAIL — where alerts go}"
HOST="${HOST:?Set HOST, the public hostname the web service answers on}"
WEB="nc-${ENV}-web"

# Written to real files rather than passed via process substitution: <(...)
# yields a /proc/<pid>/fd/NN path, which the Windows-native gcloud can't open
# when this script runs under Git Bash.
TMP=$(mktemp -d -t nc-monitoring.XXXXXX)
trap 'rm -rf "${TMP}"' EXIT
winpath() { cygpath -w "$1" 2>/dev/null || echo "$1"; }

policy_exists() {
    # Matched with grep rather than --filter: gcloud's server-side filter on
    # displayName can silently match nothing, which would create duplicates.
    gcloud alpha monitoring policies list --project "${PROJECT_ID}" \
        --format='value(displayName)' 2>/dev/null | grep -qxF "$1"
}

echo "== Notification channel =="
CHANNEL=$(gcloud beta monitoring channels list --project "${PROJECT_ID}" \
    --format='value(name,displayName)' 2>/dev/null \
    | grep -F "navy-compare alerts" | head -n1 | cut -f1)
if [ -z "${CHANNEL}" ]; then
    CHANNEL=$(gcloud beta monitoring channels create --project "${PROJECT_ID}" \
        --display-name="navy-compare alerts" \
        --type=email \
        --channel-labels="email_address=${NOTIFICATION_EMAIL}" \
        --format="value(name)")
    echo "   created ${CHANNEL}"
else
    echo "   using ${CHANNEL}"
fi

echo
echo "== 1 + 2. Uptime check on /healthz =="
CHECK_NAME="${WEB} healthz"
if gcloud monitoring uptime list-configs --project "${PROJECT_ID}" \
        --format='value(displayName)' 2>/dev/null | grep -qxF "${CHECK_NAME}"; then
    echo "   already exists"
else
    gcloud monitoring uptime create "${CHECK_NAME}" \
        --project "${PROJECT_ID}" \
        --resource-type=uptime-url \
        --resource-labels="host=${HOST},project_id=${PROJECT_ID}" \
        --path=/healthz --protocol=https --period=5 \
        --matcher-content='"db": "ok"' --matcher-type=contains-string >/dev/null
    echo "   created"
fi
POLICY_UP="${WEB}: /healthz failing"
if policy_exists "${POLICY_UP}"; then
    echo "   uptime alert already exists"
else
    cat > "${TMP}/up.json" <<JSON
{
  "displayName": "${POLICY_UP}",
  "documentation": {"content": "/healthz is failing from Google's probes, or no longer reports db ok. Check Cloud Run logs for ${WEB}; roll back the revision if a deploy just went out.", "mimeType": "text/markdown"},
  "conditions": [{
    "displayName": "Uptime check failing",
    "conditionThreshold": {
      "filter": "metric.type=\"monitoring.googleapis.com/uptime_check/check_passed\" AND resource.type=\"uptime_url\" AND resource.labels.host=\"${HOST}\"",
      "comparison": "COMPARISON_GT", "thresholdValue": 1, "duration": "300s",
      "aggregations": [{"alignmentPeriod": "300s", "perSeriesAligner": "ALIGN_NEXT_OLDER",
                        "crossSeriesReducer": "REDUCE_COUNT_FALSE", "groupByFields": ["resource.label.host"]}],
      "trigger": {"count": 1}
    }
  }],
  "combiner": "OR", "enabled": true, "notificationChannels": ["${CHANNEL}"]
}
JSON
    gcloud alpha monitoring policies create --project "${PROJECT_ID}" \
        --policy-from-file="$(winpath "${TMP}/up.json")" >/dev/null
    echo "   uptime alert created"
fi

echo
echo "== 3. ERROR lines from the web service or a job =="
POLICY_ERR="nc-${ENV}: errors logged"
if policy_exists "${POLICY_ERR}"; then
    echo "   already exists"
else
    cat > "${TMP}/err.json" <<JSON
{
  "displayName": "${POLICY_ERR}",
  "documentation": {"content": "The web service or a job logged at ERROR. Cloud Logging has the detail.", "mimeType": "text/markdown"},
  "conditions": [{
    "displayName": "ERROR logged by nc-${ENV}-*",
    "conditionMatchedLog": {
      "filter": "(resource.type=\\"cloud_run_revision\\" AND resource.labels.service_name=\\"${WEB}\\" OR resource.type=\\"cloud_run_job\\" AND resource.labels.job_name:\\"nc-${ENV}-\\") AND severity>=ERROR"
    }
  }],
  "alertStrategy": {"notificationRateLimit": {"period": "1800s"}, "autoClose": "3600s"},
  "combiner": "OR", "enabled": true, "notificationChannels": ["${CHANNEL}"]
}
JSON
    gcloud alpha monitoring policies create --project "${PROJECT_ID}" \
        --policy-from-file="$(winpath "${TMP}/err.json")" >/dev/null
    echo "   created — at most one email per 30 minutes"
fi

echo
echo "== 4. Failed job executions =="
POLICY_JOB="nc-${ENV}: Cloud Run Job failure"
if policy_exists "${POLICY_JOB}"; then
    echo "   already exists"
else
    cat > "${TMP}/job.yaml" <<EOF
displayName: "${POLICY_JOB}"
combiner: OR
conditions:
  - displayName: "Any nc-${ENV}-* execution failed"
    conditionThreshold:
      filter: >-
        resource.type="cloud_run_job" AND
        metric.type="run.googleapis.com/job/completed_execution_count" AND
        metric.label.result="failed"
      comparison: COMPARISON_GT
      thresholdValue: 0
      duration: 0s
      aggregations:
        - alignmentPeriod: 300s
          perSeriesAligner: ALIGN_COUNT
          crossSeriesReducer: REDUCE_SUM
EOF
    gcloud alpha monitoring policies create --project "${PROJECT_ID}" \
        --notification-channels="${CHANNEL}" \
        --policy-from-file="$(winpath "${TMP}/job.yaml")" >/dev/null
    echo "   created"
fi

echo
echo "Done."
gcloud alpha monitoring policies list --project "${PROJECT_ID}" \
    --format="table(displayName,enabled)" 2>/dev/null || true
