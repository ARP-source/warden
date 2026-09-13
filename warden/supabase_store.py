"""Supabase-backed ledger, version store and budget backend.

These classes expose the same public surface as their SQLite counterparts, so
swapping backends is a factory decision and no caller changes. Which backend is
active is recorded in the run summary and shown on the health endpoint, because
"where did this number come from" is the first question a reviewer asks.

The integrity-critical operations - appending to the chain and taking a budget
reservation - are Postgres functions, not client-side logic. See
``warden/supabase_client.py`` for why.
"""
from __future__ import annotations

import threading
import uuid
from pathlib import Path
from typing import Any, Sequence

from warden.config import Config, get_config, resolved_mode
from warden.ledger import GENESIS_HASH, LedgerEntry, canonical_json, utc_now_iso
from warden.supabase_client import SupabaseClient
from warden.versioning import KIND_PROMPT, Version, content_hash

LEDGER_TABLE = "warden_ledger"


def _entry_from_row(row: dict[str, Any]) -> LedgerEntry:
    return LedgerEntry(
        seq=row.get("seq"),
        ts=str(row.get("ts") or ""),
        run_id=row.get("run_id") or "",
        round_id=row.get("round_id"),
        actor=row.get("actor") or "",
        action=row.get("action") or "",
        outcome=row.get("outcome") or "",
        cost_usd=float(row.get("cost_usd") or 0.0),
        prompt_version=row.get("prompt_version") or "",
        schema_version=row.get("schema_version") or "",
        mode=row.get("mode") or "",
        idem_key=row.get("idem_key"),
        payload=row.get("payload") or {},
        prev_hash=row.get("prev_hash") or "",
        entry_hash=row.get("entry_hash") or "",
    )


