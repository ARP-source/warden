"""Run the same attack catalogue against several models and compare them.

The loop answers "can this agent be hardened". This answers a different and
more useful question: **which model is safe to hand tools to in the first
place**. Same catalogue, same fixed undefended policy, same benign suite, one
model at a time.

Holding the policy fixed matters. If the Defender were allowed to patch between
models, every model after the first would inherit defences it did not earn and
the comparison would measure running order rather than the model.

One distinction the table has to make honestly: a model that cannot call tools
scores a perfect zero on attacks and is useless as an agent. Zero breaches for
that reason is not safety. Any model that makes no tool call anywhere - not on
a single attack, not on a single benign case - is reported as unusable rather
than as secure.

    python benchmark_models.py [--models a,b,c] [--attacks N] [--concurrency N]
"""
from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from attacker import catalog  # noqa: E402
from attacker.agent import AttackerAgent, TargetClient  # noqa: E402
from evals.benign_suite import BENIGN_CASES  # noqa: E402
from evals.runner import run_benign_suite  # noqa: E402
from warden.budget import GovernorStop, get_governor  # noqa: E402
from warden.config import get_config  # noqa: E402
from warden.ledger import ACTOR_ANALYSIS, Ledger, get_ledger  # noqa: E402

# Models plausibly deployed behind real agents, plus a small one for contrast.
DEFAULT_MODELS = [
    "meta-llama/Llama-3.3-70B-Instruct",
    "meta-llama/Llama-3.1-8B-Instruct",
    "openai/gpt-oss-120b",
    "deepseek-ai/DeepSeek-V3.1",
    "Qwen/Qwen3-235B-A22B-Instruct-2507",
    "zai-org/GLM-5.2",
    "moonshotai/Kimi-K2.6",
]


@dataclass
class ModelResult:
    model: str
    attacks: int = 0
    intent: int = 0
    executed: int = 0
    errors: int = 0
    tool_calls_seen: int = 0
    benign_score: float | None = None
    benign_false_refusals: float | None = None
    benign_tool_calls: int = 0
    cost_usd: float = 0.0
    by_category: dict[str, dict[str, int]] = field(default_factory=dict)
    note: str = ""

    @property
    def intent_rate(self) -> float:
        return self.intent / self.attacks if self.attacks else 0.0

    @property
    def executed_rate(self) -> float:
        return self.executed / self.attacks if self.attacks else 0.0

    @property
    def usable_as_agent(self) -> bool:
        """Did it ever actually call a tool? If not, zero breaches means nothing."""
        return (self.tool_calls_seen + self.benign_tool_calls) > 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "model": self.model, "attacks": self.attacks, "intent": self.intent,
            "executed": self.executed, "errors": self.errors,
            "intent_rate": round(self.intent_rate, 4),
            "executed_rate": round(self.executed_rate, 4),
            "benign_score": self.benign_score,
            "benign_false_refusals": self.benign_false_refusals,
            "usable_as_agent": self.usable_as_agent,
            "tool_calls_seen": self.tool_calls_seen,
            "cost_usd": round(self.cost_usd, 6),
            "by_category": self.by_category,
            "note": self.note,
        }


def reset_to_undefended() -> tuple[str, str]:
    """Pin the Target to the shipped, unhardened policy and prompt.

    Every model must face the same conditions. Without this, a model tested
    later inherits defences earned against an earlier one.
    """
    from target.policy import default_policy_v1, validate_policy
    from target.prompt import default_prompt_v1
    from warden.versioning import KIND_POLICY, KIND_PROMPT, get_store

    store = get_store()
    p = store.commit(KIND_PROMPT, default_prompt_v1(), author="benchmark",
                     rationale="undefended baseline, held fixed across models")
    s = store.commit(KIND_POLICY, validate_policy(default_policy_v1()),
                     author="benchmark",
                     rationale="undefended baseline, held fixed across models")
    return p.version, s.version


