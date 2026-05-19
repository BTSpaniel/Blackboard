from __future__ import annotations

import re
import urllib.parse
from typing import Any, Dict, List

_AD_DOMAINS = frozenset([
    "doubleclick.net", "googlesyndication.com", "googleadservices.com",
    "adservice.google.com", "adnxs.com", "adsninja.ca", "ads-twitter.com",
    "adsrvr.org", "adroll.com", "amazon-adsystem.com", "criteo.com",
    "criteo.net", "taboola.com", "outbrain.com", "mgid.com",
    "adskeeper.co.uk", "adsterra.com", "propellerads.com", "popads.net",
    "popcash.net", "exoclick.com", "hilltopads.net", "trafficjunky.net",
    "juicyads.com", "onesignal.com", "pushwoosh.com", "push-sdk.com",
    "zedo.com", "ad.plus", "disqusads.com", "monetag.com", "a-ads.com",
    "brid.tv", "whos.amung.us", "bidgear.com", "juicycpm.com",
    "tsyndicate.com", "rediads.com", "pubfuture.com", "clickadu.com",
    "richads.com", "rollerads.com", "profitablegatecpm.com",
    "highcpmrevenuegate.com", "bidsoptimized.com",
    "facebook.net", "connect.facebook.net", "pixel.facebook.com",
    "analytics.google.com", "google-analytics.com", "hotjar.com",
    "mouseflow.com", "fullstory.com", "clarity.ms", "newrelic.com",
    "segment.io", "segment.com", "mixpanel.com", "amplitude.com",
    "optimizely.com", "crazyegg.com",
])

_AD_URL_PATTERNS = frozenset([
    "/ads/", "?ad=", "&ad=", "adservice", "googlesyndication", "doubleclick",
    "prebid", "popunder", "popup", "banner", "interstitial", "/vast.xml",
    "offerwall", "notification-permission", "push-notification",
    "/bid/redirect", "/adserve", "/adframe", "clickunder", "/pagead/",
    "track.php", "tracker.php", "/pixel/", "beacon.js",
    "duckduckgo.com/y.js", "ad_domain=", "ad_provider=",
])

_TRUSTED_DOMAINS = frozenset([
    "docs.python.org", "developer.mozilla.org", "stackoverflow.com",
    "github.com", "learn.microsoft.com", "docs.rs", "pkg.go.dev",
    "en.wikipedia.org", "arxiv.org", "python.org",
    "nodejs.org", "npmjs.com", "pypi.org", "crates.io",
    "rust-lang.org", "go.dev", "typescriptlang.org",
    "react.dev", "reactjs.org", "vuejs.org", "angular.io", "svelte.dev",
    "fastapi.tiangolo.com", "flask.palletsprojects.com",
    "djangoproject.com", "expressjs.com",
])

_LOW_QUALITY_DOMAINS = frozenset([
    "w3schools.com", "geeksforgeeks.org", "tutorialspoint.com",
    "javatpoint.com", "programiz.com",
])

_AD_TAG_PATTERNS = [
    r'<script[^>]*(?:analytics|tracking|pixel|gtag|fbevents|adsbygoogle)[^>]*>.*?</script>',
    r'<ins\s+class="adsbygoogle"[^>]*>.*?</ins>',
    r'<div[^>]*(?:class|id)="[^"]*(?:ad-|ads-|advert|banner|sponsor|popup|overlay|cookie-consent)[^"]*"[^>]*>.*?</div>',
    r'<iframe[^>]*(?:doubleclick|googlesyndication|adserver)[^>]*>.*?</iframe>',
    r'<noscript[^>]*>.*?</noscript>',
]


def _host(url: str) -> str:
    try:
        host = urllib.parse.urlparse(str(url or "").strip().lower()).netloc.lower()
    except Exception:
        return ""
    return host[4:] if host.startswith("www.") else host


def is_ad_url(url: str) -> bool:
    lowered = str(url or "").strip().lower()
    if not lowered:
        return False
    host = _host(lowered)
    if any(domain in host for domain in _AD_DOMAINS):
        return True
    return any(pattern in lowered for pattern in _AD_URL_PATTERNS)


def score_domain_credibility(url: str) -> float:
    host = _host(url)
    if not host:
        return 0.3
    if any(domain in host for domain in _TRUSTED_DOMAINS):
        return 0.9
    if any(suffix in host for suffix in [".edu", ".gov", ".ac.uk"]):
        return 0.85
    if any(domain in host for domain in _LOW_QUALITY_DOMAINS):
        return 0.35
    if any(domain in host for domain in _AD_DOMAINS):
        return 0.0
    if ".org" in host:
        return 0.65
    return 0.5


def should_block_resource(url: str, resource_type: str = "") -> bool:
    lowered = str(url or "").strip().lower()
    if is_ad_url(lowered):
        return True
    kind = str(resource_type or "").strip().lower()
    if kind in {"media", "font"}:
        return True
    return False


def clean_html_for_research(html: str, aggressive: bool = True) -> str:
    text = str(html or "")
    for pattern in _AD_TAG_PATTERNS:
        text = re.sub(pattern, " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    if aggressive:
        text = re.sub(r"<(?:nav|footer|header|aside|iframe|form)[^>]*>.*?</(?:nav|footer|header|aside|iframe|form)>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"&amp;", "&", text)
    text = re.sub(r"&lt;", "<", text)
    text = re.sub(r"&gt;", ">", text)
    text = re.sub(r"&quot;", '"', text)
    text = re.sub(r"\s{3,}", "\n\n", text)
    return text.strip()


def filter_search_results(results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    filtered: List[Dict[str, Any]] = []
    seen = set()
    for item in results:
        url = str((item or {}).get("url", "") or "").strip()
        if not url or url in seen or is_ad_url(url):
            continue
        seen.add(url)
        next_item = dict(item)
        next_item["credibility"] = score_domain_credibility(url)
        filtered.append(next_item)
    return filtered
