from __future__ import annotations

import asyncio
import datetime as dt
import html
import json
import os
import re
import sys
import threading
import time
import urllib.parse
from typing import Any, Dict, List, Optional, Tuple

import httpx

try:
    from ddgs import DDGS
    _HAS_DDGS = True
except Exception:
    try:
        from duckduckgo_search import DDGS
        _HAS_DDGS = True
    except Exception:
        DDGS = None
        _HAS_DDGS = False

from blackboard.react.tools.adblock import clean_html_for_research, filter_search_results, score_domain_credibility, should_block_resource
from blackboard.react.tool_registry import ToolRegistry
from blackboard.workspace.redaction import guard_untrusted_text

_MAX_CONTENT = 8000
_TIMEOUT_S = 20.0
_STOPWORDS = {
    "the", "and", "for", "that", "this", "with", "from", "what", "how",
    "does", "will", "when", "where", "which", "have", "been", "about",
    "into", "more", "also", "than", "your", "using", "used", "best",
}
_CONVERSATIONAL_FILLER_RE = re.compile(
    r"\b(hey|hi|hello|please|can you|could you|would you|i want|i need|i'd like|tell me|show me|let me know|find out|check out|look up|go ahead|just|really|actually|basically|maybe|probably|anyway|right now|right|ok|okay|thanks|thank you|sure|yeah|yes|no|well|so|like|um|uh|oh|ah|hmm|lol|haha|btw|fyi|imo|imho|tbh|ngl|idk|yo|bro|dude|man|bruh)\b",
    re.IGNORECASE,
)
_CONVERSATIONAL_PREFIX_RE = re.compile(
    r"^\s*(?:hey\b|hi\b|hello\b|yo\b|ok\b|okay\b|so\b|well\b|please\b|can you\b|could you\b|would you\b|i want to\b|i need to\b|i'd like to\b)[,;:!?\s]*",
    re.IGNORECASE,
)
_CONVERSATIONAL_SUFFIX_RE = re.compile(
    r"[,;:!?\s]*(?:please|thanks|thank you|thx|ok|okay|right|yeah|for me|if you can|when you get a chance)\s*[.!?]*\s*$",
    re.IGNORECASE,
)
_SEARCH_INTENT_VERBS_RE = re.compile(
    r"\b(search|find|look up|lookup|check|browse|google|research|fetch|get|show|tell me about|what is|what are|what was|what's|who is|who are|who was|who's|when is|when was|when did|where is|where are|how to|how do|how does|how did|how can|why is|why are|why did|is there|are there|has there been)\b",
    re.IGNORECASE,
)
_QUOTED_PHRASE_RE = re.compile(r'"([^"]{2,80})"')
_CAPITALIZED_ENTITY_RE = re.compile(r'\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,4})\b')
_YEAR_RE = re.compile(r'\b(19\d{2}|20[0-3]\d)\b')
_VERSION_RE = re.compile(r'\b(v?\d+\.\d+(?:\.\d+)?)\b')
_ENTITY_FILLER_WORDS = {
    "hey", "hi", "hello", "please", "can", "could", "would", "should",
    "may", "might", "shall", "let", "want", "need", "find", "look",
    "check", "search", "show", "tell", "get", "give", "take", "make",
    "know", "think", "see", "try", "use", "the", "and", "for", "but",
    "not", "you", "your", "our", "its", "his", "her", "their", "this",
    "that", "what", "when", "where", "which", "how", "why", "who",
    "whom", "are", "was", "were", "has", "had", "have", "does", "did",
    "will", "been", "being", "got", "just", "really", "actually",
    "basically", "maybe", "probably", "anyway", "right", "okay", "sure",
    "yeah", "yes", "well", "like", "thanks", "thank", "ok", "so", "no",
    "yo", "bro", "dude", "man", "bruh", "lol", "haha", "btw", "fyi",
    "imo", "imho", "tbh", "ngl", "idk", "some", "also", "very", "much",
    "about", "from", "with", "into", "more", "than", "been", "too",
}


def _distill_search_query(message: str) -> str:
    """Extract a search-optimized query from a conversational message.

    Strips conversational filler, preserves entities, quoted phrases,
    years, and version numbers. Falls back to the cleaned message if
    distillation would produce something too short.
    """
    value = str(message or "").strip()
    if not value:
        return ""
    if len(value) <= 60 and not _CONVERSATIONAL_FILLER_RE.search(value):
        return value
    quoted = _QUOTED_PHRASE_RE.findall(value)
    entities = [
        e for e in _CAPITALIZED_ENTITY_RE.findall(value)
        if not all(word.lower() in _ENTITY_FILLER_WORDS for word in e.split())
    ]
    years = _YEAR_RE.findall(value)
    versions = _VERSION_RE.findall(value)
    cleaned = _CONVERSATIONAL_PREFIX_RE.sub("", value)
    cleaned = _CONVERSATIONAL_SUFFIX_RE.sub("", cleaned)
    cleaned = _SEARCH_INTENT_VERBS_RE.sub("", cleaned)
    cleaned = _CONVERSATIONAL_FILLER_RE.sub(" ", cleaned)
    cleaned = re.sub(r"[\s]+", " ", cleaned).strip()
    cleaned = re.sub(r"^[,;:!?\s]+", "", cleaned).strip()
    cleaned = re.sub(r"[,;:!?\s]+$", "", cleaned).strip()
    preserved = set()
    for phrase in quoted:
        preserved.add(phrase.strip())
    for entity in entities:
        preserved.add(entity.strip())
    for year in years:
        preserved.add(year)
    for ver in versions:
        preserved.add(ver)
    if cleaned and len(cleaned) >= 8:
        for item in sorted(preserved, key=lambda x: -len(x)):
            if item.lower() not in cleaned.lower():
                cleaned = f"{cleaned} {item}"
        return cleaned.strip()
    if preserved:
        return " ".join(sorted(preserved, key=lambda x: -len(x)))[:200]
    fallback = re.sub(r"[^a-zA-Z0-9\s\-_.'/]", " ", value)
    fallback = re.sub(r"\s+", " ", fallback).strip()
    return fallback[:200] if fallback else value[:200]


def _fan_out_queries(query: str, *, max_variants: int = 2) -> List[str]:
    """Generate search query variants for broader coverage.

    Returns a list starting with the original query followed by up to
    max_variants alternative phrasings that target different angles.
    """
    query = str(query or "").strip()
    if not query or len(query) < 6:
        return [query] if query else []
    variants: List[str] = [query]
    terms = [t for t in query.split() if t.lower() not in _STOPWORDS and len(t) >= 3]
    if len(terms) >= 3:
        core = " ".join(terms[:5])
        if core.lower() != query.lower():
            variants.append(core)
    if _query_wants_freshness(query) and not re.search(r"\b(latest|recent|new)\b", query, re.IGNORECASE):
        variants.append(f"{query} latest")
    elif not _query_wants_freshness(query) and len(terms) >= 2:
        entities = _CAPITALIZED_ENTITY_RE.findall(query)
        if entities:
            entity_query = " ".join(entities[:3])
            if entity_query.lower() != query.lower() and len(entity_query) >= 6:
                variants.append(entity_query)
    seen = set()
    deduped: List[str] = []
    for v in variants:
        key = v.strip().lower()
        if key and key not in seen:
            seen.add(key)
            deduped.append(v.strip())
    return deduped[:1 + max(0, int(max_variants))]


_LOW_VALUE_RE = re.compile(
    r"\b(cookie|privacy policy|terms of service|all rights reserved|subscribe|newsletter|sign in|log in|advertisement|sponsored|share this|related articles|table of contents|skip to content|breadcrumb|javascript required|enable cookies|accept cookies)\b",
    re.IGNORECASE,
)
_BINARY_URL_RE = re.compile(r"\.(?:pdf|zip|gz|png|jpe?g|gif|svg|webp|ico|mp4|mp3|avi|mov|wmv|docx?|xlsx?|pptx?)($|[?#])", re.IGNORECASE)
_TRACKING_QUERY_KEYS = frozenset({
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "gclid", "fbclid", "msclkid", "ref", "ref_src", "source", "ved", "ei",
    "oq", "aqs", "sa", "usg",
})
_SEARCH_CHALLENGE_RE = re.compile(
    r"\b(captcha|unusual traffic|automated queries|verify (?:that )?you(?:'re| are)? human|access denied|security check|security challenge|challenge required|attention required|press & hold|robot check|prove you are human|sorry)\b",
    re.IGNORECASE,
)
_BACKEND_WEIGHTS = {
    "google_cse": 1.9,
    "browser_google": 1.5,
    "searxng": 1.2,
    "bing_html": 1.05,
    "browser_bing": 1.15,
    "yahoo_html": 0.9,
    "browser_yahoo": 1.0,
    "duckduckgo_ddgs": 1.0,
    "duckduckgo_html": 0.95,
    "duckduckgo_lite": 0.9,
}
_SEARCH_HISTORY: List[Dict[str, Any]] = []
_SEARCH_CACHE: Dict[str, List[Dict[str, Any]]] = {}
_BROWSER_SERP_STATE_LOCK = threading.Lock()
_BROWSER_SERP_STATE: Dict[str, Dict[str, Any]] = {}
_WEB_SEARCH_CONFIG: Dict[str, Any] = {
    "backend_weights": dict(_BACKEND_WEIGHTS),
    "browser_serp": {
        "min_interval_s": 20.0,
        "failure_cooldown_s": 180.0,
        "challenge_cooldown_s": 900.0,
        "adaptive_backoff_multiplier": 1.6,
        "max_cooldown_multiplier": 4.0,
        "telemetry_enabled": True,
    },
}
_RESEARCH_CATEGORY_ALIASES = {
    "anime": "anime",
    "manga": "anime",
    "character": "anime",
    "media": "anime",
    "game": "games",
    "gaming": "games",
    "paper": "academic",
    "research": "academic",
    "academic": "academic",
    "science": "academic",
    "docs": "docs",
    "documentation": "docs",
    "wiki": "general",
    "general": "general",
}
_RESEARCH_CATEGORY_KEYWORDS = {
    "anime": {"anime", "manga", "episode", "episodes", "character", "characters", "shonen", "naruto", "boruto", "viz", "crunchyroll"},
    "games": {"game", "games", "gameplay", "patch", "patches", "steam", "xbox", "playstation", "ps5", "nintendo", "trailer"},
    "academic": {"paper", "research", "study", "preprint", "arxiv", "abstract", "citation", "benchmark", "doi", "journal"},
    "docs": {"api", "docs", "documentation", "library", "framework", "error", "class", "function", "python", "javascript", "typescript"},
    "general": {"wiki", "overview", "history", "explained"},
}
_RESEARCH_CATEGORY_DOMAINS = {
    "anime": ["animenewsnetwork.com", "viz.com", "crunchyroll.com", "wikipedia.org", "anidb.net", "myanimelist.net", "anime-planet.com", "hulu.com", "fandom.com"],
    "games": ["wikipedia.org", "ign.com", "gamespot.com", "metacritic.com", "store.steampowered.com", "gamefaqs.gamespot.com", "fandom.com"],
    "academic": ["arxiv.org", "semanticscholar.org", "openreview.net", "acm.org", "ieeexplore.ieee.org", "pubmed.ncbi.nlm.nih.gov"],
    "docs": ["docs.python.org", "developer.mozilla.org", "readthedocs.io", "pypi.org", "github.com"],
    "general": ["wikipedia.org", "britannica.com", "fandom.com"],
}
_RESEARCH_DOMAIN_CLASSES = {
    "anime": {
        "animenewsnetwork.com": "news",
        "viz.com": "official",
        "crunchyroll.com": "official",
        "wikipedia.org": "reference",
        "anidb.net": "reference",
        "myanimelist.net": "reference",
        "anime-planet.com": "reference",
        "hulu.com": "distribution",
        "fandom.com": "wiki",
    },
    "games": {
        "ign.com": "news",
        "gamespot.com": "news",
        "store.steampowered.com": "official",
        "metacritic.com": "reference",
        "gamefaqs.gamespot.com": "reference",
        "fandom.com": "wiki",
        "wikipedia.org": "reference",
    },
    "academic": {
        "arxiv.org": "preprint",
        "semanticscholar.org": "reference",
        "openreview.net": "review",
        "acm.org": "publisher",
        "ieeexplore.ieee.org": "publisher",
        "pubmed.ncbi.nlm.nih.gov": "index",
    },
    "docs": {
        "docs.python.org": "official",
        "developer.mozilla.org": "official",
        "readthedocs.io": "reference",
        "pypi.org": "reference",
        "github.com": "source",
    },
    "general": {
        "wikipedia.org": "reference",
        "britannica.com": "reference",
        "fandom.com": "wiki",
    },
}
_RESEARCH_CATEGORY_SUFFIX = {
    "anime": "anime",
    "games": "game",
    "academic": "research paper",
    "docs": "documentation",
    "general": "wiki",
}
_SOURCE_HINT_DOMAIN_MAP = {
    "wikipedia": "wikipedia.org",
    "wiki": "wikipedia.org",
    "anidb": "anidb.net",
    "myanimelist": "myanimelist.net",
    "mal": "myanimelist.net",
    "anime-planet": "anime-planet.com",
    "animenewsnetwork": "animenewsnetwork.com",
    "ann": "animenewsnetwork.com",
    "fandom": "fandom.com",
    "arxiv": "arxiv.org",
    "semanticscholar": "semanticscholar.org",
    "openreview": "openreview.net",
    "pubmed": "pubmed.ncbi.nlm.nih.gov",
    "viz": "viz.com",
    "crunchyroll": "crunchyroll.com",
    "hulu": "hulu.com",
    "ign": "ign.com",
    "gamespot": "gamespot.com",
}
_RESEARCH_DISAMBIGUATION_TERMS = {
    "anime": {
        "franchise": {"anime", "manga", "series", "franchise", "shonen", "episode", "media"},
        "entity": {"character", "characters", "protagonist", "fictional", "hero"},
        "game": {"game", "games", "gaming", "mmorpg", "mobile", "rpg", "steam"},
    },
    "games": {
        "game": {"game", "games", "gaming", "gameplay", "patch", "dlc", "release", "trailer"},
        "franchise": {"series", "franchise", "universe", "lore"},
        "entity": {"character", "characters", "boss", "npc", "hero"},
    },
    "general": {
        "franchise": {"series", "franchise", "universe", "media", "brand"},
        "entity": {"character", "person", "company", "product", "city"},
    },
}
_RESEARCH_DISAMBIGUATION_FALLBACK_TERMS = {
    "anime": {
        "franchise": ["anime", "series"],
        "entity": ["character"],
        "game": ["game"],
    },
    "games": {
        "game": ["game"],
        "franchise": ["franchise"],
        "entity": ["character"],
    },
}
_PLAYWRIGHT_STEALTH_SCRIPT = """
(() => {
  try {
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    window.chrome = window.chrome || {};
    window.chrome.runtime = window.chrome.runtime || { id: undefined };
    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
    Object.defineProperty(document, 'visibilityState', { get: () => 'visible', configurable: true });
    Object.defineProperty(document, 'hidden', { get: () => false, configurable: true });
  } catch (e) {}
})();
"""


