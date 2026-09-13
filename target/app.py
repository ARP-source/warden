"""The Target Agent as a live HTTP service.

The Target is deliberately a deployed service rather than an in-process
function. That is what lets the Attacker run as a separate process (so its crash
cannot take the Target down), lets a reviewer probe the boundary directly with
curl, and makes the health endpoint a real operational surface.

Endpoints
---------
POST /v1/chat            hold a conversation; returns every tool judgement made
GET  /healthz            liveness plus spend, rates, versions, uptime
GET  /v1/versions        active versions and their full lineage
GET  /v1/policy          the policy document in force and its open gaps
POST /admin/reload       force a version reload
POST /admin/halt         trip the kill switch
POST /admin/clear-halt   re-arm after review
GET  /v1/ledger/verify   recompute the audit chain and report integrity
"""
from __future__ import annotations

import hmac
import os
import time
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from target.agent import TargetAgent, default_scope
from target.oracle import Scope
from warden import metrics
from warden import observability as obs
from warden.budget import GovernorStop, get_governor
from warden.config import get_config, resolved_mode, resolved_store
from warden.ledger import ACT_SERVICE_START, ACTOR_TARGET, get_ledger
from warden.versioning import KIND_POLICY, KIND_PROMPT

START_TIME = time.time()

cfg = get_config()
ledger = get_ledger(cfg)
governor = get_governor(cfg, ledger)
agent = TargetAgent(cfg, ledger)

app = FastAPI(
    title="Warden Target Agent",
    version="0.1.0",
    description=(
        "Nimbus Support Assistant: the agent under test. All four tools are "
        "simulated - no email is sent, no money moves, no account changes."
    ),
)


class ChatMessage(BaseModel):
    role: str = Field(default="user")
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage]
    session_id: str | None = None
    customer_id: str | None = None
    round_id: int | None = None
    idem_prefix: str | None = None
    tags: dict[str, Any] = Field(default_factory=dict)
    # Scope overrides exist so the harness can construct a session with a real
    # supervisor escalation. Nothing in a conversation can set these.
    # Benchmarking several models against the same fixed policy needs the model
    # chosen per request, not per process.
    target_model: str | None = None
    supervisor_escalation: bool | None = None
    refund_limit_usd: float | None = None
    allowed_tiers: list[int] | None = None


@app.on_event("startup")
def _on_startup() -> None:
    obs.init()
    ledger.log(
        ACTOR_TARGET, ACT_SERVICE_START, outcome="up",
        prompt_version=agent.prompt_version, schema_version=agent.policy.version,
        payload={"mode": resolved_mode(cfg), "pid": os.getpid(),
                 "base_url": cfg.target.base_url, "open_gaps": agent.policy.open_gaps(),
                 "store": resolved_store(), **obs.summary_for_health()},
    )


# A serverless instance is frozen the moment it responds, so traces sitting on
# Weave's background queue would never be sent. Long-running processes flush on
# shutdown instead; here it has to happen per request, before the response is
# handed back.
SERVERLESS = bool(os.environ.get("VERCEL"))


# Only these paths make model calls, so only these have traces to ship. Flushing
# on the others was costing a network round trip on every health check for
# nothing, which showed up as /healthz going from 0.4s to 2.2s.
TRACED_PATHS = ("/v1/chat",)


@app.middleware("http")
async def _flush_traces(request, call_next):
    response = await call_next(request)
    if SERVERLESS and request.url.path in TRACED_PATHS:
        try:
            obs.flush(timeout_s=5.0)
        except Exception:
            # Losing a trace is a reporting loss; the ledger is the record.
            pass
    return response


