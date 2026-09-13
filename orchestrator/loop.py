"""The round loop that runs unattended.

Each round: attack, defend anything that breached, prove normal behaviour still
works, and periodically analyse. The loop is built on the assumption that any
phase can fail, so every phase is isolated - a crash in analysis must not stop
attacks, and a broken attack must not stop the round.

Stopping is a first-class outcome, not an error. The loop ends cleanly and writes
a final summary when any of these happen:

* the budget ceiling or a rate limit is reached (``GovernorStop``),
* the kill switch file appears,
* the configured round count completes,
* too many consecutive rounds fail.
"""
from __future__ import annotations

import signal
import time
from dataclasses import dataclass, field
from typing import Any

from analysis.service import AnalysisService
from attacker.agent import AttackerAgent, AttackOutcome, TargetClient, TargetUnavailable
from defender.agent import DefenderAgent, PatchResult
from evals.runner import EvalReport, run_benign_suite
from warden import metrics
from warden import observability as obs
from warden.budget import BudgetGovernor, GovernorStop, get_governor
from warden.config import Config, get_config, resolved_mode
from warden.ledger import (
    ACT_ERROR,
    ACT_HALT,
    ACT_ROUND_END,
    ACT_ROUND_START,
    ACTOR_ORCHESTRATOR,
    Ledger,
    get_ledger,
)


@dataclass
class RoundReport:
    round_id: int
    attacks: int = 0
    intent_breaches: int = 0
    enforcement_breaches: int = 0
    patches_applied: int = 0
    patches_held: int = 0
    patches_reverted: int = 0
    benign_score: float | None = None
    cost_usd: float = 0.0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "round_id": self.round_id, "attacks": self.attacks,
            "intent_breaches": self.intent_breaches,
            "enforcement_breaches": self.enforcement_breaches,
            "patches_applied": self.patches_applied,
            "patches_held": self.patches_held,
            "patches_reverted": self.patches_reverted,
            "benign_score": self.benign_score,
            "cost_usd": round(self.cost_usd, 6),
            "errors": self.errors,
        }


