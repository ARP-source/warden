"""Thin Supabase HTTP client: PostgREST reads, RPC for anything transactional.

Why RPC rather than plain inserts for the ledger and the budget gate: both need
serialisation that a REST insert cannot express. The hash chain has to read the
current head under a lock, and the budget gate has to check the ceiling and the
rate limits and insert the reservation as one atomic step. Both live in Postgres
functions, so correctness does not depend on how well the client behaves or on
how many processes happen to be running.

Reads go through PostgREST, including the aggregate views, so the database does
the aggregation rather than shipping every row to the dashboard.
"""
from __future__ import annotations

import os
import random
import time
from typing import Any

import httpx


class SupabaseError(RuntimeError):
    """A Supabase call failed in a way retrying will not fix."""


class SupabaseUnavailable(RuntimeError):
    """A transport or 5xx failure; worth retrying."""


def supabase_configured() -> bool:
    return bool(os.environ.get("SUPABASE_URL")
                and os.environ.get("SUPABASE_SERVICE_ROLE_KEY"))


class SupabaseClient:
    """Minimal PostgREST + RPC client with retry and backoff."""

    def __init__(self, url: str | None = None, service_key: str | None = None,
                 timeout: float = 30.0, max_attempts: int = 4):
        self.url = (url or os.environ.get("SUPABASE_URL") or "").rstrip("/")
        self.key = service_key or os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or ""
        if not self.url or not self.key:
            raise SupabaseError(
                "SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must both be set; "
                "see .env.example"
            )
        self.max_attempts = max_attempts
        self._client = httpx.Client(
            base_url=f"{self.url}/rest/v1",
            timeout=timeout,
            headers={
                "apikey": self.key,
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )

    def close(self) -> None:
        self._client.close()

    # --- transport -------------------------------------------------------------
    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        delay = 0.4
        last: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                resp = self._client.request(method, path, **kwargs)
                if resp.status_code >= 500:
                    raise SupabaseUnavailable(f"{resp.status_code}: {resp.text[:300]}")
                if resp.status_code >= 400:
                    raise SupabaseError(
                        f"{method} {path} -> {resp.status_code}: {resp.text[:400]}"
                    )
                if not resp.content or resp.status_code == 204:
                    return None
                return resp.json()
            except (SupabaseUnavailable, httpx.TransportError, httpx.HTTPError) as exc:
                last = exc
                if isinstance(exc, SupabaseError):
                    raise
                if attempt == self.max_attempts:
                    break
                time.sleep(random.uniform(0.0, delay))
                delay = min(delay * 2, 6.0)
        raise SupabaseUnavailable(
            f"supabase unreachable after {self.max_attempts} attempts: {last}"
        )

    # --- operations ------------------------------------------------------------
    def rpc(self, fn: str, args: dict[str, Any] | None = None) -> Any:
        return self._request("POST", f"/rpc/{fn}", json=args or {})

    def select(self, table: str, *, columns: str = "*", filters: dict[str, str] | None = None,
               order: str | None = None, limit: int | None = None,
               offset: int | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"select": columns}
        for key, value in (filters or {}).items():
            params[key] = value
        if order:
            params["order"] = order
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        out = self._request("GET", f"/{table}", params=params)
        return out or []

    def insert(self, table: str, rows: list[dict[str, Any]] | dict[str, Any], *,
               upsert: bool = False, on_conflict: str | None = None,
               returning: bool = True) -> list[dict[str, Any]]:
        headers = {"Prefer": ", ".join(filter(None, [
            "return=representation" if returning else "return=minimal",
            "resolution=merge-duplicates" if upsert else None,
        ]))}
        params = {"on_conflict": on_conflict} if on_conflict else None
        out = self._request("POST", f"/{table}", json=rows, headers=headers, params=params)
        return out or []

    def update(self, table: str, patch: dict[str, Any],
               filters: dict[str, str]) -> list[dict[str, Any]]:
        out = self._request("PATCH", f"/{table}", json=patch, params=dict(filters),
                            headers={"Prefer": "return=representation"})
        return out or []

    def count(self, table: str, filters: dict[str, str] | None = None) -> int:
        params: dict[str, Any] = {"select": "*"}
        for key, value in (filters or {}).items():
            params[key] = value
        resp = self._client.request("HEAD", f"/{table}", params=params,
                                    headers={"Prefer": "count=exact"})
        rng = resp.headers.get("content-range", "")
        if "/" in rng:
            tail = rng.split("/")[-1]
            if tail.isdigit():
                return int(tail)
        return 0

    def health(self) -> dict[str, Any]:
        try:
            self.select("warden_customers", columns="customer_id", limit=1)
            return {"ok": True, "url": self.url}
        except Exception as exc:
            return {"ok": False, "url": self.url, "error": str(exc)[:200]}