@app.post("/v1/chat")
def chat(req: ChatRequest) -> dict[str, Any]:
    if not req.messages:
        raise HTTPException(status_code=422, detail="messages must not be empty")
    overrides: dict[str, Any] = {}
    if req.supervisor_escalation is not None:
        overrides["supervisor_escalation"] = req.supervisor_escalation
    if req.refund_limit_usd is not None:
        overrides["refund_limit_usd"] = float(req.refund_limit_usd)
    if req.allowed_tiers is not None:
        overrides["allowed_tiers"] = tuple(int(t) for t in req.allowed_tiers)

    scope: Scope = default_scope(
        session_id=req.session_id,
        customer_id=(req.customer_id or "CUST-1041"),
        cfg=cfg, **overrides,
    )
    try:
        result = agent.handle(
            [{"role": m.role, "content": m.content} for m in req.messages],
            scope, round_id=req.round_id, idem_prefix=req.idem_prefix, tags=req.tags,
            model_override=req.target_model,
        )
    except GovernorStop as exc:
        # The governor stopping is not a server fault. Say so precisely, with a
        # 429 so a caller can back off rather than retry blindly.
        return JSONResponse(
            status_code=429,
            content={"halted": True, "reason": exc.reason, "detail": str(exc),
                     "governor": governor.snapshot()},
        )
    return result.as_dict()


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    """What a reviewer hits to see the system state live."""
    agent.reload()
    stats = metrics.attack_stats_any(ledger, ledger.run_id)
    benign = metrics.latest_benign_any(ledger, ledger.run_id)
    snap = governor.snapshot()
    healthy = not snap["halted"] and snap["remaining_usd"] > 0
    return {
        "status": "ok" if healthy else "halted",
        "service": "target",
        "uptime_s": round(time.time() - START_TIME, 1),
        "mode": resolved_mode(cfg),
        "store": resolved_store(),
        "store_note": getattr(ledger, "mirror_note", ""),
        "run_id": ledger.run_id,
        "tracing": obs.summary_for_health(),
        "versions": {
            "prompt": agent.prompt_version,
            "policy": agent.policy.version,
        },
        "open_gaps": agent.policy.open_gaps(),
        "budget": snap,
        "attacks": {
            "attempts": stats["attempts"],
            "intent_rate": stats["intent_rate"],
            "enforcement_rate": stats["enforcement_rate"],
        },
        "benign": {
            "score": benign["score"] if benign else None,
            "false_refusal_rate": benign["false_refusal_rate"] if benign else None,
            "at_versions": (f"{benign['prompt_version']}/{benign['policy_version']}"
                            if benign else None),
        },
        "ledger_rows": ledger.count(),
    }


@app.get("/v1/versions")
def versions() -> dict[str, Any]:
    return {
        "active": agent.versions(),
        "prompt_history": [
            {"version": v.version, "created_at": v.created_at, "parent": v.parent,
             "author": v.author, "rationale": v.rationale,
             "clauses": v.content.get("clauses", [])}
            for v in agent.store.history(KIND_PROMPT)
        ],
        "policy_history": [
            {"version": v.version, "created_at": v.created_at, "parent": v.parent,
             "author": v.author, "rationale": v.rationale}
            for v in agent.store.history(KIND_POLICY)
        ],
    }


@app.get("/v1/policy")
def policy() -> dict[str, Any]:
    agent.reload()
    return {
        "version": agent.policy.version,
        "document": agent.policy.doc,
        "open_gaps": agent.policy.open_gaps(),
        "note": "Gaps are enforcement gaps measured against target/oracle.py, "
                "which is fixed and not patchable.",
    }


ADMIN_TOKEN = (os.environ.get("WARDEN_ADMIN_TOKEN") or "").strip()


def require_admin(x_warden_admin: str | None = Header(default=None)) -> None:
    """Gate the admin routes. These trip the kill switch and reload versions.

    Secure by default: with no token configured the routes are disabled rather
    than left open, because this service is deployed to a public URL. An open
    /admin/halt is a kill switch anyone on the internet can pull.
    """
    if not ADMIN_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="admin endpoints are disabled; set WARDEN_ADMIN_TOKEN to enable them",
        )
    supplied = x_warden_admin or ""
    # Constant-time compare so a wrong token cannot be discovered byte by byte.
    if not hmac.compare_digest(supplied, ADMIN_TOKEN):
        raise HTTPException(status_code=401, detail="missing or invalid X-Warden-Admin header")


@app.post("/admin/reload", dependencies=[Depends(require_admin)])
def admin_reload() -> dict[str, Any]:
    changed = agent.reload(force=True)
    return {"reloaded": changed, "versions": agent.versions()}


@app.post("/admin/halt", dependencies=[Depends(require_admin)])
def admin_halt(reason: str = "manual kill switch") -> dict[str, Any]:
    governor.halt(reason, {"source": "admin_endpoint"})
    return {"halted": True, "governor": governor.snapshot()}


@app.post("/admin/clear-halt", dependencies=[Depends(require_admin)])
def admin_clear_halt() -> dict[str, Any]:
    governor.clear_halt()
    return {"halted": governor.is_halted(), "governor": governor.snapshot()}


@app.get("/v1/ledger/verify")
def verify_ledger() -> dict[str, Any]:
    return {"chain": ledger.verify_chain(), "rows": ledger.count()}


@app.get("/")
def root() -> dict[str, Any]:
    return {
        "service": "Warden Target Agent",
        "description": "Nimbus Support Assistant under permission test. All tools simulated.",
        "endpoints": ["/v1/chat", "/healthz", "/v1/versions", "/v1/policy",
                      "/v1/ledger/verify", "/docs"],
        "mode": resolved_mode(cfg),
    }
