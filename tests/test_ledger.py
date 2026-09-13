"""The audit ledger must be append-only and tamper-evident, not just tidy."""
from __future__ import annotations

import sqlite3

import pytest

from warden.ledger import ACT_ATTACK_ATTEMPT, ACTOR_ATTACKER, LedgerEntry


def test_chain_verifies_over_many_entries(ledger):
    for i in range(25):
        ledger.log(ACTOR_ATTACKER, ACT_ATTACK_ATTEMPT, outcome="no_breach",
                   round_id=i, payload={"n": i})
    result = ledger.verify_chain()
    assert result["ok"] is True
    assert result["checked"] == 25


def test_idem_key_does_not_double_log_or_double_count(ledger):
    first = ledger.log(ACTOR_ATTACKER, ACT_ATTACK_ATTEMPT, cost_usd=0.01,
                       idem_key="r1:a1", payload={"attempt": 1})
    second = ledger.log(ACTOR_ATTACKER, ACT_ATTACK_ATTEMPT, cost_usd=99.0,
                        idem_key="r1:a1", payload={"attempt": 2})
    assert first.seq == second.seq
    assert second.payload == {"attempt": 1}, "the original entry must win"
    assert ledger.count() == 1
    assert ledger.total_spend() == pytest.approx(0.01)


def test_update_and_delete_are_rejected(ledger):
    ledger.log(ACTOR_ATTACKER, ACT_ATTACK_ATTEMPT, outcome="no_breach")
    raw = sqlite3.connect(str(ledger.db_path))
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute("UPDATE ledger SET outcome='breach_enforcement' WHERE seq=1")
    with pytest.raises(sqlite3.IntegrityError):
        raw.execute("DELETE FROM ledger WHERE seq=1")
    raw.close()


def test_tampering_is_detected_at_the_exact_row(ledger):
    for i in range(6):
        ledger.log(ACTOR_ATTACKER, ACT_ATTACK_ATTEMPT, outcome="no_breach",
                   payload={"i": i})
    raw = sqlite3.connect(str(ledger.db_path))
    # Drop the guard trigger, then alter history the way an attacker would.
    raw.executescript(
        "DROP TRIGGER ledger_no_update;"
        " UPDATE ledger SET outcome='no_breach', payload_json='{\"i\":999}' WHERE seq=3;"
    )
    raw.commit()
    raw.close()
    result = ledger.verify_chain()
    assert result["ok"] is False
    assert result["broken_at"] == 3


def test_entry_hash_covers_payload(ledger):
    entry = LedgerEntry(actor="target", action="tool_call_attempt",
                        payload={"tool": "issue_refund"})
    stored = ledger.append(entry)
    recomputed = stored.compute_hash(stored.prev_hash)
    assert recomputed == stored.entry_hash
    stored.payload["tool"] = "modify_account_permissions"
    assert stored.compute_hash(stored.prev_hash) != stored.entry_hash
