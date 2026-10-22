# One image for the web service and every job; Cloud Run Jobs override the
# command (python -m jobs.run_migrate, and later run_ingest / run_rebuild).
# Build from the repo root: docker build -f deploy/Dockerfile -t navy-compare .
FROM python:3.11-slim
WORKDIR /app

# WeasyPrint (week 4, PDF) will need libpango and fonts here, about 60 MB.

COPY pyproject.toml ./
COPY app/ ./app/
COPY ai/ ./ai/
COPY jobs/ ./jobs/
COPY identity/ ./identity/
COPY config/ ./config/
COPY migrations/ ./migrations/
COPY alembic.ini wsgi.py ./
RUN pip install --no-cache-dir .

# Fail the build, not the first request, if the app cannot be imported.
RUN python -c "import wsgi"

# Which code this image is. Passed by cloudbuild-deploy.yaml as the commit
# and build id, and reported by /healthz so "has it deployed?" is answerable
# from outside without signing in.
ARG APP_BUILD=dev
ENV APP_BUILD=$APP_BUILD
ENV PORT=8080
# Unbuffered, or the app's own error lines never reach Cloud Run's logs
ENV PYTHONUNBUFFERED=1
EXPOSE 8080
# 2 workers x 4 threads (plan, Speed design). SSE streams hold a thread each;
# deploy.sh caps Cloud Run concurrency at 8 to match, so extra load starts
# another instance instead of queueing in gunicorn.
CMD exec gunicorn wsgi:app --bind 0.0.0.0:${PORT} --workers 2 --threads 4 --timeout 60