def _clean_text(text: str) -> str:
    value = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"\n{3,}", "\n\n", value)
    value = re.sub(r"[ \t]{2,}", " ", value)
    return value.strip()


def _strip_html(html: str) -> str:
    return _clean_text(clean_html_for_research(html, aggressive=True))


def _query_terms(query: str) -> set[str]:
    return {
        token for token in re.findall(r"\b[a-z0-9]{3,}\b", str(query or "").lower())
        if token not in _STOPWORDS
    }


def _dedupe_terms(items: List[str], *, limit: int = 0) -> List[str]:
    out: List[str] = []
    seen = set()
    for item in list(items or []):
        value = str(item or "").strip().lower()
        if not value or value in seen:
            continue
        seen.add(value)
        out.append(value)
        if limit and len(out) >= int(limit):
            break
    return out


def _score_text(query: str, title: str, body: str) -> float:
    query_words = _query_terms(query)
    haystack = f"{title} {body}".lower()
    words = set(re.findall(r"\b[a-z0-9]{3,}\b", haystack))
    overlap = len(query_words & words)
    title_overlap = len(query_words & set(re.findall(r"\b[a-z0-9]{3,}\b", str(title or "").lower())))
    score = float(overlap) + (0.75 * float(title_overlap))
    if len(str(body or "")) >= 120:
        score += 0.15
    return score


def _truncate(text: str, limit: int) -> Tuple[str, bool]:
    value = str(text or "")
    if len(value) <= limit:
        return value, False
    return value[:limit].rstrip() + "\n... (truncated)", True


def _guard_web_text(text: str, max_chars: int) -> str:
    return guard_untrusted_text(_clean_text(text), max_chars=max_chars)


