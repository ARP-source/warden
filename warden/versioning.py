"""Versioned artifact store for anything the Defender is allowed to patch.

Two artifact kinds are versioned: the Target system prompt (``prompt``) and the
permission policy ruleset (``policy``). Nothing is ever overwritten. Each patch
writes a new immutable version file and moves a small pointer file, so the full
lineage of how the Target was hardened stays on disk and every ledger row can
name the exact versions in force when it was written.

Hot reload works by polling the pointer file: the Target service checks it on
each request, which is a stat call, and reloads only when the pointer changes.
"""
from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from warden.config import Config, get_config
from warden.ledger import canonical_json, utc_now_iso

KIND_PROMPT = "prompt"
KIND_POLICY = "policy"
_PREFIX = {KIND_PROMPT: "p", KIND_POLICY: "s"}


@dataclass
class Version:
    """One immutable revision of a patchable artifact."""

    kind: str
    version: str
    created_at: str
    content: Any
    parent: str | None = None
    author: str = "bootstrap"
    rationale: str = "initial version"
    content_hash: str = ""
    meta: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "version": self.version,
            "created_at": self.created_at,
            "parent": self.parent,
            "author": self.author,
            "rationale": self.rationale,
            "content_hash": self.content_hash,
            "meta": self.meta,
            "content": self.content,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> Version:
        return Version(
            kind=d["kind"], version=d["version"], created_at=d["created_at"],
            content=d["content"], parent=d.get("parent"), author=d.get("author", ""),
            rationale=d.get("rationale", ""), content_hash=d.get("content_hash", ""),
            meta=d.get("meta", {}),
        )


def content_hash(content: Any) -> str:
    return hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()[:16]


class VersionStore:
    """File-backed, append-only store of artifact revisions (local backend)."""

    backend = "files"

    def __init__(self, cfg: Config | None = None, root: Path | str | None = None):
        self.cfg = cfg or get_config()
        self.root = Path(root) if root else self.cfg.versions_dir
        self._lock = threading.Lock()
        for kind in (KIND_PROMPT, KIND_POLICY):
            (self.root / kind).mkdir(parents=True, exist_ok=True)

    # --- paths -----------------------------------------------------------------
    def _dir(self, kind: str) -> Path:
        return self.root / kind

    def _pointer(self, kind: str) -> Path:
        return self._dir(kind) / "ACTIVE"

    def _file(self, kind: str, version: str) -> Path:
        return self._dir(kind) / f"{version}.json"

    # --- reads -----------------------------------------------------------------
    def versions(self, kind: str) -> list[str]:
        """All version ids for a kind, in creation order."""
        out = []
        for p in self._dir(kind).glob("*.json"):
            out.append(p.stem)
        return sorted(out, key=lambda v: int(v[1:]) if v[1:].isdigit() else 0)

    def next_version_id(self, kind: str) -> str:
        existing = self.versions(kind)
        n = max((int(v[1:]) for v in existing if v[1:].isdigit()), default=0)
        return f"{_PREFIX[kind]}{n + 1}"

    def get(self, kind: str, version: str) -> Version:
        path = self._file(kind, version)
        if not path.exists():
            raise KeyError(f"no such {kind} version: {version}")
        return Version.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def active_id(self, kind: str) -> str | None:
        ptr = self._pointer(kind)
        if not ptr.exists():
            return None
        return ptr.read_text(encoding="utf-8").strip() or None

    def active(self, kind: str) -> Version:
        vid = self.active_id(kind)
        if vid is None:
            raise KeyError(f"no active {kind} version; store not initialised")
        return self.get(kind, vid)

    def pointer_mtime(self, kind: str) -> float:
        ptr = self._pointer(kind)
        return ptr.stat().st_mtime if ptr.exists() else 0.0

    def history(self, kind: str) -> list[Version]:
        return [self.get(kind, v) for v in self.versions(kind)]

    # --- writes ----------------------------------------------------------------
    def commit(self, kind: str, content: Any, *, author: str, rationale: str,
               activate: bool = True, meta: dict[str, Any] | None = None) -> Version:
        """Write a new immutable version and optionally make it active."""
        with self._lock:
            parent = self.active_id(kind)
            vid = self.next_version_id(kind)
            ver = Version(
                kind=kind, version=vid, created_at=utc_now_iso(), content=content,
                parent=parent, author=author, rationale=rationale,
                content_hash=content_hash(content), meta=meta or {},
            )
            path = self._file(kind, vid)
            if path.exists():
                raise RuntimeError(f"refusing to overwrite existing version file {path}")
            path.write_text(json.dumps(ver.to_dict(), indent=2), encoding="utf-8")
            if activate:
                self._pointer(kind).write_text(vid, encoding="utf-8")
            return ver

    def activate(self, kind: str, version: str) -> Version:
        """Point at an existing version. Used for rollback after a bad patch."""
        ver = self.get(kind, version)
        with self._lock:
            self._pointer(kind).write_text(version, encoding="utf-8")
        return ver

    def ensure_initialised(self, kind: str, default_content: Any, *,
                           rationale: str = "initial version") -> Version:
        """Create v1 from a default if the store is empty. Idempotent."""
        if self.active_id(kind) is not None:
            return self.active(kind)
        return self.commit(kind, default_content, author="bootstrap", rationale=rationale)


_default_store: Any = None
_store_lock = threading.Lock()


def get_store(cfg: Config | None = None):
    """Process-wide version store, on whichever backend is configured."""
    global _default_store
    from warden.config import resolved_store

    with _store_lock:
        if _default_store is None:
            if resolved_store() == "supabase":
                from warden.supabase_store import SupabaseVersionStore

                _default_store = SupabaseVersionStore(cfg)
            else:
                _default_store = VersionStore(cfg)
        return _default_store
