"""Warden live dashboard. Run with: marimo run dashboard/app.py

Reads the ledger directly, on whichever backend is configured, so the dashboard
and the posture report can never disagree. Every panel states its provenance:
which run, which backend, and whether the numbers came from live models or from
the deterministic test doubles.

Laid out for a projector: few panels, large type, and the two curves together at
the top, because that pairing is the whole argument.
"""
from __future__ import annotations

import marimo

__generated_with = "0.9.0"
app = marimo.App(width="full", app_title="Warden - Agent Permission Immune System")


@app.cell
def _imports():
    import os
    import sys
    from pathlib import Path

    ROOT = Path(__file__).resolve().parent.parent
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    import altair as alt
    import marimo as mo
    import pandas as pd

    from warden import metrics
    from warden.budget import get_governor
    from warden.config import get_config, resolved_mode, resolved_store
    from warden.ledger import get_ledger

    _ = alt.data_transformers.disable_max_rows()  # suppress repr output
    return (alt, get_config, get_governor, get_ledger, metrics, mo, os, pd,
            resolved_mode, resolved_store)


@app.cell
def _refresh(mo):
    # Reactive auto-refresh: every cell that reads this re-runs on each tick, so
    # the page follows an overnight run with nobody touching it.
    refresh = mo.ui.refresh(options=["5s", "15s", "30s", "60s"],
                            default_interval="15s", label="Auto-refresh")
    return (refresh,)


@app.cell
def _load(get_config, get_governor, get_ledger, metrics, os, refresh, resolved_mode,
          resolved_store):
    refresh  # dependency: re-read on every tick

    cfg = get_config()
    ledger = get_ledger(cfg)
    governor = get_governor(cfg, ledger)
    # A reader has no run of its own; default to the newest run on record so the
    # panels describe the run that actually happened.
    run_id = os.environ.get("WARDEN_RUN_ID") or metrics.latest_run_id(ledger)         or ledger.run_id

    series = metrics.attack_series_any(ledger, run_id)
    stats = metrics.attack_stats_any(ledger, run_id)
    benign = metrics.benign_history_any(ledger, run_id)
    patches = metrics.patch_history_any(ledger, run_id)
    spend = metrics.spend_summary_any(ledger, run_id)
    tools = metrics.tool_breakdown_any(ledger, run_id)
    budget = governor.snapshot()
    recent = ledger.recent(limit=40)
    chain = ledger.verify_chain()
    mode = resolved_mode(cfg)
    store = resolved_store()
    return (benign, budget, chain, mode, patches, recent, run_id, series, spend,
            stats, store, tools)


@app.cell
def _header(benign, budget, chain, mo, mode, run_id, stats, store):
    latest_benign = benign[-1] if benign else None
    mode_note = (
        "SIMULATED - the agents are deterministic test doubles and the spend figure is "
        "modelled from real token counts and prices, not money spent"
        if mode == "simulated" else
        "LIVE - real model calls and real spend"
    )
    chain_txt = (f"verified over {chain.get('checked', 0)} entries"
                 if chain.get("ok") else f"BROKEN at entry {chain.get('broken_at')}")

    def tile(label, value, sub=""):
        return mo.Html(
            "<div style='flex:1;min-width:158px;padding:14px 16px;border-radius:10px;"
            "background:rgba(127,127,127,0.10);border:1px solid rgba(127,127,127,0.22)'>"
            f"<div style='font-size:0.72rem;letter-spacing:.09em;text-transform:uppercase;"
            f"opacity:.65'>{label}</div>"
            f"<div style='font-size:1.95rem;font-weight:650;line-height:1.15'>{value}</div>"
            f"<div style='font-size:0.76rem;opacity:.6'>{sub}</div></div>"
        )

    benign_value = f"{100 * latest_benign['score']:.1f}%" if latest_benign else "not run"
    benign_sub = (f"false refusals {100 * latest_benign['false_refusal_rate']:.1f}%"
                  if latest_benign else "")
    tiles = mo.hstack([
        tile("Attack success", f"{100 * stats['intent_rate']:.1f}%",
             f"{stats['intent_breaches']} of {stats['attempts']} proposed out of scope"),
        tile("Executed out of scope", f"{100 * stats['enforcement_rate']:.1f}%",
             f"{stats['enforcement_breaches']} reached a tool"),
        tile("Benign suite", benign_value, benign_sub),
        tile("Spend", f"${budget['committed_usd']:.3f}",
             f"of ${budget['ceiling_usd']:.2f} ceiling "
             f"({budget['pct_consumed']}%)"),
        tile("Governor", budget["state"].upper(),
             f"{budget['calls_last_hour']}/{budget['max_calls_per_hour']} calls this hour"),
    ], gap=0.6, wrap=True)

    header = mo.md(
        f"# Warden - Agent Permission Immune System\n\n"
        f"**Run `{run_id}`** &nbsp;|&nbsp; mode **{mode}** &nbsp;|&nbsp; "
        f"store **{store}** &nbsp;|&nbsp; audit chain {chain_txt}\n\n"
        f"> {mode_note}\n"
    )
    return (header, tiles)


