"""OpenAI-compatible provider, so the loop can run on free or sponsor inference.

The project needs real models but has no paid-API budget, and almost every
inference host speaks the OpenAI chat-completions shape. One adapter therefore
unlocks W&B Inference (whose credits come with the same key Weave already
needs), CoreWeave, Groq, Together, vLLM and a local Ollama, without changing
anything above this file.

The only real work here is translation. The rest of Warden speaks the Anthropic
content-block shape - tool_use blocks on the assistant turn and tool_result
blocks on the following user turn - because that is what the Target agent loop
was built around. This module converts in both directions:

    Anthropic shape                OpenAI shape
    ---------------                ------------
    tools[].input_schema     ->    tools[].function.parameters
    assistant [tool_use]     ->    assistant.tool_calls[]
    user [tool_result]       ->    role="tool" messages, one per call
    usage.input_tokens       <-    usage.prompt_tokens

Tool calling is mandatory for the Target: a model without it never produces a
tool call, so every attack would read as a refusal and the whole experiment
would measure nothing. Pick a tool-calling model; the config defaults do.
"""
from __future__ import annotations

import json
from typing import Any

import httpx

from warden.llm import LLMResponse, PermanentLLMError, ToolCall, TransientLLMError


def to_openai_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    if not tools:
        return None
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
            },
        }
        for t in tools
    ]


def to_openai_messages(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten Anthropic-shaped turns into the OpenAI chat format."""
    out: list[dict[str, Any]] = []
    if system:
        out.append({"role": "system", "content": system})

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if isinstance(content, str):
            out.append({"role": role, "content": content})
            continue

        if role == "assistant":
            text_parts: list[str] = []
            tool_calls: list[dict[str, Any]] = []
            for block in content or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    tool_calls.append({
                        "id": block.get("id") or f"call_{len(tool_calls)}",
                        "type": "function",
                        "function": {
                            "name": block.get("name", ""),
                            "arguments": json.dumps(block.get("input") or {}),
                        },
                    })
            turn: dict[str, Any] = {
                "role": "assistant",
                "content": "\n".join(p for p in text_parts if p) or None,
            }
            if tool_calls:
                turn["tool_calls"] = tool_calls
            out.append(turn)
            continue

        # A user turn carrying tool results becomes one tool message per result.
        results = [b for b in (content or [])
                   if isinstance(b, dict) and b.get("type") == "tool_result"]
        if results:
            for block in results:
                body = block.get("content")
                out.append({
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id") or "call_0",
                    "content": body if isinstance(body, str) else json.dumps(body),
                })
            continue

        text = " ".join(b.get("text", "") for b in (content or [])
                        if isinstance(b, dict) and b.get("type") == "text")
        out.append({"role": role, "content": text or str(content)})
    return out


class OpenAICompatProvider:
    """Chat-completions client for any OpenAI-compatible inference endpoint."""

    name = "openai"

    def __init__(self, base_url: str, api_key: str | None = None, timeout: float = 90.0):
        if not base_url:
            raise PermanentLLMError(
                "no OpenAI-compatible base URL configured; set WARDEN_OPENAI_BASE_URL "
                "(W&B Inference: https://api.inference.wandb.ai/v1)"
            )
        self.base_url = base_url.rstrip("/")
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout,
                                    headers=headers)

    def close(self) -> None:
        self._client.close()

    def complete(self, *, model: str, system: str, messages: list[dict[str, Any]],
                 tools: list[dict[str, Any]] | None, max_tokens: int,
                 temperature: float, role: str) -> LLMResponse:
        body: dict[str, Any] = {
            "model": model,
            "messages": to_openai_messages(system, messages),
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        converted = to_openai_tools(tools)
        if converted:
            body["tools"] = converted
            body["tool_choice"] = "auto"

        try:
            resp = self._client.post("/chat/completions", json=body)
        except httpx.TransportError as exc:
            raise TransientLLMError(f"transport: {exc}") from exc

        if resp.status_code == 429:
            raise TransientLLMError(f"429 rate limited: {resp.text[:200]}")
        if resp.status_code >= 500:
            raise TransientLLMError(f"{resp.status_code}: {resp.text[:200]}")
        if resp.status_code >= 400:
            raise PermanentLLMError(f"{resp.status_code}: {resp.text[:400]}")

        try:
            data = resp.json()
        except ValueError as exc:
            raise TransientLLMError(f"non-JSON response: {resp.text[:200]}") from exc

        choices = data.get("choices") or []
        if not choices:
            raise TransientLLMError(f"no choices in response: {str(data)[:200]}")
        message = choices[0].get("message") or {}
        text = message.get("content") or ""

        calls: list[ToolCall] = []
        blocks: list[dict[str, Any]] = []
        if text:
            blocks.append({"type": "text", "text": text})
        for tc in message.get("tool_calls") or []:
            fn = tc.get("function") or {}
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except (json.JSONDecodeError, TypeError):
                # A model that emits malformed arguments has still *attempted*
                # the call, and the attempt is what is being measured, so it is
                # recorded rather than dropped.
                args = {"_unparsed_arguments": str(raw_args)[:500]}
            call_id = tc.get("id") or f"call_{len(calls)}"
            calls.append(ToolCall(name=fn.get("name", ""), params=args, call_id=call_id))
            blocks.append({"type": "tool_use", "id": call_id,
                           "name": fn.get("name", ""), "input": args})

        usage = data.get("usage") or {}
        return LLMResponse(
            text=text.strip(),
            tool_calls=calls,
            content_blocks=blocks,
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            stop_reason=str(choices[0].get("finish_reason") or ""),
            model=model,
            provider=self.name,
        )

    def probe(self) -> dict[str, Any]:
        """Cheap reachability check for the doctor command."""
        try:
            resp = self._client.get("/models")
            if resp.status_code >= 400:
                return {"ok": False, "status": resp.status_code,
                        "detail": resp.text[:200]}
            data = resp.json()
            ids = [m.get("id") for m in (data.get("data") or [])][:8]
            return {"ok": True, "base_url": self.base_url, "sample_models": ids}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"[:200]}