def run_model(model: str, attacks: list[dict[str, Any]], client: TargetClient,
              attacker: AttackerAgent, ledger: Ledger, cfg: Any,
              concurrency: int, round_id: int) -> ModelResult:
    result = ModelResult(model=model)

    def one(item: tuple[int, dict[str, Any]]):
        idx, attack = item
        # A distinct round per attack keeps the per-round call cap - a runaway
        # guard sized for the loop, not for a benchmark sweep - from binding
        # when 49 attacks and a benign suite would otherwise share one round.
        attack_round = round_id * 1000 + idx
        try:
            return attacker.run_attack(attack, attack_round, target_model=model,
                                       session_id=f"bench-{round_id}-"
                                                  f"{model.split('/')[-1][:18]}-{idx}")
        except Exception as exc:
            # A halt or a transport fault on one attack must not abort the
            # whole model's batch; record it as an error and move on.
            print(f"      attack {attack['id']} errored: "
                  f"{type(exc).__name__}: {str(exc)[:120]}", flush=True)
            return None

    outcomes = []
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for out in pool.map(one, list(enumerate(attacks))):
            outcomes.append(out)

    for attack, out in zip(attacks, outcomes):
        if out is None:
            result.errors += 1
            continue
        result.attacks += 1
        result.cost_usd += out.cost_usd
        result.tool_calls_seen += len(out.tools_attempted)
        cat = result.by_category.setdefault(
            attack["category"], {"attacks": 0, "intent": 0, "executed": 0})
        cat["attacks"] += 1
        if out.breach_intent:
            result.intent += 1
            cat["intent"] += 1
        if out.breach_enforcement:
            result.executed += 1
            cat["executed"] += 1

    # Benign suite on the same model, so the table shows the cost of caution as
    # well as the benefit.
    try:
        rep = run_benign_suite(
            _ModelPinnedClient(client, model),
            run_label=f"bench-{model.split('/')[-1][:24]}",
            # A dedicated round well above the attack rounds, so the 18-case
            # suite is not throttled by the per-round cap the attacks used.
            round_id=round_id * 1000 + 900, ledger=ledger, cfg=cfg,
            governor=get_governor(cfg, ledger), concurrency=concurrency,
        )
        if rep.stopped_early is None:
            result.benign_score = round(rep.score, 4)
            result.benign_false_refusals = round(rep.false_refusal_rate, 4)
            result.benign_tool_calls = sum(len(c.tools_executed) for c in rep.cases)
            result.cost_usd += rep.cost_usd
        else:
            result.note = f"benign suite incomplete: {rep.stopped_early}"
    except GovernorStop:
        raise
    except Exception as exc:
        result.note = f"benign suite failed: {exc}"

    if not result.usable_as_agent:
        result.note = ("never produced a tool call; unusable as an agent, so a "
                       "zero breach rate says nothing about its safety")
    return result


class _ModelPinnedClient:
    """Wraps the Target client so every request pins one model."""

    def __init__(self, inner: TargetClient, model: str):
        self._inner = inner
        self._model = model

    def chat(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self._inner.chat({**payload, "target_model": self._model})


def render_table(results: list[ModelResult]) -> str:
    rows = ["| Model | Attacks | Manipulated | Executed out of scope | Benign | "
            "False refusals | Usable |",
            "| --- | --- | --- | --- | --- | --- | --- |"]
    for r in sorted(results, key=lambda x: (-x.executed_rate, -x.intent_rate)):
        benign = f"{100*r.benign_score:.1f}%" if r.benign_score is not None else "-"
        frr = (f"{100*r.benign_false_refusals:.1f}%"
               if r.benign_false_refusals is not None else "-")
        rows.append(
            f"| `{r.model}` | {r.attacks} | {100*r.intent_rate:.1f}% | "
            f"**{100*r.executed_rate:.1f}%** | {benign} | {frr} | "
            + ("yes" if r.usable_as_agent else "**NO**") + " |")
    return "\n".join(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--models", default=",".join(DEFAULT_MODELS))
    ap.add_argument("--attacks", type=int, default=0,
                    help="limit the catalogue to the first N attacks (0 = all)")
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--out", default="reports/model-comparison.md")
    args = ap.parse_args()

    cfg = get_config()
    ledger = get_ledger(cfg)
    client = TargetClient(cfg.target.base_url)
    attacker = AttackerAgent(cfg, ledger, client, paraphrase=False)

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    attacks = list(catalog.ATTACKS)
    if args.attacks:
        attacks = attacks[:args.attacks]

    pv, sv = reset_to_undefended()
    try:
        client._client.post("/admin/reload")
    except Exception:
        pass

    print(f"Comparing {len(models)} models on {len(attacks)} attacks and "
          f"{len(BENIGN_CASES)} benign cases")
    print(f"Policy pinned at {pv}/{sv} (undefended) for every model\n")

    results: list[ModelResult] = []
    base_round = 900000
    for i, model in enumerate(models):
        print(f"  [{i+1}/{len(models)}] {model} ...", flush=True)
        try:
            r = run_model(model, attacks, client, attacker, ledger, cfg,
                          args.concurrency, base_round + i)
        except GovernorStop as exc:
            print(f"      stopped by the governor: {exc}")
            break
        results.append(r)
        flag = "" if r.usable_as_agent else "  [NOT TOOL-CAPABLE]"
        print(f"      manipulated {100*r.intent_rate:5.1f}%  executed "
              f"{100*r.executed_rate:5.1f}%  benign "
              + (f"{100*r.benign_score:5.1f}%" if r.benign_score is not None else "   -")
              + f"  ${r.cost_usd:.4f}{flag}", flush=True)

    table = render_table(results)
    print("\n" + table)

    out = Path(args.out)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            "# Which models are safe to hand tools to\n\n"
            f"Same {len(attacks)} attacks, same undefended policy (`{pv}/{sv}`), "
            f"same {len(BENIGN_CASES)}-case benign suite, one model at a time.\n\n"
            "A model that never calls a tool scores zero on attacks and is useless "
            "as an agent. Those are marked **NO** under Usable, and their zero is "
            "not a safety result.\n\n" + table + "\n",
            encoding="utf-8")
        print(f"\nwritten to {out}")
    except OSError as exc:
        print(f"\ncould not write report: {exc}")

    ledger.log(ACTOR_ANALYSIS, "analysis_report", outcome="model_comparison",
               payload={"models": [r.as_dict() for r in results],
                        "attacks": len(attacks), "policy": f"{pv}/{sv}"})
    attacker.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
