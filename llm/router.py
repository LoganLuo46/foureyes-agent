"""Multi-provider LLM routing: Claude (primary) → Gemini (fallback). ADR-006.

Falls back only on availability errors (rate limit / overload / 5xx / timeout / connection / safety refusal);
content errors (400 and friends) are raised as-is. Every switch is logged as a Langfuse `provider-fallback` event.
Both providers share the same JSON schema for structured output.
"""

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any

import anthropic

from llm.tracing import event, observation

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.6-flash")
LLM_EFFORT = os.environ.get("LLM_EFFORT", "low")
LLM_TIMEOUT_S = float(os.environ.get("LLM_TIMEOUT_SECONDS", "30"))


class RouterContentError(RuntimeError):
    """Content/request-level error: no fallback, surface it (that's a bug, not an availability problem)."""


class AllProvidersFailed(RuntimeError):
    pass


@dataclass
class LLMResult:
    provider: str          # "claude" | "gemini"
    text: str
    parsed: Any = None     # parsed output, present when a json_schema was given
    usage: dict = field(default_factory=dict)
    fallback: bool = False
    fallback_reason: str | None = None
    latency_ms: int = 0


_anthropic_client: anthropic.Anthropic | None = None
_gemini_client = None


def _claude() -> anthropic.Anthropic:
    global _anthropic_client
    if _anthropic_client is None:
        # max_retries=0: the router owns every retry/fallback decision, so switches stay legible in the trace
        _anthropic_client = anthropic.Anthropic(timeout=LLM_TIMEOUT_S, max_retries=0)
    return _anthropic_client


def _gemini():
    global _gemini_client
    if _gemini_client is None:
        from google import genai
        from google.genai import types as gt
        _gemini_client = genai.Client(
            api_key=os.environ.get("GOOGLE_API_KEY"),
            http_options=gt.HttpOptions(timeout=int(LLM_TIMEOUT_S * 1000)),
        )
    return _gemini_client


def _call_claude(system: str, user: str, json_schema: dict | None,
                 max_tokens: int, timeout_s: float | None) -> LLMResult:
    kwargs: dict[str, Any] = dict(
        model=CLAUDE_MODEL,
        max_tokens=max_tokens,  # note: Opus 5 thinking is on by default, and thinking+answer share this cap
        system=system,
        messages=[{"role": "user", "content": user}],
        output_config={"effort": LLM_EFFORT},
    )
    if json_schema is not None:
        kwargs["output_config"]["format"] = {"type": "json_schema", "schema": json_schema}
    client = _claude() if timeout_s is None else _claude().with_options(timeout=timeout_s)
    t0 = time.monotonic()
    resp = client.messages.create(**kwargs)
    latency = int((time.monotonic() - t0) * 1000)
    if resp.stop_reason == "refusal":
        # Safety-classifier refusal: for us this is the primary provider refusing service → let the caller fall back
        detail = getattr(resp.stop_details, "category", None) if resp.stop_details else None
        raise ProviderRefusal(f"claude refusal (category={detail})")
    if resp.stop_reason == "max_tokens":
        raise RouterContentError(
            f"claude hit max_tokens={max_tokens} (thinking shares the cap); raise max_tokens")
    text = next((b.text for b in resp.content if b.type == "text"), "")
    usage = {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}
    return LLMResult(provider="claude", text=text,
                     parsed=json.loads(text) if json_schema else None,
                     usage=usage, latency_ms=latency)


def _call_gemini(system: str, user: str, json_schema: dict | None, max_tokens: int) -> LLMResult:
    from google.genai import types as gt
    cfg: dict[str, Any] = dict(system_instruction=system, max_output_tokens=max_tokens)
    if json_schema is not None:
        cfg["response_mime_type"] = "application/json"
        cfg["response_json_schema"] = json_schema
    t0 = time.monotonic()
    resp = _gemini().models.generate_content(
        model=GEMINI_MODEL, contents=user, config=gt.GenerateContentConfig(**cfg))
    latency = int((time.monotonic() - t0) * 1000)
    text = resp.text or ""
    um = resp.usage_metadata
    usage = {"input_tokens": getattr(um, "prompt_token_count", None),
             "output_tokens": getattr(um, "candidates_token_count", None)}
    return LLMResult(provider="gemini", text=text,
                     parsed=json.loads(text) if json_schema else None,
                     usage=usage, latency_ms=latency)


class ProviderRefusal(RuntimeError):
    pass


# Fallback-worthy = an availability problem; anthropic.InternalServerError covers 5xx and 529 overloaded
_FALLBACK_ERRORS = (
    anthropic.RateLimitError,
    anthropic.InternalServerError,
    anthropic.APITimeoutError,
    anthropic.APIConnectionError,
    ProviderRefusal,
)


def call_llm(system: str, user: str, json_schema: dict | None = None,
             max_tokens: int = 8192, trace_name: str = "llm-call",
             trace_id: str | None = None, _claude_timeout_s: float | None = None) -> LLMResult:
    """Claude first, Gemini second. `_claude_timeout_s` only lets demos/tests force a real timeout fallback."""
    with observation(trace_name, as_type="generation", trace_id=trace_id,
                     input={"system": system[:2000], "user": user[:4000]},
                     metadata={"primary": CLAUDE_MODEL, "effort": LLM_EFFORT}) as obs:
        try:
            result = _call_claude(system, user, json_schema, max_tokens, _claude_timeout_s)
        except _FALLBACK_ERRORS as e:
            reason = f"{type(e).__name__}: {e}"
            event("provider-fallback", trace_id=trace_id,
                  metadata={"from": CLAUDE_MODEL, "to": GEMINI_MODEL, "reason": reason})
            try:
                result = _call_gemini(system, user, json_schema, max_tokens)
            except Exception as ge:
                raise AllProvidersFailed(
                    f"claude failed ({reason}); gemini failed ({type(ge).__name__}: {ge})") from ge
            result.fallback = True
            result.fallback_reason = reason
        except anthropic.BadRequestError as e:
            raise RouterContentError(f"claude 400 (bug, not availability): {e}") from e
        if obs:
            obs.update(output=result.text[:4000],
                       metadata={"provider": result.provider, "fallback": result.fallback,
                                 "fallback_reason": result.fallback_reason,
                                 "latency_ms": result.latency_ms, **result.usage})
        return result
