"""Budget governor: the hard stop on real-dollar spend.

Enforcement model
-----------------
Spend is controlled by *pre-flight reservation*, not post-hoc monitoring. Before
any model call, the caller must obtain a reservation for the worst-case cost of
that call. Reservations are recorded in SQLite inside a single IMMEDIATE
transaction, so two processes cannot both squeeze past the ceiling by checking
at the same moment. After the call the reservation is settled with the actual
token counts, releasing the difference.

Committed spend = sum(actual cost of settled reservations)
               + sum(projected cost of still-open reservations)

Three independent gates must all pass:
  1. the dollar ceiling (with a graceful drain band above zero),
  2. per-round and per-hour model-call rate limits,
  3. the kill switch (a halt file, or an explicit halt call).

Reaching a limit is a normal, logged outcome. The governor raises a
``GovernorStop`` subclass that callers catch to wind down cleanly. It never
calls sys.exit and never crashes the process.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from warden.config import Config, get_config
from warden.ledger import (
    ACT_BUDGET_CHECK,
    ACT_BUDGET_HALT,
    ACT_HALT,
    ACT_MODEL_CALL,
    ACT_RATE_LIMIT,
    ACTOR_GOVERNOR,
    Ledger,
    get_ledger,
    utc_now_iso,
)

# Roles that may keep running while the budget drains, so an interrupted run
# still finishes its diagnosis, regression check and final report.
DRAIN_ALLOWED_ROLES = frozenset({"defender", "analysis", "eval", "judge"})

STATE_NORMAL = "normal"
STATE_DRAIN = "drain"
STATE_HALTED = "halted"


class GovernorStop(Exception):
    """Base class for every governor refusal. Callers stop cleanly on this."""

    def __init__(self, message: str, *, reason: str, detail: dict[str, Any] | None = None):
        super().__init__(message)
        self.reason = reason
        self.detail = detail or {}


class BudgetExhausted(GovernorStop):
    pass


class RateLimited(GovernorStop):
    pass


class Halted(GovernorStop):
    pass


@dataclass
class Reservation:
    """A pre-approved allowance for exactly one model call."""

    id: int
    role: str
    model: str
    projected_usd: float
    round_id: int | None
    idem_key: str | None


_SCHEMA = """
CREATE TABLE IF NOT EXISTS budget_ops (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             TEXT    NOT NULL,
    run_id         TEXT    NOT NULL DEFAULT '',
    round_id       INTEGER,
    role           TEXT    NOT NULL,
    model          TEXT    NOT NULL,
    state          TEXT    NOT NULL,
    projected_usd  REAL    NOT NULL DEFAULT 0.0,
    actual_usd     REAL    NOT NULL DEFAULT 0.0,
    input_tokens   INTEGER NOT NULL DEFAULT 0,
    output_tokens  INTEGER NOT NULL DEFAULT 0,
    settled_ts     TEXT,
    idem_key       TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_ops_state ON budget_ops(state);
CREATE INDEX IF NOT EXISTS idx_ops_ts    ON budget_ops(ts);
CREATE INDEX IF NOT EXISTS idx_ops_round ON budget_ops(round_id);

CREATE TABLE IF NOT EXISTS round_marks (
    run_id    TEXT    NOT NULL DEFAULT '',
    round_id  INTEGER NOT NULL,
    ts        TEXT    NOT NULL,
    PRIMARY KEY (run_id, round_id)
);
CREATE INDEX IF NOT EXISTS idx_rounds_ts ON round_marks(ts);
"""


class BudgetGovernor:
    """Enforces the spend ceiling, the rate limits and the kill switch (SQLite)."""

    backend = "sqlite"

    def __init__(self, cfg: Config | None = None, ledger: Ledger | None = None,
                 db_path: Path | str | None = None):
        self.cfg = cfg or get_config()
        self.ledger = ledger or get_ledger(self.cfg)
        self.db_path = Path(db_path) if db_path else self.cfg.ledger_db
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._local = threading.local()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.db_path), timeout=30.0, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    def _init_db(self) -> None:
        with self._lock:
            self._conn().executescript(_SCHEMA)

    # --- accounting ------------------------------------------------------------
    def committed_usd(self, conn: sqlite3.Connection | None = None) -> float:
        """Settled spend plus the worst case of everything still in flight."""
        conn = conn or self._conn()
        row = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN state = 'settled' THEN actual_usd"
            "                         WHEN state = 'open'    THEN projected_usd"
            "                         ELSE 0.0 END), 0.0) AS total"
            " FROM budget_ops"
        ).fetchone()
        return float(row["total"])

    def settled_usd(self) -> float:
        row = self._conn().execute(
            "SELECT COALESCE(SUM(actual_usd), 0.0) AS t FROM budget_ops WHERE state = 'settled'"
        ).fetchone()
        return float(row["t"])

    def remaining_usd(self) -> float:
        return max(0.0, self.cfg.budget.ceiling_usd - self.committed_usd())

    def project_cost(self, model: str, est_input_tokens: int, max_output_tokens: int) -> float:
        """Worst-case cost of a call: full input plus the full output allowance."""
        return self.cfg.price_for(model).cost(est_input_tokens, max_output_tokens)

    # --- kill switch -----------------------------------------------------------
    def is_halted(self) -> bool:
        return self.cfg.halt_file.exists()

    def halt(self, reason: str, detail: dict[str, Any] | None = None) -> None:
        """Trip the kill switch. Idempotent and safe to call from anywhere."""
        self.cfg.halt_file.parent.mkdir(parents=True, exist_ok=True)
        if not self.cfg.halt_file.exists():
            payload = {"reason": reason, "ts": utc_now_iso(), **(detail or {})}
            self.cfg.halt_file.write_text(
                f"halted at {payload['ts']}\nreason: {reason}\n", encoding="utf-8"
            )
            self.ledger.log(ACTOR_GOVERNOR, ACT_HALT, outcome="halted", payload=payload)

    def clear_halt(self) -> None:
        """Re-arm the system after an operator has reviewed the halt."""
        if self.cfg.halt_file.exists():
            self.cfg.halt_file.unlink()
            self.ledger.log(ACTOR_GOVERNOR, ACT_HALT, outcome="cleared", payload={})

    # --- rate limiting ---------------------------------------------------------
    def mark_round(self, round_id: int) -> None:
        conn = self._conn()
        with self._lock:
            conn.execute(
                "INSERT OR IGNORE INTO round_marks (run_id, round_id, ts) VALUES (?,?,?)",
                (self.ledger.run_id, round_id, utc_now_iso()),
            )

    def _since_iso(self, seconds: int) -> str:
        return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat(
            timespec="microseconds"
        )

    def calls_in_last_hour(self) -> int:
        row = self._conn().execute(
            "SELECT COUNT(*) AS c FROM budget_ops WHERE ts >= ? AND state != 'released'",
            (self._since_iso(3600),),
        ).fetchone()
        return int(row["c"])

    def calls_in_round(self, round_id: int | None) -> int:
        """Scoped to this run: round numbers repeat between runs, so counting
        them globally would make a fresh run inherit an older run's usage and
        trip its own per-round limit before doing any work."""
        if round_id is None:
            return 0
        row = self._conn().execute(
            "SELECT COUNT(*) AS c FROM budget_ops"
            " WHERE round_id = ? AND run_id = ? AND state != 'released'",
            (round_id, self.ledger.run_id),
        ).fetchone()
        return int(row["c"])

    def rounds_in_last_hour(self) -> int:
        row = self._conn().execute(
            "SELECT COUNT(*) AS c FROM round_marks WHERE ts >= ? AND run_id = ?",
            (self._since_iso(3600), self.ledger.run_id),
        ).fetchone()
        return int(row["c"])

    # --- state ----------------------------------------------------------------
    def state(self) -> str:
        if self.is_halted():
            return STATE_HALTED
        remaining = self.remaining_usd()
        if remaining <= 0.0:
            return STATE_HALTED
        if remaining <= self.cfg.budget.reserve_floor_usd:
            return STATE_DRAIN
        return STATE_NORMAL

    def snapshot(self) -> dict[str, Any]:
        """Everything the health endpoint and dashboard need, in one read."""
        committed = self.committed_usd()
        ceiling = self.cfg.budget.ceiling_usd
        return {
            "state": self.state(),
            "ceiling_usd": round(ceiling, 4),
            "committed_usd": round(committed, 6),
            "settled_usd": round(self.settled_usd(), 6),
            "remaining_usd": round(max(0.0, ceiling - committed), 6),
            "reserve_floor_usd": self.cfg.budget.reserve_floor_usd,
            "pct_consumed": round(100.0 * committed / ceiling if ceiling else 0.0, 2),
            "calls_last_hour": self.calls_in_last_hour(),
            "max_calls_per_hour": self.cfg.limits.max_model_calls_per_hour,
            "rounds_last_hour": self.rounds_in_last_hour(),
            "max_rounds_per_hour": self.cfg.limits.max_rounds_per_hour,
            "halted": self.is_halted(),
        }

    def _deny(self, exc: type[GovernorStop], reason: str, message: str, *, role: str, model: str,
              projected: float, round_id: int | None, detail: dict[str, Any]) -> GovernorStop:
        """Record a refusal in the ledger and build the exception to raise."""
        self.ledger.log(
            ACTOR_GOVERNOR,
            ACT_RATE_LIMIT if exc is RateLimited else ACT_BUDGET_CHECK,
            outcome="denied",
            round_id=round_id,
            payload={"role": role, "model": model, "reason": reason,
                     "projected_usd": round(projected, 6), **detail},
        )
        return exc(message, reason=reason, detail=detail)

    # --- the gate --------------------------------------------------------------
    def reserve(self, role: str, model: str, est_input_tokens: int, max_output_tokens: int,
                round_id: int | None = None, idem_key: str | None = None) -> Reservation:
        """Obtain permission for exactly one model call.

        Raises Halted, BudgetExhausted or RateLimited. Every decision, allow or
        deny, is written to the ledger.
        """
        projected = self.project_cost(model, est_input_tokens, max_output_tokens)
        ceiling = self.cfg.budget.ceiling_usd
        dn = dict(role=role, model=model, projected=projected, round_id=round_id)

        # The operator kill switch outranks every other consideration.
        if self.is_halted():
            raise self._deny(Halted, "kill_switch", "run halted by kill switch", detail={}, **dn)

        # A single call priced above the per-call cap is a configuration error,
        # not a reason to burn the remaining budget discovering it.
        if projected > self.cfg.budget.max_call_usd:
            raise self._deny(
                BudgetExhausted, "max_call_exceeded",
                f"projected {projected:.4f} USD exceeds per-call cap "
                f"{self.cfg.budget.max_call_usd:.4f}",
                detail={"max_call_usd": self.cfg.budget.max_call_usd}, **dn,
            )

        conn = self._conn()
        res_id: int | None = None
        with self._lock:
            for attempt in range(6):
                pending: GovernorStop | None = None
                try:
                    conn.execute("BEGIN IMMEDIATE")

                    if idem_key:
                        prior = conn.execute(
                            "SELECT * FROM budget_ops WHERE idem_key = ?", (idem_key,)
                        ).fetchone()
                        if prior is not None:
                            conn.execute("COMMIT")
                            # A retry of an already-charged call: hand back the
                            # original reservation so nothing is double-counted.
                            return Reservation(
                                id=int(prior["id"]), role=prior["role"], model=prior["model"],
                                projected_usd=float(prior["projected_usd"]),
                                round_id=prior["round_id"], idem_key=idem_key,
                            )

                    committed = self.committed_usd(conn)
                    remaining = ceiling - committed
                    per_round = self.cfg.limits.max_model_calls_per_round
                    per_hour = self.cfg.limits.max_model_calls_per_hour

                    if remaining <= 0.0 or committed + projected > ceiling:
                        pending = self._pending(
                            BudgetExhausted, "ceiling_reached",
                            f"spend ceiling {ceiling:.2f} USD reached (committed "
                            f"{committed:.4f}, this call {projected:.4f})",
                            {"committed_usd": round(committed, 6), "ceiling_usd": ceiling,
                             "remaining_usd": round(max(0.0, remaining), 6)},
                        )
                    elif remaining <= self.cfg.budget.reserve_floor_usd and role not in DRAIN_ALLOWED_ROLES:
                        pending = self._pending(
                            BudgetExhausted, "drain_mode",
                            f"budget below reserve floor {self.cfg.budget.reserve_floor_usd:.2f} "
                            f"USD; role {role} shed so defence and reporting can finish",
                            {"remaining_usd": round(remaining, 6),
                             "reserve_floor_usd": self.cfg.budget.reserve_floor_usd,
                             "drain_allowed_roles": sorted(DRAIN_ALLOWED_ROLES)},
                        )
                    elif round_id is not None and self.calls_in_round(round_id) >= per_round:
                        used = self.calls_in_round(round_id)
                        pending = self._pending(
                            RateLimited, "per_round_calls",
                            f"round {round_id} already used {used} of {per_round} model calls",
                            {"calls_in_round": used, "max_per_round": per_round},
                        )
                    elif self.calls_in_last_hour() >= per_hour:
                        used = self.calls_in_last_hour()
                        pending = self._pending(
                            RateLimited, "per_hour_calls",
                            f"{used} model calls in the last hour reaches the limit of {per_hour}",
                            {"calls_last_hour": used, "max_per_hour": per_hour},
                        )

                    if pending is None:
                        cur = conn.execute(
                            "INSERT INTO budget_ops (ts, run_id, round_id, role, model, state,"
                            " projected_usd, idem_key) VALUES (?,?,?,?,?,'open',?,?)",
                            (utc_now_iso(), self.ledger.run_id, round_id, role, model,
                             projected, idem_key),
                        )
                        res_id = int(cur.lastrowid)
                    conn.execute("COMMIT")
                    break
                except sqlite3.OperationalError:
                    try:
                        conn.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
                    if attempt == 5:
                        raise
                    time.sleep(0.05 * (2**attempt))

        # Ledger writes happen outside the reservation transaction so that
        # audit logging can never hold the budget lock.
        if pending is not None:
            # Reaching the ceiling is terminal, not merely a refused call: if no
            # call of this size can fit, every later attempt would be refused
            # too. Trip the kill switch so the run winds down instead of
            # busy-looping on denials.
            if pending.reason == "ceiling_reached":
                self.ledger.log(
                    ACTOR_GOVERNOR, ACT_BUDGET_HALT, outcome="ceiling_reached",
                    round_id=round_id, payload=dict(pending.detail, role=role, model=model),
                )
                self.halt("spend ceiling reached", pending.detail)
            raise self._deny(type(pending), pending.reason, str(pending),
                             detail=pending.detail, **dn)

        assert res_id is not None
        self.ledger.log(
            ACTOR_GOVERNOR, ACT_BUDGET_CHECK, outcome="approved", round_id=round_id,
            payload={"role": role, "model": model, "projected_usd": round(projected, 6),
                     "reservation_id": res_id,
                     "remaining_usd": round(max(0.0, ceiling - self.committed_usd()), 6)},
        )
        return Reservation(id=res_id, role=role, model=model, projected_usd=projected,
                           round_id=round_id, idem_key=idem_key)

    @staticmethod
    def _pending(exc: type[GovernorStop], reason: str, message: str,
                 detail: dict[str, Any]) -> GovernorStop:
        """Build a refusal to raise after the transaction closes."""
        return exc(message, reason=reason, detail=detail)

    # --- settlement ------------------------------------------------------------
    def settle(self, reservation: Reservation, input_tokens: int, output_tokens: int,
               outcome: str = "ok", extra: dict[str, Any] | None = None) -> float:
        """Close a reservation with the real token counts. Returns actual cost."""
        actual = self.cfg.price_for(reservation.model).cost(input_tokens, output_tokens)
        conn = self._conn()
        with self._lock:
            conn.execute(
                "UPDATE budget_ops SET state='settled', actual_usd=?, input_tokens=?,"
                " output_tokens=?, settled_ts=? WHERE id=? AND state='open'",
                (actual, input_tokens, output_tokens, utc_now_iso(), reservation.id),
            )
        self.ledger.log(
            ACTOR_GOVERNOR, ACT_MODEL_CALL, outcome=outcome, round_id=reservation.round_id,
            cost_usd=actual,
            payload={"role": reservation.role, "model": reservation.model,
                     "input_tokens": input_tokens, "output_tokens": output_tokens,
                     "projected_usd": round(reservation.projected_usd, 6),
                     "actual_usd": round(actual, 6),
                     "reservation_id": reservation.id, **(extra or {})},
        )
        # Crossing the ceiling on settlement trips the switch for everyone else.
        if self.committed_usd() >= self.cfg.budget.ceiling_usd:
            self.ledger.log(
                ACTOR_GOVERNOR, ACT_BUDGET_HALT, outcome="ceiling_reached",
                round_id=reservation.round_id,
                payload={"committed_usd": round(self.committed_usd(), 6),
                         "ceiling_usd": self.cfg.budget.ceiling_usd},
            )
            self.halt("spend ceiling reached", {"committed_usd": round(self.committed_usd(), 6)})
        return actual

    def release(self, reservation: Reservation, reason: str = "call_failed") -> None:
        """Return an unused reservation to the pool, e.g. after a failed call.

        A released reservation is kept as a row for auditability but stops
        counting against both the ceiling and the rate limits.
        """
        conn = self._conn()
        with self._lock:
            conn.execute(
                "UPDATE budget_ops SET state='released', actual_usd=0.0, settled_ts=?"
                " WHERE id=? AND state='open'",
                (utc_now_iso(), reservation.id),
            )
        self.ledger.log(
            ACTOR_GOVERNOR, ACT_BUDGET_CHECK, outcome="released",
            round_id=reservation.round_id,
            payload={"role": reservation.role, "model": reservation.model, "reason": reason,
                     "reservation_id": reservation.id,
                     "returned_usd": round(reservation.projected_usd, 6)},
        )

    def reap_stale(self, older_than_s: int = 900) -> int:
        """Release reservations orphaned by a crashed process.

        Without this, a process that dies between reserve and settle would hold
        its worst-case projection against the ceiling forever.
        """
        cutoff = self._since_iso(older_than_s)
        conn = self._conn()
        with self._lock:
            cur = conn.execute(
                "UPDATE budget_ops SET state='released', settled_ts=?"
                " WHERE state='open' AND ts < ?",
                (utc_now_iso(), cutoff),
            )
            n = int(cur.rowcount or 0)
        if n:
            self.ledger.log(
                ACTOR_GOVERNOR, ACT_BUDGET_CHECK, outcome="reaped",
                payload={"released_count": n, "older_than_s": older_than_s},
            )
        return n


_default_governor: Any = None
_governor_lock = threading.Lock()


def get_governor(cfg: Config | None = None, ledger: Ledger | None = None):
    """Process-wide default governor, on whichever backend is configured."""
    global _default_governor
    from warden.config import resolved_store

    with _governor_lock:
        if _default_governor is None:
            if resolved_store() == "supabase":
                from warden.supabase_store import SupabaseGovernor

                _default_governor = SupabaseGovernor(cfg, ledger or get_ledger(cfg))
            else:
                _default_governor = BudgetGovernor(cfg, ledger)
        return _default_governor
