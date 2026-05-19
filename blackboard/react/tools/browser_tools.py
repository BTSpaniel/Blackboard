from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time as _time
import urllib.parse
from typing import Any, Dict, List, Optional

from blackboard.react.tool_registry import ToolRegistry
from blackboard.react.tools.adblock import clean_html_for_research, should_block_resource
from blackboard.react.tools.web_tools import _crawl4ai_extract, _playwright_extract

_STEALTH_SCRIPT = """
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

_BROWSER: Optional["_SharedBrowser"] = None
_BROWSER_LOCK = asyncio.Lock()
_CHALLENGE_MARKERS = (
    "verify you are human",
    "checking your browser",
    "just a moment",
    "attention required",
    "enable javascript",
    "cf-browser-verification",
    "challenge-platform",
    "challenge running",
    "turnstile",
    "bot protection",
    "security check",
    "ddos protection",
    "press and hold",
    "press & hold",
)


def _search_url(query: str, engine: str) -> str:
    encoded = urllib.parse.quote_plus(query)
    value = str(engine or "duckduckgo").strip().lower()
    if value == "google":
        return f"https://www.google.com/search?q={encoded}"
    if value == "bing":
        return f"https://www.bing.com/search?q={encoded}"
    if value == "brave":
        return f"https://search.brave.com/search?q={encoded}"
    return f"https://duckduckgo.com/?q={encoded}"


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


def _normalize_browser_label(value: Any) -> str:
    return re.sub(r"[^a-z0-9.]+", " ", str(value or "").lower()).strip()


def _browser_element_label(element: Dict[str, Any]) -> str:
    for key in ("text", "aria", "title", "placeholder", "href"):
        value = str(element.get(key) or "").strip()
        if value:
            return value[:140]
    return str(element.get("tag") or "element")


def _browser_overlap_score(target: str, element: Dict[str, Any]) -> float:
    normalized_target = _normalize_browser_label(target)
    if not normalized_target:
        return 0.0
    label = _normalize_browser_label(" ".join([
        str(element.get("text") or ""),
        str(element.get("aria") or ""),
        str(element.get("title") or ""),
        str(element.get("placeholder") or ""),
        str(element.get("href") or ""),
    ]))
    if not label:
        return 0.0
    score = 0.0
    if normalized_target in label:
        score += 45.0
    target_tokens = {token for token in normalized_target.split() if token}
    label_tokens = {token for token in label.split() if token}
    if target_tokens and label_tokens:
        score += float(len(target_tokens & label_tokens)) * 18.0
    return score


def _score_browser_candidate(command: str, target: str, element: Dict[str, Any]) -> float:
    label = _normalize_browser_label(_browser_element_label(element))
    href = _normalize_browser_label(element.get("href") or "")
    score = 0.0
    index = int(element.get("index") or 0)

    if command in {"type", "type_search"}:
        if element.get("editable"):
            score += 40.0
        if str(element.get("tag") or "") == "input":
            score += 12.0
        if command == "type_search":
            if element.get("search_like"):
                score += 120.0
            if "search" in label:
                score += 35.0
        score += _browser_overlap_score(target, element)
        score += max(0.0, 10.0 - min(index, 10))
    elif command in {"click", "open_result"}:
        if element.get("clickable"):
            score += 18.0
        if element.get("href"):
            score += 18.0
        if command == "open_result":
            if element.get("result_like"):
                score += 110.0
            if re.search(r"\b(login|sign in|sign up|register|privacy|terms|cookie|advert|sponsored|menu)\b", label):
                score -= 80.0
            if re.search(r"\b(login|signin|signup|privacy|terms|cookies?)\b", href):
                score -= 50.0
        score += _browser_overlap_score(target, element)
        score += max(0.0, 14.0 - min(index, 14))
    return score


def _pick_browser_candidate(command: str, target: str, elements: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    best: Optional[Dict[str, Any]] = None
    best_score = float("-inf")
    for element in elements or []:
        score = _score_browser_candidate(command, target, element)
        if score > best_score:
            best = dict(element)
            best["score"] = score
            best_score = score
    if best is None or best_score <= 0.0:
        return None
    return best


def _rank_browser_candidates(command: str, target: str, elements: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    ranked: List[Dict[str, Any]] = []
    for element in elements or []:
        score = _score_browser_candidate(command, target, element)
        if score <= 0.0:
            continue
        candidate = dict(element)
        candidate["score"] = score
        ranked.append(candidate)
    ranked.sort(key=lambda item: (-float(item.get("score", 0.0)), int(item.get("index", 0) or 0), str(item.get("fingerprint", ""))))
    return ranked


def _detect_browser_challenge(title: str, url: str, text: str, console: List[Dict[str, Any]] | None = None, network: List[Dict[str, Any]] | None = None) -> Dict[str, Any]:
    title_value = str(title or "").strip()
    url_value = str(url or "").strip()
    text_value = str(text or "").strip()
    console_text = " ".join(str(item.get("text") or "") for item in (console or []))
    network_text = " ".join(str(item.get("url") or "") for item in (network or []))
    haystack = "\n".join(part for part in [title_value, url_value, text_value[:5000], console_text[:1500], network_text[:1500]] if part)
    normalized = haystack.lower()
    marker = ""
    if any(token in normalized for token in ("/cdn-cgi/challenge-platform", "cf_chl", "turnstile", "captcha", "cloudflare")):
        marker = "cloudflare_or_challenge_url"
    if not marker:
        for candidate in _CHALLENGE_MARKERS:
            if candidate in normalized:
                marker = candidate
                break
    detected = bool(marker)
    excerpt = ""
    if detected:
        source = text_value or title_value or url_value
        excerpt = source[:240]
    return {
        "detected": detected,
        "marker": marker,
        "title": title_value,
        "url": url_value,
        "excerpt": excerpt,
    }


class _SharedBrowser:
    def __init__(self) -> None:
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._console_log: List[Dict[str, Any]] = []
        self._network_errors: List[Dict[str, Any]] = []
        self._fingerprints: Dict[str, str] = {}
        self._pw_loop: Optional[asyncio.AbstractEventLoop] = None
        self._pw_thread: Optional[threading.Thread] = None

    @property
    def page(self):
        return self._page

    def is_alive(self) -> bool:
        return self._page is not None and not self._page.is_closed()

    def _start_proactor_loop(self) -> None:
        if self._pw_loop is not None:
            return
        self._pw_loop = asyncio.ProactorEventLoop()

        def _run_loop() -> None:
            asyncio.set_event_loop(self._pw_loop)
            self._pw_loop.run_forever()

        self._pw_thread = threading.Thread(
            target=_run_loop,
            daemon=True,
            name="blackboard-browser-proactor",
        )
        self._pw_thread.start()

    async def _pw(self, coro):
        if self._pw_loop is None:
            return await coro
        future = asyncio.run_coroutine_threadsafe(coro, self._pw_loop)
        return await asyncio.wrap_future(future)

    async def recover_page(self) -> bool:
        if self.is_alive():
            return True
        if self._context is None:
            return False
        try:
            self._page = await self._pw(self._context.new_page())
            await self._pw(self._context.add_init_script(_STEALTH_SCRIPT))
            self._attach_listeners()
            return True
        except Exception:
            self._page = None
            return False

    async def launch(self) -> None:
        async_playwright = _playwright_impl()
        if async_playwright is None:
            raise RuntimeError("Playwright runtime unavailable")

        if sys.platform == "win32":
            self._start_proactor_loop()
        self._playwright = await self._pw(async_playwright().start())
        self._browser = await self._pw(self._playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        ))
        self._context = await self._pw(self._browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            java_script_enabled=True,
            locale="en-US",
            timezone_id="America/New_York",
            ignore_https_errors=True,
        ))
        await self._pw(self._context.add_init_script(_STEALTH_SCRIPT))
        await self._pw(self._context.route("**/*", self._route_request))
        self._page = await self._pw(self._context.new_page())
        self._attach_listeners()

    async def _route_request(self, route) -> None:
        request = route.request
        if should_block_resource(request.url, request.resource_type):
            await route.abort()
            return
        await route.fallback()

    def _attach_listeners(self) -> None:
        if self._page is None:
            return
        self._page.on("console", self._on_console)
        self._page.on("pageerror", self._on_page_error)
        self._page.on("requestfailed", self._on_request_failed)
        self._page.on("response", self._on_response)

    def _reset(self) -> None:
        self._console_log = []
        self._network_errors = []

    def _on_console(self, msg) -> None:
        self._console_log.append({
            "type": msg.type,
            "text": msg.text,
            "location": f"{msg.location.get('url', '')}:{msg.location.get('lineNumber', '')}",
        })

    def _on_page_error(self, error) -> None:
        self._console_log.append({
            "type": "pageerror",
            "text": str(error),
            "location": "",
        })

    def _on_request_failed(self, request) -> None:
        self._network_errors.append({
            "url": request.url,
            "method": request.method,
            "failure": request.failure or "unknown failure",
        })

    def _on_response(self, response) -> None:
        if response.status >= 400:
            self._network_errors.append({
                "url": response.url,
                "method": response.request.method,
                "status": response.status,
            })

    async def goto(self, url: str) -> Dict[str, Any]:
        if self._page is None:
            await self.launch()
        await self.recover_page()
        self._reset()
        assert self._page is not None
        try:
            await self._pw(self._page.goto(url, timeout=30000, wait_until="networkidle"))
        except Exception as exc:
            return {"url": url, "title": "", "error": str(exc)}
        return {"url": self._page.url, "title": await self._pw(self._page.title()), "error": ""}

    async def get_page_text(self, max_chars: int = 8000) -> str:
        if self._page is None and not await self.recover_page():
            return ""
        assert self._page is not None
        text = await self._pw(self._page.evaluate("() => document.body ? document.body.innerText : ''"))
        value = str(text or "").strip()
        if not value:
            value = clean_html_for_research(await self._pw(self._page.content()))
        return value[:max_chars]

    async def get_page_html(self, max_chars: int = 20000) -> str:
        if self._page is None and not await self.recover_page():
            return ""
        assert self._page is not None
        return str(await self._pw(self._page.content()))[:max_chars]

    async def screenshot_file(self, path: str, *, full_page: bool = True) -> bool:
        if self._page is None and not await self.recover_page():
            return False
        try:
            assert self._page is not None
            await self._pw(self._page.screenshot(path=path, full_page=full_page))
            return True
        except Exception:
            return False

    async def eval_js(self, expression: str, timeout_ms: int = 10000) -> Any:
        if self._page is None and not await self.recover_page():
            return {"error": "no active page"}
        try:
            assert self._page is not None
            return await asyncio.wait_for(self._pw(self._page.evaluate(expression)), timeout=max(float(timeout_ms) / 1000.0, 0.1))
        except Exception as exc:
            return {"error": str(exc)}

    async def wait_for_selector(self, selector: str, timeout_ms: int = 10000) -> bool:
        if self._page is None and not await self.recover_page():
            return False
        try:
            assert self._page is not None
            await self._pw(self._page.wait_for_selector(selector, timeout=timeout_ms))
            return True
        except Exception:
            return False

    def get_console_log(self, *, levels: Optional[List[str]] = None, max_entries: int = 100) -> List[Dict[str, Any]]:
        if not levels:
            return list(self._console_log)[:max_entries]
        wanted = {level.lower() for level in levels}
        return [entry for entry in self._console_log if str(entry.get("type", "")).lower() in wanted][:max_entries]

    def get_network_errors(self, max_entries: int = 50) -> List[Dict[str, Any]]:
        return list(self._network_errors)[:max_entries]

    async def get_challenge_state(self, max_chars: int = 6000) -> Dict[str, Any]:
        title = await self._pw(self._page.title()) if self._page is not None else ""
        url = self._page.url if self._page is not None else ""
        text = await self.get_page_text(max_chars=max_chars)
        return _detect_browser_challenge(title, url, text, self.get_console_log(max_entries=30), self.get_network_errors(max_entries=30))

    async def inspect_page(self, limit: int = 60) -> Dict[str, Any]:
        if self._page is None and not await self.recover_page():
            return {"error": "no active page"}
        self._fingerprints = {}
        script = r"""
        () => {
          const selectorFor = (el) => {
            if (!el) return '';
            if (el.id) return `#${el.id}`;
            const path = [];
            let node = el;
            while (node && node.nodeType === 1 && path.length < 6) {
              let sel = node.tagName.toLowerCase();
              if (node.classList && node.classList.length) {
                sel += '.' + Array.from(node.classList).slice(0, 2).join('.');
              }
              const siblings = node.parentElement ? Array.from(node.parentElement.children).filter((child) => child.tagName === node.tagName) : [];
              if (siblings.length > 1) {
                sel += `:nth-of-type(${siblings.indexOf(node) + 1})`;
              }
              path.unshift(sel);
              node = node.parentElement;
            }
            return path.join(' > ');
          };
          const nodes = Array.from(document.querySelectorAll('a, button, input, textarea, select, [role="button"], [onclick], [contenteditable="true"]'));
          return nodes.map((el, index) => ({
            index: index + 1,
            tag: (el.tagName || '').toLowerCase(),
            text: (el.innerText || el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 120),
            aria: el.getAttribute('aria-label') || '',
            placeholder: el.getAttribute('placeholder') || '',
            title: el.getAttribute('title') || '',
            href: el.getAttribute('href') || '',
            editable: ['input', 'textarea', 'select'].includes((el.tagName || '').toLowerCase()) || el.getAttribute('contenteditable') === 'true',
            clickable: true,
            selector: selectorFor(el),
          }));
        }
        """
        assert self._page is not None
        elements = await self._pw(self._page.evaluate(script))
        shaped: List[Dict[str, Any]] = []
        for item in list(elements or [])[:max(1, limit)]:
            selector = str(item.get("selector", "") or "").strip()
            raw = f"{item.get('tag', '')}|{selector}|{item.get('text', '')}|{item.get('href', '')}"
            fingerprint = hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:12]
            self._fingerprints[fingerprint] = selector
            next_item = dict(item)
            combined = " ".join([
                str(next_item.get("text") or ""),
                str(next_item.get("aria") or ""),
                str(next_item.get("title") or ""),
                str(next_item.get("placeholder") or ""),
            ])
            href = str(next_item.get("href") or "")
            next_item["search_like"] = bool(next_item.get("editable") and re.search(r"search|find|lookup|query", combined, re.IGNORECASE))
            next_item["result_like"] = bool(next_item.get("href") and not next_item.get("editable") and not re.search(r"login|sign in|sign up|register|privacy|terms|cookie", f"{combined} {href}", re.IGNORECASE))
            next_item["fingerprint"] = fingerprint
            shaped.append(next_item)
        return {
            "title": await self._pw(self._page.title()),
            "url": self._page.url,
            "element_count": len(shaped),
            "elements": shaped,
        }

    async def find_best_element(self, command: str, target: str = "", limit: int = 120) -> Optional[Dict[str, Any]]:
        snapshot = await self.inspect_page(limit=limit)
        if snapshot.get("error"):
            return None
        return _pick_browser_candidate(command, target, list(snapshot.get("elements") or []))

    async def find_element_candidates(self, command: str, target: str = "", limit: int = 120) -> List[Dict[str, Any]]:
        snapshot = await self.inspect_page(limit=limit)
        if snapshot.get("error"):
            return []
        return _rank_browser_candidates(command, target, list(snapshot.get("elements") or []))

    async def click_element(self, fingerprint: str) -> Dict[str, Any]:
        selector = self._fingerprints.get(str(fingerprint or "").strip())
        if not selector or (self._page is None and not await self.recover_page()):
            return {"ok": False, "error": "unknown fingerprint"}
        try:
            assert self._page is not None
            await self._pw(self._page.locator(selector).first.click(timeout=10000))
            await self._pw(self._page.wait_for_timeout(1500))
            return {"ok": True, "label": selector}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    async def type_into_element(self, fingerprint: str, text: str) -> Dict[str, Any]:
        selector = self._fingerprints.get(str(fingerprint or "").strip())
        if not selector or (self._page is None and not await self.recover_page()):
            return {"ok": False, "error": "unknown fingerprint"}
        try:
            assert self._page is not None
            locator = self._page.locator(selector).first
            await self._pw(locator.click(timeout=10000))
            await self._pw(locator.fill(str(text or ""), timeout=10000))
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    async def click_best_element(self, target: str = "", *, command: str = "click", max_attempts: int = 3) -> Dict[str, Any]:
        tried: set[str] = set()
        last_error = f"no matching element for {target or command}"
        attempts = max(1, int(max_attempts or 1))
        for _ in range(attempts):
            candidates = await self.find_element_candidates(command, target)
            candidate = next((item for item in candidates if str(item.get("fingerprint") or "") not in tried), None)
            if candidate is None:
                break
            fingerprint = str(candidate.get("fingerprint") or "")
            tried.add(fingerprint)
            before_url = self._page.url if self._page is not None else ""
            before_title = await self._pw(self._page.title()) if self._page is not None else ""
            before_text = await self.get_page_text(max_chars=400) if self._page is not None else ""
            result = await self.click_element(fingerprint)
            if not result.get("ok"):
                last_error = str(result.get("error") or last_error)
                continue
            after_url = self._page.url if self._page is not None else ""
            after_title = await self._pw(self._page.title()) if self._page is not None else ""
            after_text = await self.get_page_text(max_chars=400) if self._page is not None else ""
            if command == "open_result" and after_url == before_url and after_title == before_title and after_text == before_text:
                last_error = f"candidate {fingerprint} did not change page state"
                continue
            result["label"] = _browser_element_label(candidate)
            result["fingerprint"] = fingerprint
            result["attempts"] = len(tried)
            return result
        return {"ok": False, "error": last_error, "attempts": len(tried)}

    async def type_best_element(self, target: str, text: str, *, command: str = "type") -> Dict[str, Any]:
        candidate = await self.find_best_element(command, target)
        if not candidate:
            return {"ok": False, "error": f"no matching input for {target or command}"}
        result = await self.type_into_element(str(candidate.get("fingerprint") or ""), text)
        if result.get("ok"):
            result["fingerprint"] = candidate.get("fingerprint", "")
            result["label"] = _browser_element_label(candidate)
        return result

    async def type_into_search(self, text: str, *, press_enter: bool = True) -> Dict[str, Any]:
        result = await self.type_best_element("search", text, command="type_search")
        if result.get("ok") and press_enter and self._page is not None:
            await self._pw(self._page.keyboard.press("Enter"))
            await self._pw(self._page.wait_for_timeout(2000))
        return result

    async def press_key(self, key: str) -> None:
        if self._page is None and not await self.recover_page():
            return
        assert self._page is not None
        await self._pw(self._page.keyboard.press(key))

    async def scroll(self, direction: str = "down", pixels: int = 500) -> None:
        if self._page is None and not await self.recover_page():
            return
        value = str(direction or "down").strip().lower()
        assert self._page is not None
        if value == "top":
            await self._pw(self._page.evaluate("() => window.scrollTo(0, 0)"))
            return
        if value == "bottom":
            await self._pw(self._page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)"))
            return
        delta = int(pixels or 500)
        if value == "up":
            delta = -abs(delta)
        else:
            delta = abs(delta)
        await self._pw(self._page.evaluate("(amount) => window.scrollBy(0, amount)", delta))
        await self._pw(self._page.wait_for_timeout(500))

    async def get_page_info(self) -> Dict[str, Any]:
        if self._page is None and not await self.recover_page():
            return {"error": "no active page"}
        assert self._page is not None
        payload = await self._pw(self._page.evaluate(
            r"""
            () => ({
              title: document.title || '',
              url: window.location.href,
              meta: Object.fromEntries(Array.from(document.querySelectorAll('meta[name],meta[property]')).slice(0, 20).map((meta) => [meta.getAttribute('name') || meta.getAttribute('property') || '', meta.getAttribute('content') || '']).filter((pair) => pair[0])),
              headings: Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6')).slice(0, 24).map((node) => ({ level: node.tagName.toLowerCase(), text: (node.innerText || '').trim() })),
              links: Array.from(document.querySelectorAll('a[href]')).slice(0, 60).map((node) => ({ text: (node.innerText || node.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 120), href: node.href || '' })),
            })
            """
        ))
        return dict(payload or {})

    async def close(self) -> None:
        try:
            if self._page is not None:
                await self._pw(self._page.close())
            if self._context is not None:
                await self._pw(self._context.close())
            if self._browser is not None:
                await self._pw(self._browser.close())
            if self._playwright is not None:
                await self._pw(self._playwright.stop())
        finally:
            if self._pw_loop is not None:
                self._pw_loop.call_soon_threadsafe(self._pw_loop.stop)
                self._pw_loop = None
                self._pw_thread = None
            self._page = None
            self._context = None
            self._browser = None
            self._playwright = None
            self._fingerprints = {}


async def _get_browser() -> _SharedBrowser:
    global _BROWSER
    async with _BROWSER_LOCK:
        if _BROWSER is None or not _BROWSER.is_alive():
            _BROWSER = _SharedBrowser()
            await _BROWSER.launch()
        return _BROWSER


async def _browse(args: Dict[str, Any]) -> str:
    url = str(args.get("url") or "").strip()
    if not url:
        return "Error: url is required."
    browser = await _get_browser()
    nav = await browser.goto(url)
    text = await browser.get_page_text(max_chars=8000)
    lines = [f"# {nav.get('title', '')}", f"URL: {nav.get('url', url)}"]
    if nav.get("error"):
        lines.append(f"Navigation error: {nav['error']}")
    console = browser.get_console_log(levels=["error", "warning", "pageerror"], max_entries=20)
    network = browser.get_network_errors(max_entries=20)
    lines.append("")
    lines.append("Console issues: none ✓" if not console else f"Console issues ({len(console)}):")
    for entry in console[:10]:
        loc = f" @ {entry.get('location', '')}" if entry.get("location") else ""
        lines.append(f"  [{entry.get('type', '').upper()}] {entry.get('text', '')}{loc}")
    lines.append("")
    lines.append("Network issues: none ✓" if not network else f"Network issues ({len(network)}):")
    for entry in network[:10]:
        if "failure" in entry:
            lines.append(f"  [FAIL] {entry.get('method', 'GET')} {entry.get('url', '')} — {entry.get('failure', '')}")
        else:
            lines.append(f"  [{entry.get('status', '?')}] {entry.get('method', 'GET')} {entry.get('url', '')}")
    challenge = await browser.get_challenge_state(max_chars=3000)
    if challenge.get("detected"):
        lines.append("")
        lines.append(f"Challenge detected: {challenge.get('marker', 'unknown')}")
    lines.append("")
    lines.append(text)
    return "\n".join(lines)


async def _browse_search(args: Dict[str, Any]) -> str:
    query = str(args.get("query") or "").strip()
    engine = str(args.get("engine") or "duckduckgo").strip().lower()
    if not query:
        return "Error: query is required."
    browser = await _get_browser()
    search_url = _search_url(query, engine)
    await browser.goto(search_url)
    await browser.page.wait_for_timeout(2000)
    text = await browser.get_page_text(max_chars=8000)
    challenge = await browser.get_challenge_state(max_chars=3000)
    header = f"# Search: {query}\nEngine: {engine}\nURL: {browser.page.url}"
    if challenge.get("detected"):
        header += f"\nChallenge detected: {challenge.get('marker', 'unknown')}"
    return f"{header}\n\n{text}"


async def _browse_inspect(args: Dict[str, Any]) -> str:
    limit = int(args.get("limit") or 60)
    browser = await _get_browser()
    snapshot = await browser.inspect_page(limit=limit)
    if snapshot.get("error"):
        return f"Browse inspect error: {snapshot['error']}"
    lines = [
        f"Page: {snapshot.get('title', '')} ({snapshot.get('url', '')})",
        f"Interactive elements ({snapshot.get('element_count', 0)}):",
        "",
    ]
    for el in snapshot.get("elements", []):
        label = el.get("text") or el.get("aria") or el.get("placeholder") or el.get("title") or el.get("tag")
        flags = []
        if el.get("editable"):
            flags.append("editable")
        if el.get("clickable"):
            flags.append("clickable")
        line = f"  [{el.get('index', '?')}] <{el.get('tag', '')}> {label}"
        if el.get("href"):
            line += f" -> {el.get('href', '')}"
        if flags:
            line += f" ({', '.join(flags)})"
        line += f"\n       fingerprint: {el.get('fingerprint', '')}"
        lines.append(line)
    return "\n".join(lines)


async def _browse_click(args: Dict[str, Any]) -> str:
    fingerprint = str(args.get("fingerprint") or "").strip()
    label = str(args.get("label") or args.get("query") or "").strip()
    if not fingerprint and not label:
        return "Error: fingerprint or label is required."
    browser = await _get_browser()
    result = await (browser.click_element(fingerprint) if fingerprint else browser.click_best_element(label, command="click"))
    title = await browser.page.title() if browser.page else ""
    url = browser.page.url if browser.page else ""
    if result.get("ok"):
        return f"Clicked: {result.get('label', 'element')}\nNow on: {title} ({url})"
    return f"Click failed: {result.get('error', 'unknown')}\nStill on: {title} ({url})"


async def _browse_type(args: Dict[str, Any]) -> str:
    fingerprint = str(args.get("fingerprint") or "").strip()
    label = str(args.get("label") or args.get("query") or "").strip()
    text = str(args.get("text") or "")
    press_enter = str(args.get("press_enter") or "false").strip().lower() == "true"
    if not fingerprint and not label:
        return "Error: fingerprint or label is required."
    browser = await _get_browser()
    result = await (browser.type_into_element(fingerprint, text) if fingerprint else browser.type_best_element(label, text, command="type"))
    if press_enter:
        await browser.press_key("Enter")
        await browser.page.wait_for_timeout(2000)
    title = await browser.page.title() if browser.page else ""
    url = browser.page.url if browser.page else ""
    if result.get("ok"):
        return f"Typed '{text}' into element.\nNow on: {title} ({url})"
    return f"Type failed: {result.get('error', 'unknown')}\nStill on: {title} ({url})"


async def _browse_scroll(args: Dict[str, Any]) -> str:
    direction = str(args.get("direction") or "down")
    pixels = int(args.get("pixels") or 500)
    browser = await _get_browser()
    await browser.scroll(direction=direction, pixels=pixels)
    text = await browser.get_page_text(max_chars=4000)
    return f"Scrolled {direction}.\n\nVisible text:\n{text}"


async def _browse_read(args: Dict[str, Any]) -> str:
    max_chars = int(args.get("max_chars") or 8000)
    browser = await _get_browser()
    text = await browser.get_page_text(max_chars=max_chars)
    title = await browser.page.title() if browser.page else ""
    url = browser.page.url if browser.page else ""
    return f"# {title}\nURL: {url}\n\n{text}"


async def _browse_links(args: Dict[str, Any]) -> str:
    browser = await _get_browser()
    info = await browser.get_page_info()
    if info.get("error"):
        return f"Error: {info['error']}"
    lines = [f"# {info.get('title', '')}", f"URL: {info.get('url', '')}", ""]
    meta = info.get("meta", {})
    if meta:
        lines.append("## Meta")
        for key, value in list(meta.items())[:10]:
            lines.append(f"  {key}: {value}")
        lines.append("")
    headings = info.get("headings", [])
    if headings:
        lines.append("## Headings")
        for heading in headings:
            indent = "  " * (int(str(heading.get("level", "h1"))[1]) - 1) if len(str(heading.get("level", ""))) == 2 else ""
            lines.append(f"  {indent}{heading.get('level', '')}: {heading.get('text', '')}")
        lines.append("")
    links = info.get("links", [])
    if links:
        lines.append(f"## Links ({len(links)})")
        for link in links:
            lines.append(f"  [{link.get('text', '')}]({link.get('href', '')})")
    return "\n".join(lines)


async def _browse_type_search(args: Dict[str, Any]) -> str:
    query = str(args.get("query") or "").strip()
    press_enter = str(args.get("press_enter") or "true").strip().lower() != "false"
    if not query:
        return "Error: query is required."
    browser = await _get_browser()
    result = await browser.type_into_search(query, press_enter=press_enter)
    title = await browser.page.title() if browser.page else ""
    url = browser.page.url if browser.page else ""
    if result.get("ok"):
        return f"Typed search query '{query}' into {result.get('label', 'search field')}.\nNow on: {title} ({url})"
    return f"Search typing failed: {result.get('error', 'unknown')}\nStill on: {title} ({url})"


async def _browse_open_result(args: Dict[str, Any]) -> str:
    query = str(args.get("query") or args.get("label") or "").strip()
    max_attempts = int(args.get("max_attempts") or 3)
    browser = await _get_browser()
    result = await browser.click_best_element(query, command="open_result", max_attempts=max_attempts)
    title = await browser.page.title() if browser.page else ""
    url = browser.page.url if browser.page else ""
    challenge = await browser.get_challenge_state(max_chars=3000)
    if result.get("ok"):
        extra = f"\nChallenge detected: {challenge.get('marker', 'unknown')}" if challenge.get("detected") else ""
        return f"Opened result: {result.get('label', 'result')}\nNow on: {title} ({url}){extra}"
    extra = f"\nChallenge detected: {challenge.get('marker', 'unknown')}" if challenge.get("detected") else ""
    return f"Open result failed: {result.get('error', 'unknown')}\nStill on: {title} ({url}){extra}"


async def _check_browser_challenge(args: Dict[str, Any]) -> str:
    url = str(args.get("url") or "").strip()
    browser = await _get_browser()
    if url:
        nav = await browser.goto(url)
        if nav.get("error"):
            return f"Navigation error: {nav['error']}"
    challenge = await browser.get_challenge_state(max_chars=4000)
    if challenge.get("detected"):
        return "\n".join([
            f"Challenge detected: {challenge.get('marker', 'unknown')}",
            f"Title: {challenge.get('title', '')}",
            f"URL: {challenge.get('url', '')}",
            f"Excerpt: {challenge.get('excerpt', '')}",
        ])
    return f"No browser challenge detected on {challenge.get('url', browser.page.url if browser.page else '')}. ✓"


async def _browse_research_path(args: Dict[str, Any]) -> str:
    query = str(args.get("query") or "").strip()
    start_url = str(args.get("start_url") or "").strip()
    subquery = str(args.get("subquery") or "").strip()
    target = str(args.get("target") or "").strip()
    engine = str(args.get("engine") or "duckduckgo").strip().lower()
    max_attempts = max(1, int(args.get("max_attempts") or 3))
    read_max_chars = max(500, int(args.get("read_max_chars") or 5000))
    if not query and not start_url:
        return "Error: query or start_url is required."
    browser = await _get_browser()
    steps: List[str] = []
    if start_url:
        nav = await browser.goto(start_url)
        steps.append(f"Opened start URL: {nav.get('url', start_url)}")
    else:
        search_url = _search_url(query, engine)
        nav = await browser.goto(search_url)
        steps.append(f"Searched for: {query} via {engine}")
    if nav.get("error"):
        return f"Research path navigation failed: {nav['error']}"
    challenge = await browser.get_challenge_state(max_chars=3000)
    if challenge.get("detected"):
        return "\n".join([
            "# Browser research path",
            *[f"- {step}" for step in steps],
            f"Challenge detected: {challenge.get('marker', 'unknown')}",
            f"URL: {challenge.get('url', '')}",
            f"Excerpt: {challenge.get('excerpt', '')}",
        ])
    if subquery:
        sub_result = await browser.type_into_search(subquery, press_enter=True)
        if sub_result.get("ok"):
            steps.append(f"Typed on-page sub-search: {subquery}")
        else:
            steps.append(f"Sub-search failed: {sub_result.get('error', 'unknown')}")
        challenge = await browser.get_challenge_state(max_chars=3000)
        if challenge.get("detected"):
            return "\n".join([
                "# Browser research path",
                *[f"- {step}" for step in steps],
                f"Challenge detected: {challenge.get('marker', 'unknown')}",
                f"URL: {challenge.get('url', '')}",
                f"Excerpt: {challenge.get('excerpt', '')}",
            ])
    open_bias = target or subquery or query
    open_result = await browser.click_best_element(open_bias, command="open_result", max_attempts=max_attempts)
    if not open_result.get("ok") and open_bias:
        open_result = await browser.click_best_element("", command="open_result", max_attempts=max_attempts)
    if open_result.get("ok"):
        steps.append(f"Opened result: {open_result.get('label', 'result')} ({open_result.get('attempts', 1)} attempt(s))")
    else:
        steps.append(f"Open result failed: {open_result.get('error', 'unknown')}")
    challenge = await browser.get_challenge_state(max_chars=3000)
    info = await browser.get_page_info()
    text = await browser.get_page_text(max_chars=read_max_chars)
    lines = [
        "# Browser research path",
        f"Start query: {query or start_url}",
        f"Final page: {info.get('title', '')}",
        f"URL: {info.get('url', browser.page.url if browser.page else '')}",
        "",
        "Steps:",
    ]
    for step in steps:
        lines.append(f"- {step}")
    if challenge.get("detected"):
        lines.extend(["", f"Challenge detected: {challenge.get('marker', 'unknown')}", f"Excerpt: {challenge.get('excerpt', '')}"])
    lines.extend(["", text])
    return "\n".join(lines)


async def _browse_extract(args: Dict[str, Any]) -> str:
    url = str(args.get("url") or "").strip()
    max_chars = int(args.get("max_chars") or 12000)
    if not url:
        return "Error: url is required."
    content, title = await _crawl4ai_extract(url, max_chars)
    backend = "crawl4ai"
    if not content:
        content, title = await _playwright_extract(url, max_chars)
        backend = "playwright"
    if not content:
        return f"Extract failed for {url}"
    header = f"# {title}\nURL: {url}\nBackend: {backend}\n\n" if title else f"URL: {url}\nBackend: {backend}\n\n"
    return header + content


def _screenshot_dir() -> str:
    path = os.environ.get("BLACKBOARD_SCREENSHOT_DIR") or os.path.join(tempfile.gettempdir(), "blackboard_screenshots")
    os.makedirs(path, exist_ok=True)
    return path


async def _site_screenshot(args: Dict[str, Any]) -> str:
    url = str(args.get("url") or "").strip()
    describe = str(args.get("describe") or "false").strip().lower() == "true"
    full_page = str(args.get("full_page") or "true").strip().lower() != "false"
    if not url:
        return "Error: url is required."
    browser = await _get_browser()
    nav = await browser.goto(url)
    if nav.get("error"):
        return f"Navigation error: {nav['error']}"
    safe_name = url.replace("://", "_").replace("/", "_").replace(":", "_")[:60]
    filename = os.path.join(_screenshot_dir(), f"blackboard_{safe_name}_{int(_time.time())}.png")
    ok = await browser.screenshot_file(filename, full_page=full_page)
    if not ok:
        return f"Screenshot failed. Page: {nav.get('title', '')} ({nav.get('url', '')})"
    result = [f"Screenshot saved: {filename}", f"Page: {nav.get('title', '')}", f"URL: {nav.get('url', '')}"]
    if describe:
        result.append("\nVisual description unavailable in Blackboard browser tools.")
    return "\n".join(result)


async def _check_console_errors(args: Dict[str, Any]) -> str:
    browser = await _get_browser()
    all_levels = str(args.get("all_levels") or "false").strip().lower() == "true"
    levels = None if all_levels else ["error", "warning", "pageerror"]
    entries = browser.get_console_log(levels=levels, max_entries=100)
    if not entries:
        label = "all levels" if all_levels else "errors/warnings"
        return f"No console {label} captured since last page load. ✓"
    lines = [f"Console messages ({len(entries)}):"]
    for entry in entries:
        loc = f" @ {entry['location']}" if entry.get("location") else ""
        lines.append(f"  [{entry['type'].upper()}] {entry['text']}{loc}")
    return "\n".join(lines)


async def _check_network_errors(args: Dict[str, Any]) -> str:
    browser = await _get_browser()
    errors = browser.get_network_errors(max_entries=50)
    if not errors:
        return "No network errors captured since last page load. ✓"
    lines = [f"Network errors ({len(errors)}):"]
    for entry in errors:
        if "failure" in entry:
            lines.append(f"  [FAIL] {entry.get('method', 'GET')} {entry['url']} — {entry['failure']}")
        else:
            lines.append(f"  [{entry['status']}] {entry.get('method', 'GET')} {entry['url']}")
    return "\n".join(lines)


async def _eval_js_tool(args: Dict[str, Any]) -> str:
    expression = str(args.get("expression") or "").strip()
    if not expression:
        return "Error: expression is required."
    timeout_ms = int(args.get("timeout_ms") or 10000)
    browser = await _get_browser()
    result = await browser.eval_js(expression, timeout_ms=timeout_ms)
    if isinstance(result, dict) and "error" in result:
        return f"JS error: {result['error']}"
    if result is None:
        return "Result: null"
    try:
        return f"Result: {json.dumps(result, indent=2, ensure_ascii=False)}"
    except Exception:
        return f"Result: {result}"


async def _get_page_html_tool(args: Dict[str, Any]) -> str:
    max_chars = int(args.get("max_chars") or 20000)
    browser = await _get_browser()
    html = await browser.get_page_html(max_chars=max_chars)
    url = browser.page.url if browser.page else "unknown"
    return f"<!-- HTML source: {url} -->\n{html}"


async def _wait_for_element(args: Dict[str, Any]) -> str:
    selector = str(args.get("selector") or "").strip()
    if not selector:
        return "Error: selector is required."
    timeout_ms = int(args.get("timeout_ms") or 10000)
    browser = await _get_browser()
    found = await browser.wait_for_selector(selector, timeout_ms=timeout_ms)
    url = browser.page.url if browser.page else ""
    if found:
        count_result = await browser.eval_js(f"document.querySelectorAll({json.dumps(selector)}).length")
        count = count_result if isinstance(count_result, int) else "?"
        return f"✓ Found {count} element(s) matching {selector!r} on {url}"
    return f"✗ Selector {selector!r} not found within {timeout_ms}ms on {url}"


async def _check_page_health(args: Dict[str, Any]) -> str:
    url = str(args.get("url") or "").strip()
    if not url:
        return "Error: url is required."
    browser = await _get_browser()
    nav = await browser.goto(url)
    nav_error = nav.get("error")
    title = nav.get("title", "")
    final_url = nav.get("url", url)
    sections = [f"# Page Health: {title or url}", f"URL: {final_url}"]
    if nav_error:
        sections.append(f"\n⚠ Navigation error: {nav_error}")
    safe_name = url.replace("://", "_").replace("/", "_").replace(":", "_")[:60]
    filename = os.path.join(_screenshot_dir(), f"health_{safe_name}_{int(_time.time())}.png")
    ok = await browser.screenshot_file(filename, full_page=True)
    sections.append(f"\n📸 Screenshot: {filename}" if ok else "\n📸 Screenshot: failed")
    console_entries = browser.get_console_log(levels=["error", "warning", "pageerror"], max_entries=20)
    if console_entries:
        sections.append(f"\n🔴 Console errors/warnings ({len(console_entries)}):")
        for entry in console_entries[:20]:
            loc = f" @ {entry['location']}" if entry.get("location") else ""
            sections.append(f"  [{entry['type'].upper()}] {entry['text'][:200]}{loc}")
    else:
        sections.append("\n✓ No console errors")
    net_errors = browser.get_network_errors(max_entries=20)
    if net_errors:
        sections.append(f"\n🔴 Network errors ({len(net_errors)}):")
        for entry in net_errors[:20]:
            if "failure" in entry:
                sections.append(f"  [FAIL] {entry.get('method', 'GET')} {entry['url'][:120]} — {entry['failure']}")
            else:
                sections.append(f"  [{entry['status']}] {entry.get('method', 'GET')} {entry['url'][:120]}")
    else:
        sections.append("\n✓ No network errors")
    challenge = await browser.get_challenge_state(max_chars=2500)
    if challenge.get("detected"):
        sections.append(f"\n⚠ Challenge detected: {challenge.get('marker', 'unknown')}")
    page_text_preview = await browser.get_page_text(max_chars=500)
    if page_text_preview.strip():
        sections.append(f"\n📄 Page preview:\n{page_text_preview.strip()[:400]}")
    return "\n".join(sections)


def register_browser_tools(registry: ToolRegistry) -> None:
    registry.register_fn(
        "browse",
        "Navigate to a URL using a shared Playwright browser and return the visible page text plus browser issues.",
        {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
        _browse,
        timeout_s=45.0,
        tags=["browser", "web", "research"],
        domain="browser",
    )
    registry.register_fn(
        "site_screenshot",
        "Navigate to a URL and take a full-page screenshot using the shared browser session.",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "describe": {"type": "string", "default": "false"},
                "full_page": {"type": "string", "default": "true"},
            },
            "required": ["url"],
        },
        _site_screenshot,
        timeout_s=45.0,
        tags=["browser", "playwright", "research"],
        domain="browser",
    )
    registry.register_fn(
        "check_console_errors",
        "Return browser console errors and warnings captured since the last page load.",
        {"type": "object", "properties": {"all_levels": {"type": "string", "default": "false"}}},
        _check_console_errors,
        timeout_s=10.0,
        tags=["browser", "playwright", "research"],
        domain="browser",
    )
    registry.register_fn(
        "check_network_errors",
        "Return failed network requests and HTTP errors captured since the last page load.",
        {"type": "object", "properties": {}},
        _check_network_errors,
        timeout_s=10.0,
        tags=["browser", "playwright", "research"],
        domain="browser",
    )
    registry.register_fn(
        "check_browser_challenge",
        "Detect whether the current page or an optional URL is showing a browser challenge such as Cloudflare verification.",
        {"type": "object", "properties": {"url": {"type": "string"}}},
        _check_browser_challenge,
        timeout_s=20.0,
        tags=["browser", "playwright", "research"],
        domain="browser",
    )
    registry.register_fn(
        "eval_js",
        "Evaluate a JavaScript expression in the current page context.",
        {
            "type": "object",
            "properties": {
                "expression": {"type": "string"},
                "timeout_ms": {"type": "integer", "default": 10000},
            },
            "required": ["expression"],
        },
        _eval_js_tool,
        timeout_s=15.0,
        tags=["browser", "playwright", "research"],
        domain="browser",
    )
    registry.register_fn(
        "get_page_html",
        "Return the full HTML source of the current page.",
        {"type": "object", "properties": {"max_chars": {"type": "integer", "default": 20000}}},
        _get_page_html_tool,
        timeout_s=15.0,
        tags=["browser", "playwright", "read", "research"],
        domain="browser",
    )
    registry.register_fn(
        "wait_for_element",
        "Wait for a CSS selector to appear on the current page.",
        {
            "type": "object",
            "properties": {
                "selector": {"type": "string"},
                "timeout_ms": {"type": "integer", "default": 10000},
            },
            "required": ["selector"],
        },
        _wait_for_element,
        timeout_s=15.0,
        tags=["browser", "playwright", "research"],
        domain="browser",
    )
    registry.register_fn(
        "check_page_health",
        "Navigate to a URL, capture a screenshot, and report console/network issues in one call.",
        {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]},
        _check_page_health,
        timeout_s=45.0,
        tags=["browser", "playwright", "research"],
        domain="browser",
    )
    registry.register_fn(
        "browse_search",
        "Search the web in the shared browser. Supports engines like duckduckgo, google, bing, and brave.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "engine": {"type": "string", "default": "duckduckgo"},
            },
            "required": ["query"],
        },
        _browse_search,
        timeout_s=45.0,
        tags=["browser", "web", "research"],
        domain="browser",
    )
    registry.register_fn(
        "browse_inspect",
        "Inspect the current page's interactive elements and return fingerprints for later click/type actions.",
        {"type": "object", "properties": {"limit": {"type": "integer", "default": 60}}},
        _browse_inspect,
        timeout_s=20.0,
        tags=["browser", "research"],
        domain="browser",
    )
    registry.register_fn(
        "browse_click",
        "Click an interactive element on the current page by fingerprint or approximate label.",
        {"type": "object", "properties": {"fingerprint": {"type": "string"}, "label": {"type": "string"}}, "anyOf": [{"required": ["fingerprint"]}, {"required": ["label"]}]},
        _browse_click,
        timeout_s=20.0,
        tags=["browser", "research"],
        domain="browser",
    )
    registry.register_fn(
        "browse_type",
        "Type into an interactive element on the current page by fingerprint or approximate label.",
        {
            "type": "object",
            "properties": {
                "fingerprint": {"type": "string"},
                "label": {"type": "string"},
                "text": {"type": "string"},
                "press_enter": {"type": "string", "default": "false"},
            },
            "required": ["text"],
            "anyOf": [{"required": ["fingerprint", "text"]}, {"required": ["label", "text"]}],
        },
        _browse_type,
        timeout_s=20.0,
        tags=["browser", "research"],
        domain="browser",
    )
    registry.register_fn(
        "browse_scroll",
        "Scroll the current page. Directions: down, up, bottom, top.",
        {
            "type": "object",
            "properties": {
                "direction": {"type": "string"},
                "pixels": {"type": "integer", "default": 500},
            },
            "required": ["direction"],
        },
        _browse_scroll,
        timeout_s=10.0,
        tags=["browser", "research"],
        domain="browser",
    )
    registry.register_fn(
        "browse_read",
        "Read the current page's visible text content.",
        {"type": "object", "properties": {"max_chars": {"type": "integer", "default": 8000}}},
        _browse_read,
        timeout_s=15.0,
        tags=["browser", "read", "research"],
        domain="browser",
    )
    registry.register_fn(
        "browse_links",
        "Get structured page info including headings, links, and meta tags.",
        {"type": "object", "properties": {}},
        _browse_links,
        timeout_s=15.0,
        tags=["browser", "read", "research"],
        domain="browser",
    )
    registry.register_fn(
        "browse_type_search",
        "Find a likely search field on the current page, type a query into it, and optionally press Enter.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "press_enter": {"type": "string", "default": "true"},
            },
            "required": ["query"],
        },
        _browse_type_search,
        timeout_s=20.0,
        tags=["browser", "web", "research"],
        domain="browser",
    )
    registry.register_fn(
        "browse_open_result",
        "Open the best likely result link on the current page, optionally biased toward a query or label.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "label": {"type": "string"},
                "max_attempts": {"type": "integer", "default": 3},
            },
        },
        _browse_open_result,
        timeout_s=20.0,
        tags=["browser", "web", "research"],
        domain="browser",
    )
    registry.register_fn(
        "browse_research_path",
        "Follow a lightweight browser research plan: search or open a site, optionally sub-search within the page, open a likely result with retries, and read the destination.",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "start_url": {"type": "string"},
                "subquery": {"type": "string"},
                "target": {"type": "string"},
                "engine": {"type": "string", "default": "duckduckgo"},
                "max_attempts": {"type": "integer", "default": 3},
                "read_max_chars": {"type": "integer", "default": 5000},
            },
        },
        _browse_research_path,
        timeout_s=60.0,
        tags=["browser", "web", "research"],
        domain="browser",
    )
    registry.register_fn(
        "browse_extract",
        "Extract clean, research-friendly page content from a URL using crawl4ai with Playwright fallback.",
        {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "max_chars": {"type": "integer", "default": 12000},
            },
            "required": ["url"],
        },
        _browse_extract,
        timeout_s=45.0,
        tags=["browser", "web", "research"],
        domain="browser",
    )
