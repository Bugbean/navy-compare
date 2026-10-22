#!/usr/bin/env bash
# Runs the built image once and imports the app and the job entry points, so
# an image that builds but cannot start never reaches the registry or Cloud
# Run. Called from cloudbuild.yaml and cloudbuild-deploy.yaml between build
# and push.
#
#   smoke-check.sh <image>
#
# It runs the image as Cloud Run will (image WORKDIR, no env vars), with
# --entrypoint python so the CMD is not what runs.

set -euo pipefail

image="${1:?usage: smoke-check.sh <image>}"

echo "smoke-check: ${image}"
docker run --rm --entrypoint python "${image}" -c "import wsgi; import jobs.run_migrate"
echo "smoke-check: ok"