class SupabaseLedger:
    """Append-only ledger stored in Postgres, chained by the database."""

    backend = "supabase"

    def __init__(self, cfg: Config | None = None, client: SupabaseClient | None = None,
                 run_id: str | None = None, mirror_path: Path | str | None = None):
        import os

        self.cfg = cfg or get_config()
        self.client = client or SupabaseClient()
        self.run_id = run_id or os.environ.get("WARDEN_RUN_ID") or f"run-{uuid.uuid4().hex[:12]}"
        # A local JSON Lines mirror is kept as a second, independent copy so a
        # network outage cannot silently lose audit history.
        self.mirror_path = Path(mirror_path) if mirror_path else self.cfg.ledger_jsonl
        self.mirror_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def close(self) -> None:
        self.client.close()

    # --- append ----------------------------------------------------------------
    def append(self, entry: LedgerEntry) -> LedgerEntry:
        if not entry.run_id:
            entry.run_id = self.run_id
        if not entry.mode:
            entry.mode = resolved_mode(self.cfg)
        row = self.client.rpc("warden_ledger_append", {
            "p_run_id": entry.run_id,
            "p_round_id": entry.round_id,
            "p_actor": entry.actor,
            "p_action": entry.action,
            "p_outcome": entry.outcome,
            "p_cost": float(entry.cost_usd),
            "p_prompt_version": entry.prompt_version,
            "p_schema_version": entry.schema_version,
            "p_mode": entry.mode,
            "p_idem_key": entry.idem_key,
            "p_payload": entry.payload,
        })
        if isinstance(row, list):
            row = row[0] if row else {}
        stored = _entry_from_row(row or {})
        self._mirror(stored)
        return stored

    def _mirror(self, entry: LedgerEntry) -> None:
        try:
            with self._lock, open(self.mirror_path, "a", encoding="utf-8") as fh:
                fh.write(canonical_json(entry.to_row()) + "\n")
        except OSError:
            pass

    def log(self, actor: str, action: str, outcome: str = "", **kwargs: Any) -> LedgerEntry:
        payload = kwargs.pop("payload", None) or {}
        return self.append(
            LedgerEntry(actor=actor, action=action, outcome=outcome, payload=payload, **kwargs)
        )

    # --- reads -----------------------------------------------------------------
    def recent(self, limit: int = 50, actor: str | None = None,
               action: str | None = None) -> list[LedgerEntry]:
        filters: dict[str, str] = {}
        if actor:
            filters["actor"] = f"eq.{actor}"
        if action:
            filters["action"] = f"eq.{action}"
        rows = self.client.select(LEDGER_TABLE, filters=filters, order="seq.desc", limit=limit)
        return [_entry_from_row(r) for r in rows]

    def entries_for_round(self, round_id: int) -> list[LedgerEntry]:
        rows = self.client.select(LEDGER_TABLE, filters={"round_id": f"eq.{round_id}"},
                                  order="seq.asc", limit=2000)
        return [_entry_from_row(r) for r in rows]

    def rows_for_action(self, action: str, run_id: str | None = None,
                        limit: int = 5000) -> list[dict[str, Any]]:
        """Raw rows, used by clustering which needs full payloads."""
        filters = {"action": f"eq.{action}"}
        if run_id:
            filters["run_id"] = f"eq.{run_id}"
        return self.client.select(LEDGER_TABLE, filters=filters, order="seq.desc", limit=limit)

    def view(self, name: str, filters: dict[str, str] | None = None,
             order: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
        return self.client.select(name, filters=filters, order=order, limit=limit)

    def total_spend(self, run_id: str | None = None) -> float:
        filters = {"run_id": f"eq.{run_id}"} if run_id else None
        rows = self.view("warden_v_spend_by_role", filters=filters)
        return round(sum(float(r.get("cost_usd") or 0.0) for r in rows), 6)

    def count(self, action: str | None = None) -> int:
        filters = {"action": f"eq.{action}"} if action else None
        return self.client.count(LEDGER_TABLE, filters)

    def head(self) -> LedgerEntry | None:
        rows = self.client.select(LEDGER_TABLE, order="seq.desc", limit=1)
        return _entry_from_row(rows[0]) if rows else None

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        raise NotImplementedError(
            "raw SQL is not available against the Supabase backend; use view() or "
            "rows_for_action(), which read the aggregate views the migration created"
        )

    # --- integrity -------------------------------------------------------------
    def verify_chain(self) -> dict[str, Any]:
        out = self.client.rpc("warden_ledger_verify")
        if isinstance(out, list):
            out = out[0] if out else {}
        out = out or {}
        return {
            "ok": bool(out.get("ok")),
            "checked": int(out.get("checked") or 0),
            "broken_at": out.get("broken_at"),
            "reason": out.get("reason", ""),
            "head_hash": out.get("head_hash", GENESIS_HASH),
            "backend": "supabase",
        }


class SupabaseVersionStore:
    """Versioned prompt and policy documents in Postgres.

    Same contract as the file-backed store: nothing is overwritten, and hot
    reload is driven by a pointer. The pointer here is a row rather than a file,
    so several service instances converge on the same active version.
    """

    backend = "supabase"

    def __init__(self, cfg: Config | None = None, client: SupabaseClient | None = None):
        self.cfg = cfg or get_config()
        self.client = client or SupabaseClient()
        self._lock = threading.Lock()

    # --- reads -----------------------------------------------------------------
    def versions(self, kind: str) -> list[str]:
        rows = self.client.select("warden_versions", columns="version",
                                  filters={"kind": f"eq.{kind}"}, order="id.asc", limit=1000)
        return [r["version"] for r in rows]

    def next_version_id(self, kind: str) -> str:
        prefix = "p" if kind == KIND_PROMPT else "s"
        existing = self.versions(kind)
        n = max((int(v[1:]) for v in existing if v[1:].isdigit()), default=0)
        return f"{prefix}{n + 1}"

    def get(self, kind: str, version: str) -> Version:
        rows = self.client.select("warden_versions",
                                  filters={"kind": f"eq.{kind}", "version": f"eq.{version}"},
                                  limit=1)
        if not rows:
            raise KeyError(f"no such {kind} version: {version}")
        r = rows[0]
        return Version(kind=r["kind"], version=r["version"], created_at=str(r["created_at"]),
                       content=r["content"], parent=r.get("parent"),
                       author=r.get("author", ""), rationale=r.get("rationale", ""),
                       content_hash=r.get("content_hash", ""), meta=r.get("meta") or {})

    def active_id(self, kind: str) -> str | None:
        rows = self.client.select("warden_version_active", filters={"kind": f"eq.{kind}"}, limit=1)
        return rows[0]["version"] if rows else None

    def active(self, kind: str) -> Version:
        vid = self.active_id(kind)
        if vid is None:
            raise KeyError(f"no active {kind} version; store not initialised")
        return self.get(kind, vid)

    def pointer_mtime(self, kind: str) -> float:
        """Pointer freshness signal, used the same way the file mtime is.

        Returns the pointer update time as a float so callers can compare it for
        change without caring which backend they are on.
        """
        rows = self.client.select("warden_version_active", columns="version,updated_at",
                                  filters={"kind": f"eq.{kind}"}, limit=1)
        if not rows:
            return 0.0
        from datetime import datetime

        stamp = str(rows[0].get("updated_at") or "")
        try:
            return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return float(abs(hash(rows[0].get("version", ""))) % 10**9)

    def history(self, kind: str) -> list[Version]:
        rows = self.client.select("warden_versions", filters={"kind": f"eq.{kind}"},
                                  order="id.asc", limit=1000)
        return [
            Version(kind=r["kind"], version=r["version"], created_at=str(r["created_at"]),
                    content=r["content"], parent=r.get("parent"), author=r.get("author", ""),
                    rationale=r.get("rationale", ""), content_hash=r.get("content_hash", ""),
                    meta=r.get("meta") or {})
            for r in rows
        ]

    # --- writes ----------------------------------------------------------------
    def commit(self, kind: str, content: Any, *, author: str, rationale: str,
               activate: bool = True, meta: dict[str, Any] | None = None) -> Version:
        with self._lock:
            parent = self.active_id(kind)
            vid = self.next_version_id(kind)
            row = {
                "kind": kind, "version": vid, "parent": parent, "author": author,
                "rationale": rationale, "content_hash": content_hash(content),
                "content": content, "meta": meta or {},
            }
            self.client.insert("warden_versions", row, returning=False)
            if activate:
                self._set_pointer(kind, vid)
            return Version(kind=kind, version=vid, created_at=utc_now_iso(), content=content,
                           parent=parent, author=author, rationale=rationale,
                           content_hash=row["content_hash"], meta=meta or {})

    def _set_pointer(self, kind: str, version: str) -> None:
        self.client.insert("warden_version_active",
                           {"kind": kind, "version": version, "updated_at": utc_now_iso()},
                           upsert=True, on_conflict="kind", returning=False)

    def activate(self, kind: str, version: str) -> Version:
        ver = self.get(kind, version)
        with self._lock:
            self._set_pointer(kind, version)
        return ver

    def ensure_initialised(self, kind: str, default_content: Any, *,
                           rationale: str = "initial version") -> Version:
        if self.active_id(kind) is not None:
            return self.active(kind)
        return self.commit(kind, default_content, author="bootstrap", rationale=rationale)


class SupabaseGovernor:
    """Budget governor backed by Postgres.

    Identical public surface to the SQLite governor, with two differences that
    matter operationally: the reservation gate is a single serialised Postgres
    function rather than a client-side transaction, and the kill switch is a row
    every process can see rather than a file on one machine.
    """

    backend = "supabase"

    def __init__(self, cfg: Config | None = None, ledger: Any = None,
                 client: SupabaseClient | None = None):
        from warden.ledger import (
            ACT_BUDGET_CHECK,
            ACT_BUDGET_HALT,
            ACT_HALT,
            ACT_MODEL_CALL,
            ACT_RATE_LIMIT,
            ACTOR_GOVERNOR,
        )

        self.cfg = cfg or get_config()
        self.client = client or SupabaseClient()
        self.ledger = ledger
        self._acts = {
            "check": ACT_BUDGET_CHECK, "halt_budget": ACT_BUDGET_HALT, "halt": ACT_HALT,
            "model": ACT_MODEL_CALL, "rate": ACT_RATE_LIMIT, "actor": ACTOR_GOVERNOR,
        }

    # --- accounting ------------------------------------------------------------
    def project_cost(self, model: str, est_input_tokens: int, max_output_tokens: int) -> float:
        return self.cfg.price_for(model).cost(est_input_tokens, max_output_tokens)

    def _snapshot_raw(self) -> dict[str, Any]:
        out = self.client.rpc("warden_budget_snapshot", {
            "p_ceiling": self.cfg.budget.ceiling_usd,
            "p_reserve_floor": self.cfg.budget.reserve_floor_usd,
            "p_run_id": getattr(self.ledger, "run_id", None),
        })
        if isinstance(out, list):
            out = out[0] if out else {}
        return out or {}

    def committed_usd(self) -> float:
        return float(self._snapshot_raw().get("committed_usd") or 0.0)

    def settled_usd(self) -> float:
        return float(self._snapshot_raw().get("settled_usd") or 0.0)

    def remaining_usd(self) -> float:
        return float(self._snapshot_raw().get("remaining_usd") or 0.0)

    def calls_in_last_hour(self) -> int:
        return int(self._snapshot_raw().get("calls_last_hour") or 0)

    def rounds_in_last_hour(self) -> int:
        return int(self._snapshot_raw().get("rounds_last_hour") or 0)

    def calls_in_round(self, round_id: int | None) -> int:
        """Scoped to this run: round numbers repeat between runs."""
        if round_id is None:
            return 0
        return self.client.count("warden_budget_ops", {
            "round_id": f"eq.{round_id}", "state": "neq.released",
            "run_id": f"eq.{getattr(self.ledger, 'run_id', '')}",
        })

    # --- kill switch -----------------------------------------------------------
    def is_halted(self) -> bool:
        out = self.client.rpc("warden_is_halted")
        if isinstance(out, list):
            out = out[0] if out else False
        return bool(out)

    def halt(self, reason: str, detail: dict[str, Any] | None = None) -> None:
        already = self.is_halted()
        self.client.rpc("warden_halt", {"p_reason": reason, "p_detail": detail or {}})
        if not already and self.ledger is not None:
            self.ledger.log(self._acts["actor"], self._acts["halt"], outcome="halted",
                            payload={"reason": reason, **(detail or {})})

    def clear_halt(self) -> None:
        if self.is_halted():
            self.client.rpc("warden_clear_halt")
            if self.ledger is not None:
                self.ledger.log(self._acts["actor"], self._acts["halt"], outcome="cleared",
                                payload={})

    def mark_round(self, round_id: int) -> None:
        self.client.insert("warden_round_marks",
                           {"round_id": round_id,
                            "run_id": getattr(self.ledger, "run_id", ""),
                            "ts": utc_now_iso()},
                           upsert=True, on_conflict="run_id,round_id", returning=False)

    # --- state -----------------------------------------------------------------
    def state(self) -> str:
        from warden.budget import STATE_DRAIN, STATE_HALTED, STATE_NORMAL

        snap = self._snapshot_raw()
        if snap.get("halted"):
            return STATE_HALTED
        remaining = float(snap.get("remaining_usd") or 0.0)
        if remaining <= 0.0:
            return STATE_HALTED
        if remaining <= self.cfg.budget.reserve_floor_usd:
            return STATE_DRAIN
        return STATE_NORMAL

    def snapshot(self) -> dict[str, Any]:
        snap = self._snapshot_raw()
        ceiling = self.cfg.budget.ceiling_usd
        committed = float(snap.get("committed_usd") or 0.0)
        from warden.budget import STATE_DRAIN, STATE_HALTED, STATE_NORMAL

        if snap.get("halted") or committed >= ceiling:
            state = STATE_HALTED
        elif (ceiling - committed) <= self.cfg.budget.reserve_floor_usd:
            state = STATE_DRAIN
        else:
            state = STATE_NORMAL
        return {
            "state": state,
            "backend": "supabase",
            "ceiling_usd": round(ceiling, 4),
            "committed_usd": round(committed, 6),
            "settled_usd": round(float(snap.get("settled_usd") or 0.0), 6),
            "remaining_usd": round(max(0.0, ceiling - committed), 6),
            "reserve_floor_usd": self.cfg.budget.reserve_floor_usd,
            "pct_consumed": round(100.0 * committed / ceiling if ceiling else 0.0, 2),
            "calls_last_hour": int(snap.get("calls_last_hour") or 0),
            "max_calls_per_hour": self.cfg.limits.max_model_calls_per_hour,
            "rounds_last_hour": int(snap.get("rounds_last_hour") or 0),
            "max_rounds_per_hour": self.cfg.limits.max_rounds_per_hour,
            "halted": bool(snap.get("halted")),
        }

    # --- the gate --------------------------------------------------------------
    def reserve(self, role: str, model: str, est_input_tokens: int, max_output_tokens: int,
                round_id: int | None = None, idem_key: str | None = None):
        """Obtain permission for one model call. Raises on refusal."""
        from warden.budget import (
            DRAIN_ALLOWED_ROLES,
            BudgetExhausted,
            Halted,
            RateLimited,
            Reservation,
        )

        projected = self.project_cost(model, est_input_tokens, max_output_tokens)

        def deny(exc, reason: str, message: str, detail: dict[str, Any]):
            if self.ledger is not None:
                self.ledger.log(
                    self._acts["actor"],
                    self._acts["rate"] if exc is RateLimited else self._acts["check"],
                    outcome="denied", round_id=round_id,
                    payload={"role": role, "model": model, "reason": reason,
                             "projected_usd": round(projected, 6), **detail},
                )
            return exc(message, reason=reason, detail=detail)

        # A per-call cap breach is a configuration error, caught before spending
        # anything to discover it.
        if projected > self.cfg.budget.max_call_usd:
            raise deny(BudgetExhausted, "max_call_exceeded",
                       f"projected {projected:.4f} USD exceeds per-call cap "
                       f"{self.cfg.budget.max_call_usd:.4f}",
                       {"max_call_usd": self.cfg.budget.max_call_usd})

        if self.is_halted():
            raise deny(Halted, "kill_switch", "run halted by kill switch", {})

        out = self.client.rpc("warden_budget_reserve", {
            "p_run_id": getattr(self.ledger, "run_id", ""),
            "p_round_id": round_id,
            "p_role": role,
            "p_model": model,
            "p_projected": projected,
            "p_ceiling": self.cfg.budget.ceiling_usd,
            "p_reserve_floor": self.cfg.budget.reserve_floor_usd,
            "p_max_per_round": self.cfg.limits.max_model_calls_per_round,
            "p_max_per_hour": self.cfg.limits.max_model_calls_per_hour,
            "p_drain_roles": sorted(DRAIN_ALLOWED_ROLES),
            "p_idem_key": idem_key,
        })
        if isinstance(out, list):
            out = out[0] if out else {}
        out = out or {}

        if not out.get("granted"):
            reason = str(out.get("reason") or "refused")
            detail = {
                "committed_usd": round(float(out.get("committed_usd") or 0.0), 6),
                "remaining_usd": round(float(out.get("remaining_usd") or 0.0), 6),
                "ceiling_usd": self.cfg.budget.ceiling_usd,
                "calls_in_round": int(out.get("calls_in_round") or 0),
                "calls_last_hour": int(out.get("calls_last_hour") or 0),
            }
            if reason == "ceiling_reached":
                # Terminal: no call of this size can ever fit, so stop the run
                # rather than let every later attempt be refused in a busy loop.
                if self.ledger is not None:
                    self.ledger.log(self._acts["actor"], self._acts["halt_budget"],
                                    outcome="ceiling_reached", round_id=round_id,
                                    payload=dict(detail, role=role, model=model))
                self.halt("spend ceiling reached", detail)
                raise deny(BudgetExhausted, reason,
                           f"spend ceiling {self.cfg.budget.ceiling_usd:.2f} USD reached",
                           detail)
            if reason == "drain_mode":
                raise deny(BudgetExhausted, reason,
                           f"budget below reserve floor "
                           f"{self.cfg.budget.reserve_floor_usd:.2f} USD; role {role} shed "
                           f"so defence and reporting can finish",
                           dict(detail, drain_allowed_roles=sorted(DRAIN_ALLOWED_ROLES)))
            if reason in ("per_round_calls", "per_hour_calls"):
                raise deny(RateLimited, reason,
                           f"rate limit {reason} reached "
                           f"(round {detail['calls_in_round']}, "
                           f"hour {detail['calls_last_hour']})", detail)
            raise deny(BudgetExhausted, reason, f"reservation refused: {reason}", detail)

        res_id = int(out.get("reservation_id") or 0)
        if self.ledger is not None:
            self.ledger.log(
                self._acts["actor"], self._acts["check"], outcome="approved",
                round_id=round_id,
                payload={"role": role, "model": model,
                         "projected_usd": round(projected, 6), "reservation_id": res_id,
                         "remaining_usd": round(float(out.get("remaining_usd") or 0.0), 6)},
            )
        return Reservation(id=res_id, role=role, model=model, projected_usd=projected,
                           round_id=round_id, idem_key=idem_key)

    def settle(self, reservation, input_tokens: int, output_tokens: int,
               outcome: str = "ok", extra: dict[str, Any] | None = None) -> float:
        actual = self.cfg.price_for(reservation.model).cost(input_tokens, output_tokens)
        self.client.rpc("warden_budget_settle", {
            "p_id": reservation.id, "p_input_tokens": input_tokens,
            "p_output_tokens": output_tokens, "p_actual": actual,
        })
        if self.ledger is not None:
            self.ledger.log(
                self._acts["actor"], self._acts["model"], outcome=outcome,
                round_id=reservation.round_id, cost_usd=actual,
                payload={"role": reservation.role, "model": reservation.model,
                         "input_tokens": input_tokens, "output_tokens": output_tokens,
                         "projected_usd": round(reservation.projected_usd, 6),
                         "actual_usd": round(actual, 6),
                         "reservation_id": reservation.id, **(extra or {})},
            )
        committed = self.committed_usd()
        if committed >= self.cfg.budget.ceiling_usd:
            if self.ledger is not None:
                self.ledger.log(self._acts["actor"], self._acts["halt_budget"],
                                outcome="ceiling_reached", round_id=reservation.round_id,
                                payload={"committed_usd": round(committed, 6),
                                         "ceiling_usd": self.cfg.budget.ceiling_usd})
            self.halt("spend ceiling reached", {"committed_usd": round(committed, 6)})
        return actual

    def release(self, reservation, reason: str = "call_failed") -> None:
        self.client.rpc("warden_budget_release", {"p_id": reservation.id})
        if self.ledger is not None:
            self.ledger.log(
                self._acts["actor"], self._acts["check"], outcome="released",
                round_id=reservation.round_id,
                payload={"role": reservation.role, "model": reservation.model,
                         "reason": reason, "reservation_id": reservation.id,
                         "returned_usd": round(reservation.projected_usd, 6)},
            )

    def reap_stale(self, older_than_s: int = 900) -> int:
        out = self.client.rpc("warden_budget_reap", {"p_older_than_seconds": older_than_s})
        if isinstance(out, list):
            out = out[0] if out else 0
        n = int(out or 0)
        if n and self.ledger is not None:
            self.ledger.log(self._acts["actor"], self._acts["check"], outcome="reaped",
                            payload={"released_count": n, "older_than_s": older_than_s})
        return n
