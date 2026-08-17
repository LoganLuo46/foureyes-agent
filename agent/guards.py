"""Layer 1 defense (content_layer): separate data from instructions, flag suspicious patterns.

The principle (spec §8) is to detect and **flag**, never to silently delete: attack signatures are
evidence, and the audit log and the red-team report both need them. The one physical edit we make is
stripping zero-width/invisible characters (concealment is their only purpose, and leaving them in
would mislead downstream). Even that strip is recorded in injection_flags.
"""

import base64
import re
import unicodedata

# Instruction-like patterns, one (flag name, regex) per entry. All case-insensitive.
_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("direct_override", re.compile(
        r"(ignore|disregard|forget)\s+(all\s+|any\s+)?(previous|prior|above|earlier)\s+"
        r"(instructions?|rules?|prompts?|policy|policies)", re.I)),
    # role marker + colon: counts at line start, after a newline, or **mid-paragraph** (right after
    # sentence punctuation). Attacks like to hide "SYSTEM:" behind an innocent question, which is how
    # the red-team demo_structural_gate case slipped past an earlier version of this regex.
    ("fake_system_message", re.compile(
        r"(?:(?:^|\n|[.!?]\s*)\s*\[?\s*(?:system|assistant|developer)\s*\]?\s*[:>]"
        r"|<\s*/?\s*(?:system|instructions?)\s*>)", re.I)),
    ("role_hijack", re.compile(
        r"(you\s+are\s+(now|no\s+longer)|act\s+as\s+(an?\s+)?(admin|administrator|root|developer)|"
        r"(admin|god|developer|debug|maintenance)\s+mode)", re.I)),
    # Tolerates adverbs between has/was/is and approved ("has already been granted"), and also
    # catches phrasings that never say "approval" at all, like "no human review is required"
    ("fake_approval_claim", re.compile(
        r"((approval|refund|request|ticket|this)\s+(id\s*[:#]?\s*\S+\s+)?"
        r"(has|was|is|have|been)\s+(\w+\s+){0,3}(pre[- ]?)?(approved|authorized|granted)"
        r"|(approval|review|sign[- ]?off)\s+(is\s+)?not\s+(required|needed|necessary)"
        r"|skip\s+(the\s+)?(human\s+)?(approval|review)"
        r"|(no|without)\s+(\w+\s+){0,2}(approval|review|sign[- ]?off)\s+(is\s+)?"
        r"(required|needed|necessary)"
        r"|(pre[- ]?)(approved|authorized))", re.I)),
    ("urgency_pressure", re.compile(
        r"(immediately\s+or\s+(we|i)\s+will|legal\s+action|lawsuit|attorney|"
        r"ceo\s+(said|approved|demands)|within\s+the\s+hour|right\s+now\s+or)", re.I)),
    ("tool_direction", re.compile(
        r"(call|invoke|use|execute)\s+(the\s+)?(issue_refund|escalate_ticket|close_ticket|tool)", re.I)),
    ("prompt_extraction", re.compile(
        r"(reveal|show|print|repeat)\s+(your\s+)?(system\s+prompt|instructions|rules)", re.I)),
]

_ZERO_WIDTH = re.compile(r"[​‌‍⁠﻿­]")
_LONG_WHITESPACE = re.compile(r"[ \t]{80,}|\n{12,}")
_BASE64_BLOB = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{60,}={0,2}(?![A-Za-z0-9+/=])")


def _decodes_to_text(blob: str) -> bool:
    try:
        raw = base64.b64decode(blob + "=" * (-len(blob) % 4), validate=False)
        text = raw.decode("utf-8", errors="strict")
        printable = sum(c.isprintable() or c.isspace() for c in text)
        return len(text) > 8 and printable / len(text) > 0.9
    except Exception:
        return False


def sanitize(body: str) -> tuple[str, list[str]]:
    """Returns (sanitized_body, injection_flags). Nothing but zero-width characters is altered."""
    flags: list[str] = []

    stripped = _ZERO_WIDTH.sub("", body)
    if stripped != body:
        flags.append("zero_width_chars_stripped")

    # Unicode homoglyphs: non-ASCII letters smuggled into an ASCII word (e.g. Cyrillic 'a' among Latin)
    for word in re.findall(r"\w{3,}", stripped):
        scripts = {unicodedata.name(c, "?").split()[0] for c in word if c.isalpha()}
        if "LATIN" in scripts and ("CYRILLIC" in scripts or "GREEK" in scripts):
            flags.append("mixed_script_homoglyph")
            break

    if _LONG_WHITESPACE.search(stripped):
        flags.append("suspicious_whitespace_padding")

    for m in _BASE64_BLOB.finditer(stripped):
        if _decodes_to_text(m.group(0)):
            flags.append("base64_encoded_text")
            break

    for name, pat in _PATTERNS:
        if pat.search(stripped):
            flags.append(name)

    return stripped, flags


UNTRUSTED_OPEN = "<<<UNTRUSTED_CUSTOMER_TEXT>>>"
UNTRUSTED_CLOSE = "<<<END_UNTRUSTED_CUSTOMER_TEXT>>>"


def wrap_untrusted(text: str) -> str:
    """Wrap the customer's text in boundary markers. Used by the classify prompt."""
    # If the body contains the boundary markers themselves (an escape attempt), break them first
    text = text.replace(UNTRUSTED_OPEN, "<<UNTRUSTED-MARKER-REMOVED>>")
    text = text.replace(UNTRUSTED_CLOSE, "<<UNTRUSTED-MARKER-REMOVED>>")
    return f"{UNTRUSTED_OPEN}\n{text}\n{UNTRUSTED_CLOSE}"
