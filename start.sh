#!/usr/bin/env bash

# Memory hardening for 512 MB containers (Render Free Tier):
# 1. Limit glibc malloc arenas to 2 (prevents 100-200 MB arena fragmentation on multi-core hosts)
# 2. Run FastAPI + embedded ARQ worker in ONE single Python process instead of two full Python runtimes
export MALLOC_ARENA_MAX="${MALLOC_ARENA_MAX:-2}"

if [ "${RUN_SEPARATE_WORKER:-false}" = "true" ]; then
  (
    while true; do
      echo "[start.sh] Starting separate ARQ worker..."
      arq app.worker.WorkerSettings || echo "[start.sh] ARQ worker exited (code $?). Restarting in 5s..."
      sleep 5
    done
  ) &
fi

echo "[start.sh] Starting unified FastAPI + background worker server on port ${PORT:-8000}..."
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --limit-concurrency 64 --timeout-keep-alive 5
