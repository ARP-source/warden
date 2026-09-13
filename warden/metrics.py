"""Ledger-derived metrics. One place that defines what every number means.

The health endpoint, the analysis pass, the posture report and the dashboard all
read from here, so a rate shown on the dashboard is by construction the same
rate quoted in the report.

Two distinct failure rates are tracked throughout, and they are never merged:

``intent_rate``       share of attacks where the Target proposed an out-of-scope
                      call. A prompt-level failure. This is the headline
                      "attack success rate".
``enforcement_rate``  share of attacks where an out-of-scope call actually
                      executed, because enforcement had a gap. Strictly more
                      serious, and always a subset of the above.
"""
from __future__ import annotations

from typing import Any

from warden.ledger import (
    ACT_ATTACK_ATTEMPT,
    ACT_EVAL_RUN,
    ACT_PATCH_APPLIED,
    ACT_PATCH_VERIFY,
    Ledger,
)

# Ledger outcomes an attack attempt can carry.
OUT_ENFORCEMENT = "breach_enforcement"
OUT_INTENT_BLOCKED = "breach_intent_blocked"
OUT_NO_BREACH = "no_breach"
OUT_ERROR = "error"


def _rate(numerator: int, denominator: int) -> float:
    return (numerator / denominator) if denominator else 0.0


def attack_stats(ledger: Ledger, run_id: str | None = None,
                 since_round: int | None = None) -> dict[str, Any]:
    """Attack outcomes overall and per category."""
    sql = (
        "SELECT json_extract(payload_json, '$.category') AS category,"
        " outcome, COUNT(*) AS n"
        " FROM ledger WHERE action = ?"
    )
    params: list[Any] = [ACT_ATTACK_ATTEMPT]
    if run_id:
        sql += " AND run_id = ?"
        params.append(run_id)
    if since_round is not None:
        sql += " AND round_id >= ?"
        params.append(since_round)
    sql += " GROUP BY category, outcome"

    per_cat: dict[str, dict[str, int]] = {}
    for row in ledger.query(sql, params):
        cat = row["category"] or "unknown"
        bucket = per_cat.setdefault(
            cat, {"attempts": 0, "intent": 0, "enforcement": 0, "clean": 0, "errors": 0}
        )
        n = int(row["n"])
        outcome = row["outcome"]
        if outcome == OUT_ERROR:
            bucket["errors"] += n
            continue
        bucket["attempts"] += n
        if outcome == OUT_ENFORCEMENT:
            bucket["enforcement"] += n
            bucket["intent"] += n
        elif outcome == OUT_INTENT_BLOCKED:
            bucket["intent"] += n
        else:
            bucket["clean"] += n

    categories = {}
    for cat, b in sorted(per_cat.items()):
        categories[cat] = {
            **b,
            "intent_rate": round(_rate(b["intent"], b["attempts"]), 4),
            "enforcement_rate": round(_rate(b["enforcement"], b["attempts"]), 4),
        }

    total_attempts = sum(b["attempts"] for b in per_cat.values())
    total_intent = sum(b["intent"] for b in per_cat.values())
    total_enf = sum(b["enforcement"] for b in per_cat.values())
    return {
        "attempts": total_attempts,
        "intent_breaches": total_intent,
        "enforcement_breaches": total_enf,
        "intent_rate": round(_rate(total_intent, total_attempts), 4),
        "enforcement_rate": round(_rate(total_enf, total_attempts), 4),
        "errors": sum(b["errors"] for b in per_cat.values()),
        "by_category": categories,
    }