def _sanitize_search_result_item(item: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(item or {})
    if "snippet" in result:
        result["snippet"] = _guard_web_text(str(result.get("snippet", "") or ""), max_chars=280)
    return result


def _search_cache_key(query: str, max_results: int) -> str:
    return f"{str(query or '').strip().lower()}::{int(max_results)}"


def get_search_history(limit: int = 20) -> List[Dict[str, Any]]:
    return list(_SEARCH_HISTORY[-max(1, int(limit or 20)):])


def clear_search_cache(query: Optional[str] = None, max_results: Optional[int] = None) -> None:
    if query is None:
        _SEARCH_CACHE.clear()
        return
    query_value = str(query or "").strip().lower()
    if max_results is None:
        keys = [key for key in _SEARCH_CACHE.keys() if key.startswith(f"{query_value}::")]
        for key in keys:
            _SEARCH_CACHE.pop(key, None)
        return
    _SEARCH_CACHE.pop(_search_cache_key(query_value, int(max_results)), None)


def get_search_cache_stats() -> Dict[str, Any]:
    return {
        "cached_queries": len(_SEARCH_CACHE),
        "total_searches": len(_SEARCH_HISTORY),
        "cache_size": sum(len(str(value)) for value in _SEARCH_CACHE.values()),
    }


def configure_web_search(config: Dict[str, Any]) -> None:
    cfg = dict(config or {})
    backend_weights = dict(_BACKEND_WEIGHTS)
    raw_weights = cfg.get("backend_weights") or {}
    if isinstance(raw_weights, dict):
        for key, value in raw_weights.items():
            try:
                backend_weights[str(key).strip()] = float(value)
            except Exception:
                continue
    browser = dict(_WEB_SEARCH_CONFIG.get("browser_serp") or {})
    raw_browser = cfg.get("browser_serp") or {}
    if isinstance(raw_browser, dict):
        for key in ("min_interval_s", "failure_cooldown_s", "challenge_cooldown_s", "adaptive_backoff_multiplier", "max_cooldown_multiplier"):
            if key in raw_browser:
                try:
                    browser[key] = float(raw_browser[key])
                except Exception:
                    continue
        if "telemetry_enabled" in raw_browser:
            browser["telemetry_enabled"] = bool(raw_browser.get("telemetry_enabled"))
    _WEB_SEARCH_CONFIG["backend_weights"] = backend_weights
    _WEB_SEARCH_CONFIG["browser_serp"] = browser


def _record_search_history(query: str, max_results: int, backend: str, result_count: int, cache_hit: bool) -> None:
    _SEARCH_HISTORY.append({
        "query": str(query or ""),
        "max_results": int(max_results),
        "backend": str(backend or ""),
        "result_count": int(result_count),
        "cache_hit": bool(cache_hit),
        "timestamp": dt.datetime.now(dt.UTC).isoformat(),
    })


def _host(url: str) -> str:
    try:
        host = urllib.parse.urlparse(str(url or "").strip()).netloc.lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def _google_credentials() -> Tuple[str, str]:
    api_key = str(os.environ.get("GOOGLE_SEARCH_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
    cx = str(os.environ.get("GOOGLE_SEARCH_CX") or os.environ.get("GOOGLE_CSE_ID") or "").strip()
    return api_key, cx


def _searxng_base() -> str:
    return str(os.environ.get("SEARXNG_API_BASE") or "").strip().rstrip("/")


def _browser_serp_enabled() -> bool:
    return str(os.environ.get("ENABLE_BROWSER_SERP_FALLBACK") or "").strip().lower() in {"1", "true", "yes", "on"}


def _browser_serp_engines() -> List[str]:
    raw = str(os.environ.get("BROWSER_SERP_ENGINES") or "google,bing,yahoo").strip().lower()
    allowed = ["google", "bing", "yahoo"]
    if raw in {"", "all", "*"}:
        return allowed
    requested = [part.strip() for part in raw.split(",") if part.strip()]
    selected = [engine for engine in requested if engine in allowed]
    return selected or allowed


def _browser_serp_min_interval_s() -> float:
    configured = ((_WEB_SEARCH_CONFIG.get("browser_serp") or {}).get("min_interval_s"))
    try:
        if configured is not None:
            return max(0.0, float(configured))
    except Exception:
        pass
    try:
        return max(0.0, float(os.environ.get("BROWSER_SERP_MIN_INTERVAL_S") or 20.0))
    except Exception:
        return 20.0


def _browser_serp_failure_cooldown_s() -> float:
    configured = ((_WEB_SEARCH_CONFIG.get("browser_serp") or {}).get("failure_cooldown_s"))
    try:
        if configured is not None:
            return max(0.0, float(configured))
    except Exception:
        pass
    try:
        return max(0.0, float(os.environ.get("BROWSER_SERP_FAILURE_COOLDOWN_S") or 180.0))
    except Exception:
        return 180.0


def _browser_serp_challenge_cooldown_s() -> float:
    configured = ((_WEB_SEARCH_CONFIG.get("browser_serp") or {}).get("challenge_cooldown_s"))
    try:
        if configured is not None:
            return max(0.0, float(configured))
    except Exception:
        pass
    try:
        return max(0.0, float(os.environ.get("BROWSER_SERP_CHALLENGE_COOLDOWN_S") or 900.0))
    except Exception:
        return 900.0


def _browser_serp_adaptive_backoff_multiplier() -> float:
    configured = ((_WEB_SEARCH_CONFIG.get("browser_serp") or {}).get("adaptive_backoff_multiplier"))
    try:
        if configured is not None:
            return max(1.0, float(configured))
    except Exception:
        pass
    return 1.6


def _browser_serp_max_cooldown_multiplier() -> float:
    configured = ((_WEB_SEARCH_CONFIG.get("browser_serp") or {}).get("max_cooldown_multiplier"))
    try:
        if configured is not None:
            return max(1.0, float(configured))
    except Exception:
        pass
    return 4.0


def _search_telemetry_enabled() -> bool:
    configured = ((_WEB_SEARCH_CONFIG.get("browser_serp") or {}).get("telemetry_enabled"))
    if configured is not None:
        return bool(configured)
    return True


def _search_backend_weight(backend: str) -> float:
    weights = dict(_WEB_SEARCH_CONFIG.get("backend_weights") or _BACKEND_WEIGHTS)
    return float(weights.get(str(backend or "").strip(), 1.0))


def _search_telemetry_bucket(query: str, requested: int, expanded: int) -> Dict[str, Any]:
    return {
        "query": str(query or ""),
        "requested_results": int(requested),
        "expanded_results": int(expanded),
        "backends": {},
        "browser_state": {},
    }


def _search_telemetry_note(telemetry: Dict[str, Any], backend: str, *, status: str, result_count: int = 0, detail: str = "", challenge: bool = False, weight: Optional[float] = None) -> None:
    if not _search_telemetry_enabled():
        return
    bucket = telemetry.setdefault("backends", {})
    entry = {
        "status": str(status or ""),
        "result_count": max(0, int(result_count or 0)),
    }
    if detail:
        entry["detail"] = str(detail or "")[:200]
    if challenge:
        entry["challenge"] = True
    if weight is not None:
        entry["weight"] = float(weight)
    bucket[str(backend or "")] = entry


def _search_telemetry_browser_state() -> Dict[str, Dict[str, Any]]:
    snapshot: Dict[str, Dict[str, Any]] = {}
    now = time.monotonic()
    with _BROWSER_SERP_STATE_LOCK:
        for engine, raw in _BROWSER_SERP_STATE.items():
            state = dict(raw or {})
            snapshot[str(engine)] = {
                "last_status": str(state.get("last_status") or ""),
                "last_error": str(state.get("last_error") or "")[:200],
                "consecutive_failures": int(state.get("consecutive_failures") or 0),
                "cooldown_remaining_s": max(0.0, float(state.get("cooldown_until") or 0.0) - now),
                "last_result_count": int(state.get("last_result_count") or 0),
            }
    return snapshot


def _browser_serp_claim_attempt(engine: str) -> Tuple[bool, str]:
    name = str(engine or "").strip().lower()
    if not name:
        return False, "invalid_engine"
    now = time.monotonic()
    with _BROWSER_SERP_STATE_LOCK:
        state = dict(_BROWSER_SERP_STATE.get(name) or {})
        cooldown_until = float(state.get("cooldown_until") or 0.0)
        if cooldown_until > now:
            state["last_status"] = "cooldown"
            _BROWSER_SERP_STATE[name] = state
            return False, "cooldown"
        last_attempt = float(state.get("last_attempt") or 0.0)
        if last_attempt and (now - last_attempt) < _browser_serp_min_interval_s():
            state["last_status"] = "rate_limit"
            _BROWSER_SERP_STATE[name] = state
            return False, "rate_limit"
        state["last_attempt"] = now
        _BROWSER_SERP_STATE[name] = state
    return True, ""


def _browser_serp_mark_success(engine: str, result_count: int) -> None:
    name = str(engine or "").strip().lower()
    now = time.monotonic()
    with _BROWSER_SERP_STATE_LOCK:
        state = dict(_BROWSER_SERP_STATE.get(name) or {})
        state["last_success"] = now
        state["last_status"] = "ok"
        state["last_result_count"] = max(0, int(result_count or 0))
        state["cooldown_until"] = 0.0
        state["consecutive_failures"] = 0
        state["last_error"] = ""
        _BROWSER_SERP_STATE[name] = state


def _browser_serp_mark_failure(engine: str, *, challenge: bool, detail: str = "") -> None:
    name = str(engine or "").strip().lower()
    now = time.monotonic()
    with _BROWSER_SERP_STATE_LOCK:
        state = dict(_BROWSER_SERP_STATE.get(name) or {})
        failures = int(state.get("consecutive_failures") or 0) + 1
        base_cooldown = _browser_serp_challenge_cooldown_s() if challenge else _browser_serp_failure_cooldown_s()
        multiplier = min(_browser_serp_max_cooldown_multiplier(), _browser_serp_adaptive_backoff_multiplier() ** max(0, failures - 1))
        cooldown = base_cooldown * multiplier
        state["cooldown_until"] = max(float(state.get("cooldown_until") or 0.0), now + cooldown)
        state["consecutive_failures"] = failures
        state["last_status"] = "challenge" if challenge else "error"
        state["last_error"] = str(detail or "")[:200]
        _BROWSER_SERP_STATE[name] = state


def _search_headers(*, referer: str = "") -> Dict[str, str]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }
    if str(referer or "").strip():
        headers["Referer"] = str(referer).strip()
    return headers


def _crawl4ai_content(result: Any) -> Tuple[str, str]:
    title = str(getattr(result, "title", "") or "").strip()
    metadata = getattr(result, "metadata", None)
    if isinstance(metadata, dict):
        title = title or str(metadata.get("title") or metadata.get("og:title") or "").strip()
    markdown = getattr(result, "markdown", None)
    candidates: List[str] = []
    if markdown is not None:
        for attr in ("fit_markdown", "raw_markdown", "markdown"):
            value = str(getattr(markdown, attr, "") or "").strip()
            if value:
                candidates.append(value)
    for attr in ("fit_markdown", "raw_markdown", "markdown_v2", "extracted_content"):
        value = getattr(result, attr, "")
        if isinstance(value, str) and value.strip():
            candidates.append(value.strip())
    html = str(getattr(result, "cleaned_html", "") or getattr(result, "html", "") or "").strip()
    if html:
        candidates.append(_strip_html(html))
    for candidate in candidates:
        cleaned = _clean_text(candidate)
        if cleaned:
            return cleaned, title
    return "", title


def _canonicalize_search_url(url: str) -> str:
    raw = str(url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urllib.parse.urlparse(raw)
    except Exception:
        return raw
    if not parsed.scheme or not parsed.netloc:
        return raw
    filtered = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
        if str(key or "").strip().lower() not in _TRACKING_QUERY_KEYS
    ]
    host = parsed.netloc.lower()
    if host.startswith("www."):
        host = host[4:]
    normalized = parsed._replace(netloc=host, query=urllib.parse.urlencode(filtered, doseq=True), fragment="")
    return urllib.parse.urlunparse(normalized).rstrip("/")


def _decode_google_result_url(url: str) -> str:
    value = html.unescape(str(url or "").strip())
    if not value:
        return ""
    if value.startswith("/url?"):
        try:
            parsed = urllib.parse.parse_qs(urllib.parse.urlparse(value).query)
            target = str(parsed.get("q", [""])[0] or "").strip()
            return urllib.parse.unquote(target) if target else ""
        except Exception:
            return ""
    return value


def _decode_yahoo_result_url(url: str) -> str:
    value = html.unescape(str(url or "").strip())
    if not value:
        return ""
    try:
        parsed = urllib.parse.urlparse(value)
    except Exception:
        return value
    query = urllib.parse.parse_qs(parsed.query)
    direct = str(query.get("RU", [""])[0] or query.get("u", [""])[0] or "").strip()
    if direct:
        return urllib.parse.unquote(direct)
    path_match = re.search(r"/RU=([^/]+)(?:/|$)", str(parsed.path or ""), flags=re.IGNORECASE)
    if path_match:
        return urllib.parse.unquote(path_match.group(1))
    match = re.search(r"[?&]RU=([^&]+)", value, flags=re.IGNORECASE)
    if match:
        return urllib.parse.unquote(match.group(1))
    return value


def _normalize_search_result_url(url: str, *, engine: str = "") -> str:
    value = html.unescape(str(url or "").strip())
    if not value:
        return ""
    if engine == "google":
        value = _decode_google_result_url(value)
    elif engine == "yahoo":
        value = _decode_yahoo_result_url(value)
    else:
        value = _decode_ddg_url(value)
    return html.unescape(str(value or "").strip())


def _extract_google_html_results(page_html: str, max_results: int) -> List[Dict[str, Any]]:
    pattern = re.compile(
        r'<a[^>]+href="(?P<url>/url\?q=[^"]+)"[^>]*>(?P<title>.*?)</a>(?P<tail>.*?)(?=<a[^>]+href="/url\?q=|$)',
        re.DOTALL | re.IGNORECASE,
    )
    results: List[Dict[str, Any]] = []
    for match in pattern.finditer(str(page_html or "")):
        url = _normalize_search_result_url(match.group("url"), engine="google")
        title = _strip_html(match.group("title"))
        if not url or not title:
            continue
        if "google." in _host(url):
            continue
        tail = match.group("tail")
        snippet = ""
        for snippet_pattern in (
            r'<div[^>]+class="[^"]*(?:VwiC3b|yXK7lf|s3v9rd)[^"]*"[^>]*>(.*?)</div>',
            r'<span[^>]+class="[^"]*aCOpRe[^"]*"[^>]*>(.*?)</span>',
            r'<div[^>]*>(.*?)</div>',
        ):
            snippet_match = re.search(snippet_pattern, tail, flags=re.DOTALL | re.IGNORECASE)
            snippet = _strip_html(snippet_match.group(1) if snippet_match else "")
            if snippet:
                break
        results.append({"title": title, "url": url, "snippet": snippet[:280]})
        if len(results) >= max_results:
            break
    return filter_search_results(results)


def _extract_bing_html_results(page_html: str, max_results: int) -> List[Dict[str, Any]]:
    pattern = re.compile(
        r'<li[^>]+class="[^"]*\bb_algo\b[^"]*"[^>]*>(?P<body>.*?)</li>',
        re.DOTALL | re.IGNORECASE,
    )
    results: List[Dict[str, Any]] = []
    for match in pattern.finditer(str(page_html or "")):
        body = match.group("body")
        link_match = re.search(r'<h2[^>]*>\s*<a[^>]+href="(?P<url>[^"]+)"[^>]*>(?P<title>.*?)</a>', body, flags=re.DOTALL | re.IGNORECASE)
        if not link_match:
            continue
        url = _normalize_search_result_url(link_match.group("url"), engine="bing")
        title = _strip_html(link_match.group("title"))
        if not url or not title or "bing.com" in _host(url):
            continue
        snippet = ""
        for snippet_pattern in (
            r'<div[^>]+class="[^"]*\bb_caption\b[^"]*"[^>]*>.*?<p[^>]*>(.*?)</p>',
            r'<p[^>]*>(.*?)</p>',
        ):
            snippet_match = re.search(snippet_pattern, body, flags=re.DOTALL | re.IGNORECASE)
            snippet = _strip_html(snippet_match.group(1) if snippet_match else "")
            if snippet:
                break
        results.append({"title": title, "url": url, "snippet": snippet[:280]})
        if len(results) >= max_results:
            break
    return filter_search_results(results)


def _extract_yahoo_html_results(page_html: str, max_results: int) -> List[Dict[str, Any]]:
    patterns = [
        re.compile(
            r'<div[^>]+class="[^"]*\balgo\b[^"]*"[^>]*>(?P<body>.*?)</div>\s*</div>',
            re.DOTALL | re.IGNORECASE,
        ),
        re.compile(
            r'<div[^>]+class="[^"]*\bcompTitle\b[^"]*"[^>]*>(?P<body>.*?)</div>(?P<tail>.*?)<div[^>]+class="[^"]*\bcompText\b[^"]*"[^>]*>(?P<snippet>.*?)</div>',
            re.DOTALL | re.IGNORECASE,
        ),
    ]
    results: List[Dict[str, Any]] = []
    seen = set()
    html_text = str(page_html or "")
    for pattern in patterns:
        for match in pattern.finditer(html_text):
            body = match.groupdict().get("body") or ""
            link_match = re.search(r'<a[^>]+href="(?P<url>[^"]+)"[^>]*>(?P<title>.*?)</a>', body, flags=re.DOTALL | re.IGNORECASE)
            if not link_match:
                continue
            url = _normalize_search_result_url(link_match.group("url"), engine="yahoo")
            title = _strip_html(link_match.group("title"))
            canonical = _canonicalize_search_url(url)
            if not url or not title or not canonical or canonical in seen:
                continue
            if "yahoo.com" in _host(url):
                continue
            seen.add(canonical)
            snippet = _strip_html(match.groupdict().get("snippet") or "")
            if not snippet:
                snippet_match = re.search(r'<div[^>]+class="[^"]*\bcompText\b[^"]*"[^>]*>(.*?)</div>', match.group(0), flags=re.DOTALL | re.IGNORECASE)
                snippet = _strip_html(snippet_match.group(1) if snippet_match else "")
            results.append({"title": title, "url": url, "snippet": snippet[:280]})
            if len(results) >= max_results:
                return filter_search_results(results)
    return filter_search_results(results)


def _extract_engine_html_results(engine: str, page_html: str, max_results: int) -> List[Dict[str, Any]]:
    name = str(engine or "").strip().lower()
    if name == "google":
        return _extract_google_html_results(page_html, max_results)
    if name == "bing":
        return _extract_bing_html_results(page_html, max_results)
    if name == "yahoo":
        return _extract_yahoo_html_results(page_html, max_results)
    return []


def _browser_serp_search_url(engine: str, query: str) -> str:
    encoded = urllib.parse.quote_plus(str(query or ""))
    name = str(engine or "").strip().lower()
    if name == "google":
        return f"https://www.google.com/search?q={encoded}&hl=en&num=10"
    if name == "bing":
        return f"https://www.bing.com/search?q={encoded}&setlang=en-US"
    if name == "yahoo":
        return f"https://search.yahoo.com/search?p={encoded}"
    return ""


def _detect_search_challenge(engine: str, page_html: str, *, page_title: str = "", final_url: str = "") -> str:
    combined = "\n".join([
        str(engine or ""),
        str(page_title or ""),
        str(final_url or ""),
        _strip_html(str(page_html or ""))[:3000],
    ])
    lowered_url = str(final_url or "").lower()
    if any(token in lowered_url for token in ("/sorry/", "sorry/index", "captcha", "challenge", "interstitial")):
        return lowered_url
    match = _SEARCH_CHALLENGE_RE.search(combined)
    if match:
        return str(match.group(0) or "challenge")
    return ""


def _normalize_research_category(category: str) -> str:
    value = str(category or "").strip().lower()
    if not value or value == "auto":
        return "auto"
    return str(_RESEARCH_CATEGORY_ALIASES.get(value, value)).strip() or "auto"


def _normalize_source_hints(source_hints: Any) -> List[str]:
    raw = source_hints or []
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",") if part.strip()]
    return [str(item).strip().lower() for item in list(raw or []) if str(item or "").strip()]


