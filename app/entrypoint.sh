#!/usr/bin/env bash
set -e

RESTART_INTERVAL="${RESTART_INTERVAL:-3600}"
GRACE_SECONDS="${GRACE_SECONDS:-15}"
UVICORN_LIMIT_CONCURRENCY="${UVICORN_LIMIT_CONCURRENCY:-30}"

echo "[entrypoint] iniciando loop (restart a cada ${RESTART_INTERVAL}s)"

while true; do
    uvicorn app.main:app \
        --host 0.0.0.0 \
        --port 8000 \
        --workers 1 \
        --limit-concurrency "${UVICORN_LIMIT_CONCURRENCY}" \
        --no-access-log \
        --log-config ./app/log_config.yaml &
    UVICORN_PID=$!

    (
        sleep "$RESTART_INTERVAL"
        kill -TERM "$UVICORN_PID" 2>/dev/null || true
    ) &
    KILLER_PID=$!

    wait "$UVICORN_PID" || true
    kill "$KILLER_PID" 2>/dev/null || true
    wait "$KILLER_PID" 2>/dev/null || true

    echo "[entrypoint] uvicorn encerrou, aguardando ${GRACE_SECONDS}s antes de reiniciar"
    sleep "$GRACE_SECONDS"
done
