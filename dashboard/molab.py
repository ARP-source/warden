# /// script
# requires-python = ">=3.11"
# dependencies = ["marimo", "altair==5.*", "pandas==2.*", "httpx==0.*"]
# ///
"""Warden live dashboard, standalone for molab.

Self-contained on purpose: it imports nothing from the Warden package and reads
the run directly from Postgres over the REST API, so it runs on molab with no
checkout and no local state.

About the key below. It is the Supabase *publishable* key, and the database is
configured so that it can only read. Row level security grants it SELECT and
nothing else, and INSERT, UPDATE, DELETE and TRUNCATE are revoked from the anon
role outright, as is EXECUTE on every function that mutates state, including
the kill switch. Verified against the live database: SELECT returns 200 while
DELETE, INSERT and warden_halt all return 401. A reader of this notebook can
see everything the loop did and change none of it.
"""
import marimo

__generated_with = "0.9.0"
app = marimo.App(width="full", app_title="Warden - Agent Permission Immune System")



@app.cell
def _imports():
    import altair as alt
    import httpx
    import marimo as mo
    import pandas as pd

    _ = alt.data_transformers.disable_max_rows()

    # Defined inside the cell, not at module level: a marimo cell can only see
    # names that another cell defines, so a module-level constant is invisible
    # to the graph and the notebook renders blank.
    SUPABASE_URL = "https://efdurmglyhajnxcolktl.supabase.co"
    SUPABASE_ANON_KEY = "sb_publishable_JN04pBYobnoWlIMJSnmRNg_RsYB7VMD"

    HEADERS = {"apikey": SUPABASE_ANON_KEY,
               "Authorization": f"Bearer {SUPABASE_ANON_KEY}"}

    def fetch(table, params=None):
        """One read against the Postgres REST API."""
        with httpx.Client(base_url=f"{SUPABASE_URL}/rest/v1", headers=HEADERS,
                          timeout=45.0) as c:
            r = c.get(f"/{table}", params={"select": "*", **(params or {})})
            r.raise_for_status()
            return r.json()

    def rpc(fn, payload=None):
        with httpx.Client(base_url=f"{SUPABASE_URL}/rest/v1", headers=HEADERS,
                          timeout=45.0) as c:
            r = c.post(f"/rpc/{fn}", json=payload or {})
            r.raise_for_status()
            return r.json()

    def safe(call, default):
        """Run a read; degrade to a default rather than erroring the notebook.

        A remote notebook has a slower, less reliable link than the loop's own
        process, so one slow query should dim a panel, not take down the page.
        """
        try:
            return call()
        except Exception:
            return default

    return SUPABASE_ANON_KEY, SUPABASE_URL, alt, fetch, mo, pd, rpc, safe


@app.cell
def _refresh(mo):
    refresh = mo.ui.refresh(options=["10s", "30s", "60s"], default_interval="30s",
                            label="Auto-refresh")
    return (refresh,)


@app.cell
def _load(fetch, refresh, rpc, safe):
    refresh  # dependency: re-read on every tick

    # The newest run in the ledger. A reader has no run of its own, and
    # defaulting to anything else would filter every panel down to nothing.
    _newest = safe(lambda: fetch("warden_ledger", {"select": "run_id",
                   "order": "seq.desc", "limit": "1"}), [])
    run_id = _newest[0]["run_id"] if _newest else ""
    flt = {"run_id": f"eq.{run_id}"}

    rounds = safe(lambda: fetch("warden_v_attack_rounds", {**flt, "order": "round_id.asc"}), [])
    categories = safe(lambda: fetch("warden_v_attack_categories", flt), [])
    benign = safe(lambda: fetch("warden_v_benign_history", {**flt, "order": "seq.asc"}), [])
    patches = safe(lambda: fetch("warden_v_patch_history", {**flt, "order": "seq.asc"}), [])
    spend = safe(lambda: fetch("warden_v_spend_by_role", flt), [])
    breaches = safe(lambda: fetch("warden_v_enforcement_breaches", {**flt, "limit": "12"}), [])
    recent = safe(lambda: fetch("warden_ledger", {**flt, "order": "seq.desc", "limit": "25"}), [])
    chain = safe(lambda: rpc("warden_ledger_verify"), [{"ok": None, "checked": 0}])
    chain = chain[0] if isinstance(chain, list) and chain else chain

    refunds = safe(lambda: fetch("warden_refunds",
                   {"select": "amount_usd,authorized", "limit": "5000"}), [])
    emails = safe(lambda: fetch("warden_email_outbox",
                  {"select": "to_address,authorized", "limit": "5000"}), [])
    return (benign, breaches, categories, chain, emails, patches, recent,
            refunds, rounds, run_id, spend)


