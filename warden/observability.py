"""Weave instrumentation, with a no-op fallback that never blocks a run.

Weave is where a reviewer watches the loop live. It is also an external service
that can be unavailable, unauthenticated or simply not installed, and none of
those is a reason to stop red-teaming. So every call here is wrapped: if Weave is
missing the decorators become pass-throughs, and the ledger remains the system of
record either way.

Trace shape
-----------
The requirement is that a round reads as one connected chain rather than scattered
events, so spans are nested to mirror causality:

    round(N)
      attack(category, attack_id)
        target_turn(step)
          tool_attempt(tool, authorized, allowed)
      defend(attack_id)
        defender_proposal(attempt)
        patch_verify(held)
        benign_suite(score)
      analysis(round)

Every span carries the prompt and policy version in force, so a trace can be read
against the exact Target that produced it.
"""
from __future__ import annotations

import functools
import os
import threading
from contextlib import contextmanager
from typing import Any, Callable, Iterator

_state = threading.local()
_init_lock = threading.Lock()
_initialised = False
_weave: Any = None
_enabled = False
_status: dict[str, Any] = {"enabled": False, "reason": "not initialised", "project": ""}


def init(project: str | None = None, force: bool = False) -> dict[str, Any]:
    """Initialise Weave once per process. Safe to call from anywhere."""
    global _initialised, _weave, _enabled, _status
    with _init_lock:
        if _initialised and not force:
            return _status
        _initialised = True

        from warden.config import get_config

        cfg = get_config()
        project = project or cfg.weave.project

        if not cfg.weave.enabled:
            _status = {"enabled": False, "reason": "disabled in config", "project": project}
            return _status
        if not os.environ.get("WANDB_API_KEY"):
            _status = {
                "enabled": False,
                "reason": "WANDB_API_KEY is not set; tracing is off and the ledger "
                          "remains the system of record",
                "project": project,
            }
            return _status
        try:
            import weave  # type: ignore

            entity = os.environ.get("WANDB_ENTITY")
            target = f"{entity}/{project}" if entity else project
            weave.init(target)
            _weave = weave
            _enabled = True
            _status = {"enabled": True, "reason": "ok", "project": target}
        except Exception as exc:
            _status = {
                "enabled": False,
                "reason": f"weave unavailable: {type(exc).__name__}: {exc}"[:200],
                "project": project,
            }
        return _status


def status() -> dict[str, Any]:
    return dict(_status)


def enabled() -> bool:
    return _enabled


def _stack() -> list[Any]:
    if not hasattr(_state, "stack"):
        _state.stack = []
    return _state.stack


def current_attributes() -> dict[str, Any]:
    """Attributes inherited by any span opened right now."""
    merged: dict[str, Any] = {}
    for frame in _stack():
        merged.update(frame)
    return merged


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[dict[str, Any]]:
    """Open a nested trace span. A no-op when Weave is unavailable.

    Attributes accumulate down the stack, so a tool_attempt span carries the
    round, attack and version context without each caller repeating it.
    """
    frame = {k: v for k, v in attributes.items() if v is not None}
    _stack().append(frame)
    ctx: Any = None
    try:
        if _enabled and _weave is not None:
            try:
                ctx = _weave.attributes({"warden_span": name, **current_attributes()})
                ctx.__enter__()
            except Exception:
                ctx = None
        yield frame
    finally:
        if ctx is not None:
            try:
                ctx.__exit__(None, None, None)
            except Exception:
                pass
        try:
            _stack().pop()
        except IndexError:
            pass


def op(name: str | None = None) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Mark a function as a traced operation.

    Applied at import time, before Weave is initialised, so the wrapper decides
    per call rather than binding a decorator that may not exist yet.
    """
    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        label = name or getattr(fn, "__qualname__", getattr(fn, "__name__", "op"))
        traced: dict[str, Any] = {}

        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            if not _enabled or _weave is None:
                return fn(*args, **kwargs)
            target = traced.get("fn")
            if target is None:
                try:
                    target = _weave.op(name=label)(fn)
                except Exception:
                    target = fn
                traced["fn"] = target
            try:
                return target(*args, **kwargs)
            except Exception:
                raise

        return wrapper

    return decorate


def log_event(name: str, payload: dict[str, Any]) -> None:
    """Record a standalone event. Silent when Weave is off."""
    if not _enabled or _weave is None:
        return
    try:
        with _weave.attributes({"warden_event": name, **current_attributes()}):
            pass
    except Exception:
        pass


def summary_for_health() -> dict[str, Any]:
    """What the health endpoint reports about tracing."""
    st = status()
    return {
        "weave_enabled": st.get("enabled", False),
        "weave_project": st.get("project", ""),
        "weave_note": st.get("reason", ""),
    }
