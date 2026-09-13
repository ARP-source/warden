"""Unattended supervisor for a long run.

The orchestrator already isolates failures inside a round. This wrapper handles
the level above that: the whole process dying. It restarts the loop with
backoff, keeps going until a real stop condition is reached, and never restarts
past one - a budget ceiling or a kill switch means stop, not retry.

Run:  python run_overnight.py [max_hours] [attacks_per_round]
"""
from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone

from orchestrator.loop import Orchestrator
from warden.budget import get_governor
from warden.config import get_config, resolved_mode, resolved_store
from warden.ledger import ACT_ERROR, ACTOR_ORCHESTRATOR, get_ledger

# Stop conditions that mean the run is over, not that it should be retried.
TERMINAL = ("ceiling_reached", "kill switch", "drain_mode", "round count reached",
            "signal", "max_call_exceeded")


def log(msg: str) -> None:
    stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{stamp}] {msg}", flush=True)


def main() -> int:
    max_hours = float(sys.argv[1]) if len(sys.argv) > 1 else 8.0
    per_round = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    deadline = time.time() + max_hours * 3600

    cfg = get_config()
    ledger = get_ledger(cfg)
    governor = get_governor(cfg, ledger)

    log(f"run_id={ledger.run_id} mode={resolved_mode(cfg)} store={resolved_store()} "
        f"ceiling=${cfg.budget.ceiling_usd:.2f} deadline={max_hours}h")

    attempt = 0
    backoff = 5.0
    last: dict = {}
    while time.time() < deadline:
        attempt += 1
        # Reservations orphaned by a previous crash would hold their worst-case
        # projection against the ceiling forever.
        reaped = governor.reap_stale(older_than_s=600)
        if reaped:
            log(f"released {reaped} orphaned reservation(s) from a prior process")

        orch = Orchestrator(cfg, ledger, governor)
        try:
            last = orch.run(rounds=None, attacks_per_round=per_round)
        except Exception as exc:
            ledger.log(ACTOR_ORCHESTRATOR, ACT_ERROR, outcome="supervisor_caught",
                       payload={"attempt": attempt, "error": str(exc)[:400]})
            log(f"loop crashed ({type(exc).__name__}: {exc}); restarting in {backoff:.0f}s")
            time.sleep(min(backoff, 120))
            backoff = min(backoff * 2, 120)
            continue
        finally:
            orch.close()

        reason = str(last.get("stop_reason", ""))
        log(f"loop returned: {reason}")
        if any(t in reason for t in TERMINAL):
            log("terminal stop condition; not restarting")
            break
        backoff = 5.0
        time.sleep(5)

    snap = governor.snapshot()
    log(f"finished. spend ${snap['committed_usd']:.4f} of ${snap['ceiling_usd']:.2f}, "
        f"state={snap['state']}, ledger rows={ledger.count()}")
    print(json.dumps(last, indent=2, default=str)[:4000])
    return 0


if __name__ == "__main__":
    sys.exit(main())
