"""Entry point for the round loop. Run with: python run_loop.py [rounds] [attacks]"""
from __future__ import annotations

import json
import sys

from orchestrator.loop import Orchestrator

if __name__ == "__main__":
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else None
    per_round = int(sys.argv[2]) if len(sys.argv) > 2 else 5
    orch = Orchestrator()
    try:
        summary = orch.run(rounds=rounds, attacks_per_round=per_round)
    finally:
        orch.close()
    print(json.dumps(summary, indent=2, default=str))
