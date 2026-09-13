"""Vercel serverless entrypoint for the Target Agent.

Vercel runs this as a stateless function: no shared memory between invocations
and an ephemeral filesystem. That is not a limitation to work around, it is the
reason the Target keeps every piece of durable state in Postgres - session
refund totals, the active prompt and policy versions, the budget ledger and the
audit chain. A deployment without Supabase configured would keep that state in a
SQLite file that vanishes between requests, so the module refuses to pretend
otherwise and says which variable is missing.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Load .env when running locally so `uvicorn api.index:app` behaves the same way
# the deployed function does. On Vercel there is no .env and this is a no-op;
# configuration comes from the project environment variables.
try:
    from warden.config import load_dotenv

    load_dotenv()
except Exception:
    pass


def _missing_config() -> list[str]:
    missing = []
    if not os.environ.get("SUPABASE_URL"):
        missing.append("SUPABASE_URL")
    if not os.environ.get("SUPABASE_SERVICE_ROLE_KEY"):
        missing.append("SUPABASE_SERVICE_ROLE_KEY")
    return missing


missing = _missing_config()
if missing:
    # Fail loudly and usefully rather than serving a Target whose state
    # evaporates between requests.
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse

    app = FastAPI(title="Warden Target Agent (misconfigured)")

    @app.get("/{full_path:path}")
    @app.post("/{full_path:path}")
    def _config_error(full_path: str = "") -> JSONResponse:
        # 503, not 200: a misconfigured deployment must fail a health check
        # rather than look healthy while serving an error body.
        return JSONResponse(
            status_code=503,
            content={
                "status": "misconfigured",
                "missing": missing,
                "detail": (
                    "The Target stores session state, versions and the audit ledger "
                    "in Postgres because serverless instances share no memory. Set "
                    "these environment variables in the Vercel project and redeploy."
                ),
            },
        )
else:
    os.environ.setdefault("WARDEN_STORE", "supabase")
    from target.app import app  # noqa: F401  (ASGI app discovered by Vercel)