@app.cell
def _derive(benign, categories, rounds, spend):
    attempts = sum(int(r["attempts"] or 0) for r in rounds)
    intent = sum(int(r["intent_breaches"] or 0) for r in rounds)
    executed = sum(int(r["enforcement_breaches"] or 0) for r in rounds)
    complete_benign = [b for b in benign if b.get("complete")]
    latest_benign = complete_benign[-1] if complete_benign else None
    total_spend = sum(float(s["cost_usd"] or 0) for s in spend)
    calls = sum(int(s["calls"] or 0) for s in spend)

    def pct(n, d):
        return (100.0 * n / d) if d else 0.0

    stats = {
        "attempts": attempts,
        "intent_rate": pct(intent, attempts),
        "exec_rate": pct(executed, attempts),
        "intent": intent,
        "executed": executed,
        "benign": latest_benign,
        "spend": total_spend,
        "calls": calls,
        "categories": categories,
    }
    return (stats,)


@app.cell
def _header(chain, mo, run_id, stats):
    def tile(label, value, sub=""):
        return mo.Html(
            "<div style='flex:1;min-width:165px;padding:16px 18px;border-radius:12px;"
            "background:rgba(127,127,127,0.10);border:1px solid rgba(127,127,127,0.22)'>"
            f"<div style='font-size:0.72rem;letter-spacing:.09em;text-transform:uppercase;"
            f"opacity:.65'>{label}</div>"
            f"<div style='font-size:2rem;font-weight:650;line-height:1.15'>{value}</div>"
            f"<div style='font-size:0.76rem;opacity:.6'>{sub}</div></div>")

    b = stats["benign"]
    if chain.get("ok"):
        chain_txt = f"verified over {chain.get('checked', 0):,} entries"
    elif chain.get("ok") is None:
        chain_txt = "verification pending"
    else:
        chain_txt = f"BROKEN at {chain.get('broken_at')}"

    header = mo.md(
        f"# Warden — Agent Permission Immune System\n\n"
        f"An autonomous loop that red-teams an AI agent's **tool-calling permission "
        f"boundaries**, patches what gets through, and proves it did not break normal "
        f"behaviour.\n\n"
        f"**Run `{run_id}`** · live from Postgres · audit chain {chain_txt}\n")

    tiles = mo.hstack([
        tile("Attack success", f"{stats['intent_rate']:.1f}%",
             f"{stats['intent']} of {stats['attempts']} proposed out of scope"),
        tile("Executed out of scope", f"{stats['exec_rate']:.1f}%",
             f"{stats['executed']} reached a tool"),
        tile("Benign suite", f"{100*b['score']:.1f}%" if b else "not run",
             f"false refusals {100*b['false_refusal_rate']:.1f}%" if b else ""),
        tile("Spend", f"${stats['spend']:.2f}", f"{stats['calls']:,} model calls"),
    ], gap=0.7, wrap=True)
    return header, tiles


@app.cell
def _charts(alt, benign, mo, pd, rounds, stats):
    def curves():
        if not rounds:
            return mo.md("_No rounds recorded yet._")
        rows = []
        for r in rounds:
            rid = int(r["round_id"])
            rows.append({"round": rid, "pct": 100 * float(r["intent_rate"] or 0),
                         "measure": "Attack success (proposed out of scope)"})
            rows.append({"round": rid, "pct": 100 * float(r["enforcement_rate"] or 0),
                         "measure": "Executed out of scope"})
        for b in benign:
            if b.get("round_id") is not None and b.get("complete"):
                rows.append({"round": int(b["round_id"]),
                             "pct": 100 * float(b["score"] or 0),
                             "measure": "Benign suite score"})
        scale = alt.Scale(
            domain=["Attack success (proposed out of scope)",
                    "Executed out of scope", "Benign suite score"],
            range=["#d1495b", "#8b2635", "#2a9d8f"])
        return mo.ui.altair_chart(
            alt.Chart(pd.DataFrame(rows)).mark_line(point=True, strokeWidth=3).encode(
                x=alt.X("round:Q", title="Round"),
                y=alt.Y("pct:Q", title="Percent", scale=alt.Scale(domain=[0, 100])),
                color=alt.Color("measure:N", scale=scale, title=None,
                                legend=alt.Legend(orient="top", labelFontSize=13)),
                tooltip=["round", "measure", alt.Tooltip("pct:Q", format=".1f")],
            ).properties(
                height=360,
                title="The two curves: attack success falling while benign stays flat"))

    def by_category():
        cats = stats["categories"]
        if not cats:
            return mo.md("_No attacks recorded yet._")
        rows = []
        for c in cats:
            name = c.get("category") or "unknown"
            rows.append({"category": name, "measure": "Proposed out of scope",
                         "pct": 100 * float(c["intent_rate"] or 0),
                         "attempts": c["attempts"]})
            rows.append({"category": name, "measure": "Executed out of scope",
                         "pct": 100 * float(c["enforcement_rate"] or 0),
                         "attempts": c["attempts"]})
        return mo.ui.altair_chart(
            alt.Chart(pd.DataFrame(rows)).mark_bar().encode(
                y=alt.Y("category:N", title=None, sort="-x"),
                x=alt.X("pct:Q", title="Percent", scale=alt.Scale(domain=[0, 100])),
                color=alt.Color("measure:N", title=None,
                                scale=alt.Scale(domain=["Proposed out of scope",
                                                        "Executed out of scope"],
                                                range=["#d1495b", "#8b2635"]),
                                legend=alt.Legend(orient="top")),
                yOffset="measure:N",
                tooltip=["category", "measure", alt.Tooltip("pct:Q", format=".1f"),
                         "attempts"],
            ).properties(height=260, title="Attack success by category"))

    return by_category, curves