def _host_matches_domain(host: str, domain: str) -> bool:
    current = str(host or "").strip().lower()
    expected = str(domain or "").strip().lower()
    return bool(current and expected and (current == expected or current.endswith(f".{expected}")))


def _research_domain_class(category: str, host: str) -> str:
    for domain, label in dict(_RESEARCH_DOMAIN_CLASSES.get(str(category or ""), {}) or {}).items():
        if _host_matches_domain(host, domain):
            return str(label or "")
    return ""


def _query_wants_freshness(query: str) -> bool:
    return bool(re.search(r"\b(new|latest|recent|today|current|upcoming|news|release|202[4-9])\b", str(query or ""), flags=re.IGNORECASE))


def _source_hint_domains(source_hints: List[str]) -> List[str]:
    domains: List[str] = []
    seen = set()
    for hint in list(source_hints or []):
        mapped = str(_SOURCE_HINT_DOMAIN_MAP.get(str(hint or "").strip().lower(), hint or "")).strip().lower()
        cleaned = mapped[4:] if mapped.startswith("www.") else mapped
        if "." not in cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        domains.append(cleaned)
    return domains


def _research_disambiguation_vocab(category: str) -> Dict[str, set[str]]:
    resolved = _normalize_research_category(category)
    if resolved == "auto":
        resolved = "general"
    raw = dict(_RESEARCH_DISAMBIGUATION_TERMS.get(resolved, {}) or {})
    return {
        family: {str(term).strip().lower() for term in list(terms or []) if str(term or "").strip()}
        for family, terms in raw.items()
    }


def _build_research_disambiguation_query(query: str, disambiguation: Dict[str, Any]) -> str:
    query_text = str(query or "").strip()
    query_lower = query_text.lower()
    parts = [query_text]
    for term in list((disambiguation or {}).get("positive_terms") or [])[:1]:
        value = str(term or "").strip().lower()
        if not value or value in query_lower:
            continue
        parts.append(value)
    return " ".join(part for part in parts if str(part or "").strip())


def _infer_research_disambiguation(query: str, results: List[Dict[str, Any]], *, category: str, source_hints: Optional[List[str]] = None) -> Dict[str, Any]:
    vocab = _research_disambiguation_vocab(category)
    query_terms = _query_terms(query)
    resolved = _normalize_research_category(category)
    if resolved == "auto":
        resolved = "general"
    empty = {
        "family": "",
        "scores": {},
        "positive_terms": [],
        "family_terms": [],
        "conflict_terms": [],
        "ambiguous": False,
        "source_hints": _normalize_source_hints(source_hints or []),
    }
    if not vocab or len(query_terms) >= 5:
        return empty
    scores: Dict[str, float] = {family: 0.0 for family in vocab.keys()}
    matched_terms: Dict[str, set[str]] = {family: set() for family in vocab.keys()}
    for item in list(results or [])[:8]:
        title = str(item.get("title") or "")
        snippet = str(item.get("snippet") or "")
        url = str(item.get("url") or "")
        host = _host(url)
        combined = f"{title} {snippet} {url}"
        combined_terms = _query_terms(combined)
        title_terms = _query_terms(title)
        lowered = combined.lower()
        for family, markers in vocab.items():
            overlap = combined_terms & set(markers)
            if not overlap:
                continue
            matched_terms[family].update(overlap)
            scores[family] += 0.85 * float(len(overlap))
            scores[family] += 0.35 * float(len(title_terms & overlap))
        if resolved == "anime":
            if any(_host_matches_domain(host, domain) for domain in _RESEARCH_CATEGORY_DOMAINS.get("anime", [])):
                scores["franchise"] = float(scores.get("franchise") or 0.0) + 0.2
            if re.search(r"\b(anime|manga|series|franchise)\b", lowered):
                scores["franchise"] = float(scores.get("franchise") or 0.0) + 0.65
            if re.search(r"\b(character|characters|protagonist|fictional)\b", lowered):
                scores["entity"] = float(scores.get("entity") or 0.0) + 0.65
            if re.search(r"\b(game|games|gaming|mmorpg|mobile|rpg|steam)\b", lowered):
                scores["game"] = float(scores.get("game") or 0.0) + 0.9
    ordered = sorted(scores.items(), key=lambda item: (-float(item[1] or 0.0), item[0]))
    primary_family = ""
    primary_score = 0.0
    if ordered and float(ordered[0][1] or 0.0) >= 1.0:
        primary_family = str(ordered[0][0] or "")
        primary_score = float(ordered[0][1] or 0.0)
    secondary_score = float(ordered[1][1] or 0.0) if len(ordered) > 1 else 0.0
    ambiguous = bool(primary_family and len(query_terms) <= 2 and secondary_score >= max(0.8, primary_score * 0.5))
    fallbacks = dict(_RESEARCH_DISAMBIGUATION_FALLBACK_TERMS.get(resolved, {}) or {})
    positive_terms: List[str] = []
    family_terms: List[str] = []
    conflict_terms: List[str] = []
    if primary_family:
        positive_terms = _dedupe_terms([
            *sorted(term for term in matched_terms.get(primary_family, set()) if term not in query_terms),
            *list(fallbacks.get(primary_family) or []),
        ], limit=2)
        family_terms = _dedupe_terms(sorted(vocab.get(primary_family, set())), limit=8)
        conflict_pool: List[str] = []
        conflict_threshold = max(0.8, primary_score * 0.55)
        if len(query_terms) <= 2:
            conflict_threshold = min(conflict_threshold, max(1.1, primary_score * 0.38))
        for family, score in ordered[1:]:
            if float(score or 0.0) < conflict_threshold:
                continue
            conflict_pool.extend(sorted(matched_terms.get(str(family or ""), set())))
            conflict_pool.extend(list(fallbacks.get(str(family or "")) or []))
        conflict_terms = [
            term for term in _dedupe_terms(conflict_pool, limit=4)
            if term not in positive_terms and term not in query_terms
        ]
    return {
        "family": primary_family,
        "scores": scores,
        "positive_terms": positive_terms,
        "family_terms": family_terms,
        "conflict_terms": conflict_terms,
        "ambiguous": ambiguous,
        "source_hints": _normalize_source_hints(source_hints or []),
    }


def _infer_research_category(query: str, results: List[Dict[str, Any]], *, category: str = "auto", source_hints: Optional[List[str]] = None) -> Tuple[str, Dict[str, Any]]:
    explicit = _normalize_research_category(category)
    hints = _normalize_source_hints(source_hints or [])
    if explicit != "auto":
        return explicit, {"mode": "explicit", "scores": {explicit: 999.0}, "source_hints": hints}
    terms = _query_terms(query)
    scores: Dict[str, float] = {name: 0.0 for name in _RESEARCH_CATEGORY_DOMAINS.keys()}
    hint_domains = _source_hint_domains(hints)
    for name, keywords in _RESEARCH_CATEGORY_KEYWORDS.items():
        scores[name] += 1.35 * float(len(terms & set(keywords)))
    for item in list(results or [])[:6]:
        host = _host(item.get("url") or "")
        title = str(item.get("title") or "")
        snippet = str(item.get("snippet") or "")
        combined_terms = _query_terms(f"{title} {snippet}")
        for name, domains in _RESEARCH_CATEGORY_DOMAINS.items():
            if any(_host_matches_domain(host, domain) for domain in domains):
                scores[name] += 1.5
        for name, keywords in _RESEARCH_CATEGORY_KEYWORDS.items():
            scores[name] += 0.45 * float(len(combined_terms & set(keywords)))
    for domain in hint_domains:
        for name, domains in _RESEARCH_CATEGORY_DOMAINS.items():
            if any(_host_matches_domain(domain, expected) for expected in domains):
                scores[name] += 2.0
    best = max(scores.items(), key=lambda item: (item[1], item[0]))[0] if scores else "general"
    if float(scores.get(best, 0.0)) < 1.2:
        best = "general"
    return best, {"mode": "auto", "scores": scores, "source_hints": hints}


def _build_research_source_lanes(query: str, category: str, *, source_hints: Optional[List[str]] = None, max_lanes: int = 5, disambiguation: Optional[Dict[str, Any]] = None) -> List[Dict[str, str]]:
    resolved = _normalize_research_category(category)
    if resolved == "auto":
        resolved = "general"
    hinted_domains = _source_hint_domains(_normalize_source_hints(source_hints or []))
    category_domains = list(_RESEARCH_CATEGORY_DOMAINS.get(resolved, []))
    suffix = str(_RESEARCH_CATEGORY_SUFFIX.get(resolved) or "").strip()
    freshness = _query_wants_freshness(query)
    positive_terms = [str(term or "").strip().lower() for term in list((disambiguation or {}).get("positive_terms") or []) if str(term or "").strip()]
    if freshness:
        domain_profiles = dict(_RESEARCH_DOMAIN_CLASSES.get(resolved, {}) or {})
        category_domains = sorted(
            category_domains,
            key=lambda domain: ({"news": 0, "official": 1, "preprint": 1, "review": 2, "publisher": 2, "reference": 3, "index": 3, "wiki": 4, "distribution": 5}.get(str(domain_profiles.get(domain) or ""), 6), domain),
        )
    domains = hinted_domains + category_domains
    lanes: List[Dict[str, str]] = []
    seen = set()
    query_lower = str(query or "").lower()
    for domain in domains:
        if domain in seen:
            continue
        seen.add(domain)
        parts = [str(query or "").strip()]
        for term in positive_terms[:2]:
            if term and term not in query_lower:
                parts.append(term)
        if freshness and not re.search(r"\b(latest|recent|current|upcoming|new|news|202[4-9])\b", query_lower):
            parts.append("latest")
        if suffix and suffix not in query_lower and suffix not in positive_terms:
            parts.append(suffix)
        parts.append(f"site:{domain}")
        lanes.append({
            "label": domain,
            "domain": domain,
            "query": " ".join(part for part in parts if str(part or "").strip()),
        })
        if len(lanes) >= max(1, int(max_lanes or 1)):
            break
    return lanes


