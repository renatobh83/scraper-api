FROM mcr.microsoft.com/playwright/python:v1.61.0-noble

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    RESTART_INTERVAL=3600 \
    GRACE_SECONDS=15

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
RUN pip install playwright-stealth
# RUN pip install tor-proxy

COPY app ./app

COPY perfil_broadcast_noproxy ./app/perfil_broadcast_noproxy

COPY perfil_investing_proxy ./app/perfil_investing_proxy

RUN chmod +x ./app/entrypoint.sh

# healthcheck opcional, mas ajuda o compose a saber se está vivo
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS http://localhost:8000/health || exit 1

# Executa o Chromium como usuário sem privilégios.
USER pwuser

EXPOSE 8000

# CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
ENTRYPOINT ["./app/entrypoint.sh"]
