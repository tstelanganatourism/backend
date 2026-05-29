#!/usr/bin/env bash

# Exit immediately if any command fails
set -e

echo "Starting ARQ worker in the background..."
arq app.worker.WorkerSettings &

echo "Starting FastAPI web server..."
uvicorn app.main:app --host 0.0.0.0 --port $PORT
