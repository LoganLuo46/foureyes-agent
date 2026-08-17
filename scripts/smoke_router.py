"""Router smoke: 1) Claude primary path (structured output); 2) forced real timeout → Gemini fallback.

2) uses _claude_timeout_s=0.001 to provoke a **real** APITimeoutError, not a mock, so the
   error classification, the fallback event, and Gemini's structured output all line up.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv  # noqa: E402

load_dotenv(ROOT / ".env")

from llm.router import call_llm  # noqa: E402
from llm.tracing import flush, trace_url  # noqa: E402

SCHEMA = {
    "type": "object",
    "properties": {
        "sentiment": {"type": "string", "enum": ["positive", "negative", "neutral"]},
        "word_count": {"type": "integer"},
    },
    "required": ["sentiment", "word_count"],
    "additionalProperties": False,
}
SYSTEM = "You classify short texts. Respond only with the JSON requested."
USER = "Text: 'The device arrived broken and support ignored me for a week.'"

print("=== path 1: claude primary ===")
r1 = call_llm(SYSTEM, USER, json_schema=SCHEMA, trace_name="smoke-claude")
print(f"provider={r1.provider} fallback={r1.fallback} latency={r1.latency_ms}ms parsed={r1.parsed}")
assert r1.provider == "claude" and not r1.fallback
assert r1.parsed["sentiment"] == "negative"

print("=== path 2: forced real timeout -> gemini fallback ===")
r2 = call_llm(SYSTEM, USER, json_schema=SCHEMA, trace_name="smoke-fallback",
              _claude_timeout_s=0.001)
print(f"provider={r2.provider} fallback={r2.fallback} reason={r2.fallback_reason!r} "
      f"latency={r2.latency_ms}ms parsed={r2.parsed}")
assert r2.provider == "gemini" and r2.fallback
assert "APITimeoutError" in r2.fallback_reason
assert r2.parsed["sentiment"] == "negative"

flush()
print("trace url (any):", trace_url())
print("SMOKE PASS: llm router (claude primary + real-timeout gemini fallback)")