class Orchestrator:
    """Runs rounds until told to stop, or until the governor stops it."""

    def __init__(self, cfg: Config | None = None, ledger: Ledger | None = None,
                 governor: BudgetGovernor | None = None):
        self.cfg = cfg or get_config()
        self.ledger = ledger or get_ledger(self.cfg)
        self.governor = governor or get_governor(self.cfg, self.ledger)
        self.target = TargetClient(self.cfg.target.base_url)
        self.attacker = AttackerAgent(self.cfg, self.ledger, self.target)
        self.defender = DefenderAgent(self.cfg, self.ledger, target=self.target,
                                      attacker=self.attacker)
        self.analysis = AnalysisService(self.cfg, self.ledger)
        self.tracing = obs.init()
        self.last_benign: EvalReport | None = None
        self.stop_reason: str | None = None
        self._stopping = False
        self._install_signal_handlers()

    def _install_signal_handlers(self) -> None:
        """Make Ctrl-C and a terminate signal end the run cleanly."""
        def handler(signum, _frame):  # pragma: no cover - signal path
            self._stopping = True
            self.stop_reason = f"signal {signum}"

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError, AttributeError):
                # Not available on every platform or thread; the halt file and
                # round limits still bound the run.
                pass

    # --- preflight -------------------------------------------------------------
    def wait_for_target(self, timeout_s: float = 30.0) -> bool:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            try:
                self.target.healthz()
                return True
            except Exception:
                time.sleep(1.0)
        return False

    # --- one round -------------------------------------------------------------
    def run_round(self, round_id: int, attacks_per_round: int) -> RoundReport:
        report = RoundReport(round_id=round_id)
        self.governor.mark_round(round_id)
        self.ledger.log(
            ACTOR_ORCHESTRATOR, ACT_ROUND_START, outcome="begin", round_id=round_id,
            payload={"attacks_planned": attacks_per_round,
                     "budget": self.governor.snapshot()},
        )

        outcomes: list[AttackOutcome] = self.attacker.run_round(round_id, attacks_per_round)
        report.attacks = len(outcomes)
        report.intent_breaches = sum(1 for o in outcomes if o.breach_intent)
        report.enforcement_breaches = sum(1 for o in outcomes if o.breach_enforcement)
        report.cost_usd += sum(o.cost_usd for o in outcomes)

        # Defend every breach. Enforcement breaches first: they are the ones with
        # a real side effect, so they get the remaining budget before the rest.
        breaches = [o for o in outcomes if o.breach_intent or o.breach_enforcement]
        breaches.sort(key=lambda o: (not o.breach_enforcement, o.attack_id))
        for outcome in breaches:
            if self._should_stop():
                break
            try:
                result: PatchResult = self.defender.defend(
                    outcome, round_id, benign_before=self.last_benign
                )
            except GovernorStop:
                raise
            except Exception as exc:
                report.errors.append(f"defend({outcome.attack_id}): {exc}")
                self.ledger.log(
                    ACTOR_ORCHESTRATOR, ACT_ERROR, outcome="defend_failed", round_id=round_id,
                    payload={"attack_id": outcome.attack_id, "error": str(exc)[:400]},
                )
                continue
            report.cost_usd += result.cost_usd
            if result.applied:
                report.patches_applied += 1
            if result.held:
                report.patches_held += 1
            if result.reverted:
                report.patches_reverted += 1
            # The defender ran the suite as its regression check; reuse that score
            # as the baseline for the next patch rather than paying for it twice.
            if result.benign_after is not None and self.last_benign is not None:
                self.last_benign.score = result.benign_after

        return report

    def _should_stop(self) -> bool:
        return self._stopping or self.governor.is_halted()

    def maybe_eval(self, round_id: int, force: bool = False) -> None:
        """Run the benign suite on schedule, independently of patching."""
        due = force or self.last_benign is None or (
            round_id % max(1, self.cfg.eval.every_n_rounds) == 0
        )
        if not due:
            return
        report = run_benign_suite(
            self.target, run_label=f"scheduled-r{round_id}", round_id=round_id,
            ledger=self.ledger, cfg=self.cfg,
        )
        if report.stopped_early is None:
            self.last_benign = report

    def maybe_analyse(self, round_id: int, force: bool = False) -> None:
        due = force or (round_id % max(1, self.cfg.analysis.every_n_rounds) == 0)
        if not due:
            return
        try:
            self.analysis.run(round_id=round_id, target=self.target)
        except GovernorStop:
            raise
        except Exception as exc:
            # Analysis is reporting, not control. Losing it must not stop the run.
            self.ledger.log(
                ACTOR_ORCHESTRATOR, ACT_ERROR, outcome="analysis_failed", round_id=round_id,
                payload={"error": str(exc)[:400]},
            )

    # --- the run ---------------------------------------------------------------
    def run(self, rounds: int | None = None, attacks_per_round: int = 5,
            start_round: int | None = None) -> dict[str, Any]:
        """Run rounds until a stop condition. Returns the final summary."""
        if not self.wait_for_target():
            self.stop_reason = "target unreachable"
            self.ledger.log(ACTOR_ORCHESTRATOR, ACT_ERROR, outcome="target_unreachable",
                            payload={"base_url": self.cfg.target.base_url})
            return self.final_summary()

        round_id = start_round if start_round is not None else self._next_round_id()
        completed = 0
        consecutive_errors = 0

        # Establish the baseline before any attack, so every later score has
        # something to be compared against.
        try:
            self.maybe_eval(round_id, force=True)
        except GovernorStop as exc:
            self.stop_reason = f"{exc.reason}: {exc}"
            return self.final_summary()

        while True:
            if rounds is not None and completed >= rounds:
                self.stop_reason = self.stop_reason or "round count reached"
                break
            if self._should_stop():
                self.stop_reason = self.stop_reason or "kill switch"
                break
            if self.governor.rounds_in_last_hour() >= self.cfg.limits.max_rounds_per_hour:
                # A rate limit is a reason to wait, not to end the run.
                self.ledger.log(
                    ACTOR_ORCHESTRATOR, ACT_ERROR, outcome="round_rate_limited",
                    round_id=round_id,
                    payload={"rounds_last_hour": self.governor.rounds_in_last_hour(),
                             "max": self.cfg.limits.max_rounds_per_hour},
                )
                if not self._sleep(60.0):
                    break
                continue

            try:
                report = self.run_round(round_id, attacks_per_round)
                consecutive_errors = 0
            except GovernorStop as exc:
                self.stop_reason = f"{exc.reason}: {exc}"
                self.ledger.log(
                    ACTOR_ORCHESTRATOR, ACT_HALT, outcome="governor_stop", round_id=round_id,
                    payload={"reason": exc.reason, "detail": exc.detail},
                )
                break
            except TargetUnavailable as exc:
                consecutive_errors += 1
                self.ledger.log(ACTOR_ORCHESTRATOR, ACT_ERROR, outcome="target_unavailable",
                                round_id=round_id, payload={"error": str(exc)[:300]})
                if consecutive_errors >= self.cfg.limits.max_consecutive_round_errors:
                    self.stop_reason = "target unavailable repeatedly"
                    break
                if not self._sleep(5.0 * consecutive_errors):
                    break
                round_id += 1
                continue
            except Exception as exc:
                consecutive_errors += 1
                self.ledger.log(ACTOR_ORCHESTRATOR, ACT_ERROR, outcome="round_failed",
                                round_id=round_id, payload={"error": str(exc)[:400]})
                if consecutive_errors >= self.cfg.limits.max_consecutive_round_errors:
                    self.stop_reason = f"{consecutive_errors} consecutive round failures"
                    break
                round_id += 1
                continue

            try:
                self.maybe_eval(round_id)
                self.maybe_analyse(round_id)
            except GovernorStop as exc:
                self.stop_reason = f"{exc.reason}: {exc}"
                break

            report.benign_score = self.last_benign.score if self.last_benign else None
            self.ledger.log(
                ACTOR_ORCHESTRATOR, ACT_ROUND_END, outcome="complete", round_id=round_id,
                cost_usd=report.cost_usd, payload=report.as_dict(),
            )
            completed += 1
            round_id += 1
            if not self._sleep(self.cfg.limits.round_cooldown_s):
                break

        # A final report even when stopping early is the difference between a
        # clean shutdown and a process that simply vanished.
        try:
            self.maybe_analyse(round_id, force=True)
        except Exception:
            pass
        return self.final_summary()

    def _sleep(self, seconds: float) -> bool:
        """Sleep in short slices so a stop signal is noticed promptly."""
        end = time.time() + max(0.0, seconds)
        while time.time() < end:
            if self._should_stop():
                return False
            time.sleep(min(0.25, max(0.0, end - time.time())))
        return not self._should_stop()

    def _next_round_id(self) -> int:
        """Continue numbering after whatever this run already recorded.

        Backend-aware: the Postgres ledger deliberately refuses raw SQL, so the
        highest round is read through PostgREST ordering instead.
        """
        if getattr(self.ledger, "backend", "sqlite") == "supabase":
            rows = self.ledger.view(
                "warden_ledger",
                filters={"run_id": f"eq.{self.ledger.run_id}",
                         "round_id": "not.is.null"},
                order="round_id.desc", limit=1,
            )
            return (int(rows[0]["round_id"]) + 1) if rows else 1
        rows = self.ledger.query(
            "SELECT COALESCE(MAX(round_id), 0) AS m FROM ledger WHERE run_id = ?",
            (self.ledger.run_id,),
        )
        return int(rows[0]["m"]) + 1

    def final_summary(self) -> dict[str, Any]:
        from warden.config import resolved_store

        summary = {
            "stop_reason": self.stop_reason or "unspecified",
            "mode": resolved_mode(self.cfg),
            "store": resolved_store(),
            "tracing": self.tracing,
            "run_id": self.ledger.run_id,
            "budget": self.governor.snapshot(),
            "metrics": metrics.run_summary_any(self.ledger, self.ledger.run_id),
            "chain": self.ledger.verify_chain(),
        }
        self.ledger.log(
            ACTOR_ORCHESTRATOR, ACT_ROUND_END, outcome="run_complete",
            payload={"stop_reason": summary["stop_reason"],
                     "budget": summary["budget"],
                     "attacks": summary["metrics"]["attacks"],
                     "benign_latest": summary["metrics"]["benign_latest"]},
        )
        return summary

    def close(self) -> None:
        self.target.close()
