"""Model-call layer: one gate every model call passes through.

Every call is wrapped so that the budget governor approves it first and settles
it afterwards with real token counts. There is no code path that reaches a
provider without a reservation, which is what makes the spend ceiling a control
rather than a hope.

Two providers implement the same interface:

* ``AnthropicProvider`` - real API calls, with retry and backoff on transient
  failures.
* ``SimulatedProvider`` - deterministic, seeded test doubles (see
  ``warden/simulator.py``). Costs nothing and lets the whole loop run with no
  credentials.

Which one is in force is recorded on every ledger row as ``mode``, so simulated
results can never be mistaken for live ones.
"""
from __future__ import annotations

import os
import random
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from warden.budget import BudgetGovernor, GovernorStop, get_governor
from warden.config import Config, get_config, resolved_mode
from warden.ledger import ACT_ERROR, Ledger, get_ledger
from warden import observability as obs

# Rough characters-per-token ratio, used only for the pre-flight projection.
# Settlement always uses the real counts the provider reports.
CHARS_PER_TOKEN = 3.6


@dataclass
class ToolCall:
    """A tool invocation the model asked for."""

    name: str
    params: dict[str, Any]
    call_id: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"name": self.name, "params": self.params, "call_id": self.call_id}


@dataclass
class LLMResponse:
    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str = ""
    model: str = ""
    provider: str = ""
    attempts: int = 1
    # Provider-native assistant content, replayed verbatim on the next turn so a
    # tool-use conversation continues in the shape the API expects.
    content_blocks: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


class TransientLLMError(RuntimeError):
    """A failure worth retrying: rate limit, overload, connection reset."""


class PermanentLLMError(RuntimeError):
    """A failure retrying cannot fix: bad request, auth, missing model."""


class Provider(Protocol):
    name: str

    def complete(self, *, model: str, system: str, messages: list[dict[str, Any]],
                 tools: list[dict[str, Any]] | None, max_tokens: int,
                 temperature: float, role: str) -> LLMResponse:
        ...


def estimate_input_tokens(system: str, messages: list[dict[str, Any]],
                          tools: list[dict[str, Any]] | None) -> int:
    """Cheap upper-ish estimate for the pre-flight reservation."""
    chars = len(system or "")
    for m in messages:
        content = m.get("content", "")
        chars += len(content) if isinstance(content, str) else len(str(content))
    if tools:
        chars += sum(len(str(t)) for t in tools)
    # Round up and add headroom so the projection is not an under-count.
    return int(chars / CHARS_PER_TOKEN) + 64


class AnthropicProvider:
    """Real Anthropic API calls."""

    name = "anthropic"

    def __init__(self, api_key: str | None = None):
        try:
            import anthropic  # noqa: F401
        except ImportError as exc:  # pragma: no cover - depends on install
            raise PermanentLLMError(
                "the anthropic package is not installed; install with the 'llm' extra "
                "or run in simulated mode"
            ) from exc
        import anthropic

        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise PermanentLLMError("ANTHROPIC_API_KEY is not set")
        self._client = anthropic.Anthropic(api_key=key, max_retries=0, timeout=60.0)
        self._sdk = anthropic

    def complete(self, *, model: str, system: str, messages: list[dict[str, Any]],
                 tools: list[dict[str, Any]] | None, max_tokens: int,
                 temperature: float, role: str) -> LLMResponse:
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,
            "system": system,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = tools
        try:
            resp = self._client.messages.create(**kwargs)
        except Exception as exc:
            raise self._classify(exc) from exc

        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in resp.content:
            btype = getattr(block, "type", "")
            if btype == "text":
                text_parts.append(getattr(block, "text", ""))
            elif btype == "tool_use":
                calls.append(
                    ToolCall(
                        name=getattr(block, "name", ""),
                        params=dict(getattr(block, "input", {}) or {}),
                        call_id=getattr(block, "id", ""),
                    )
                )
        blocks: list[dict[str, Any]] = []
        for block in resp.content:
            btype = getattr(block, "type", "")
            if btype == "text":
                blocks.append({"type": "text", "text": getattr(block, "text", "")})
            elif btype == "tool_use":
                blocks.append({
                    "type": "tool_use",
                    "id": getattr(block, "id", ""),
                    "name": getattr(block, "name", ""),
                    "input": dict(getattr(block, "input", {}) or {}),
                })
        usage = getattr(resp, "usage", None)
        return LLMResponse(
            content_blocks=blocks,
            text="\n".join(p for p in text_parts if p).strip(),
            tool_calls=calls,
            input_tokens=int(getattr(usage, "input_tokens", 0) or 0),
            output_tokens=int(getattr(usage, "output_tokens", 0) or 0),
            stop_reason=str(getattr(resp, "stop_reason", "") or ""),
            model=model,
            provider=self.name,
        )

    def _classify(self, exc: Exception) -> Exception:
        sdk = self._sdk
        transient = tuple(
            t for t in (
                getattr(sdk, "RateLimitError", None),
                getattr(sdk, "APIConnectionError", None),
                getattr(sdk, "InternalServerError", None),
                getattr(sdk, "APITimeoutError", None),
            ) if t is not None
        )
        if transient and isinstance(exc, transient):
            return TransientLLMError(f"{type(exc).__name__}: {exc}")
        status = getattr(exc, "status_code", None)
        if status is not None and int(status) >= 500:
            return TransientLLMError(f"HTTP {status}: {exc}")
        if status == 429:
            return TransientLLMError(f"HTTP 429: {exc}")
        return PermanentLLMError(f"{type(exc).__name__}: {exc}")