def _research_result_priority(query: str, item: Dict[str, Any], *, category: str, source_hints: Optional[List[str]] = None, disambiguation: Optional[Dict[str, Any]] = None) -> float:
    url = str(item.get("url") or "")
    title = str(item.get("title") or "")
    snippet = str(item.get("snippet") or "")
    host = _host(url)
    combined_terms = _query_terms(f"{title} {snippet} {url}")
    source_domains = _source_hint_domains(_normalize_source_hints(source_hints or []))
    domain_class = _research_domain_class(category, host)
    base_score = float(item.get("score") or _score_text(query, title, snippet) + float(item.get("credibility") or score_domain_credibility(url)))
    bonus = 0.0
    if any(_host_matches_domain(host, domain) for domain in _RESEARCH_CATEGORY_DOMAINS.get(category, [])):
        bonus += 1.25
    if any(_host_matches_domain(host, domain) for domain in source_domains):
        bonus += 1.5
    if domain_class == "official":
        bonus += 0.9
    elif domain_class in {"reference", "preprint", "publisher", "review", "index"}:
        bonus += 0.65
    elif domain_class == "news":
        bonus += 0.7
    elif domain_class == "wiki":
        bonus += 0.35
    elif domain_class == "distribution":
        bonus += 0.15
    if str(item.get("source_lane") or "").strip() and str(item.get("source_lane") or "") != "base":
        bonus += 0.7
    positive_terms = {
        str(term or "").strip().lower()
        for term in list((disambiguation or {}).get("positive_terms") or [])
        if str(term or "").strip()
    }
    family_terms = {
        str(term or "").strip().lower()
        for term in list((disambiguation or {}).get("family_terms") or [])
        if str(term or "").strip()
    }
    conflict_terms = {
        str(term or "").strip().lower()
        for term in list((disambiguation or {}).get("conflict_terms") or [])
        if str(term or "").strip()
    }
    if family_terms:
        family_matches = len(combined_terms & family_terms)
        if family_matches:
            bonus += 0.2 * float(family_matches)
    if positive_terms:
        positive_matches = len(combined_terms & positive_terms)
        if positive_matches:
            bonus += 0.45 + (0.15 * float(max(0, positive_matches - 1)))
        elif bool((disambiguation or {}).get("ambiguous")):
            bonus -= 0.1
    if conflict_terms:
        conflict_matches = len(combined_terms & conflict_terms)
        if conflict_matches:
            bonus -= min(1.15, 0.45 * float(conflict_matches))
    if _query_wants_freshness(query):
        freshness_text = f"{title} {snippet} {url}"
        if domain_class in {"news", "official", "preprint", "publisher", "review"}:
            bonus += 0.55
        if re.search(r"\b(latest|recent|current|upcoming|news|release|202[4-9])\b", freshness_text, flags=re.IGNORECASE):
            bonus += 0.55
        if re.search(r"\b(201\d|2020|2021|2022)\b", freshness_text):
            bonus -= 0.35
    if category == "anime" and "game" in host and not any(_host_matches_domain(host, domain) for domain in _RESEARCH_CATEGORY_DOMAINS.get(category, [])):
        bonus -= 1.1
    if category == "academic" and not any(_host_matches_domain(host, domain) for domain in _RESEARCH_CATEGORY_DOMAINS.get(category, [])):
        bonus -= 0.5
    return base_score + bonus


