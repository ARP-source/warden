# Warden Target service, containerised so it can run in a customer's own
# infrastructure rather than only on the hosted demo. The loop talks to this
# over HTTP, so the attacker, defender and orchestrator can live elsewhere.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first, so a source change does not reinstall the world.
COPY pyproject.toml requirements.txt ./
RUN pip install --no-cache-dir . && pip install --no-cache-dir "uvicorn[standard]>=0.30"

COPY . .

# Postgres is the system of record. The container keeps no durable state, so it
# is safe to kill and reschedule at any point - the audit chain lives in the
# database, not on this filesystem.
EXPOSE 8801

# Configuration is entirely by environment; see .env.example for the full list.
#   WARDEN_SUPABASE_URL, WARDEN_SUPABASE_SERVICE_ROLE_KEY  - durable state
#   WANDB_API_KEY                                          - inference + tracing
#   WARDEN_BUDGET_CEILING_USD                              - hard spend ceiling
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8801/healthz', timeout=4).status==200 else 1)"

CMD ["uvicorn", "target.app:app", "--host", "0.0.0.0", "--port", "8801"]
