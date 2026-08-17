"""Thin Langfuse wrapper: degrades to a no-op whenever keys are missing or init fails (ADR-006).

Usage:
    from llm.tracing import observation, flush, trace_url, ticket_trace_id
    with observation("classify", as_type="generation", input=...) as obs:
        ...
        if obs: obs.update(output=...)
"""

import os
from contextlib import contextmanager

_client = None
_disabled = False


def get_tracer():
    global _client, _disabled
    if _disabled:
        return None
    if _client is None:
        if not (os.environ.get("LANGFUSE_PUBLIC_KEY") and os.environ.get("LANGFUSE_SECRET_KEY")):
            _disabled = True
            return None
        try:
            from langfuse import Langfuse
            client = Langfuse()
            # Invalid key: degrade to no-op immediately, or the background exporter thread spams 401s
            if not client.auth_check():
                raise RuntimeError("langfuse auth_check failed")
            _client = client
        except Exception as e:
            import sys
            print(f"[tracing] Langfuse unavailable, running without traces: "
                  f"{type(e).__name__}: {str(e)[:120]}", file=sys.stderr)
            _disabled = True
            return None
    return _client


def ticket_trace_id(ticket_id: str) -> str | None:
    """All spans for one ticket land on one trace (trace_id is derived deterministically from ticket_id)."""
    lf = get_tracer()
    if lf is None:
        return None
    return lf.create_trace_id(seed=f"foureyes-ticket-{ticket_id}")


@contextmanager
def observation(name: str, as_type: str = "span", trace_id: str | None = None, **kwargs):
    lf = get_tracer()
    if lf is None:
        yield None
        return
    ctx = {"trace_id": trace_id} if trace_id else None
    with lf.start_as_current_observation(name=name, as_type=as_type, trace_context=ctx, **kwargs) as obs:
        yield obs


def event(name: str, trace_id: str | None = None, **kwargs) -> None:
    lf = get_tracer()
    if lf is None:
        return
    ctx = {"trace_id": trace_id} if trace_id else None
    lf.create_event(name=name, trace_context=ctx, **kwargs)


def trace_url(trace_id: str | None = None) -> str | None:
    lf = get_tracer()
    if lf is None:
        return None
    try:
        return lf.get_trace_url(trace_id=trace_id)
    except Exception:
        return None


def flush() -> None:
    lf = get_tracer()
    if lf is not None:
        lf.flush()