def _rerank_research_seed_results(query: str, results: List[Dict[str, Any]], *, category: str, source_hints: Optional[List[str]] = None, disambiguation: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    merged: Dict[str, Dict[str, Any]] = {}
    for item in list(results or []):
        url = str(item.get("url") or "").strip()
        canonical = _canonicalize_search_url(url)
        if not canonical or not _is_content_candidate_url(url):
            continue
        current = dict(item or {})
        current["research_score"] = _research_result_priority(query, current, category=category, source_hints=source_hints, disambiguation=disambiguation)
        existing = merged.get(canonical)
        if existing is None or float(current.get("research_score") or 0.0) > float(existing.get("research_score") or 0.0):
            merged[canonical] = current
    ranked = list(merged.values())
    ranked.sort(key=lambda item: (-float(item.get("research_score") or 0.0), -float(item.get("score") or 0.0), str(item.get("title") or "")))
    return ranked


async def _enhanced_research_seed_results(query: str, max_results: int, *, category: str = "auto", source_hints: Optional[List[str]] = None) -> Tuple[str, List[Dict[str, Any]], Dict[str, Any]]:
    base_backend, base_results, base_debug = await _seed_search_results_debug(query, max_results)
    resolved_category, category_debug = _infer_research_category(query, base_results, category=category, source_hints=source_hints)
    disambiguation = _infer_research_disambiguation(query, base_results, category=resolved_category, source_hints=source_hints)
    refined_query = _build_research_disambiguation_query(query, disambiguation)
    lanes = _build_research_source_lanes(query, resolved_category, source_hints=source_hints, max_lanes=min(5, max_results), disambiguation=disambiguation)
    refined_results: List[Dict[str, Any]] = []
    refined_debug: Dict[str, Any] = dict(disambiguation or {})
    refined_needed = bool(refined_query and refined_query.strip().lower() != str(query or "").strip().lower())
    lane_results: List[Dict[str, Any]] = []
    lane_debug: List[Dict[str, Any]] = []
    task_specs: List[Dict[str, Any]] = []
    if refined_needed:
        task_specs.append({"kind": "disambiguation", "query": refined_query})
    task_specs.extend([{"kind": "lane", **lane} for lane in lanes])
    tasks = [
        _seed_search_results_debug(
            str(spec.get("query") or ""),
            max(3, min(5, max_results)) if str(spec.get("kind") or "") == "disambiguation" else min(3, max_results),
        )
        for spec in task_specs
    ]
    outputs = await asyncio.gather(*tasks, return_exceptions=True) if tasks else []
    for spec, output in zip(task_specs, outputs):
        if str(spec.get("kind") or "") == "disambiguation":
            if isinstance(output, Exception):
                refined_debug.update({"query": refined_query, "status": "error", "detail": str(output)[:200], "result_count": 0})
                continue
            refined_backend, items, debug = output
            refined_results = [dict(item or {}, source_lane="disambiguation", lane_backend=refined_backend) for item in list(items or [])]
            refined_debug.update({
                "query": refined_query,
                "backend": refined_backend,
                "status": "ok" if refined_results else "empty",
                "result_count": len(refined_results),
                "debug": debug,
            })
            continue
        lane = spec
        if isinstance(output, Exception):
            lane_debug.append({"label": str(lane.get("label") or ""), "domain": str(lane.get("domain") or ""), "query": str(lane.get("query") or ""), "status": "error", "detail": str(output)[:200]})
            continue
        lane_backend, items, debug = output
        filtered: List[Dict[str, Any]] = []
        for item in list(items or []):
            host = _host(item.get("url") or "")
            expected_domain = str(lane.get("domain") or "")
            if expected_domain and not _host_matches_domain(host, expected_domain):
                continue
            filtered.append(dict(item or {}, source_lane=str(lane.get("label") or expected_domain), source_domain=expected_domain, lane_backend=lane_backend))
        lane_results.extend(filtered)
        lane_debug.append({
            "label": str(lane.get("label") or ""),
            "domain": str(lane.get("domain") or ""),
            "query": str(lane.get("query") or ""),
            "backend": lane_backend,
            "status": "ok" if filtered else "empty",
            "result_count": len(filtered),
            "debug": debug,
        })
    if not refined_needed:
        refined_debug.update({"query": "", "status": "not_needed", "result_count": 0})
    combined = [dict(item or {}, source_lane="base") for item in list(base_results or [])] + refined_results + lane_results
    reranked = _rerank_research_seed_results(query, combined, category=resolved_category, source_hints=source_hints, disambiguation=disambiguation)
    backend_label = base_backend if not lane_debug else f"enhanced:{resolved_category}:{base_backend}"
    return backend_label, reranked[:max_results], {
        "category": resolved_category,
        "category_debug": category_debug,
        "disambiguation": refined_debug,
        "base": base_debug,
        "lanes": lane_debug,
    }


def _merge_seed_search_results(query: str, max_results: int, result_sets: List[Tuple[str, List[Dict[str, Any]]]]) -> Tuple[str, List[Dict[str, Any]]]:
    merged: Dict[str, Dict[str, Any]] = {}
    active_backends: List[str] = []
    for backend, items in result_sets:
        cleaned_items = list(items or [])
        if not cleaned_items:
            continue
        active_backends.append(str(backend or ""))
        for idx, item in enumerate(cleaned_items):
            raw_url = str((item or {}).get("url") or "").strip()
            canonical = _canonicalize_search_url(raw_url)
            if not canonical:
                continue
            title = _clean_text(str((item or {}).get("title") or raw_url))
            snippet = _clean_text(str((item or {}).get("snippet") or (item or {}).get("content") or ""))[:280]
            current = merged.get(canonical)
            if current is None:
                merged[canonical] = {
                    "title": title,
                    "url": raw_url or canonical,
                    "snippet": snippet,
                    "credibility": float((item or {}).get("credibility", score_domain_credibility(raw_url or canonical))),
                    "backends": {str(backend or "")},
                    "best_position": idx,
                }
                continue
            current["backends"].add(str(backend or ""))
            current["best_position"] = min(int(current.get("best_position", idx)), idx)
            if _score_text(query, title, snippet) > _score_text(query, str(current.get("title") or ""), str(current.get("snippet") or "")):
                current["title"] = title or str(current.get("title") or "")
                current["snippet"] = snippet or str(current.get("snippet") or "")
                current["url"] = raw_url or str(current.get("url") or canonical)
            current["credibility"] = max(float(current.get("credibility") or 0.0), float((item or {}).get("credibility", score_domain_credibility(raw_url or canonical))))

    ranked: List[Dict[str, Any]] = []
    for item in merged.values():
        title = str(item.get("title") or item.get("url") or "")
        snippet = str(item.get("snippet") or "")
        backend_names = sorted(str(name) for name in (item.get("backends") or set()) if str(name or ""))
        backend_votes = len(backend_names)
        backend_weight = sum(_search_backend_weight(name) for name in backend_names)
        best_position = int(item.get("best_position") or 0)
        score = _score_text(query, title, snippet) + float(item.get("credibility") or 0.0) + float(backend_weight) + (0.35 * float(backend_votes)) + max(0.0, 0.6 - (0.08 * float(best_position)))
        ranked.append({
            "title": title,
            "url": str(item.get("url") or ""),
            "snippet": _guard_web_text(snippet, max_chars=280),
            "credibility": float(item.get("credibility") or 0.0),
            "backend_votes": backend_votes,
            "backend_weight": float(backend_weight),
            "backends": backend_names,
            "score": score,
        })
    ranked.sort(key=lambda item: (-float(item.get("score", 0.0)), str(item.get("title", ""))))
    backend_label = active_backends[0] if len(active_backends) == 1 else f"hybrid:{'+'.join(active_backends)}"
    return backend_label or "none", ranked[:max(1, int(max_results or 1))]


def _merge_seed_search_results_with_debug(query: str, max_results: int, result_sets: List[Tuple[str, List[Dict[str, Any]]]], telemetry: Dict[str, Any]) -> Tuple[str, List[Dict[str, Any]]]:
    for backend, items in result_sets:
        _search_telemetry_note(
            telemetry,
            backend,
            status="ok" if list(items or []) else "empty",
            result_count=len(list(items or [])),
            weight=_search_backend_weight(backend),
        )
    backend, ranked = _merge_seed_search_results(query, max_results, result_sets)
    telemetry["selected_backend"] = backend
    telemetry["ranked_results"] = len(ranked)
    telemetry["browser_state"] = _search_telemetry_browser_state()
    return backend, ranked


async def _http_get(url: str) -> Tuple[int, str, str]:
    async with httpx.AsyncClient(timeout=_TIMEOUT_S, follow_redirects=True, http2=True, headers={"User-Agent": "Blackboard/1.0 (research assistant)"}) as client:
        response = await client.get(url)
        return response.status_code, response.text, str(response.headers.get("content-type", "") or "")


def _decode_ddg_url(url: str) -> str:
    value = str(url or "").strip()
    if "uddg=" in value:
        try:
            parsed = urllib.parse.parse_qs(urllib.parse.urlparse(value).query)
            decoded = parsed.get("uddg", [value])[0]
            return urllib.parse.unquote(decoded)
        except Exception:
            return value
    return value


async def _search_ddgs_results(query: str, max_results: int) -> List[Dict[str, Any]]:
    if not _HAS_DDGS or DDGS is None:
        return []

    def _run() -> List[Dict[str, Any]]:
        for backend in ("html", "lite"):
            try:
                client = DDGS(timeout=15)
                raw = client.text(query, max_results=max_results, backend=backend)
                if not raw:
                    continue
                results: List[Dict[str, Any]] = []
                for item in list(raw)[:max_results]:
                    url = _decode_ddg_url(item.get("href", ""))
                    title = _clean_text(item.get("title", ""))
                    snippet = _clean_text(item.get("body", ""))
                    if not title or not url:
                        continue
                    results.append({
                        "title": title,
                        "url": url,
                        "snippet": snippet[:280],
                    })
                filtered = filter_search_results(results)
                if filtered:
                    return filtered
            except Exception:
                continue
        return []

    return await asyncio.to_thread(_run)


async def _search_bing_results(query: str, max_results: int) -> List[Dict[str, Any]]:
    async with httpx.AsyncClient(
        timeout=_TIMEOUT_S,
        follow_redirects=True,
        http2=True,
        headers=_search_headers(referer="https://www.bing.com/"),
    ) as client:
        try:
            response = await client.get("https://www.bing.com/search", params={"q": query, "count": max(10, int(max_results or 1))})
        except Exception:
            return []
    if int(response.status_code) >= 400:
        return []
    return _extract_bing_html_results(response.text, max_results)


async def _search_yahoo_results(query: str, max_results: int) -> List[Dict[str, Any]]:
    async with httpx.AsyncClient(
        timeout=_TIMEOUT_S,
        follow_redirects=True,
        http2=True,
        headers=_search_headers(referer="https://search.yahoo.com/"),
    ) as client:
        try:
            response = await client.get("https://search.yahoo.com/search", params={"p": query, "n": max(10, int(max_results or 1))})
        except Exception:
            return []
    if int(response.status_code) >= 400:
        return []
    return _extract_yahoo_html_results(response.text, max_results)


async def _search_html_results(query: str, max_results: int) -> List[Dict[str, Any]]:
    async with httpx.AsyncClient(
        timeout=_TIMEOUT_S,
        follow_redirects=True,
        http2=True,
        headers={"User-Agent": "Blackboard/1.0 (research assistant)"},
    ) as client:
        response = await client.post("https://html.duckduckgo.com/html/", data={"q": query})
        status = int(response.status_code)
        text = response.text
    if status >= 400:
        return []
    pattern = re.compile(
        r'<a[^>]+class="result__a"[^>]+href="(?P<url>[^"]+)"[^>]*>(?P<title>.*?)</a>(?P<tail>.*?)(?=<a[^>]+class="result__a"|$)',
        re.DOTALL | re.IGNORECASE,
    )
    results: List[Dict[str, Any]] = []
    for match in pattern.finditer(text):
        url = _decode_ddg_url(re.sub(r"\s+", " ", match.group("url")).strip())
        title = _strip_html(match.group("title"))
        tail = match.group("tail")
        snippet_match = re.search(r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>', tail, flags=re.DOTALL | re.IGNORECASE)
        snippet = _strip_html(snippet_match.group(1) if snippet_match else tail)
        if not url or "duckduckgo.com" in url:
            continue
        results.append({"title": title, "url": url, "snippet": snippet[:280]})
        if len(results) >= max_results:
            break
    return filter_search_results(results)


async def _search_lite_results(query: str, max_results: int) -> List[Dict[str, Any]]:
    async with httpx.AsyncClient(
        timeout=_TIMEOUT_S,
        follow_redirects=True,
        http2=True,
        headers={"User-Agent": "Blackboard/1.0 (research assistant)"},
    ) as client:
        response = await client.post("https://lite.duckduckgo.com/lite/", data={"q": query})
        status = int(response.status_code)
        text = response.text
    if status >= 400:
        return []
    links = re.findall(
        r'<a[^>]*class="result-link"[^>]*href="([^"]+)"[^>]*>(.*?)</a>',
        text,
        re.DOTALL | re.IGNORECASE,
    )
    snippets = re.findall(
        r'<td[^>]*class="result-snippet"[^>]*>(.*?)</td>',
        text,
        re.DOTALL | re.IGNORECASE,
    )
    results: List[Dict[str, Any]] = []
    for idx, (raw_url, raw_title) in enumerate(links[:max_results]):
        url = _decode_ddg_url(raw_url)
        title = _strip_html(raw_title)
        snippet = _strip_html(snippets[idx]) if idx < len(snippets) else ""
        if not title or not url:
            continue
        results.append({"title": title, "url": url, "snippet": snippet[:280]})
    return filter_search_results(results)


def _search_browser_serp_sync_blocking(query: str, max_results: int) -> Dict[str, List[Dict[str, Any]]]:
    sync_playwright = _playwright_sync_impl()
    if sync_playwright is None:
        return {}
    engines = _browser_serp_engines()
    if not engines:
        return {}
    discovered: Dict[str, List[Dict[str, Any]]] = {}
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
            )
            try:
                context = browser.new_context(
                    viewport={"width": 1366, "height": 900},
                    user_agent=_search_headers()["User-Agent"],
                    locale="en-US",
                    timezone_id="America/New_York",
                    ignore_https_errors=True,
                )
                context.add_init_script(_PLAYWRIGHT_STEALTH_SCRIPT)
                for engine in engines:
                    allowed, reason = _browser_serp_claim_attempt(engine)
                    if not allowed:
                        continue
                    page = context.new_page()
                    try:
                        target_url = _browser_serp_search_url(engine, query)
                        if not target_url:
                            continue
                        page.goto(target_url, timeout=int(_TIMEOUT_S * 1000), wait_until="domcontentloaded")
                        page.wait_for_timeout(1500)
                        page_title = str(page.title() or "")
                        page_html = str(page.content() or "")
                        challenge_reason = _detect_search_challenge(engine, page_html, page_title=page_title, final_url=str(page.url or target_url))
                        if challenge_reason:
                            _browser_serp_mark_failure(engine, challenge=True, detail=challenge_reason)
                            continue
                        parsed = _extract_engine_html_results(engine, page_html, max_results)
                        if parsed:
                            discovered[f"browser_{engine}"] = parsed
                        _browser_serp_mark_success(engine, len(parsed))
                    except Exception as exc:
                        _browser_serp_mark_failure(engine, challenge=False, detail=str(exc))
                        continue
                    finally:
                        try:
                            page.close()
                        except Exception:
                            pass
            finally:
                browser.close()
    except Exception:
        return {}
    return discovered


async def _search_browser_serp_results(query: str, max_results: int) -> Dict[str, List[Dict[str, Any]]]:
    if not _browser_serp_enabled():
        return {}
    return await asyncio.to_thread(_search_browser_serp_sync_blocking, query, max_results)


async def _seed_search_results_debug(query: str, max_results: int) -> Tuple[str, List[Dict[str, Any]], Dict[str, Any]]:
    requested = max(1, int(max_results or 1))
    expanded = min(max(4, requested + 3), 12)
    telemetry = _search_telemetry_bucket(query, requested, expanded)
    browser_sources: Dict[str, List[Dict[str, Any]]] = {}
    if _browser_serp_enabled():
        try:
            browser_sources = await _search_browser_serp_results(query, expanded)
        except Exception as exc:
            browser_sources = {}
            _search_telemetry_note(telemetry, "browser_serp", status="error", detail=str(exc))
    results = await asyncio.gather(
        _search_google_results(query, expanded),
        _search_searxng_results(query, expanded),
        _search_bing_results(query, expanded),
        _search_yahoo_results(query, expanded),
        _search_ddgs_results(query, expanded),
        _search_html_results(query, expanded),
        _search_lite_results(query, expanded),
        return_exceptions=True,
    )
    sources: List[Tuple[str, List[Dict[str, Any]]]] = []
    for backend, payload in zip(
        ["google_cse", "searxng", "bing_html", "yahoo_html", "duckduckgo_ddgs", "duckduckgo_html", "duckduckgo_lite"],
        results,
    ):
        if isinstance(payload, Exception):
            _search_telemetry_note(telemetry, backend, status="error", detail=str(payload), weight=_search_backend_weight(backend))
            continue
        sources.append((backend, [_sanitize_search_result_item(item) for item in list(payload or [])]))
    for backend, payload in browser_sources.items():
        sources.append((backend, [_sanitize_search_result_item(item) for item in list(payload or [])]))
    backend, merged = _merge_seed_search_results_with_debug(query, requested, sources, telemetry)
    if merged:
        return backend, merged, telemetry
    fallback_backend, fallback_results = await _search_results(query, requested)
    _search_telemetry_note(telemetry, fallback_backend, status="fallback", result_count=len(fallback_results), weight=_search_backend_weight(fallback_backend))
    telemetry["selected_backend"] = fallback_backend
    telemetry["browser_state"] = _search_telemetry_browser_state()
    return fallback_backend, fallback_results, telemetry


async def _search_results(query: str, max_results: int) -> Tuple[str, List[Dict[str, Any]]]:
    cache_key = _search_cache_key(query, max_results)
    cached = _SEARCH_CACHE.get(cache_key)
    if cached is not None:
        backend = str(cached[0].get("_backend", "duckduckgo_ddgs") if cached else "duckduckgo_ddgs")
        replay = [_sanitize_search_result_item({k: v for k, v in item.items() if k != "_backend"}) for item in cached]
        _record_search_history(query, max_results, backend, len(replay), True)
        return backend, replay

    backend = "duckduckgo_ddgs"
    results = await _search_ddgs_results(query, max_results)
    if not results:
        backend = "duckduckgo_html"
        results = await _search_html_results(query, max_results)
    if not results:
        backend = "duckduckgo_lite"
        results = await _search_lite_results(query, max_results)

    results = [_sanitize_search_result_item(item) for item in results]

    cached_results = [dict(item, _backend=backend) for item in results]
    _SEARCH_CACHE[cache_key] = cached_results
    _record_search_history(query, max_results, backend, len(results), False)
    return backend, results


async def _search_google_results(query: str, max_results: int) -> List[Dict[str, Any]]:
    api_key, cx = _google_credentials()
    if not api_key or not cx:
        return []
    results: List[Dict[str, Any]] = []
    start = 1
    async with httpx.AsyncClient(
        timeout=_TIMEOUT_S,
        follow_redirects=True,
        http2=True,
        headers={"User-Agent": "Blackboard/1.0 (research assistant)"},
    ) as client:
        while len(results) < max_results and start <= 91:
            batch_size = max(1, min(10, max_results - len(results)))
            response = await client.get(
                "https://customsearch.googleapis.com/customsearch/v1",
                params={"key": api_key, "cx": cx, "q": query, "num": batch_size, "start": start},
            )
            if int(response.status_code) >= 400:
                return []
            payload = response.json()
            items = list(payload.get("items") or [])
            for item in items:
                url = str(item.get("link") or "").strip()
                title = _clean_text(item.get("title") or "")
                snippet = _clean_text(item.get("snippet") or "")
                if not url or not title:
                    continue
                results.append({"title": title, "url": url, "snippet": snippet[:280]})
                if len(results) >= max_results:
                    break
            next_page = (((payload.get("queries") or {}).get("nextPage") or [{}])[0].get("startIndex"))
            if not items or not next_page:
                break
            start = int(next_page)
    return filter_search_results(results)


async def _search_searxng_results(query: str, max_results: int) -> List[Dict[str, Any]]:
    base = _searxng_base()
    if not base:
        return []
    params: Dict[str, Any] = {
        "q": query,
        "format": "json",
        "pageno": 1,
        "language": str(os.environ.get("SEARXNG_LANGUAGE") or "en-US").strip() or "en-US",
    }
    engines = str(os.environ.get("SEARXNG_ENGINES") or "").strip()
    if engines:
        params["engines"] = engines
    async with httpx.AsyncClient(
        timeout=_TIMEOUT_S,
        follow_redirects=True,
        http2=True,
        headers={"User-Agent": "Blackboard/1.0 (research assistant)"},
    ) as client:
        try:
            response = await client.get(f"{base}/search", params=params)
        except Exception:
            return []
    if int(response.status_code) >= 400:
        return []
    try:
        payload = response.json()
    except Exception:
        return []
    results: List[Dict[str, Any]] = []
    for item in list(payload.get("results") or [])[:max(1, int(max_results or 1)) * 2]:
        url = str(item.get("url") or "").strip()
        title = _clean_text(str(item.get("title") or ""))
        snippet = _clean_text(str(item.get("content") or item.get("snippet") or ""))
        if not url or not title:
            continue
        results.append({"title": title, "url": url, "snippet": snippet[:280]})
    return filter_search_results(results)


async def _seed_search_results(query: str, max_results: int) -> Tuple[str, List[Dict[str, Any]]]:
    backend, results, _telemetry = await _seed_search_results_debug(query, max_results)
    return backend, results


async def _crawl4ai_extract(url: str, max_chars: int, user_query: str = "") -> Tuple[str, str]:
    try:
        from crawl4ai import AsyncWebCrawler, CacheMode, CrawlerRunConfig
    except Exception:
        return "", ""
    try:
        config_kwargs: Dict[str, Any] = {"cache_mode": CacheMode.BYPASS, "word_count_threshold": 20}
        try:
            from crawl4ai.content_filter_strategy import BM25ContentFilter, PruningContentFilter
            from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator
            if str(user_query or "").strip():
                content_filter = BM25ContentFilter(user_query=str(user_query).strip(), bm25_threshold=1.2, language="english")
            else:
                content_filter = PruningContentFilter(threshold=0.5, threshold_type="dynamic", min_word_threshold=40)
            config_kwargs["markdown_generator"] = DefaultMarkdownGenerator(content_filter=content_filter, options={"ignore_links": True})
        except Exception:
            pass
        config = CrawlerRunConfig(**config_kwargs)
        async with AsyncWebCrawler() as crawler:
            result = await crawler.arun(url=url, config=config)
    except Exception:
        return "", ""
    if not getattr(result, "success", False):
        return "", ""
    content, title = _crawl4ai_content(result)
    if not content:
        return "", ""
    truncated, _ = _truncate(content, max_chars)
    return truncated, title


def _playwright_impl():
    try:
        from patchright.async_api import async_playwright
        return async_playwright
    except Exception:
        try:
            from playwright.async_api import async_playwright
            return async_playwright
        except Exception:
            return None


def _playwright_sync_impl():
    try:
        from patchright.sync_api import sync_playwright
        return sync_playwright
    except Exception:
        try:
            from playwright.sync_api import sync_playwright
            return sync_playwright
        except Exception:
            return None


def _playwright_extract_sync_blocking(url: str, max_chars: int) -> Tuple[str, str]:
    sync_playwright = _playwright_sync_impl()
    if sync_playwright is None:
        return "", ""
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
            )
            try:
                context = browser.new_context(
                    viewport={"width": 1280, "height": 800},
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/131.0.0.0 Safari/537.36"
                    ),
                    locale="en-US",
                    timezone_id="America/New_York",
                    ignore_https_errors=True,
                )
                context.add_init_script(_PLAYWRIGHT_STEALTH_SCRIPT)

                def _route_request(route) -> None:
                    request = route.request
                    if should_block_resource(request.url, request.resource_type):
                        route.abort()
                        return
                    route.fallback()

                context.route("**/*", _route_request)
                page = context.new_page()
                page.goto(url, timeout=int(_TIMEOUT_S * 1000), wait_until="networkidle")
                page.wait_for_timeout(1200)
                title = str(page.title() or "")
                text = str(page.evaluate("() => document.body ? document.body.innerText : ''") or "")
                if not text.strip():
                    text = _strip_html(page.content())
            finally:
                browser.close()
    except Exception:
        return "", ""
    content = _clean_text(text)
    if not content:
        return "", ""
    truncated, _ = _truncate(content, max_chars)
    return truncated, title


