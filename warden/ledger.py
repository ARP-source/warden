"""Append-only, hash-chained audit ledger.

Design notes
------------
This is the audit trail a security reviewer reads, so it is built to be
*verifiably* append-only rather than merely conventionally so. Every entry
carries the hash of its predecessor, so removing or editing any historical row
breaks the chain and ``verify_chain`` reports the exact sequence number of the
break.

Storage is SQLite in WAL mode: one writer at a time, many concurrent readers
(the dashboard reads while the orchestrator writes). Each row is also mirrored
to a JSON Lines file, which gives an independent, greppable copy that survives
database corruption.

Idempotency: an entry may carry ``idem_key``. Appending the same key twice is a
no-op that returns the original entry, so a retried attack or patch step cannot
double-log or double-count.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from warden.config import Config, get_config

GENESIS_HASH = "0" * 64

# --- actors -------------------------------------------------------------------
ACTOR_TARGET = "target"
ACTOR_ATTACKER = "attacker"
ACTOR_DEFENDER = "defender"
ACTOR_ANALYSIS = "analysis"
ACTOR_EVAL = "eval"
ACTOR_GOVERNOR = "governor"
ACTOR_ORCHESTRATOR = "orchestrator"

# --- actions ------------------------------------------------------------------
ACT_TOOL_CALL_ATTEMPT = "tool_call_attempt"
ACT_ATTACK_ATTEMPT = "attack_attempt"
# A patch-verification replay. Deliberately a distinct action: replays are
# mostly blocked by design, so counting them as attacks would bias the
# headline success rate downward by construction.
ACT_PATCH_PROBE = "patch_probe"
ACT_PATCH_APPLIED = "patch_applied"
ACT_PATCH_VERIFY = "patch_verify"
ACT_PATCH_REVERTED = "patch_reverted"
ACT_BUDGET_CHECK = "budget_check"
ACT_BUDGET_HALT = "budget_halt"
ACT_RATE_LIMIT = "rate_limit"
ACT_MODEL_CALL = "model_call"
ACT_EVAL_RUN = "eval_run"
ACT_ROUND_START = "round_start"
ACT_ROUND_END = "round_end"
ACT_ANALYSIS_REPORT = "analysis_report"
ACT_SERVICE_START = "service_start"
ACT_ERROR = "error"
ACT_HALT = "halt"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def canonical_json(obj: Any) -> str:
    """Stable JSON for hashing: sorted keys, no incidental whitespace."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)


@dataclass
class LedgerEntry:
    """One immutable audit record."""

    actor: str
    action: str
    outcome: str = ""
    run_id: str = ""
    round_id: int | None = None
    cost_usd: float = 0.0
    prompt_version: str = ""
    schema_version: str = ""
    mode: str = ""
    idem_key: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    seq: int | None = None
    ts: str = ""
    prev_hash: str = ""
    entry_hash: str = ""

    def hash_core(self) -> dict[str, Any]:
        """The subset of fields covered by the hash chain."""
        return {
            "ts": self.ts,
            "run_id": self.run_id,
            "round_id": self.round_id,
            "actor": self.actor,
            "action": self.action,
            "outcome": self.outcome,
            "cost_usd": round(float(self.cost_usd), 10),
            "prompt_version": self.prompt_version,
            "schema_version": self.schema_version,
            "mode": self.mode,
            "idem_key": self.idem_key,
            "payload": self.payload,
        }

    def compute_hash(self, prev_hash: str) -> str:
        material = prev_hash + canonical_json(self.hash_core())
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    def to_row(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "ts": self.ts,
            "run_id": self.run_id,
            "round_id": self.round_id,
            "actor": self.actor,
            "action": self.action,
            "outcome": self.outcome,
            "cost_usd": self.cost_usd,
            "prompt_version": self.prompt_version,
            "schema_version": self.schema_version,
            "mode": self.mode,
            "idem_key": self.idem_key,
            "payload": self.payload,
            "prev_hash": self.prev_hash,
            "entry_hash": self.entry_hash,
        }


_SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger (
    seq             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT    NOT NULL,
    run_id          TEXT    NOT NULL DEFAULT '',
    round_id        INTEGER,
    actor           TEXT    NOT NULL,
    action          TEXT    NOT NULL,
    outcome         TEXT    NOT NULL DEFAULT '',
    cost_usd        REAL    NOT NULL DEFAULT 0.0,
    prompt_version  TEXT    NOT NULL DEFAULT '',
    schema_version  TEXT    NOT NULL DEFAULT '',
    mode            TEXT    NOT NULL DEFAULT '',
    idem_key        TEXT    UNIQUE,
    payload_json    TEXT    NOT NULL DEFAULT '{}',
    prev_hash       TEXT    NOT NULL,
    entry_hash      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ledger_action ON ledger(action);
CREATE INDEX IF NOT EXISTS idx_ledger_actor  ON ledger(actor);
CREATE INDEX IF NOT EXISTS idx_ledger_round  ON ledger(round_id);
CREATE INDEX IF NOT EXISTS idx_ledger_run    ON ledger(run_id);
CREATE INDEX IF NOT EXISTS idx_ledger_ts     ON ledger(ts);

-- Append-only enforcement at the storage layer. Conventions are not controls:
-- these triggers make UPDATE and DELETE fail even from a direct sqlite3 shell.
CREATE TRIGGER IF NOT EXISTS ledger_no_update
BEFORE UPDATE ON ledger
BEGIN
    SELECT RAISE(ABORT, 'ledger is append-only: UPDATE rejected');
END;
CREATE TRIGGER IF NOT EXISTS ledger_no_delete
BEFORE DELETE ON ledger
BEGIN
    SELECT RAISE(ABORT, 'ledger is append-only: DELETE rejected');
END;
"""


class Ledger:
    """Thread-safe, process-safe append-only ledger (SQLite backend)."""

    backend = "sqlite"

    def __init__(self, cfg: Config | None = None, db_path: Path | str | None = None,
                 jsonl_path: Path | str | None = None, run_id: str | None = None):
        self.cfg = cfg or get_config()
        self.db_path = Path(db_path) if db_path else self.cfg.ledger_db
        self.jsonl_path = Path(jsonl_path) if jsonl_path else self.cfg.ledger_jsonl
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.jsonl_path.parent.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id or os.environ.get("WARDEN_RUN_ID") or f"run-{uuid.uuid4().hex[:12]}"
        self._lock = threading.Lock()
        self._local = threading.local()
        self._init_db()

    # --- connection handling ---------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.db_path), timeout=30.0, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
        return conn

    def _init_db(self) -> None:
        conn = self._conn()
        with self._lock:
            conn.executescript(_SCHEMA)

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # --- append ----------------------------------------------------------------
    def append(self, entry: LedgerEntry) -> LedgerEntry:
        """Append one entry, chaining it to the current head.

        Returns the stored entry with seq, ts and hashes filled in. If the
        idem_key was already recorded, the existing entry is returned unchanged
        and nothing new is written.
        """
        if not entry.run_id:
            entry.run_id = self.run_id
        if not entry.mode:
            from warden.config import resolved_mode

            entry.mode = resolved_mode(self.cfg)
        if not entry.ts:
            entry.ts = utc_now_iso()

        conn = self._conn()
        with self._lock:
            for attempt in range(6):
                try:
                    conn.execute("BEGIN IMMEDIATE")
                    if entry.idem_key:
                        existing = conn.execute(
                            "SELECT * FROM ledger WHERE idem_key = ?", (entry.idem_key,)
                        ).fetchone()
                        if existing is not None:
                            conn.execute("COMMIT")
                            return _row_to_entry(existing)

                    head = conn.execute(
                        "SELECT entry_hash FROM ledger ORDER BY seq DESC LIMIT 1"
                    ).fetchone()
                    entry.prev_hash = head["entry_hash"] if head else GENESIS_HASH
                    entry.entry_hash = entry.compute_hash(entry.prev_hash)

                    cur = conn.execute(
                        "INSERT INTO ledger (ts, run_id, round_id, actor, action, outcome,"
                        " cost_usd, prompt_version, schema_version, mode, idem_key,"
                        " payload_json, prev_hash, entry_hash)"
                        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            entry.ts, entry.run_id, entry.round_id, entry.actor, entry.action,
                            entry.outcome, float(entry.cost_usd), entry.prompt_version,
                            entry.schema_version, entry.mode, entry.idem_key,
                            canonical_json(entry.payload), entry.prev_hash, entry.entry_hash,
                        ),
                    )
                    entry.seq = int(cur.lastrowid)
                    conn.execute("COMMIT")
                    break
                except sqlite3.IntegrityError:
                    # Lost an idem_key race with another process. The row that
                    # landed first is authoritative.
                    self._safe_rollback(conn)
                    if entry.idem_key:
                        existing = conn.execute(
                            "SELECT * FROM ledger WHERE idem_key = ?", (entry.idem_key,)
                        ).fetchone()
                        if existing is not None:
                            return _row_to_entry(existing)
                    raise
                except sqlite3.OperationalError:
                    # Busy database: roll back and back off before retrying.
                    self._safe_rollback(conn)
                    if attempt == 5:
                        raise
                    time.sleep(0.05 * (2**attempt))

        self._mirror_jsonl(entry)
        return entry

    @staticmethod
    def _safe_rollback(conn: sqlite3.Connection) -> None:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass

    def _mirror_jsonl(self, entry: LedgerEntry) -> None:
        """Best-effort second copy. A mirror failure must not lose the append."""
        try:
            with open(self.jsonl_path, "a", encoding="utf-8") as fh:
                fh.write(canonical_json(entry.to_row()) + "\n")
        except OSError:
            pass

    def log(self, actor: str, action: str, outcome: str = "", **kwargs: Any) -> LedgerEntry:
        """Convenience wrapper around append."""
        payload = kwargs.pop("payload", None) or {}
        return self.append(
            LedgerEntry(actor=actor, action=action, outcome=outcome, payload=payload, **kwargs)
        )

    # --- queries ---------------------------------------------------------------
    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return list(self._conn().execute(sql, tuple(params)).fetchall())

    def recent(self, limit: int = 50, actor: str | None = None,
               action: str | None = None) -> list[LedgerEntry]:
        sql = "SELECT * FROM ledger WHERE 1=1"
        params: list[Any] = []
        if actor:
            sql += " AND actor = ?"
            params.append(actor)
        if action:
            sql += " AND action = ?"
            params.append(action)
        sql += " ORDER BY seq DESC LIMIT ?"
        params.append(limit)
        return [_row_to_entry(r) for r in self.query(sql, params)]

    def entries_for_round(self, round_id: int) -> list[LedgerEntry]:
        rows = self.query("SELECT * FROM ledger WHERE round_id = ? ORDER BY seq", (round_id,))
        return [_row_to_entry(r) for r in rows]

    def total_spend(self, run_id: str | None = None) -> float:
        if run_id:
            rows = self.query(
                "SELECT COALESCE(SUM(cost_usd), 0.0) AS s FROM ledger WHERE run_id = ?", (run_id,)
            )
        else:
            rows = self.query("SELECT COALESCE(SUM(cost_usd), 0.0) AS s FROM ledger")
        return float(rows[0]["s"])

    def count(self, action: str | None = None) -> int:
        if action:
            rows = self.query("SELECT COUNT(*) AS c FROM ledger WHERE action = ?", (action,))
        else:
            rows = self.query("SELECT COUNT(*) AS c FROM ledger")
        return int(rows[0]["c"])

    def head(self) -> LedgerEntry | None:
        rows = self.query("SELECT * FROM ledger ORDER BY seq DESC LIMIT 1")
        return _row_to_entry(rows[0]) if rows else None

    # --- integrity -------------------------------------------------------------
    def verify_chain(self) -> dict[str, Any]:
        """Recompute the whole chain and return a verdict.

        ok is True only if every stored hash matches a recomputation from its
        predecessor. On failure, broken_at names the first bad sequence number.
        """
        prev = GENESIS_HASH
        checked = 0
        for row in self._conn().execute("SELECT * FROM ledger ORDER BY seq"):
            entry = _row_to_entry(row)
            if entry.prev_hash != prev:
                return {
                    "ok": False,
                    "checked": checked,
                    "broken_at": entry.seq,
                    "reason": "prev_hash does not match the preceding entry_hash",
                }
            if entry.compute_hash(prev) != entry.entry_hash:
                return {
                    "ok": False,
                    "checked": checked,
                    "broken_at": entry.seq,
                    "reason": "entry_hash does not match recomputation (content altered)",
                }
            prev = entry.entry_hash
            checked += 1
        return {"ok": True, "checked": checked, "broken_at": None, "head_hash": prev}


def _row_to_entry(row: sqlite3.Row) -> LedgerEntry:
    return LedgerEntry(
        seq=row["seq"],
        ts=row["ts"],
        run_id=row["run_id"],
        round_id=row["round_id"],
        actor=row["actor"],
        action=row["action"],
        outcome=row["outcome"],
        cost_usd=row["cost_usd"],
        prompt_version=row["prompt_version"],
        schema_version=row["schema_version"],
        mode=row["mode"],
        idem_key=row["idem_key"],
        payload=json.loads(row["payload_json"]),
        prev_hash=row["prev_hash"],
        entry_hash=row["entry_hash"],
    )


_default_ledger: Any = None
_default_lock = threading.Lock()


def get_ledger(cfg: Config | None = None):
    """Process-wide default ledger, on whichever backend is configured."""
    global _default_ledger
    from warden.config import resolved_store

    with _default_lock:
        if _default_ledger is None:
            if resolved_store() == "supabase":
                from warden.supabase_store import SupabaseLedger

                _default_ledger = SupabaseLedger(cfg)
            else:
                _default_ledger = Ledger(cfg)
        return _default_ledger
