"""Vercel serverless entrypoint for the Target Agent.

Vercel runs this as a stateless function: no shared memory between invocations
and an ephemeral filesystem. That is not a limitation to work around, it is the
reason the Target keeps every piece of durable state in Postgres - session
refund totals, the active prompt and policy versions, the budget ledger and the
audit chain. A deployment without Supabase configured would keep that state in
a SQLite file that vanishes between requests, so this module refuses to pretend
otherwise and returns 503 naming the missing variable.

Note on structure: ``app`` is assigned unconditionally at module level. Vercel's
build step finds the entrypoint by static analysis, so defining it only inside
a branch fails the build with "does not define a top-level app" even though the
module imports and serves correctly at runtime.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Load .env when running locally so `uvicorn api.index:app` behaves the way the
# deployed function does. On Vercel there is no .env and this is a no-op;
# configuration comes from the project environment variables.
try:
    from warden.config import load_dotenv

    load_dotenv()
except Exception:
    pass


def missing_config() -> list[str]:
    """Environment variables without which the Target cannot hold its state."""
    required = ("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY")
    return [name for name in required if not os.environ.get(name)]


# Defined at module level so the build's static check finds it. Replaced below
# by the real Target app once configuration is confirmed present.
app: FastAPI = FastAPI(
    title="Warden Target Agent (unconfigured)",
    description="Placeholder served only when required configuration is absent.",
)

_missing = missing_config()

if _missing:

    @app.api_route("/{full_path:path}", methods=["GET", "POST", "PUT", "DELETE"])
    def _misconfigured(full_path: str = "") -> JSONResponse:
        # 503, not 200: a misconfigured deployment must fail a health check
        # rather than look healthy while serving an error body.
        return JSONResponse(
            status_code=503,
            content={
                "status": "misconfigured",
                "missing": _missing,
                "detail": (
                    "The Target stores session state, versions and the audit ledger "
                    "in Postgres because serverless instances share no memory. Set "
                    "these environment variables in the Vercel project and redeploy."
                ),
            },
        )

else:
    os.environ.setdefault("WARDEN_STORE", "supabase")
    from target.app import app as _target_app

    app = _target_app