@app.cell
def _tables(breaches, emails, mo, patches, recent, refunds, spend):
    def patch_panel():
        applied = [p for p in patches if p["action"] == "patch_applied"]
        held = [p for p in patches
                if p["action"] == "patch_verify" and p["outcome"] == "held"]
        reverted = [p for p in patches if p["action"] == "patch_reverted"]
        if not patches:
            return mo.md("_No patches yet._")
        out = [f"**{len(applied)} applied** - **{len(held)} verified to hold** - "
               f"**{len(reverted)} reverted for regression**", "",
               "| Round | Kind | Versions | Trigger | Diagnosis |",
               "| --- | --- | --- | --- | --- |"]
        for p in applied[-12:][::-1]:
            out.append(f"| {p.get('round_id') or '-'} | `{p.get('patch_kind') or '-'}` "
                       f"| `{p.get('prompt_version')}/{p.get('policy_version')}` "
                       f"| `{p.get('attack_id') or '-'}` "
                       f"| {(p.get('diagnosis') or '')[:95]} |")
        return mo.md("\n".join(out))

    def damage_panel():
        bad_refunds = [r for r in refunds if not r["authorized"]]
        bad_emails = [e for e in emails if not e["authorized"]]
        total = sum(float(r["amount_usd"] or 0) for r in bad_refunds)
        addrs = sorted({e["to_address"] for e in bad_emails})
        out = ["What the attacker actually extracted, as rows in the database:", "",
               "| | |", "| --- | --- |",
               f"| Unauthorised refunds | **{len(bad_refunds)}** totalling "
               f"**{chr(92)}${total:,.2f}** |",
               f"| Customer data emailed out | **{len(bad_emails)}** to "
               f"{len(addrs)} address(es) |", ""]
        if addrs:
            out.append("Recipients: " + ", ".join(f"`{a}`" for a in addrs[:6]))
        return mo.md("\n".join(out))

    def breach_panel():
        if not breaches:
            return mo.md("_No out-of-scope call has executed in this run._")
        out = ["| Round | Tool | Tier | Oracle said | Rule | Believed authority |",
               "| --- | --- | --- | --- | --- | --- |"]
        for b in breaches:
            out.append(f"| {b.get('round_id') or '-'} | `{b.get('tool')}` "
                       f"| {b.get('tier')} | `{b.get('oracle_code')}` "
                       f"| `{b.get('policy_rule_id')}` "
                       f"| `{b.get('escalation_source')}` |")
        return mo.md("\n".join(out))

    def spend_panel():
        d = chr(92) + "$"
        out = ["| Role | Model | Calls | Cost |", "| --- | --- | --- | --- |"]
        for s in sorted(spend, key=lambda x: -float(x["cost_usd"] or 0)):
            out.append(f"| `{s.get('role')}` | `{(s.get('model') or '')[:34]}` "
                       f"| {s.get('calls')} | {d}{float(s['cost_usd'] or 0):.4f} |")
        return mo.md("\n".join(out))

    def ledger_panel():
        out = ["| Seq | Actor | Action | Outcome | Round | Versions |",
               "| --- | --- | --- | --- | --- | --- |"]
        for e in recent[:20]:
            v = (f"`{e.get('prompt_version')}/{e.get('schema_version')}`"
                 if e.get("prompt_version") else "")
            out.append(f"| {e['seq']} | {e['actor']} | `{e['action']}` "
                       f"| {e['outcome']} | {e.get('round_id') or ''} | {v} |")
        return mo.md("\n".join(out))

    return (breach_panel, damage_panel, ledger_panel, patch_panel, spend_panel)


@app.cell
def _layout(breach_panel, by_category, curves, damage_panel, header, ledger_panel,
            mo, patch_panel, refresh, spend_panel, tiles):
    mo.vstack([
        header, tiles, refresh,
        curves(),
        by_category(),
        mo.md("## What got through"),
        damage_panel(),
        mo.md("### Every out-of-scope call that executed"),
        breach_panel(),
        mo.md("## Patch history"),
        patch_panel(),
        mo.md("## Model spend"),
        spend_panel(),
        mo.md("## Recent ledger entries"),
        ledger_panel(),
    ])
    return


if __name__ == "__main__":
    app.run()