def build_provider(cfg: Config | None = None, provider: str | None = None) -> Provider:
    """Pick a provider, degrading to simulation rather than failing.

    A configured provider that cannot be constructed - missing package, missing
    key, unreachable host - falls back to the doubles with a warning. An
    overnight run that dies at 2am over a missing dependency produces nothing;
    one that keeps running in a clearly labelled degraded mode still produces
    evidence, and the label travels on every ledger row.
    """
    from warden.config import resolved_provider

    cfg = cfg or get_config()
    choice = provider or resolved_provider(cfg)
    try:
        if choice == "anthropic":
            return AnthropicProvider()
        if choice == "openai":
            from warden.openai_provider import OpenAICompatProvider

            return OpenAICompatProvider(cfg.inference.base_url, cfg.inference.api_key())
    except Exception as exc:
        import warnings

        warnings.warn(
            f"inference provider {choice!r} unavailable ({exc}); "
            f"falling back to simulated test doubles",
            RuntimeWarning, stacklevel=2,
        )
    from warden.simulator import SimulatedProvider

    return SimulatedProvider(seed=cfg.seed)


class ModelClient:
    """The only supported way to call a model.

    Wraps provider calls in: budget reservation, retry with backoff, settlement
    with real token counts, and ledger logging. A governor refusal propagates to
    the caller unchanged so the orchestrator can wind down; a provider failure
    releases the reservation so a dead call does not hold budget.
    """

    def __init__(self, cfg: Config | None = None, ledger: Ledger | None = None,
                 governor: BudgetGovernor | None = None, provider: Provider | None = None):
        self.cfg = cfg or get_config()
        self.ledger = ledger or get_ledger(self.cfg)
        self.governor = governor or get_governor(self.cfg, self.ledger)
        from warden.config import resolved_provider

        self.mode = resolved_mode(self.cfg)
        self.provider_name = resolved_provider(self.cfg)
        self.provider = provider or build_provider(self.cfg, self.provider_name)
        # The provider that was actually constructed is authoritative. A silent
        # fallback must never leave the ledger claiming a live run.
        actual = getattr(self.provider, "name", self.provider_name)
        self.provider_name = actual
        if actual == "simulated":
            self.mode = "simulated"

    @obs.op("warden.model_call")
    def call(self, role: str, *, system: str = "", messages: list[dict[str, Any]] | None = None,
             tools: list[dict[str, Any]] | None = None, max_tokens: int | None = None,
             temperature: float = 1.0, round_id: int | None = None,
             idem_key: str | None = None, max_attempts: int = 4,
             context: dict[str, Any] | None = None,
             model_override: str | None = None) -> LLMResponse:
        messages = messages or []
        # An override lets one process benchmark several models without a
        # restart. Pricing still comes from the table, so an unknown model is
        # costed pessimistically rather than free.
        model = model_override or self.cfg.model_for(role, self.provider_name)
        max_out = max_tokens or self.cfg.target.max_output_tokens
        est_in = estimate_input_tokens(system, messages, tools)

        # Gate first. A GovernorStop is not an error to swallow: it is the
        # system deciding to stop, and it travels to the orchestrator intact.
        reservation = self.governor.reserve(
            role=role, model=model, est_input_tokens=est_in, max_output_tokens=max_out,
            round_id=round_id, idem_key=idem_key,
        )

        delay = 0.75
        last_exc: Exception | None = None
        made = 0
        for attempt in range(1, max_attempts + 1):
            made = attempt
            try:
                with obs.span("model_call", role=role, model=model, attempt=attempt,
                              round_id=round_id, mode=self.mode):
                    resp = self.provider.complete(
                        model=model, system=system, messages=messages, tools=tools,
                        max_tokens=max_out, temperature=temperature, role=role,
                    )
                resp.attempts = attempt
                self.governor.settle(
                    reservation, resp.input_tokens, resp.output_tokens, outcome="ok",
                    extra={"attempts": attempt, "provider": resp.provider,
                           "stop_reason": resp.stop_reason, **(context or {})},
                )
                return resp
            except TransientLLMError as exc:
                last_exc = exc
                self.ledger.log(
                    "governor", ACT_ERROR, outcome="transient",
                    round_id=round_id,
                    payload={"role": role, "model": model, "attempt": attempt,
                             "error": str(exc)[:400]},
                )
                if attempt == max_attempts:
                    break
                # Full jitter backoff: avoids a thundering herd on retry.
                time.sleep(random.uniform(0.0, delay))
                delay = min(delay * 2, 12.0)
            except PermanentLLMError as exc:
                last_exc = exc
                break
            except Exception as exc:  # unexpected provider bug
                last_exc = exc
                break

        self.governor.release(reservation, reason="model_call_failed")
        self.ledger.log(
            "governor", ACT_ERROR, outcome="model_call_failed", round_id=round_id,
            payload={"role": role, "model": model, "attempts": made,
                     "max_attempts": max_attempts, "error": str(last_exc)[:400]},
        )
        raise PermanentLLMError(
            f"model call for role {role} on {model} failed after {made} attempt(s): "
            f"{last_exc}"
        )


_default_client: ModelClient | None = None


def get_client(cfg: Config | None = None) -> ModelClient:
    global _default_client
    if _default_client is None:
        _default_client = ModelClient(cfg)
    return _default_client


__all__ = [
    "ToolCall", "LLMResponse", "ModelClient", "Provider", "AnthropicProvider",
    "TransientLLMError", "PermanentLLMError", "GovernorStop", "build_provider",
    "get_client", "estimate_input_tokens",
]
