"""Check each deliverable against the ledger and report what is actually true.

Written so nobody has to take a claim on trust, including me. Every line either
reads real data or says plainly that the evidence is missing. Run it before a
demo; anything marked MISSING is something not to claim.

    python verify_deliverables.py [run_id]
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from warden import metrics  # noqa: E402
from warden.budget import get_governor  # noqa: E402
from warden.config import get_config, resolved_mode, resolved_provider, resolved_store  # noqa: E402
from warden.ledger import get_ledger  # noqa: E402

OK, NO, MEH = "PASS", "MISSING", "PARTIAL"


def main() -> int:
    cfg = get_config()
    ledger = get_ledger(cfg)
    run_id = sys.argv[1] if len(sys.argv) > 1 else (
        metrics.latest_run_id(ledger) or ledger.run_id)
    gov = get_governor(cfg, ledger)

    stats = metrics.attack_stats_any(ledger, run_id)
    series = metrics.attack_series_any(ledger, run_id)
    benign = metrics.benign_history_any(ledger, run_id)
    patches = metrics.patch_history_any(ledger, run_id)
    spend = metrics.spend_summary_any(ledger, run_id)
    chain = ledger.verify_chain()
    snap = gov.snapshot()

    rows: list[tuple[str, str, str]] = []

    rows.append(("execution mode", OK if resolved_mode(cfg) == "live" else MEH,
                 f"mode={resolved_mode(cfg)} provider={resolved_provider(cfg)} "
                 f"store={resolved_store()}"))

    reachable = "unreachable"
    try:
        import httpx

        h = httpx.get(f"{cfg.target.base_url}/healthz", timeout=4.0).json()
        reachable = (f"{cfg.target.base_url} up, versions "
                     f"{h.get('versions')}, {len(h.get('open_gaps', []))} gaps open")
        rows.append(("live Target endpoint", OK, reachable))
    except Exception as exc:
        rows.append(("live Target endpoint", NO, f"{cfg.target.base_url}: {exc}"))

    rounds = len(series)
    rows.append(("unattended rounds logged",
                 OK if rounds >= 100 else (MEH if rounds >= 10 else NO),
                 f"{rounds} rounds, {stats['attempts']} attacks, "
                 f"{ledger.count()} total ledger entries"))

    if len(series) >= 4:
        head = series[:max(1, len(series) // 4)]
        tail = series[-max(1, len(series) // 4):]
        h_rate = sum(r["intent_rate"] for r in head) / len(head)
        t_rate = sum(r["intent_rate"] for r in tail) / len(tail)
        h_enf = sum(r["enforcement_rate"] for r in head) / len(head)
        t_enf = sum(r["enforcement_rate"] for r in tail) / len(tail)
        falling = t_rate < h_rate or t_enf < h_enf
        rows.append(("attack success trending down", OK if falling else MEH,
                     f"intent {h_rate:.1%} -> {t_rate:.1%}, "
                     f"executed {h_enf:.1%} -> {t_enf:.1%} (first vs last quarter)"))
    else:
        rows.append(("attack success trending down", NO,
                     f"only {len(series)} rounds; need more to show a trend"))

    if benign:
        scores = [b["score"] for b in benign]
        fingerprints = {b.get("suite_fingerprint") for b in benign}
        stable = (max(scores) - min(scores)) <= 0.15 and len(fingerprints) == 1
        rows.append(("benign score stable", OK if stable else MEH,
                     f"{len(benign)} runs, {min(scores):.3f} to {max(scores):.3f}, "
                     f"latest {scores[-1]:.3f}, "
                     f"{'one fingerprint' if len(fingerprints) == 1 else 'MIXED fingerprints'}"))
    else:
        rows.append(("benign score stable", NO, "the suite has not run"))

    applied = [p for p in patches if p["action"] == "patch_applied"]
    held = [p for p in patches if p["action"] == "patch_verify" and p.get("outcome") == "held"]
    reverted = [p for p in patches if p["action"] == "patch_reverted"]
    rows.append(("defender patched and verified",
                 OK if held else (MEH if applied else NO),
                 f"{len(applied)} applied, {len(held)} verified to hold, "
                 f"{len(reverted)} reverted for regression"))

    reports = sorted((ROOT / "reports").glob("posture-*.md")) if (ROOT / "reports").exists() else []
    rows.append(("posture report generated", OK if reports else NO,
                 f"{len(reports)} report(s), latest "
                 f"{reports[-1].name if reports else 'none'}"))

    rows.append(("audit chain verified", OK if chain.get("ok") else NO,
                 f"{chain.get('checked')} entries, backend {ledger.backend}"
                 + ("" if chain.get("ok") else f", BROKEN at {chain.get('broken_at')}")))

    halts = [e for e in ledger.recent(limit=400, action="budget_halt")]
    rows.append(("budget governor has triggered", OK if halts else NO,
                 f"{len(halts)} halt event(s); spend ${snap['committed_usd']:.4f} "
                 f"of ${snap['ceiling_usd']:.2f}, state {snap['state']}"))

    rows.append(("spend within ceiling",
                 OK if snap["committed_usd"] <= snap["ceiling_usd"] + 1e-9 else NO,
                 f"${snap['committed_usd']:.4f} of ${snap['ceiling_usd']:.2f} "
                 f"({snap['pct_consumed']}%), {spend['model_calls']} model calls"))

    readme = (ROOT / "README.md")
    rows.append(("README with architecture", OK if readme.exists()
                 and "ORCHESTRATOR" in readme.read_text(encoding="utf-8") else NO,
                 f"{readme.stat().st_size} bytes" if readme.exists() else "absent"))

    print(f"Warden deliverables - run {run_id}")
    print("=" * 78)
    for name, verdict, detail in rows:
        print(f"  [{verdict:7s}] {name:30s} {detail}")
    missing = [n for n, v, _ in rows if v == NO]
    partial = [n for n, v, _ in rows if v == MEH]
    print()
    if missing:
        print("MISSING evidence for: " + ", ".join(missing))
    if partial:
        print("PARTIAL evidence for: " + ", ".join(partial))
    if not missing and not partial:
        print("Every deliverable has evidence in the ledger.")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
