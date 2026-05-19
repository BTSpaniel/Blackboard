"""Secret-aware preview sanitizer.

Used by the tool ledger, audit log, and any path that records provider/tool I/O so we never
write resolved API keys, tokens, or other obvious secrets to disk.
"""
from __future__ import annotations

import re
from typing import Any, Dict

# Conservative regex set — match shapes, not specific vendors.
_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),                     # OpenAI-style
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}"),                 # Anthropic-style
    re.compile(r"fw_[A-Za-z0-9_\-]{16,}"),                     # Fireworks-style
    re.compile(r"AKIA[0-9A-Z]{16}"),                           # AWS access key id
    re.compile(r"ghp_[A-Za-z0-9]{30,}"),                       # GitHub PAT
    re.compile(r"Bearer\s+[A-Za-z0-9_\-\.]{20,}", re.IGNORECASE),
    re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    re.compile(r"\b\d{4}[- ]\d{4}[- ]\d{4}[- ]\d{4}\b"),
    re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
    re.compile(r"(?:\+?\d{1,2}[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?)\d{3}[\s.-]?\d{4}\b"),
    re.compile(
        r"\b\d{1,5}\s+[A-Za-z0-9.'-]+(?:\s+[A-Za-z0-9.'-]+){0,5}\s"
        r"(?:street|st|avenue|ave|road|rd|drive|dr|lane|ln|court|ct|boulevard|blvd|way|place|pl)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:dob|date of birth|birthday is)\b[^.\n]{0,80}", re.IGNORECASE),
    re.compile(
        r"(?<!\[)\b(?:discord|instagram|insta|twitter|x|telegram|snapchat|snap|tiktok)\s*"
        r"(?:is|:|handle|username)?\s*@?[A-Za-z0-9_.-]{3,}",
        re.IGNORECASE,
    ),
    re.compile(r"\b[A-Za-z0-9_\-]{32,}\b"),                    # long opaque tokens (last)
]

_FIELD_BLOCKLIST = {"api_key", "apikey", "authorization", "token", "secret", "password", "safe_word", "step_up_code", "step_up_secret"}
_FIELD_BLOCKLIST_PARTS = ("api_key", "apikey", "authorization", "token", "secret", "password", "safe_word", "step_up")
_INJECTION_MARKERS = [
    re.compile(r"ignore previous instructions", re.IGNORECASE),
    re.compile(r"system\s*:\s*you are now", re.IGNORECASE),
    re.compile(r"<\|system\|>|<\|user\|>|<\|assistant\|>", re.IGNORECASE),
    re.compile(r"jailbreak|DAN mode|do anything now", re.IGNORECASE),
]
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WHITESPACE_RUNS = re.compile(r"\s+")


def sanitize_text(text: str, *, max_chars: int = 4000) -> str:
    if not text:
        return ""
    out = str(text)
    for pattern in _PATTERNS:
        out = pattern.sub("[REDACTED]", out)
    if len(out) > max_chars:
        out = out[:max_chars] + f"... [truncated, {len(out) - max_chars} chars]"
    return out


def sanitize_inline_text(text: str, *, max_chars: int = 400) -> str:
    value = sanitize_text(str(text or ""), max_chars=max_chars)
    if not value:
        return ""
    value = _CONTROL_CHARS.sub(" ", value)
    value = value.replace("\r", " ").replace("\n", " ").replace("\t", " ")
    value = _WHITESPACE_RUNS.sub(" ", value)
    return value.strip()


def sanitize_mapping(payload: Dict[str, Any], *, max_chars: int = 4000) -> Dict[str, Any]:
    if not isinstance(payload, dict):
        return {}
    out: Dict[str, Any] = {}
    for key, value in payload.items():
        kl = str(key).lower()
        if kl in _FIELD_BLOCKLIST or any(part in kl for part in _FIELD_BLOCKLIST_PARTS):
            out[key] = "[REDACTED]"
            continue
        if isinstance(value, dict):
            out[key] = sanitize_mapping(value, max_chars=max_chars)
        elif isinstance(value, list):
            out[key] = [sanitize_mapping(v, max_chars=max_chars) if isinstance(v, dict) else sanitize_text(str(v), max_chars=max_chars) for v in value]
        elif isinstance(value, str):
            out[key] = sanitize_inline_text(value, max_chars=max_chars)
        else:
            out[key] = value
    return out


def guard_untrusted_text(text: str, *, max_chars: int = 4000) -> str:
    value = sanitize_text(str(text or ""), max_chars=max_chars)
    if not value:
        return ""
    matched = any(pattern.search(value) for pattern in _INJECTION_MARKERS)
    if not matched:
        return value
    cleaned = value
    for pattern in _INJECTION_MARKERS:
        cleaned = pattern.sub("[INJECTION BLOCKED]", cleaned)
    return "[UNTRUSTED TOOL OUTPUT]\n" + cleaned