@app.cell
def _show_header(header, mo, refresh, tiles):
    mo.vstack([header, tiles, refresh])
    return


@app.cell
def _builders(alt, mo, pd):
    # Panel construction lives in functions so their locals stay out of the cell
    # namespace. marimo requires every cell-level name to be unique across the
    # notebook, and several panels naturally want the same working variables.

    def build_curves(series, benign):
        if not series:
            return mo.md("_No rounds yet. Start the loop: `python run_loop.py`._")
        # Round ids are chronological but not contiguous: benchmark_models.py
        # stamps its benign suite at round*1000+900 and the orchestrator then
        # resumes from that maximum, so later rounds carry ~900,000,000 ids.
        # Plotting the raw id crushes the whole series against the origin, and a
        # few attacks per round quantises each rate to 0/20/40. Plot position and
        # average the attack curves; keep the real id in the tooltip.
        import bisect

        rids = [int(r["round_id"]) for r in series]
        win = 10
        intents = [100 * r["intent_rate"] for r in series]
        execs = [100 * r["enforcement_rate"] for r in series]

        def roll(vals, i):
            seg = vals[max(0, i - win + 1):i + 1]
            return sum(seg) / len(seg)

        rows = []
        for n, r in enumerate(series, start=1):
            rows.append({"n": n, "round": rids[n - 1], "rate": roll(intents, n - 1),
                         "measure": "Attack success (proposed out of scope)"})
            rows.append({"n": n, "round": rids[n - 1], "rate": roll(execs, n - 1),
                         "measure": "Executed out of scope"})
        for b in benign:
            if b.get("round_id") is not None:
                pos = bisect.bisect_right(rids, int(b["round_id"]))
                if pos:
                    rows.append({"n": pos, "round": int(b["round_id"]),
                                 "rate": 100 * b["score"],
                                 "measure": "Benign suite score"})
        scale = alt.Scale(
            domain=["Attack success (proposed out of scope)", "Executed out of scope",
                    "Benign suite score"],
            range=["#d1495b", "#8b2635", "#2a9d8f"])
        return mo.ui.altair_chart(
            alt.Chart(pd.DataFrame(rows)).mark_line(point=True, strokeWidth=3).encode(
                x=alt.X("n:Q", title="Hardening round (in order)"),
                y=alt.Y("rate:Q", title="Percent", scale=alt.Scale(domain=[0, 100])),
                color=alt.Color("measure:N", scale=scale, title=None,
                                legend=alt.Legend(orient="top", labelFontSize=13)),
                tooltip=["n", "round", "measure", alt.Tooltip("rate:Q", format=".1f")],
            ).properties(
                height=340,
                title="The two curves: attack success falling while benign stays flat")
        )

    def build_categories(stats):
        cats = stats.get("by_category", {})
        if not cats:
            return mo.md("_No attacks recorded yet._")
        rows = []
        for cat, b in cats.items():
            rows.append({"category": cat, "measure": "Proposed out of scope",
                         "rate": 100 * b["intent_rate"], "attempts": b["attempts"]})
            rows.append({"category": cat, "measure": "Executed out of scope",
                         "rate": 100 * b["enforcement_rate"], "attempts": b["attempts"]})
        return mo.ui.altair_chart(
            alt.Chart(pd.DataFrame(rows)).mark_bar().encode(
                y=alt.Y("category:N", title=None, sort="-x"),
                x=alt.X("rate:Q", title="Percent", scale=alt.Scale(domain=[0, 100])),
                color=alt.Color("measure:N", title=None,
                                scale=alt.Scale(domain=["Proposed out of scope",
                                                        "Executed out of scope"],
                                                range=["#d1495b", "#8b2635"]),
                                legend=alt.Legend(orient="top")),
                yOffset="measure:N",
                tooltip=["category", "measure", alt.Tooltip("rate:Q", format=".1f"),
                         "attempts"],
            ).properties(height=240, title="Attack success by category")
        )

    return (build_categories, build_curves)


