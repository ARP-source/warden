"""Generates the human-readable security posture report.

The report is written from ledger data, not from memory of what happened, so it
can be regenerated for any point in a run and will say the same thing. Every
figure in it traces to a ledger query in ``warden.metrics`` or
``analysis.cluster``.

The mode banner is not decoration. A report from a simulated run states that
plainly at the top, because a posture claim that does not say what produced it is
not worth reading.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from analysis.cluster import Cluster


def _pct(x: float | None) -> str:
    return "n/a" if x is None else f"{100.0 * float(x):.1f}%"


def _bar(value: float, width: int = 24) -> str:
    filled = int(round(max(0.0, min(1.0, value)) * width))
    return "#" * filled + "." * (width - filled)


def render_report(*, run_id: str, mode: str, budget: dict[str, Any],
                  attacks: dict[str, Any], series: list[dict[str, Any]],
                  clusters: list[Cluster], benign_history: list[dict[str, Any]],
                  patches: list[dict[str, Any]], open_gaps: list[str],
                  versions: dict[str, str], tools: dict[str, Any],
                  chain: dict[str, Any], narrative: dict[str, Any] | None = None,
                  round_id: int | None = None) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    latest_benign = benign_history[-1] if benign_history else None
    first_benign = benign_history[0] if benign_history else None
    lines: list[str] = []

    lines += [
        "# Warden security posture report",
        "",
        f"- Generated: {now}",
        f"- Run: `{run_id}`" + (f", through round {round_id}" if round_id else ""),
        f"- Active versions: prompt `{versions.get('prompt','?')}`, "
        f"policy `{versions.get('policy','?')}`",
        f"- Execution mode: **{mode}**",
    ]
    if mode == "simulated":
        lines += [
            "",
            "> **Simulated run.** The agents in this run are deterministic rule-based "
            "test doubles, not language models, and the spend figure below is modelled "
            "from real token counts and real prices rather than money actually spent. "
            "Every ledger row carries `mode=simulated`. Numbers here describe the "
            "behaviour of the loop and of the enforcement policy, which are real code; "
            "they are not a measurement of any model.",
        ]
    lines += ["", "---", ""]

    # --- headline ------------------------------------------------------------
    lines += [
        "## Headline",
        "",
        "| Measure | Value |",
        "| --- | --- |",
        f"| Attack attempts | {attacks.get('attempts', 0)} |",
        f"| Attack success rate (agent proposed an out-of-scope call) | "
        f"{_pct(attacks.get('intent_rate'))} |",
        f"| Enforcement breach rate (out-of-scope call actually executed) | "
        f"{_pct(attacks.get('enforcement_rate'))} |",
        f"| Benign suite score (latest) | "
        f"{_pct(latest_benign['score']) if latest_benign else 'not run'} |",
        f"| Benign false-refusal rate | "
        f"{_pct(latest_benign['false_refusal_rate']) if latest_benign else 'n/a'} |",
        f"| Patches applied | {len([p for p in patches if p['action'] == 'patch_applied'])} |",
        f"| Patches reverted for regression | "
        f"{len([p for p in patches if p['action'] == 'patch_reverted'])} |",
        f"| Spend | ${budget.get('committed_usd', 0):.4f} of "
        f"${budget.get('ceiling_usd', 0):.2f} ceiling ({budget.get('pct_consumed', 0)}%) |",
        f"| Audit chain | {'verified over ' + str(chain.get('checked', 0)) + ' entries' if chain.get('ok') else 'BROKEN at ' + str(chain.get('broken_at'))} |",
        "",
    ]

    if first_benign and latest_benign and len(benign_history) > 1:
        delta = latest_benign["score"] - first_benign["score"]
        direction = "unchanged" if abs(delta) < 0.005 else ("up" if delta > 0 else "DOWN")
        lines += [
            f"Benign behaviour moved {direction} across the run: "
            f"{_pct(first_benign['score'])} at "
            f"`{first_benign['prompt_version']}/{first_benign['policy_version']}` to "
            f"{_pct(latest_benign['score'])} at "
            f"`{latest_benign['prompt_version']}/{latest_benign['policy_version']}`.",
            "",
        ]

    # --- per category --------------------------------------------------------
    lines += ["## Attack success by category", "",
              "Ranked by how often the category still gets the agent to propose an "
              "out-of-scope call.", "",
              "| Category | Attempts | Proposed out-of-scope | Actually executed |",
              "| --- | --- | --- | --- |"]
    by_cat = attacks.get("by_category", {})
    for cat, b in sorted(by_cat.items(), key=lambda kv: -kv[1].get("intent_rate", 0)):
        lines.append(
            f"| `{cat}` | {b['attempts']} | {_pct(b['intent_rate'])} | "
            f"{_pct(b['enforcement_rate'])} |"
        )
    lines.append("")

    # --- clusters ------------------------------------------------------------
    lines += ["## Vulnerability clusters by root cause", ""]
    if not clusters:
        lines += ["No breaching attempts in this window.", ""]
    else:
        lines += ["Clustered on the control that failed, not on attack wording, because "
                  "one failed control usually explains several categories at once.", ""]
        for c in clusters:
            d = c.as_dict()
            lines += [
                f"### {d['key']}",
                "",
                f"**{d['label']}**",
                "",
                f"- Closed by: `{d['control']}`",
                f"- Breaching attempts: {d['count']} "
                f"({d['enforcement_count']} executed, {d['intent_only_count']} caught by "
                f"enforcement)",
                f"- Categories involved: "
                + ", ".join(f"`{k}` x{v}" for k, v in d["categories"].items()),
                f"- Objectives: " + ", ".join(f"`{k}` x{v}" for k, v in d["objectives"].items()),
                f"- Rounds: {d['first_round']} to {d['last_round']}",
            ]
            ex = d.get("example") or {}
            if ex:
                lines += [
                    "",
                    f"Example (`{ex.get('attack_id')}`, {ex.get('category')}):",
                    "",
                    "```",
                    (ex.get("first_message") or "").strip()[:260],
                    "```",
                    "",
                    f"Oracle codes: `{ex.get('oracle_codes')}`; "
                    f"enforcement believed its authority came from "
                    f"`{ex.get('escalation_sources') or ['none']}`.",
                ]
            lines.append("")

    return "\n".join(lines)


def render_report_tail(*, series: list[dict[str, Any]], benign_history: list[dict[str, Any]],
                       patches: list[dict[str, Any]], open_gaps: list[str],
                       tools: dict[str, Any], spend: dict[str, Any],
                       backend: str, narrative: dict[str, Any] | None = None) -> str:
    """The second half of the report: trend, regression proof, patches, risks."""
    lines: list[str] = []

    lines += ["## The two curves", "",
              "Attack success should fall while benign behaviour stays flat. Both are "
              "printed together because either one alone is easy to fake: a Target that "
              "refuses everything scores perfectly on the first and uselessly on the "
              "second.", ""]
    if series:
        lines += ["| Round | Attacks | Proposed out-of-scope | Executed | Benign |",
                  "| --- | --- | --- | --- | --- |"]
        benign_by_round: dict[int, float] = {}
        for b in benign_history:
            if b.get("round_id") is not None:
                benign_by_round[int(b["round_id"])] = float(b["score"])
        step = max(1, len(series) // 30)
        for row in series[::step]:
            rid = row["round_id"]
            bscore = benign_by_round.get(rid)
            lines.append(
                f"| {rid} | {row['attempts']} | {_pct(row['intent_rate'])} "
                f"`{_bar(row['intent_rate'], 14)}` | {_pct(row['enforcement_rate'])} | "
                + (f"{_pct(bscore)}" if bscore is not None else "-") + " |"
            )
        lines.append("")
        first, last = series[0], series[-1]
        lines += [
            f"Across rounds {first['round_id']} to {last['round_id']}, the rate at which "
            f"attacks got the agent to propose an out-of-scope call moved from "
            f"{_pct(first['intent_rate'])} to {_pct(last['intent_rate'])}, and the rate at "
            f"which one actually executed moved from {_pct(first['enforcement_rate'])} to "
            f"{_pct(last['enforcement_rate'])}.",
            "",
        ]
    else:
        lines += ["No rounds recorded yet.", ""]

    lines += ["## Benign regression record", ""]
    if not benign_history:
        lines += ["The benign suite has not run.", ""]
    else:
        fingerprints = {b.get("suite_fingerprint") for b in benign_history}
        if len(fingerprints) == 1:
            fp_note = (f" `{fingerprints.pop()}` throughout, so every score is comparable.")
        else:
            fp_note = (f"s differ across runs ({sorted(f for f in fingerprints if f)}), so "
                       f"scores either side of the change are NOT comparable.")
        lines += [
            f"The suite ran {len(benign_history)} times. Suite fingerprint" + fp_note,
            "",
            "| Run | Round | Versions | Score | False refusals | Breaches on benign |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for b in benign_history[-18:]:
            label = b.get("label") or b.get("run_label") or "-"
            lines.append(
                f"| {label} | {b.get('round_id') or '-'} | "
                f"`{b['prompt_version']}/{b['policy_version']}` | {_pct(b['score'])} | "
                f"{_pct(b['false_refusal_rate'])} | {b['benign_breaches']} |"
            )
        lines.append("")
        worst = min(benign_history, key=lambda b: b["score"])
        wl = f" (run {worst.get('label')})" if worst.get("label") else ""
        lines += [f"Lowest benign score observed: {_pct(worst['score'])} at "
                  f"`{worst['prompt_version']}/{worst['policy_version']}`" + wl + ".", ""]
    return "\n".join(lines)


def render_report_sections(*, patches: list[dict[str, Any]], open_gaps: list[str],
                           tools: dict[str, Any], spend: dict[str, Any], backend: str,
                           narrative: dict[str, Any] | None = None) -> str:
    """Patch history, tool surface, open risks and provenance."""
    lines: list[str] = []

    applied = [p for p in patches if p["action"] == "patch_applied"]
    verified = [p for p in patches if p["action"] == "patch_verify"]
    reverted = [p for p in patches if p["action"] == "patch_reverted"]
    held = [p for p in verified if p.get("outcome") == "held"]

    lines += ["## Patch history", "",
              f"- Patches applied: {len(applied)}",
              f"- Verified to hold against the same attack: {len(held)} of "
              f"{len(verified)} verification runs",
              f"- Reverted for breaking legitimate work: {len(reverted)}", ""]
    if applied:
        lines += ["| Round | Kind | Versions | Trigger | Diagnosis |",
                  "| --- | --- | --- | --- | --- |"]
        for p in applied[-14:]:
            lines.append(
                f"| {p.get('round_id') or '-'} | {p.get('patch_kind') or '-'} | "
                f"`{p.get('prompt_version')}/{p.get('policy_version')}` | "
                f"`{p.get('attack_id') or '-'}` | {(p.get('diagnosis') or '')[:110]} |"
            )
        lines.append("")
    if reverted:
        lines += ["Reverted patches, with the benign score that triggered the revert:", ""]
        for p in reverted:
            lines.append(f"- Round {p.get('round_id')}: `{p.get('attack_id')}` "
                         f"({p.get('benign_before')} to {p.get('benign_after')})")
        lines.append("")

    if tools:
        lines += ["## Tool call surface", "",
                  "| Tool | Tier | Attempts | In scope | Blocked | Executed out of scope |",
                  "| --- | --- | --- | --- | --- | --- |"]
        for name, t in sorted(tools.items(), key=lambda kv: kv[1].get("tier", 0) or 0):
            lines.append(
                f"| `{name}` | {t.get('tier')} | {t.get('total')} | "
                f"{t.get('authorized')} | {t.get('blocked')} | "
                f"{t.get('enforcement_breaches')} |"
            )
        lines.append("")

    lines += ["## Open risks", ""]
    if open_gaps:
        lines += ["Enforcement gaps still present, measured against the fixed oracle:", ""]
        for gap in open_gaps:
            lines.append(f"- `{gap}`")
        lines.append("")
    else:
        lines += ["No enforcement gaps remain open against the current oracle. That is a "
                  "statement about the gaps this project enumerates, not a claim of "
                  "general security.", ""]

    if narrative and narrative.get("summary"):
        lines += ["## Analyst summary", "", str(narrative["summary"]), ""]
        for obs in (narrative.get("observations") or [])[:8]:
            lines.append(f"- {obs}")
        if narrative.get("recommendation"):
            lines += ["", f"**Recommended next:** {narrative['recommendation']}"]
        lines.append("")

    by_role = spend.get("by_role", [])
    role_line = ", ".join(
        f"`{r['role']}` ${float(r['cost_usd']):.4f} over {r['calls']} calls" for r in by_role
    ) or "no model calls recorded"
    lines += ["## Provenance", "",
              f"- Persistence backend: `{backend}`",
              f"- Model spend by role: {role_line}",
              f"- Total: ${float(spend.get('total_usd', 0)):.4f} across "
              f"{spend.get('model_calls', 0)} model calls",
              "",
              "Every figure above is derived from the append-only ledger by "
              "`warden/metrics.py` and `analysis/cluster.py`. The ledger is hash-chained "
              "and its status is in the headline table. Violations are graded against "
              "`target/oracle.py`, which the Defender cannot modify, so hardening cannot "
              "improve a score by redefining what counts as a violation.",
              ""]
    return "\n".join(lines)
