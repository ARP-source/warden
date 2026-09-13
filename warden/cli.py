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

            probe = OpenAICompatProvider(cfg.inference.base_url,
                                         cfg.inference.api_key()).probe()
            print(f"  inference reach : {probe}")
        except Exception as exc:
            print(f"  inference reach : FAILED {exc}")
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

    degraded = [n for n, ok, _ in checks if not ok and n != "ANTHROPIC_API_KEY"]
    print()
    if degraded:
        print("Degraded: " + ", ".join(degraded))
        print("The system runs in this state by design, but a run made now is not a "
              "measurement of real models or hosted storage.")
    else:
        print("Fully configured: real models, hosted Postgres, live tracing.")
    return 0 if not degraded else 1


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

    p_halt = sub.add_parser("halt", help="trip or clear the kill switch")
    p_halt.add_argument("--reason", default="manual halt from cli")
    p_halt.add_argument("--clear", action="store_true")
    p_halt.set_defaults(func=cmd_halt)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
