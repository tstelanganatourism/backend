#!/usr/bin/env bash

# NOTE: We do NOT use "set -e" here because we want the restart loop
# to survive individual worker crashes without killing the whole script.

# ── ARQ Worker: infinite auto-restart loop ────────────────────────────────────
# If the worker crashes (OOM, network error, any reason), it will automatically
# restart after 5 seconds. This ensures emails are NEVER stuck permanently.
(
  while true; do
    echo "[start.sh] Starting ARQ worker..."
    arq app.worker.WorkerSettings || echo "[start.sh] ARQ worker exited (code $?). Restarting in 5s..."
    sleep 5
  done
) &

echo "[start.sh] Starting FastAPI web server..."
exec uvicorn app.main:app --host 0.0.0.0 --port $PORT
