"""Each component still exposes the surface the others call.

This exists because an edit once removed AttackerAgent.select entirely. Every
unit test still passed, because none of them constructed the orchestration
path; the loss only surfaced as five consecutive round failures in a live run,
after the supervisor had restarted into the same deterministic bug repeatedly.
A missing attribute is the cheapest possible bug to catch and was the most
expensive one to find.
"""
from __future__ import annotations

import inspect

import pytest

from analysis.service import AnalysisService
from attacker.agent import AttackerAgent, TargetClient
from defender.agent import DefenderAgent
from evals.runner import run_benign_suite
from orchestrator.loop import Orchestrator
from target.agent import TargetAgent
from warden.budget import BudgetGovernor
from warden.ledger import Ledger
from warden.llm import ModelClient
from warden.versioning import VersionStore

EXPECTED = {
    AttackerAgent: ["select", "paraphrase", "run_attack", "run_round", "close",
                    "_preserves_objective", "_payload_tokens"],
    DefenderAgent: ["defend", "propose", "breach_report", "current_versions",
                    "patch_space_remaining"],
    TargetAgent: ["handle", "reload", "bootstrap", "status", "versions",
                  "active_pair", "session_state"],
    Orchestrator: ["run", "run_round", "maybe_eval", "maybe_analyse",
                   "final_summary", "wait_for_target", "close"],
    AnalysisService: ["run", "narrative"],
    BudgetGovernor: ["reserve", "settle", "release", "snapshot", "state", "halt",
                     "clear_halt", "is_halted", "mark_round", "reap_stale",
                     "calls_in_round", "rounds_in_last_hour", "project_cost"],
    Ledger: ["append", "log", "recent", "verify_chain", "count", "total_spend"],
    VersionStore: ["commit", "activate", "active", "active_id", "history",
                   "ensure_initialised", "pointer_mtime"],
    ModelClient: ["call"],
    TargetClient: ["chat", "healthz", "close"],
}


@pytest.mark.parametrize("cls,names", list(EXPECTED.items()),
                         ids=[c.__name__ for c in EXPECTED])
def test_public_surface_present(cls, names):
    missing = [n for n in names if not hasattr(cls, n)]
    assert not missing, f"{cls.__name__} is missing {missing}"


def test_supabase_backends_match_their_sqlite_counterparts():
    """The two backends are swapped by a factory, so their surfaces must agree."""
    from warden.supabase_store import SupabaseGovernor, SupabaseLedger, SupabaseVersionStore

    for sqlite_cls, supa_cls in ((Ledger, SupabaseLedger),
                                 (BudgetGovernor, SupabaseGovernor),
                                 (VersionStore, SupabaseVersionStore)):
        expected = {n for n in EXPECTED.get(sqlite_cls, [])}
        missing = [n for n in expected if not hasattr(supa_cls, n)]
        assert not missing, f"{supa_cls.__name__} is missing {missing}"


def test_orchestrator_calls_only_methods_that_exist():
    """Catch a renamed or deleted collaborator method by reading the source."""
    src = inspect.getsource(Orchestrator)
    for attr, owner in (("self.attacker.run_round", AttackerAgent),
                        ("self.defender.defend", DefenderAgent),
                        ("self.analysis.run", AnalysisService),
                        ("self.target.healthz", TargetClient)):
        if attr in src:
            name = attr.rsplit(".", 1)[1]
            assert hasattr(owner, name), f"{owner.__name__}.{name} is called but absent"


def test_run_benign_suite_accepts_the_client_the_loop_passes():
    sig = inspect.signature(run_benign_suite)
    assert "client" in sig.parameters
    assert "concurrency" in sig.parameters