async def _playwright_extract_sync(url: str, max_chars: int) -> Tuple[str, str]:
    return await asyncio.to_thread(_playwright_extract_sync_blocking, url, max_chars)


async def _playwright_extract(url: str, max_chars: int) -> Tuple[str, str]:
    if sys.platform == "win32":
        return await _playwright_extract_sync(url, max_chars)
    async_playwright = _playwright_impl()
    if async_playwright is None:
        return "", ""
    try:
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )
        try:
            context = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
                locale="en-US",
                timezone_id="America/New_York",
                ignore_https_errors=True,
            )
            await context.add_init_script(_PLAYWRIGHT_STEALTH_SCRIPT)
            async def _route_request(route) -> None:
                request = route.request
                if should_block_resource(request.url, request.resource_type):
                    await route.abort()
                    return
                await route.fallback()
            await context.route("**/*", _route_request)
            page = await context.new_page()
            await page.goto(url, timeout=int(_TIMEOUT_S * 1000), wait_until="networkidle")
            await page.wait_for_timeout(1200)
            title = await page.title()
            text = await page.evaluate("() => document.body ? document.body.innerText : ''")
            if not str(text or "").strip():
                text = _strip_html(await page.content())
        finally:
            try:
                await browser.close()
            except Exception:
                pass
            try:
                await playwright.stop()
            except Exception:
                pass
    except Exception:
        return "", ""
    content = _clean_text(str(text or ""))
    if not content:
        return "", ""
    truncated, _ = _truncate(content, max_chars)
    return truncated, str(title or "")


def _is_content_candidate_url(url: str) -> bool:
    value = str(url or "").strip()
    lowered = value.lower()
    if not value or _BINARY_URL_RE.search(value):
        return False
    if any(token in lowered for token in ("/privacy", "/terms", "/cookie", "/cookies", "/login", "/signin", "/signup", "/register", "/account", "/tag/", "/category/", "/author/", "/feed", "/search?", "?share=", "&share=")):
        return False
    return lowered.startswith(("http://", "https://"))


def _query_overlap(text: str, query: str) -> int:
    return len(_query_terms(query) & set(re.findall(r"\b[a-z0-9]{3,}\b", str(text or "").lower())))


def _distill_research_text(query: str, text: str, *, max_chars: int) -> str:
    value = _clean_text(text)
    if not value:
        return ""
    raw_chunks = [chunk.strip() for chunk in re.split(r"\n\s*\n+", value) if str(chunk or "").strip()]
    scored: List[Tuple[float, int, str]] = []
    seen = set()
    for idx, chunk in enumerate(raw_chunks):
        compact = re.sub(r"\s+", " ", chunk).strip()
        normalized = compact.lower()
        if not compact or normalized in seen:
            continue
        seen.add(normalized)
        if len(compact) < 50 or _LOW_VALUE_RE.search(compact):
            continue
        overlap = _query_overlap(compact, query)
        score = (2.0 * float(overlap)) + min(len(compact), 1200) / 1200.0
        if overlap <= 0 and len(compact) < 140:
            continue
        scored.append((score, idx, compact))
    if not scored:
        fallback, _ = _truncate(value, max_chars)
        return fallback
    chosen = sorted(sorted(scored, key=lambda item: (-item[0], item[1]))[:6], key=lambda item: item[1])
    joined = "\n\n".join(item[2] for item in chosen)
    distilled, _ = _truncate(joined, max_chars)
    return distilled


def _domain_candidates(results: List[Dict[str, Any]], limit: int) -> List[str]:
    domains: List[str] = []
    seen = set()
    for item in results:
        domain = _host(item.get("url") or "")
        if not domain or domain in seen:
            continue
        seen.add(domain)
        domains.append(domain)
        if len(domains) >= limit:
            break
    return domains


async def _seed_domain_urls(domains: List[str], query: str, *, max_urls_per_domain: int) -> Tuple[str, Dict[str, List[Dict[str, Any]]]]:
    if not domains:
        return "none", {}
    try:
        from crawl4ai import AsyncUrlSeeder, SeedingConfig
    except Exception:
        return "none", {}
    try:
        config = SeedingConfig(
            source="sitemap+cc",
            extract_head=True,
            query=query,
            scoring_method="bm25",
            score_threshold=0.35,
            max_urls=max(1, int(max_urls_per_domain)),
            concurrency=max(4, min(20, len(domains) * 3)),
            filter_nonsense_urls=True,
        )
        async with AsyncUrlSeeder() as seeder:
            results = await seeder.many_urls(domains, config)
    except Exception:
        return "none", {}
    normalized: Dict[str, List[Dict[str, Any]]] = {}
    for domain, items in dict(results or {}).items():
        cleaned: List[Dict[str, Any]] = []
        for item in list(items or []):
            url = str((item or {}).get("url") or "").strip()
            if not _is_content_candidate_url(url):
                continue
            if _host(url) != str(domain or "").strip().lower():
                continue
            cleaned.append(dict(item or {}))
        if cleaned:
            normalized[str(domain)] = cleaned
    return "crawl4ai_url_seeding", normalized


async def _crawl4ai_batch_extract(urls: List[str], *, query: str, max_chars: int) -> Dict[str, Dict[str, str]]:
    if not urls:
        return {}
    try:
        from crawl4ai import AsyncWebCrawler, CacheMode, CrawlerRunConfig
        from crawl4ai.content_filter_strategy import BM25ContentFilter
        from crawl4ai.markdown_generation_strategy import DefaultMarkdownGenerator
    except Exception:
        return {}
    try:
        config = CrawlerRunConfig(
            cache_mode=CacheMode.BYPASS,
            word_count_threshold=20,
            markdown_generator=DefaultMarkdownGenerator(
                content_filter=BM25ContentFilter(user_query=str(query or "").strip(), bm25_threshold=1.2, language="english"),
                options={"ignore_links": True},
            ),
            stream=False,
        )
        async with AsyncWebCrawler() as crawler:
            results = await crawler.arun_many(urls=urls, config=config)
    except Exception:
        return {}
    extracted: Dict[str, Dict[str, str]] = {}
    for result in list(results or []):
        if not getattr(result, "success", False):
            continue
        content, title = _crawl4ai_content(result)
        if not content:
            continue
        distilled = _distill_research_text(query, content, max_chars=max_chars)
        if not distilled:
            continue
        extracted[str(getattr(result, "url", "") or "")] = {
            "title": title,
            "content": distilled,
            "backend": "crawl4ai_batch",
        }
    return extracted


def _compile_research_markdown(query: str, sources: List[Dict[str, Any]], *, max_chars: int) -> str:
    parts = ["# Research digest", "", f"Query: {query}"]
    for idx, item in enumerate(sources, start=1):
        parts.extend([
            "",
            f"## {idx}. {str(item.get('title') or item.get('url') or 'Untitled source').strip()}",
            f"- URL: {str(item.get('url') or '').strip()}",
            f"- Domain: {str(item.get('domain') or '').strip()}",
            f"- Discovery: {str(item.get('discovery') or '').strip()}",
            f"- Backend: {str(item.get('backend') or '').strip()}",
            "",
            str(item.get('content') or '').strip(),
        ])
    rendered = _clean_text("\n".join(parts))
    truncated, _ = _truncate(rendered, max_chars)
    return truncated


async def _fetch_url_content(url: str, max_chars: int, user_query: str = "") -> Tuple[str, str]:
    extracted, _title = await _crawl4ai_extract(url, max_chars, user_query=user_query)
    if extracted:
        return "crawl4ai", _guard_web_text(extracted, max_chars=max_chars)
    status, text, content_type = await _http_get(url)
    if status < 400:
        if "text/html" in content_type.lower():
            cleaned = _strip_html(text)
        else:
            cleaned = _clean_text(text)
        if len(str(cleaned or "").strip()) >= 200:
            content, _ = _truncate(cleaned, max_chars)
            return "httpx", _guard_web_text(content, max_chars=max_chars)
    browser_text, _browser_title = await _playwright_extract(url, max_chars)
    if browser_text:
        return "playwright", _guard_web_text(browser_text, max_chars=max_chars)
    if status >= 400:
        raise RuntimeError(f"http error {status}")
    content, _ = _truncate(_clean_text(text), max_chars)
    return "httpx", _guard_web_text(content, max_chars=max_chars)


async def _fetch_url(args: Dict[str, Any]) -> str:
    url = str(args.get("url") or "").strip()
    max_chars = max(500, min(int(args.get("max_chars") or _MAX_CONTENT), 20000))
    if not url:
        return json.dumps({"error": "url required"})
    if not url.startswith(("http://", "https://")):
        return json.dumps({"error": "url must start with http:// or https://", "url": url})
    try:
        backend, content = await _fetch_url_content(url, max_chars)
    except Exception as exc:
        return json.dumps({"url": url, "error": str(exc), "content": ""})
    return json.dumps({"url": url, "backend": backend, "content": content}, default=str)


