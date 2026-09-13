"""Operator commands: warden <verb>."""
from __future__ import annotations

import argparse
import json
import shutil
import sys

from warden.config import get_config, resolved_mode


def cmd_reset(args: argparse.Namespace) -> int:
    """Archive the current run data and start clean."""
    cfg = get_config()
    data_dir = cfg.ledger_db.parent
    if data_dir.exists():
        if args.archive:
            from datetime import datetime, timezone

            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            dest = data_dir.parent / f"data-archive-{stamp}"
            shutil.move(str(data_dir), str(dest))
            print(f"archived previous run to {dest}")
        else:
            shutil.rmtree(data_dir)
            print(f"removed {data_dir}")
    data_dir.mkdir(parents=True, exist_ok=True)
    print("clean state ready")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    from warden import metrics
    from warden.budget import get_governor
    from warden.ledger import get_ledger

    cfg = get_config()
    ledger = get_ledger(cfg)
    gov = get_governor(cfg, ledger)
    out = {
        "mode": resolved_mode(cfg),
        "run_id": ledger.run_id,
        "budget": gov.snapshot(),
        "summary": metrics.run_summary_any(ledger, None if args.all_runs else ledger.run_id),
        "chain": ledger.verify_chain(),
    }
    print(json.dumps(out, indent=2, default=str))
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    from warden.ledger import get_ledger

    result = get_ledger().verify_chain()
    print(json.dumps(result, indent=2))
    return 0 if result["ok"] else 1


