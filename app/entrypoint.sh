#!/usr/bin/env bash
set -e

RESTART_INTERVAL="${RESTART_INTERVAL:-3600}"   # padrão 1h
GRACE_SECONDS="${GRACE_SECONDS:-15}"           # tempo pro uvicorn fechar limpo

echo "[entrypoint] iniciando loop (restart a cada ${RESTART_INTERVAL}s)"

while true; do
    uvicorn app.main:app \
        --host 0.0.0.0 \
        --port 8000 \
        --workers 1 \
        --limit-concurrency 2 \
        --no-access-log \
        --log-config /app/log_config.yaml &
    UVICORN_PID=$!

    # espera X segundos OU até o uvicorn morrer sozinho (o que vier primeiro)
    # 'wait -n' do bash retorna assim que qualquer filho terminar
    (
        sleep "$RESTART_INTERVAL"
        kill -TERM "$UVICORN_PID" 2>/dev/null || true
    ) &
    KILLER_PID=$!

    wait "$UVICORN_PID" || true       # espera o uvicorn sair

    kill "$KILLER_PID" 2>/dev/null || true   # mata o timer se ainda estiver vivo
    wait "$KILLER_PID" 2>/dev/null || true

    echo "[entrypoint] uvicorn encerrou, aguardando ${GRACE_SECONDS}s antes de reiniciar"
    sleep "$GRACE_SECONDS"
done