async def _web_search(args: Dict[str, Any]) -> str:
    raw_query = str(args.get("query") or "").strip()
    max_results = max(1, min(int(args.get("max_results") or 8), 10))
    debug = bool(args.get("debug") or False)
    if not raw_query:
        return json.dumps({"error": "query required"})
    query = _distill_search_query(raw_query)
    variants = _fan_out_queries(query, max_variants=1)
    if debug:
        backend, results, telemetry = await _seed_search_results_debug(query, max_results)
        if len(variants) > 1:
            for variant in variants[1:]:
                _vb, extra, _vt = await _seed_search_results_debug(variant, max(3, max_results // 2))
                results.extend(extra)
            telemetry["fan_out_variants"] = variants
        telemetry["raw_query"] = raw_query
        telemetry["distilled_query"] = query
        return json.dumps({"query": query, "backend": backend, "results": results, "debug": telemetry}, default=str)
    all_results: List[Dict[str, Any]] = []
    for idx, variant in enumerate(variants):
        vb, vr = await _search_results(variant, max_results if idx == 0 else max(3, max_results // 2))
        if idx == 0:
            backend = vb
        all_results.extend(vr)
    if len(variants) > 1:
        _, merged = _merge_seed_search_results(query, max_results, [(backend, all_results)])
        all_results = merged
    else:
        all_results = all_results[:max_results]
    return json.dumps({"query": query, "backend": backend, "results": all_results}, default=str)


async def _web_news(args: Dict[str, Any]) -> str:
    raw_query = str(args.get("query") or "").strip()
    max_results = max(1, min(int(args.get("max_results") or 8), 10))
    timelimit = str(args.get("timelimit") or "w").strip() or "w"
    debug = bool(args.get("debug") or False)
    if not raw_query:
        return json.dumps({"error": "query required"})
    query = _distill_search_query(raw_query)
    suffix = {"d": " today", "w": " this week", "m": " this month"}.get(timelimit, "")
    news_query = f"{query} news{suffix}"
    if debug:
        backend, results, telemetry = await _seed_search_results_debug(news_query, max_results)
        telemetry["raw_query"] = raw_query
        telemetry["distilled_query"] = query
        return json.dumps({"query": query, "timelimit": timelimit, "backend": backend, "results": results, "debug": telemetry}, default=str)
    backend, results = await _search_results(news_query, max_results)
    return json.dumps({"query": query, "timelimit": timelimit, "backend": backend, "results": results}, default=str)


async def _targeted_search(args: Dict[str, Any], *, prefix: str = "", suffix: str = "") -> str:
    raw_query = str(args.get("query") or "").strip()
    library = str(args.get("library") or "").strip()
    language = str(args.get("language") or "").strip()
    max_results = max(1, min(int(args.get("max_results") or 8), 10))
    if not raw_query:
        return json.dumps({"error": "query required"})
    query = _distill_search_query(raw_query)
    parts: List[str] = []
    if prefix:
        parts.append(prefix)
    if library:
        parts.append(library)
    parts.append(query)
    if language:
        parts.append(language)
    if suffix:
        parts.append(suffix)
    final_query = " ".join(part for part in parts if str(part or "").strip())
    backend, results = await _search_results(final_query, max_results)
    return json.dumps({
        "query": query,
        "search_query": final_query,
        "backend": backend,
        "results": results,
    }, default=str)


async def _search_github(args: Dict[str, Any]) -> str:
    query = str(args.get("query") or "").strip()
    language = str(args.get("language") or "").strip()
    max_results = int(args.get("max_results") or 8)
    return await _targeted_search({"query": query, "max_results": max_results}, prefix="site:github.com", suffix=f"language:{language}" if language else "")


async def _search_stackoverflow(args: Dict[str, Any]) -> str:
    return await _targeted_search(args, prefix="site:stackoverflow.com")


async def _search_documentation(args: Dict[str, Any]) -> str:
    return await _targeted_search(args, suffix="documentation")


async def _search_tutorials(args: Dict[str, Any]) -> str:
    return await _targeted_search(args, suffix="tutorial")


async def _web_research(args: Dict[str, Any]) -> str:
    raw_query = str(args.get("query") or "").strip()
    max_sources = max(1, min(int(args.get("max_sources") or 3), 5))
    max_chars = max(1000, min(int(args.get("max_chars") or _MAX_CONTENT), 20000))
    debug = bool(args.get("debug") or False)
    category = str(args.get("category") or "auto").strip()
    source_hints = _normalize_source_hints(args.get("source_hints") or [])
    if not raw_query:
        return json.dumps({"error": "query required"})
    query = _distill_search_query(raw_query)
    search_backend, results, search_debug = await _enhanced_research_seed_results(query, max(max_sources + 3, 6), category=category, source_hints=source_hints)
    per_source_chars = max(500, min(1600, int(max_chars / max(1, max_sources))))
    domains = _domain_candidates(results, max_sources)
    seed_backend, discovered = await _seed_domain_urls(domains, query, max_urls_per_domain=max(2, min(5, max_sources + 1)))

    candidates: List[Dict[str, Any]] = []
    seen_urls = set()
    for item in results:
        url = str(item.get("url", "") or "").strip()
        if not _is_content_candidate_url(url) or url in seen_urls:
            continue
        seen_urls.add(url)
        candidates.append({
            "url": url,
            "title": str(item.get("title", "") or url),
            "snippet": _guard_web_text(str(item.get("snippet", "") or ""), max_chars=280),
            "credibility": float(item.get("credibility", score_domain_credibility(url))),
            "discovery": "google" if search_backend == "google_cse" else "search",
            "seed_score": 1.0,
            "research_score": float(item.get("research_score") or 0.0),
            "source_lane": str(item.get("source_lane") or "base"),
            "domain": _host(url),
        })
        if len(candidates) >= max_sources * 2:
            break
    for domain in domains:
        for item in list(discovered.get(domain) or []):
            url = str(item.get("url") or "").strip()
            if not _is_content_candidate_url(url) or url in seen_urls:
                continue
            seen_urls.add(url)
            head_data = dict(item.get("head_data") or {})
            candidates.append({
                "url": url,
                "title": str(head_data.get("title") or url),
                "snippet": _guard_web_text(str(head_data.get("description") or ""), max_chars=280),
                "credibility": float(score_domain_credibility(url)),
                "discovery": "seeded",
                "seed_score": float(item.get("relevance_score") or 0.0),
                "research_score": 0.0,
                "source_lane": "seeded",
                "domain": _host(url),
            })
            if len(candidates) >= max_sources * 4:
                break
        if len(candidates) >= max_sources * 4:
            break

    batch_contents = await _crawl4ai_batch_extract([item["url"] for item in candidates], query=query, max_chars=per_source_chars)

    async def fetch_one(item: Dict[str, Any]) -> Dict[str, Any]:
        url = str(item.get("url", "") or "")
        title = str(item.get("title", "") or url)
        snippet = _guard_web_text(str(item.get("snippet", "") or ""), max_chars=280)
        if url in batch_contents:
            content = _guard_web_text(str(batch_contents[url].get("content") or ""), max_chars=per_source_chars)
            excerpt, _ = _truncate(content, 900)
            return {
                "title": str(batch_contents[url].get("title") or title),
                "url": url,
                "domain": str(item.get("domain") or _host(url)),
                "snippet": snippet,
                "credibility": float(item.get("credibility", score_domain_credibility(url))),
                "backend": str(batch_contents[url].get("backend") or "crawl4ai_batch"),
                "content": content,
                "excerpt": _guard_web_text(excerpt, max_chars=900),
                "discovery": str(item.get("discovery") or "search"),
                "score": _score_text(query, title, content) + float(item.get("credibility", score_domain_credibility(url))) + (0.25 * float(item.get("seed_score") or 0.0)) + (0.2 * float(item.get("research_score") or 0.0)),
            }
        try:
            backend, content = await _fetch_url_content(url, per_source_chars, user_query=query)
            distilled = _distill_research_text(query, content, max_chars=per_source_chars)
            safe_content = _guard_web_text(distilled, max_chars=per_source_chars)
            excerpt, _ = _truncate(safe_content, 900)
            return {
                "title": title,
                "url": url,
                "domain": str(item.get("domain") or _host(url)),
                "snippet": snippet,
                "credibility": float(item.get("credibility", score_domain_credibility(url))),
                "backend": backend,
                "content": safe_content,
                "excerpt": _guard_web_text(excerpt, max_chars=900),
                "discovery": str(item.get("discovery") or "search"),
                "score": _score_text(query, title, safe_content) + float(item.get("credibility", score_domain_credibility(url))) + (0.25 * float(item.get("seed_score") or 0.0)) + (0.2 * float(item.get("research_score") or 0.0)),
            }
        except Exception as exc:
            return {
                "title": title,
                "url": url,
                "domain": str(item.get("domain") or _host(url)),
                "snippet": snippet,
                "credibility": float(item.get("credibility", score_domain_credibility(url))),
                "backend": "error",
                "content": "",
                "excerpt": snippet,
                "discovery": str(item.get("discovery") or "search"),
                "score": 0.0,
                "error": str(exc),
            }

    sources = await asyncio.gather(*(fetch_one(item) for item in candidates[:max(1, max_sources * 4)]))
    ranked = sorted(
        [item for item in sources if str(item.get("content") or "").strip()],
        key=lambda item: (-float(item.get("score", 0.0)), str(item.get("title", ""))),
    )[:max_sources]
    summary = _guard_web_text(_compile_research_markdown(query, ranked, max_chars=min(max_chars, 6000)), max_chars=min(max_chars, 6000))
    payload = {
        "query": query,
        "search_backend": search_backend,
        "search_category": str(search_debug.get("category") or _normalize_research_category(category) or "general"),
        "seed_backend": seed_backend,
        "source_domains": domains,
        "sources": ranked,
        "summary": summary,
    }
    if debug:
        payload["debug"] = {
            "search": search_debug,
            "candidate_count": len(candidates),
            "batch_extract_count": len(batch_contents),
            "raw_query": raw_query,
            "distilled_query": query,
        }
    return json.dumps(payload, default=str)


def register_web_tools(registry: ToolRegistry) -> None:
    registry.register_fn(
        "fetch_url",
        "Fetch a URL and return cleaned text content. Prefers crawl4ai extraction, then direct HTTP fetch, then Playwright for JS-heavy pages.",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "max_chars": {"type": "integer", "default": 8000},
            },
            "required": ["url"],
        },
        _fetch_url,
        timeout_s=30.0,
        tags=["web", "read", "research"],
        domain="web",
    )
    registry.register_fn(
        "web_search",
        "Search the web for a query using DDGS first, then DuckDuckGo HTML and Lite fallbacks.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "default": 8},
                "debug": {"type": "boolean", "default": False},
            },
            "required": ["query"],
        },
        _web_search,
        timeout_s=30.0,
        tags=["web", "search", "research"],
        domain="web",
    )
    registry.register_fn(
        "web_news",
        "Search recent news-oriented web results for a query using DDGS first, then DuckDuckGo HTML and Lite fallbacks.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "timelimit": {"type": "string", "default": "w"},
                "max_results": {"type": "integer", "default": 8},
                "debug": {"type": "boolean", "default": False},
            },
            "required": ["query"],
        },
        _web_news,
        timeout_s=30.0,
        tags=["web", "search", "news", "research"],
        domain="web",
    )
    registry.register_fn(
        "web_research",
        "Research the web from Google-seeded results when configured, discover same-domain subpages with Crawl4AI URL seeding, extract relevant content, and return a concise markdown digest.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_sources": {"type": "integer", "default": 3},
                "max_chars": {"type": "integer", "default": 8000},
                "category": {"type": "string", "default": "auto"},
                "source_hints": {"type": "array", "items": {"type": "string"}, "default": []},
                "debug": {"type": "boolean", "default": False},
            },
            "required": ["query"],
        },
        _web_research,
        timeout_s=60.0,
        tags=["web", "search", "research"],
        domain="web",
    )
    registry.register_fn(
        "search_github",
        "Search GitHub-related web results for a query.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "language": {"type": "string"},
                "max_results": {"type": "integer", "default": 8},
            },
            "required": ["query"],
        },
        _search_github,
        timeout_s=30.0,
        tags=["web", "search", "research", "code"],
        domain="web",
    )
    registry.register_fn(
        "search_stackoverflow",
        "Search Stack Overflow-related web results for a query.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "default": 8},
            },
            "required": ["query"],
        },
        _search_stackoverflow,
        timeout_s=30.0,
        tags=["web", "search", "research", "code"],
        domain="web",
    )
    registry.register_fn(
        "search_documentation",
        "Search documentation-oriented web results for a query and optional library.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "library": {"type": "string"},
                "max_results": {"type": "integer", "default": 8},
            },
            "required": ["query"],
        },
        _search_documentation,
        timeout_s=30.0,
        tags=["web", "search", "research", "docs"],
        domain="web",
    )
    registry.register_fn(
        "search_tutorials",
        "Search tutorial-oriented web results for a query.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "max_results": {"type": "integer", "default": 8},
            },
            "required": ["query"],
        },
        _search_tutorials,
        timeout_s=30.0,
        tags=["web", "search", "research", "docs"],
        domain="web",
    )