def cmd_halt(args: argparse.Namespace) -> int:
    from warden.budget import get_governor

    gov = get_governor()
    if args.clear:
        gov.clear_halt()
        print("halt cleared")
    else:
        gov.halt(args.reason, {"source": "cli"})
        print(f"halted: {args.reason}")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Check every dependency the real run needs, and say what is missing.

    Exists because the failure mode of a credentialled system is running
    happily in a degraded mode nobody noticed. This prints the mode, the
    backend, and what each missing credential costs.
    """
    import os

    from warden.config import get_config, resolved_mode, resolved_provider, resolved_store

    cfg = get_config()
    checks: list[tuple[str, bool, str]] = []
    provider = resolved_provider(cfg)

    have_anthropic = bool(os.environ.get("ANTHROPIC_API_KEY"))
    have_openai_url = bool(cfg.inference.base_url)
    have_openai_key = bool(cfg.inference.api_key())
    have_inference = have_anthropic or (have_openai_url and (
        have_openai_key or "localhost" in cfg.inference.base_url
        or "127.0.0.1" in cfg.inference.base_url))

    checks.append((
        "inference endpoint", have_inference,
        f"provider={provider}" + (f" via {cfg.inference.base_url}"
                                  if provider == "openai" else "")
        if have_inference else
        "MISSING: no model provider. Agents run as deterministic test doubles "
        "(mode=simulated). Cheapest real option: set WARDEN_OPENAI_BASE_URL="
        "https://api.inference.wandb.ai/v1 and reuse WANDB_API_KEY",
    ))
    checks.append((
        "ANTHROPIC_API_KEY", have_anthropic,
        "present (paid)" if have_anthropic else
        "absent - optional; only needed for the Anthropic provider",
    ))

    have_supabase_url = bool(os.environ.get("SUPABASE_URL"))
    have_supabase_key = bool(os.environ.get("SUPABASE_SERVICE_ROLE_KEY"))
    checks.append((
        "SUPABASE_URL", have_supabase_url,
        os.environ.get("SUPABASE_URL", "") if have_supabase_url
        else "MISSING: persistence falls back to local SQLite",
    ))
    checks.append((
        "SUPABASE_SERVICE_ROLE_KEY", have_supabase_key,
        "present" if have_supabase_key
        else "MISSING: cannot write to Postgres; get it from the Supabase dashboard "
             "under Project Settings, API, service_role",
    ))

    have_wandb = bool(os.environ.get("WANDB_API_KEY"))
    checks.append((
        "WANDB_API_KEY", have_wandb,
        "Weave tracing on" if have_wandb
        else "MISSING: no Weave traces; the ledger is still complete. Get a key at "
             "https://wandb.ai/authorize",
    ))

    try:
        import anthropic  # noqa: F401
        checks.append(("anthropic package", True, "installed"))
    except ImportError:
        checks.append(("anthropic package", False,
                       "MISSING: pip install 'anthropic>=0.40' (the llm extra)"))
    try:
        import weave  # noqa: F401
        checks.append(("weave package", True, "installed"))
    except ImportError:
        checks.append(("weave package", False,
                       "MISSING: pip install 'weave>=0.51' (the obs extra)"))
    try:
        import marimo  # noqa: F401
        checks.append(("marimo package", True, "installed"))
    except ImportError:
        checks.append(("marimo package", False,
                       "MISSING: pip install 'marimo>=0.9' (the dash extra)"))

    print("Warden doctor")
    print("=" * 66)
    for name, ok, note in checks:
        print(f"  [{'ok ' if ok else 'XX'}] {name:28s} {note}")

    print()
    mode = resolved_mode(cfg)
    store = resolved_store()
    print(f"  resolved mode   : {mode}"
          + ("   <-- agents are test doubles, not models" if mode == "simulated" else ""))
    print(f"  inference       : {provider}"
          + (f"  {cfg.inference.base_url}" if cfg.inference.base_url else ""))
    if provider == "openai":
        try:
            from warden.openai_provider import OpenAICompatProvider

            client = OpenAICompatProvider(cfg.inference.base_url,
                                          cfg.inference.api_key())
            resp = client._client.get("/models")
            available = {m.get("id") for m in (resp.json().get("data") or [])}
            print(f"  inference reach : ok, {len(available)} models available")
            # A model id that does not exist 404s on every call and silently
            # disables whichever agent uses it. That failure is invisible in
            # aggregate metrics, so it is checked here by name.
            bad = []
            for role in ("target", "attacker", "defender", "analysis", "judge"):
                name = cfg.model_for(role, "openai")
                ok = name in available
                print(f"    {role:9s} {name:44s} {'ok' if ok else 'NOT FOUND'}")
                if not ok:
                    bad.append(f"{role}={name}")
            if bad:
                checks.append(("configured models", False,
                               "MISSING on this endpoint: " + ", ".join(bad)))
            else:
                checks.append(("configured models", True,
                               "all five roles resolve to available models"))
        except Exception as exc:
            print(f"  inference reach : FAILED {exc}")
            checks.append(("configured models", False, f"could not verify: {exc}"))
    print(f"  resolved store  : {store}"
          + ("   <-- local file, not hosted Postgres" if store == "sqlite" else ""))
    print(f"  spend ceiling   : ${cfg.budget.ceiling_usd:.2f}")
    print(f"  target url      : {cfg.target.base_url}")

    # Live connectivity checks, best effort.
    if store == "supabase":
        try:
            from warden.supabase_client import SupabaseClient

            print(f"  supabase reach  : {SupabaseClient().health()}")
        except Exception as exc:
            print(f"  supabase reach  : FAILED {exc}")
    try:
        import httpx

        r = httpx.get(f"{cfg.target.base_url}/healthz", timeout=3.0)
        body = r.json()
        print(f"  target reach    : ok, versions "
              f"{body.get('versions')} mode {body.get('mode')}")
    except Exception:
        print("  target reach    : not running (start it with python run_target.py)")

    for name, ok, note in checks:
        if name == "configured models":
            print(f"  [{'ok ' if ok else 'XX'}] {name:28s} {note}")
    degraded = [n for n, ok, _ in checks if not ok and n != "ANTHROPIC_API_KEY"]
    print()
    if degraded:
        print("Degraded: " + ", ".join(degraded))
        print("The system runs in this state by design, but a run made now is not a "
              "measurement of real models or hosted storage.")
    else:
        print("Fully configured: real models, hosted Postgres, live tracing.")
    return 0 if not degraded else 1


def cmd_reconcile(args: argparse.Namespace) -> int:
    """Compare the ledger's cost estimate against what the provider billed.

    The governor enforces its ceiling against an estimated price table, not
    against an invoice. If the table is wrong the ceiling is wrong, and it can
    be overshot without anything in the system noticing. This prints the
    tokens actually consumed and, given the real billed figure, the rate that
    implies, so the table can be corrected.
    """
    from collections import Counter

    from warden.config import get_config
    from warden.ledger import get_ledger

    cfg = get_config()
    ledger = get_ledger(cfg)

    rows = []
    if getattr(ledger, "backend", "sqlite") == "supabase":
        page = 0
        while page <= 40:
            chunk = ledger.client.select(
                "warden_ledger", filters={"action": "eq.model_call"},
                order="seq.asc", limit=1000, offset=page * 1000)
            if not chunk:
                break
            rows += chunk
            page += 1
    else:
        rows = [{"payload": e.payload, "cost_usd": e.cost_usd}
                for e in ledger.recent(limit=100000, action="model_call")]

    tin, tout, est, calls = Counter(), Counter(), Counter(), Counter()
    for r in rows:
        p = r.get("payload") or {}
        m = p.get("model", "unknown")
        tin[m] += int(p.get("input_tokens") or 0)
        tout[m] += int(p.get("output_tokens") or 0)
        est[m] += float(r.get("cost_usd") or 0)
        calls[m] += 1

    print("Ledger token accounting")
    print("=" * 78)
    print(f"  {'model':44s} {'calls':>6s} {'Mtok in':>8s} {'Mtok out':>9s} {'est':>8s}")
    ti = to = te = 0
    for m in sorted(calls, key=lambda x: -est[x]):
        print(f"  {m[:44]:44s} {calls[m]:6d} {tin[m]/1e6:8.3f} {tout[m]/1e6:9.3f} "
              f"${est[m]:7.4f}")
        ti += tin[m]; to += tout[m]; te += est[m]
    print(f"  {'TOTAL':44s} {sum(calls.values()):6d} {ti/1e6:8.3f} {to/1e6:9.3f} "
          f"${te:7.4f}")
    # Historical rows carry whatever the price table said when they were
    # written, so the estimate above is not a verdict on the current table.
    at_current = sum(cfg.price_for(m).cost(tin[m], tout[m]) for m in calls)
    print()
    print(f"  estimate as recorded   : ${te:.4f}  (prices in force at the time)")
    print(f"  same tokens, current   : ${at_current:.4f}  (current table, "
          f"{cfg.budget.pricing_safety_factor:.2f}x safety factor)")
    print(f"  ceiling                : ${cfg.budget.ceiling_usd:.2f}")

    if args.billed is not None and (ti + to):
        billed = float(args.billed)
        blended = billed / ((ti + to) / 1e6)
        ratio = billed / te if te else float("inf")
        ratio_now = billed / at_current if at_current else float("inf")
        print()
        print(f"  provider billed        : ${billed:.4f}")
        print(f"  implied blended rate   : ${blended:.3f} per Mtok")
        print(f"  estimate is off by     : {ratio:.2f}x "
              + ("(UNDER-counting; the ceiling can be overshot)" if ratio > 1.05
                 else "(over-counting; safe direction)" if ratio < 0.95 else "(close)"))
        print(f"  current table would be : {ratio_now:.2f}x "
              + ("(still UNDER-counting; raise prices)" if ratio_now > 1.05
                 else "(over-counting; safe)" if ratio_now < 0.95 else "(accurate)"))
        if ratio_now > 1.05:
            print("  action: raise the prices in config/warden.toml, or raise "
                  "pricing_safety_factor")
    else:
        print()
        print("  pass --billed <usd> with the provider's figure to compute the "
              "implied rate")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="warden", description="Warden operator commands")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_reset = sub.add_parser("reset", help="clear or archive run data")
    p_reset.add_argument("--archive", action="store_true", help="move data aside instead of deleting")
    p_reset.set_defaults(func=cmd_reset)

    p_status = sub.add_parser("status", help="print run status")
    p_status.add_argument("--all-runs", action="store_true")
    p_status.set_defaults(func=cmd_status)

    p_verify = sub.add_parser("verify", help="verify the audit chain")
    p_verify.set_defaults(func=cmd_verify)

    p_doctor = sub.add_parser("doctor", help="check credentials, backends and reachability")
    p_doctor.set_defaults(func=cmd_doctor)

    p_recon = sub.add_parser(
        "reconcile", help="compare the ledger cost estimate against real billing")
    p_recon.add_argument("--billed", type=float, default=None,
                         help="the amount the provider actually billed, in USD")
    p_recon.set_defaults(func=cmd_reconcile)

    p_halt = sub.add_parser("halt", help="trip or clear the kill switch")
    p_halt.add_argument("--reason", default="manual halt from cli")
    p_halt.add_argument("--clear", action="store_true")
    p_halt.set_defaults(func=cmd_halt)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