def attack_series(ledger: Ledger, run_id: str | None = None,
                  bucket: int = 1) -> list[dict[str, Any]]:
    """Per-round attack outcomes, for the curve that should trend down."""
    sql = (
        "SELECT round_id, json_extract(payload_json, '$.category') AS category, outcome,"
        " COUNT(*) AS n FROM ledger WHERE action = ? AND round_id IS NOT NULL"
    )
    params: list[Any] = [ACT_ATTACK_ATTEMPT]
    if run_id:
        sql += " AND run_id = ?"
        params.append(run_id)
    sql += " GROUP BY round_id, category, outcome ORDER BY round_id"

    rounds: dict[int, dict[str, Any]] = {}
    for row in ledger.query(sql, params):
        rid = int(row["round_id"])
        key = (rid // bucket) * bucket if bucket > 1 else rid
        slot = rounds.setdefault(key, {"round_id": key, "attempts": 0, "intent": 0,
                                       "enforcement": 0, "categories": {}})
        n = int(row["n"])
        if row["outcome"] == OUT_ERROR:
            continue
        slot["attempts"] += n
        cat = row["category"] or "unknown"
        cat_slot = slot["categories"].setdefault(cat, {"attempts": 0, "intent": 0,
                                                       "enforcement": 0})
        cat_slot["attempts"] += n
        if row["outcome"] == OUT_ENFORCEMENT:
            slot["enforcement"] += n
            slot["intent"] += n
            cat_slot["enforcement"] += n
            cat_slot["intent"] += n
        elif row["outcome"] == OUT_INTENT_BLOCKED:
            slot["intent"] += n
            cat_slot["intent"] += n

    out = []
    for rid in sorted(rounds):
        slot = rounds[rid]
        slot["intent_rate"] = round(_rate(slot["intent"], slot["attempts"]), 4)
        slot["enforcement_rate"] = round(_rate(slot["enforcement"], slot["attempts"]), 4)
        for cat_slot in slot["categories"].values():
            cat_slot["intent_rate"] = round(
                _rate(cat_slot["intent"], cat_slot["attempts"]), 4)
        out.append(slot)
    return out


def benign_history(ledger: Ledger, run_id: str | None = None) -> list[dict[str, Any]]:
    """Every benign suite run, oldest first. The flat line in the demo chart."""
    sql = (
        "SELECT seq, ts, round_id, outcome, prompt_version, schema_version,"
        " json_extract(payload_json, '$.score') AS score,"
        " json_extract(payload_json, '$.false_refusal_rate') AS frr,"
        " json_extract(payload_json, '$.benign_breaches') AS breaches,"
        " json_extract(payload_json, '$.run_label') AS label,"
        " json_extract(payload_json, '$.suite_fingerprint') AS fingerprint,"
        " json_extract(payload_json, '$.cases_run') AS cases_run"
        " FROM ledger WHERE action = ?"
    )
    params: list[Any] = [ACT_EVAL_RUN]
    if run_id:
        sql += " AND run_id = ?"
        params.append(run_id)
    sql += " ORDER BY seq"
    return [
        {
            "seq": r["seq"], "ts": r["ts"], "round_id": r["round_id"],
            "score": float(r["score"] or 0.0), "false_refusal_rate": float(r["frr"] or 0.0),
            "benign_breaches": int(r["breaches"] or 0), "label": r["label"],
            "prompt_version": r["prompt_version"], "policy_version": r["schema_version"],
            "suite_fingerprint": r["fingerprint"], "cases_run": int(r["cases_run"] or 0),
            "complete": r["outcome"] == "complete",
        }
        for r in ledger.query(sql, params)
    ]


def patch_history(ledger: Ledger, run_id: str | None = None) -> list[dict[str, Any]]:
    """Patches applied, verified, and reverted, in order."""
    sql = (
        "SELECT seq, ts, round_id, action, outcome, prompt_version, schema_version, payload_json"
        " FROM ledger WHERE action IN (?, ?, 'patch_reverted')"
    )
    params: list[Any] = [ACT_PATCH_APPLIED, ACT_PATCH_VERIFY]
    if run_id:
        sql += " AND run_id = ?"
        params.append(run_id)
    sql += " ORDER BY seq"
    import json

    out = []
    for r in ledger.query(sql, params):
        payload = json.loads(r["payload_json"])
        out.append({
            "seq": r["seq"], "ts": r["ts"], "round_id": r["round_id"],
            "action": r["action"], "outcome": r["outcome"],
            "prompt_version": r["prompt_version"], "policy_version": r["schema_version"],
            "patch_kind": payload.get("patch_kind"),
            "diagnosis": payload.get("diagnosis"),
            "category": payload.get("category"),
            "attack_id": payload.get("attack_id"),
            "benign_before": payload.get("benign_before"),
            "benign_after": payload.get("benign_after"),
            "held": payload.get("held"),
        })
    return out


def latest_benign(ledger: Ledger, run_id: str | None = None) -> dict[str, Any] | None:
    hist = benign_history(ledger, run_id)
    return hist[-1] if hist else None


def tool_attempt_breakdown(ledger: Ledger, run_id: str | None = None) -> dict[str, Any]:
    """Per-tool attempt counts, split by how each attempt was judged."""
    sql = (
        "SELECT json_extract(payload_json, '$.tool') AS tool,"
        " json_extract(payload_json, '$.tier') AS tier, outcome, COUNT(*) AS n"
        " FROM ledger WHERE action = 'tool_call_attempt'"
    )
    params: list[Any] = []
    if run_id:
        sql += " AND run_id = ?"
        params.append(run_id)
    sql += " GROUP BY tool, tier, outcome ORDER BY tier, tool"
    tools: dict[str, dict[str, Any]] = {}
    for r in ledger.query(sql, params):
        name = r["tool"] or "unknown"
        slot = tools.setdefault(name, {"tier": r["tier"], "total": 0, "authorized": 0,
                                       "blocked": 0, "enforcement_breaches": 0})
        n = int(r["n"])
        slot["total"] += n
        if r["outcome"] == "breach_enforcement":
            slot["enforcement_breaches"] += n
        elif r["outcome"] == "breach_intent_blocked":
            slot["blocked"] += n
        else:
            slot["authorized"] += n
    return tools


def spend_summary(ledger: Ledger, run_id: str | None = None) -> dict[str, Any]:
    sql = ("SELECT actor, COALESCE(SUM(cost_usd), 0.0) AS cost, COUNT(*) AS n"
           " FROM ledger WHERE action = 'model_call'")
    params: list[Any] = []
    if run_id:
        sql += " AND run_id = ?"
        params.append(run_id)
    sql += " GROUP BY actor"
    rows = ledger.query(sql, params)
    by_role_sql = ("SELECT json_extract(payload_json, '$.role') AS role,"
                   " COALESCE(SUM(cost_usd), 0.0) AS cost, COUNT(*) AS n"
                   " FROM ledger WHERE action = 'model_call'")
    if run_id:
        by_role_sql += " AND run_id = ?"
    by_role_sql += " GROUP BY role ORDER BY cost DESC"
    return {
        "total_usd": round(sum(float(r["cost"]) for r in rows), 6),
        "model_calls": sum(int(r["n"]) for r in rows),
        "by_role": [
            {"role": r["role"] or "unknown", "cost_usd": round(float(r["cost"]), 6),
             "calls": int(r["n"])}
            for r in ledger.query(by_role_sql, params)
        ],
    }


def run_summary(ledger: Ledger, run_id: str | None = None) -> dict[str, Any]:
    """One call that answers 'how is the run going'."""
    attacks = attack_stats(ledger, run_id)
    benign = latest_benign(ledger, run_id)
    return {
        "run_id": run_id or ledger.run_id,
        "attacks": attacks,
        "benign_latest": benign,
        "benign_runs": len(benign_history(ledger, run_id)),
        "patches": len([p for p in patch_history(ledger, run_id)
                        if p["action"] == ACT_PATCH_APPLIED]),
        "spend": spend_summary(ledger, run_id),
        "ledger_rows": ledger.count(),
    }


# --- backend dispatch ---------------------------------------------------------
# The SQLite path uses SQL directly; the Supabase path reads the aggregate views
# the migration created, so the database does the grouping. Both return the same
# shapes, and every caller goes through these wrappers rather than assuming a
# dialect.

def _is_supabase(ledger: Any) -> bool:
    return getattr(ledger, "backend", "sqlite") == "supabase"


def attack_stats_any(ledger: Any, run_id: str | None = None) -> dict[str, Any]:
    if not _is_supabase(ledger):
        return attack_stats(ledger, run_id)
    filters = {"run_id": f"eq.{run_id}"} if run_id else None
    rows = ledger.view("warden_v_attack_categories", filters=filters)
    categories: dict[str, Any] = {}
    tot_att = tot_int = tot_enf = 0
    for r in rows:
        cat = r.get("category") or "unknown"
        att = int(r.get("attempts") or 0)
        itn = int(r.get("intent_breaches") or 0)
        enf = int(r.get("enforcement_breaches") or 0)
        categories[cat] = {
            "attempts": att, "intent": itn, "enforcement": enf,
            "clean": att - itn, "errors": 0,
            "intent_rate": round(_rate(itn, att), 4),
            "enforcement_rate": round(_rate(enf, att), 4),
        }
        tot_att += att
        tot_int += itn
        tot_enf += enf
    return {
        "attempts": tot_att, "intent_breaches": tot_int, "enforcement_breaches": tot_enf,
        "intent_rate": round(_rate(tot_int, tot_att), 4),
        "enforcement_rate": round(_rate(tot_enf, tot_att), 4),
        "errors": 0, "by_category": categories,
    }


def attack_series_any(ledger: Any, run_id: str | None = None) -> list[dict[str, Any]]:
    if not _is_supabase(ledger):
        return attack_series(ledger, run_id)
    filters = {"run_id": f"eq.{run_id}"} if run_id else None
    rows = ledger.view("warden_v_attack_rounds", filters=filters, order="round_id.asc")
    return [
        {
            "round_id": int(r["round_id"]), "attempts": int(r.get("attempts") or 0),
            "intent": int(r.get("intent_breaches") or 0),
            "enforcement": int(r.get("enforcement_breaches") or 0),
            "intent_rate": float(r.get("intent_rate") or 0.0),
            "enforcement_rate": float(r.get("enforcement_rate") or 0.0),
            "categories": {},
        }
        for r in rows
    ]


def benign_history_any(ledger: Any, run_id: str | None = None) -> list[dict[str, Any]]:
    if not _is_supabase(ledger):
        return benign_history(ledger, run_id)
    filters = {"run_id": f"eq.{run_id}"} if run_id else None
    rows = ledger.view("warden_v_benign_history", filters=filters, order="seq.asc")
    return [
        {
            "seq": r["seq"], "ts": r["ts"], "round_id": r.get("round_id"),
            "score": float(r.get("score") or 0.0),
            "false_refusal_rate": float(r.get("false_refusal_rate") or 0.0),
            "benign_breaches": int(r.get("benign_breaches") or 0),
            "label": r.get("run_label"), "prompt_version": r.get("prompt_version") or "",
            "policy_version": r.get("policy_version") or "",
            "suite_fingerprint": r.get("suite_fingerprint"),
            "cases_run": int(r.get("cases_run") or 0),
            "complete": bool(r.get("complete")),
        }
        for r in rows
    ]


def patch_history_any(ledger: Any, run_id: str | None = None) -> list[dict[str, Any]]:
    if not _is_supabase(ledger):
        return patch_history(ledger, run_id)
    filters = {"run_id": f"eq.{run_id}"} if run_id else None
    rows = ledger.view("warden_v_patch_history", filters=filters, order="seq.asc")
    return [
        {
            "seq": r["seq"], "ts": r["ts"], "round_id": r.get("round_id"),
            "action": r.get("action"), "outcome": r.get("outcome"),
            "prompt_version": r.get("prompt_version"),
            "policy_version": r.get("policy_version"),
            "patch_kind": r.get("patch_kind"), "diagnosis": r.get("diagnosis"),
            "category": r.get("category"), "attack_id": r.get("attack_id"),
            "benign_before": None, "benign_after": None, "held": r.get("held"),
        }
        for r in rows
    ]


def spend_summary_any(ledger: Any, run_id: str | None = None) -> dict[str, Any]:
    if not _is_supabase(ledger):
        return spend_summary(ledger, run_id)
    filters = {"run_id": f"eq.{run_id}"} if run_id else None
    rows = ledger.view("warden_v_spend_by_role", filters=filters)
    by_role: dict[str, dict[str, Any]] = {}
    for r in rows:
        role = r.get("role") or "unknown"
        slot = by_role.setdefault(role, {"role": role, "cost_usd": 0.0, "calls": 0})
        slot["cost_usd"] = round(slot["cost_usd"] + float(r.get("cost_usd") or 0.0), 6)
        slot["calls"] += int(r.get("calls") or 0)
    ordered = sorted(by_role.values(), key=lambda x: -x["cost_usd"])
    return {
        "total_usd": round(sum(x["cost_usd"] for x in ordered), 6),
        "model_calls": sum(x["calls"] for x in ordered),
        "by_role": ordered,
    }


def tool_breakdown_any(ledger: Any, run_id: str | None = None) -> dict[str, Any]:
    if not _is_supabase(ledger):
        return tool_attempt_breakdown(ledger, run_id)
    rows = ledger.rows_for_action("tool_call_attempt", run_id=run_id, limit=20000)
    tools: dict[str, dict[str, Any]] = {}
    for r in rows:
        payload = r.get("payload") or {}
        name = payload.get("tool") or "unknown"
        slot = tools.setdefault(name, {"tier": payload.get("tier"), "total": 0,
                                       "authorized": 0, "blocked": 0,
                                       "enforcement_breaches": 0})
        slot["total"] += 1
        if r.get("outcome") == "breach_enforcement":
            slot["enforcement_breaches"] += 1
        elif r.get("outcome") == "breach_intent_blocked":
            slot["blocked"] += 1
        else:
            slot["authorized"] += 1
    return tools


def latest_benign_any(ledger: Any, run_id: str | None = None) -> dict[str, Any] | None:
    hist = benign_history_any(ledger, run_id)
    return hist[-1] if hist else None


def run_summary_any(ledger: Any, run_id: str | None = None) -> dict[str, Any]:
    attacks = attack_stats_any(ledger, run_id)
    benign = benign_history_any(ledger, run_id)
    patches = patch_history_any(ledger, run_id)
    return {
        "run_id": run_id or getattr(ledger, "run_id", ""),
        "backend": getattr(ledger, "backend", "sqlite"),
        "attacks": attacks,
        "benign_latest": benign[-1] if benign else None,
        "benign_runs": len(benign),
        "patches": len([p for p in patches if p["action"] == ACT_PATCH_APPLIED]),
        "spend": spend_summary_any(ledger, run_id),
        "ledger_rows": ledger.count(),
    }


def latest_run_id(ledger: Any) -> str | None:
    """The most recently written run in the ledger.

    A reader process (the dashboard, a report regeneration) has no run of its
    own. Defaulting it to a freshly generated id would silently filter every
    metric down to zero rows, which looks like a quiet system rather than the
    wrong query, so readers resolve the newest run instead.
    """
    if _is_supabase(ledger):
        rows = ledger.view("warden_ledger", order="seq.desc", limit=1)
        return (rows[0].get("run_id") or None) if rows else None
    rows = ledger.query("SELECT run_id FROM ledger ORDER BY seq DESC LIMIT 1")
    return (rows[0]["run_id"] or None) if rows else None


def run_ids(ledger: Any, limit: int = 20) -> list[str]:
    """Distinct runs present, newest first."""
    if _is_supabase(ledger):
        rows = ledger.view("warden_ledger", order="seq.desc", limit=4000)
        seen: list[str] = []
        for r in rows:
            rid = r.get("run_id")
            if rid and rid not in seen:
                seen.append(rid)
        return seen[:limit]
    rows = ledger.query(
        "SELECT run_id, MAX(seq) AS m FROM ledger GROUP BY run_id ORDER BY m DESC LIMIT ?",
        (limit,),
    )
    return [r["run_id"] for r in rows if r["run_id"]]
