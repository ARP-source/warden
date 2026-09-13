"""Session ids must not collide across runs.

Session state (the cumulative refund total) is durable in Postgres. An id that
repeats between runs inherits the earlier run's total, which silently turns a
legitimate request into a cap violation and corrupts the benign score. This
already happened once: `eval-scheduled-r1-b06_refund_at_limit` was reused by
three runs and accumulated $150 against a $120 cap.
"""
from __future__ import annotations

import re

from evals.benign_suite import BENIGN_CASES


def _eval_session_ids(run_id: str, label: str) -> list[str]:
    """Mirror how evals.runner builds session ids."""
    return [f"eval-{run_id}-{label}-{c['id']}" for c in BENIGN_CASES]


def test_eval_sessions_differ_between_runs():
    a = set(_eval_session_ids("run-alpha", "scheduled-r1"))
    b = set(_eval_session_ids("run-beta", "scheduled-r1"))
    assert not (a & b), "same label in two runs must not share session ids"


def test_eval_sessions_unique_within_a_run():
    ids = _eval_session_ids("run-alpha", "scheduled-r1")
    assert len(set(ids)) == len(ids)


def test_runner_builds_session_ids_with_the_run_id():
    """Guard the actual source, not just a reimplementation of it."""
    import inspect

    from evals import runner

    src = inspect.getsource(runner.run_benign_suite)
    assert "run_tag" in src and "eval-{run_tag}" in src, (
        "eval session ids must embed the run id"
    )


def test_defender_verify_sessions_embed_the_run_id():
    import inspect

    from defender.agent import DefenderAgent

    src = inspect.getsource(DefenderAgent.defend) + inspect.getsource(
        DefenderAgent._defend_inner)
    assert "run_id" in src and "verify-" in src, (
        "patch-verification sessions must embed the run id"
    )


def test_attack_sessions_are_unique_per_attempt():
    import inspect

    from attacker.agent import AttackerAgent

    src = inspect.getsource(AttackerAgent.run_attack)
    assert "uuid" in src, "attack sessions need a unique suffix per attempt"