@app.cell
def _table_builders(mo):
    def build_patches(patches):
        if not patches:
            return mo.md("_No patches yet._")
        applied = [p for p in patches if p["action"] == "patch_applied"]
        reverted = [p for p in patches if p["action"] == "patch_reverted"]
        verified = [p for p in patches if p["action"] == "patch_verify"]
        held = [p for p in verified if p.get("outcome") == "held"]
        out = [f"**{len(applied)} applied** - **{len(held)} verified to hold** - "
               f"**{len(reverted)} reverted for regression**", "",
               "| Round | Kind | Versions | Trigger | Diagnosis |",
               "| --- | --- | --- | --- | --- |"]
        for p in applied[-12:][::-1]:
            out.append(f"| {p.get('round_id') or '-'} | `{p.get('patch_kind') or '-'}` | "
                       f"`{p.get('prompt_version')}/{p.get('policy_version')}` | "
                       f"`{p.get('attack_id') or '-'}` | "
                       f"{(p.get('diagnosis') or '')[:90]} |")
        if reverted:
            out += ["", "**Reverted for breaking legitimate work:**"]
            for p in reverted[-5:]:
                out.append(f"- round {p.get('round_id')} `{p.get('attack_id')}` "
                           f"(benign {p.get('benign_before')} to {p.get('benign_after')})")
        return mo.md("\n".join(out))

    def build_tools(tools):
        if not tools:
            return mo.md("_No tool calls recorded yet._")
        out = ["| Tool | Tier | Attempts | In scope | Blocked | Executed out of scope |",
               "| --- | --- | --- | --- | --- | --- |"]
        for name, t in sorted(tools.items(), key=lambda kv: kv[1].get("tier") or 0):
            breach = t.get("enforcement_breaches", 0)
            flag = f"**{breach}**" if breach else "0"
            out.append(f"| `{name}` | {t.get('tier')} | {t.get('total')} | "
                       f"{t.get('authorized')} | {t.get('blocked')} | {flag} |")
        return mo.md("\n".join(out))

    def build_spend(spend, budget):
        # marimo markdown renders $...$ as LaTeX, so currency must be escaped or
        # the whole line disappears.
        d = chr(92) + "$"
        out = [f"**{d}{spend.get('total_usd', 0):.4f}** across "
               f"{spend.get('model_calls', 0)} model calls - ceiling "
               f"**{d}{budget['ceiling_usd']:.2f}** - state **{budget['state']}**", "",
               "| Role | Calls | Cost |", "| --- | --- | --- |"]
        for r in spend.get("by_role", []):
            out.append(f"| `{r['role']}` | {r['calls']} | "
                       f"{d}{float(r['cost_usd']):.4f} |")
        out += ["", f"Rate limits: {budget['calls_last_hour']}/"
                    f"{budget['max_calls_per_hour']} calls and "
                    f"{budget['rounds_last_hour']}/{budget['max_rounds_per_hour']} "
                    f"rounds in the last hour. Reserve floor "
                    f"{d}{budget['reserve_floor_usd']:.2f}, below which new attacks stop "
                    f"so defence and reporting can finish."]
        return mo.md("\n".join(out))

    def build_ledger(recent):
        out = ["| Seq | Actor | Action | Outcome | Round | Versions | Cost |",
               "| --- | --- | --- | --- | --- | --- | --- |"]
        for e in recent[:24]:
            cost = (chr(92) + f"${e.cost_usd:.5f}") if e.cost_usd else ""
            versions = (f"`{e.prompt_version}/{e.schema_version}`"
                        if e.prompt_version or e.schema_version else "")
            rnd = e.round_id if e.round_id is not None else ""
            out.append(f"| {e.seq} | {e.actor} | `{e.action}` | {e.outcome} | {rnd} | "
                       f"{versions} | {cost} |")
        return mo.md("\n".join(out))

    return (build_ledger, build_patches, build_spend, build_tools)


@app.cell
def _panels(benign, budget, build_categories, build_curves, build_ledger, build_patches,
            build_spend, build_tools, mo, patches, recent, series, spend, stats, tools):
    mo.vstack([
        build_curves(series, benign),
        build_categories(stats),
        mo.md("## Patch history"),
        build_patches(patches),
        mo.md("## Tool call surface"),
        build_tools(tools),
        mo.md("## Budget and rate limits"),
        build_spend(spend, budget),
        mo.md("## Recent ledger entries"),
        build_ledger(recent),
    ])
    return


if __name__ == "__main__":
    app.run()
