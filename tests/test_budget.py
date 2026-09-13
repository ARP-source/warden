"""The budget governor is a control, so these tests try to defeat it."""
from __future__ import annotations

import pytest

from warden.budget import BudgetExhausted, Halted, RateLimited


def test_ceiling_is_never_exceeded(tight_governor):
    gov = tight_governor
    model = gov.cfg.models.target
    with pytest.raises((BudgetExhausted, Halted, RateLimited)):
        for _ in range(200):
            res = gov.reserve("defender", model, 2000, 500)
            gov.settle(res, 2000, 500)
    assert gov.committed_usd() <= gov.cfg.budget.ceiling_usd + 1e-9


def test_ceiling_trips_the_kill_switch(tight_governor):
    gov = tight_governor
    model = gov.cfg.models.target
    with pytest.raises((BudgetExhausted, Halted)):
        for _ in range(200):
            res = gov.reserve("defender", model, 2000, 500)
            gov.settle(res, 2000, 500)
    assert gov.is_halted() is True, "reaching the ceiling must stop the run"
    # Once halted, nothing else is admitted.
    with pytest.raises(Halted):
        gov.reserve("defender", model, 10, 10)


def test_drain_mode_sheds_attacker_but_admits_defender(tight_governor):
    gov = tight_governor
    model = gov.cfg.models.target
    # Burn down into the drain band.
    for _ in range(100):
        if gov.remaining_usd() <= gov.cfg.budget.reserve_floor_usd:
            break
        try:
            res = gov.reserve("target", model, 1200, 300)
            gov.settle(res, 1200, 300)
        except (BudgetExhausted, RateLimited):
            break
    assert gov.state() in ("drain", "halted")
    if gov.state() == "drain":
        with pytest.raises(BudgetExhausted) as exc:
            gov.reserve("attacker", model, 100, 50)
        assert exc.value.reason == "drain_mode"
        # Defence and reporting still get through so the run ends consistently.
        res = gov.reserve("defender", model, 100, 50)
        assert res.id > 0


def test_per_round_rate_limit(governor):
    gov = governor
    model = gov.cfg.models.target
    limit = gov.cfg.limits.max_model_calls_per_round
    for _ in range(limit):
        res = gov.reserve("target", model, 50, 20, round_id=7)
        gov.settle(res, 50, 20)
    with pytest.raises(RateLimited) as exc:
        gov.reserve("target", model, 50, 20, round_id=7)
    assert exc.value.reason == "per_round_calls"


def test_per_call_cap_refuses_expensive_calls(governor):
    with pytest.raises(BudgetExhausted) as exc:
        governor.reserve("defender", "claude-opus-5", 40000, 4000)
    assert exc.value.reason == "max_call_exceeded"


def test_reservation_is_idempotent(governor):
    model = governor.cfg.models.target
    a = governor.reserve("target", model, 100, 50, round_id=1, idem_key="r1:x")
    governor.settle(a, 100, 50)
    before = governor.committed_usd()
    b = governor.reserve("target", model, 100, 50, round_id=1, idem_key="r1:x")
    assert a.id == b.id
    assert governor.committed_usd() == pytest.approx(before)


def test_release_returns_budget(governor):
    model = governor.cfg.models.target
    before = governor.committed_usd()
    res = governor.reserve("target", model, 5000, 700)
    assert governor.committed_usd() > before
    governor.release(res, reason="model_call_failed")
    assert governor.committed_usd() == pytest.approx(before)


def test_reap_releases_orphaned_reservations(governor):
    model = governor.cfg.models.target
    governor.reserve("target", model, 100, 50)
    # A reservation younger than the cutoff must not be reaped.
    assert governor.reap_stale(older_than_s=3600) == 0
    assert governor.reap_stale(older_than_s=-1) == 1


def test_unknown_model_is_priced_pessimistically(governor):
    cheap = governor.project_cost(governor.cfg.models.target, 1000, 1000)
    unknown = governor.project_cost("some-model-we-never-heard-of", 1000, 1000)
    assert unknown > cheap
