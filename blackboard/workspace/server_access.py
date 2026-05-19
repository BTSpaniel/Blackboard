from __future__ import annotations

import ipaddress
import json
import os
import secrets
import urllib.parse
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from blackboard.kernel.atomic_files import write_text_atomically as write_text_atomic
from blackboard.kernel.logger import get_logger
from blackboard.workspace.remote_share import remote_share_cookie_names, secure_cookie_preferred
from blackboard.workspace.redaction import sanitize_inline_text

logger = get_logger("workspace.server_access")

_ALLOWED_OVERRIDE_KEYS = {
    "lan_enabled",
    "remote_enabled",
    "public_base_url",
    "trust_forwarded_for",
}
_REMOTE_COOKIE_NAME = "bb_remote_access"
_REMOTE_SECURE_COOKIE_NAME = "__Host-bb_remote_access"
_CSRF_PROTECTED_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _path(data_root: Path) -> Path:
    return Path(data_root) / "server" / "access_overrides.json"


def _token_path(data_root: Path) -> Path:
    return Path(data_root) / "server" / "remote_token_override.json"


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    return text in {"1", "true", "yes", "on"}


def normalize_access(access: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    current = dict(access or {})
    public_base_url = str(current.get("public_base_url") or "").strip().rstrip("/")
    remote_token_env = str(current.get("remote_token_env") or "BLACKBOARD_REMOTE_ACCESS_TOKEN").strip() or "BLACKBOARD_REMOTE_ACCESS_TOKEN"
    return {
        "lan_enabled": bool(current.get("lan_enabled", False)),
        "remote_enabled": bool(current.get("remote_enabled", False)),
        "public_base_url": public_base_url,
        "remote_token_env": remote_token_env,
        "trust_forwarded_for": bool(current.get("trust_forwarded_for", False)),
    }


def merge_server_config(server: Optional[Mapping[str, Any]], overrides: Optional[Mapping[str, Any]] = None) -> Dict[str, Any]:
    current = dict(server or {})
    access = normalize_access(current.get("access") or {})
    if overrides:
        access = normalize_access({**access, **dict(overrides or {})})
    port_raw = current.get("port") or 8780
    try:
        port = int(port_raw)
    except Exception:
        port = 8780
    cors_origins = current.get("cors_origins") or ["*"]
    if isinstance(cors_origins, str):
        cors_origins = [cors_origins]
    return {
        "host": str(current.get("host") or "127.0.0.1").strip() or "127.0.0.1",
        "port": max(1, min(port, 65535)),
        "cors_origins": [str(item).strip() for item in list(cors_origins or []) if str(item).strip()] or ["*"],
        "access": access,
    }


def load_access_overrides(data_root: Path) -> Dict[str, Any]:
    p = _path(data_root)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("[server_access] failed to load %s: %s", p, exc)
        return {}
    if not isinstance(data, dict):
        return {}
    return normalize_access({key: data.get(key) for key in _ALLOWED_OVERRIDE_KEYS if key in data})


def save_access_overrides(data_root: Path, access: Mapping[str, Any]) -> Dict[str, Any]:
    p = _path(data_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    current = load_access_overrides(data_root)
    merged = normalize_access({**current, **{key: access.get(key) for key in _ALLOWED_OVERRIDE_KEYS if key in access}})
    payload = {key: merged[key] for key in sorted(_ALLOWED_OVERRIDE_KEYS)}
    write_text_atomic(p, json.dumps(payload, indent=2, sort_keys=True))
    try:
        from blackboard.workspace.version_control import commit_safely
        commit_safely("server: update access settings", kind="vcs.server_access", paths=[str(p)])
    except Exception:
        pass
    return merged


def effective_bind_host(server: Mapping[str, Any]) -> str:
    current = merge_server_config(server)
    access = current.get("access") or {}
    if access.get("lan_enabled") or access.get("remote_enabled"):
        return "0.0.0.0"
    return str(current.get("host") or "127.0.0.1")


def resolve_remote_token(access: Mapping[str, Any]) -> str:
    env_name = str((access or {}).get("remote_token_env") or "BLACKBOARD_REMOTE_ACCESS_TOKEN").strip() or "BLACKBOARD_REMOTE_ACCESS_TOKEN"
    return str(os.getenv(env_name, "") or "").strip()


def load_remote_token_override(data_root: Path) -> str:
    p = _token_path(data_root)
    if not p.exists():
        return ""
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("[server_access] failed to load remote token override %s: %s", p, exc)
        return ""
    if not isinstance(data, dict):
        return ""
    return str(data.get("token") or "").strip()


def save_remote_token_override(data_root: Path, token: str) -> str:
    p = _token_path(data_root)
    p.parent.mkdir(parents=True, exist_ok=True)
    value = str(token or "").strip()
    if not value:
        raise ValueError("token is required")
    write_text_atomic(p, json.dumps({"token": value}, indent=2, sort_keys=True))
    try:
        from blackboard.workspace.version_control import commit_safely
        commit_safely("server: update remote access token", kind="vcs.server_access_token", paths=[str(p)])
    except Exception:
        pass
    return value


def delete_remote_token_override(data_root: Path) -> bool:
    p = _token_path(data_root)
    if not p.exists():
        return False
    p.unlink(missing_ok=True)
    try:
        from blackboard.workspace.version_control import commit_safely
        commit_safely("server: clear remote access token", kind="vcs.server_access_token_delete", paths=[str(p)])
    except Exception:
        pass
    return True


def resolve_effective_remote_token(access: Mapping[str, Any], *, data_root: Optional[Path] = None) -> str:
    value = resolve_remote_token(access)
    if value:
        return value
    if data_root is None:
        return ""
    return load_remote_token_override(data_root)


def is_loopback_request(client_host: str, headers: Optional[Mapping[str, str]] = None, *, trust_forwarded_for: bool = False) -> bool:
    trusted_client = _client_ip(client_host, headers, trust_forwarded_for=trust_forwarded_for)
    if trusted_client == "testclient":
        return True
    return classify_client(trusted_client) == "loopback"


def runtime_bind_host(server: Mapping[str, Any]) -> str:
    return str(os.getenv("BLACKBOARD_RUNTIME_BIND_HOST") or effective_bind_host(server)).strip() or effective_bind_host(server)


def runtime_port(server: Mapping[str, Any]) -> int:
    try:
        return int(os.getenv("BLACKBOARD_RUNTIME_PORT") or merge_server_config(server).get("port") or 8780)
    except Exception:
        return int(merge_server_config(server).get("port") or 8780)


def access_snapshot(server: Mapping[str, Any], *, data_root: Optional[Path] = None, remote_share_manager: Any = None, protection_feedback_manager: Any = None) -> Dict[str, Any]:
    current = merge_server_config(server)
    access = dict(current.get("access") or {})
    bind_host = effective_bind_host(current)
    port = int(current.get("port") or 8780)
    current_runtime_host = runtime_bind_host(current)
    current_runtime_port = runtime_port(current)
    env_token = resolve_remote_token(access)
    override_token = load_remote_token_override(data_root) if data_root is not None else ""
    token_source = "env" if env_token else "override" if override_token else ""
    remote_share = remote_share_manager.status(public_base_url=str(access.get("public_base_url") or "")) if remote_share_manager is not None else None
    protection = protection_feedback_manager.snapshot(limit=10) if protection_feedback_manager is not None else None
    public_base_url = str(access.get("public_base_url") or "")
    secure_remote_cookie = remote_cookie_name(secure=secure_cookie_preferred(public_base_url))
    guidance: list[str] = []
    if public_base_url.startswith("https://"):
        guidance.append("HTTPS public base URL detected; __Host- secure cookies can be used for remote sessions.")
    elif public_base_url:
        guidance.append("Public base URL is not HTTPS; secure __Host- cookies are unavailable until TLS terminates at your reverse proxy.")
    else:
        guidance.append("Set a public base URL when serving Blackboard behind a reverse proxy so remote links and cookie policy stay consistent.")
    if bool(access.get("trust_forwarded_for")):
        guidance.append("Forwarded headers are trusted; ensure only your reverse proxy can reach Blackboard directly.")
    return {
        "host": str(current.get("host") or "127.0.0.1"),
        "port": port,
        "bind_host": bind_host,
        "runtime_bind_host": current_runtime_host,
        "runtime_port": current_runtime_port,
        "local_url": f"http://127.0.0.1:{port}",
        "public_base_url": public_base_url,
        "lan_enabled": bool(access.get("lan_enabled")),
        "remote_enabled": bool(access.get("remote_enabled")),
        "remote_token_env": str(access.get("remote_token_env") or "BLACKBOARD_REMOTE_ACCESS_TOKEN"),
        "remote_token_ready": bool(env_token or override_token),
        "remote_token_source": token_source,
        "remote_cookie_name": secure_remote_cookie,
        "remote_cookie_name_insecure": remote_cookie_name(secure=False),
        "remote_cookie_name_secure": remote_cookie_name(secure=True),
        "secure_cookie_preferred": secure_cookie_preferred(public_base_url),
        "trust_forwarded_for": bool(access.get("trust_forwarded_for")),
        "restart_required": bool(bind_host != current_runtime_host or int(port) != int(current_runtime_port)),
        "guidance": guidance,
        "protection": protection,
        "remote_share": remote_share,
    }


def _normalize_origin(value: str) -> str:
    raw = sanitize_inline_text(str(value or ""), max_chars=300)
    if not raw:
        return ""
    try:
        parsed = urllib.parse.urlparse(raw)
    except Exception:
        return ""
    scheme = str(parsed.scheme or "").strip().lower()
    host = str(parsed.netloc or parsed.path or "").strip().lower()
    if not host or scheme not in {"http", "https"}:
        return ""
    return f"{scheme}://{host}"


def _allowed_origins(server: Mapping[str, Any], headers: Optional[Mapping[str, str]] = None, *, url_scheme: str = "http") -> set[str]:
    current = merge_server_config(server)
    access = dict(current.get("access") or {})
    allowed: set[str] = set()
    public_base_url = _normalize_origin(str(access.get("public_base_url") or ""))
    if public_base_url:
        allowed.add(public_base_url)
    header_map = {str(k).lower(): str(v) for k, v in dict(headers or {}).items()}
    host = str(header_map.get("x-forwarded-host") or header_map.get("host") or "").split(",", 1)[0].strip()
    proto = str(header_map.get("x-forwarded-proto") or url_scheme or "http").strip().lower()
    if host and proto in {"http", "https"}:
        for candidate_proto in {proto, "http", "https"}:
            origin = _normalize_origin(f"{candidate_proto}://{host}")
            if origin:
                allowed.add(origin)
    return allowed


def is_same_origin_request(server: Mapping[str, Any], headers: Optional[Mapping[str, str]] = None, *, url_scheme: str = "http") -> bool:
    header_map = {str(k).lower(): str(v) for k, v in dict(headers or {}).items()}
    allowed = _allowed_origins(server, header_map, url_scheme=url_scheme)
    origin = _normalize_origin(str(header_map.get("origin") or ""))
    referer = _normalize_origin(str(header_map.get("referer") or ""))
    fetch_site = str(header_map.get("sec-fetch-site") or "").strip().lower()
    if origin and origin in allowed:
        return True
    if referer and referer in allowed:
        return True
    if fetch_site in {"same-origin", "same-site", "none"} and allowed:
        return True
    return False


def _normalize_ip(value: str) -> str:
    text = sanitize_inline_text(str(value or ""), max_chars=120)
    if not text:
        return ""
    if text.startswith("[") and text.endswith("]"):
        text = text[1:-1]
    if text.count(":") == 1 and "." in text:
        host, _sep, maybe_port = text.partition(":")
        if maybe_port.isdigit():
            text = host
    return text


def _client_ip(host: str, headers: Optional[Mapping[str, str]], *, trust_forwarded_for: bool) -> str:
    if trust_forwarded_for and headers:
        forwarded = sanitize_inline_text(str(headers.get("x-forwarded-for") or ""), max_chars=200)
        if forwarded:
            candidate = _normalize_ip(forwarded.split(",", 1)[0])
            if candidate:
                return candidate
    return _normalize_ip(host)


def classify_client(host: str) -> str:
    try:
        addr = ipaddress.ip_address(_normalize_ip(host))
    except ValueError:
        return "unknown"
    if addr.is_loopback:
        return "loopback"
    if addr.is_private or addr.is_link_local:
        return "lan"
    return "remote"


def remote_token_from_inputs(
    headers: Optional[Mapping[str, str]] = None,
    query_params: Optional[Mapping[str, Any]] = None,
    cookies: Optional[Mapping[str, Any]] = None,
) -> str:
    current_headers = {str(k).lower(): str(v) for k, v in dict(headers or {}).items()}
    auth = str(current_headers.get("authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    header_token = str(current_headers.get("x-blackboard-remote-token") or "").strip()
    if header_token:
        return header_token
    cookie_map = dict(cookies or {})
    for cookie_name in remote_cookie_names():
        cookie_token = str(cookie_map.get(cookie_name) or "").strip()
        if cookie_token:
            return cookie_token
    query = dict(query_params or {})
    return str(query.get("token") or query.get("access_token") or "").strip()


def _protection_weight_for_reason(reason: str) -> float:
    key = str(reason or "").strip().lower()
    if key in {"remote_cookie_replay_detected", "remote_protection_cooldown"}:
        return 2.0
    if key in {"remote_origin_invalid", "remote_token_invalid", "remote_invite_invalid"}:
        return 1.5
    if key in {"remote_token_required"}:
        return 0.75
    return 0.5


def request_access_decision(
    server: Mapping[str, Any],
    *,
    data_root: Optional[Path] = None,
    remote_share_manager: Any = None,
    protection_feedback_manager: Any = None,
    client_host: str,
    headers: Optional[Mapping[str, str]] = None,
    query_params: Optional[Mapping[str, Any]] = None,
    cookies: Optional[Mapping[str, Any]] = None,
    method: str = "GET",
    path: str = "/",
    url_scheme: str = "http",
) -> Dict[str, Any]:
    current = merge_server_config(server)
    access = dict(current.get("access") or {})
    trusted_client = _client_ip(client_host, headers, trust_forwarded_for=bool(access.get("trust_forwarded_for")))
    scope = classify_client(trusted_client)
    method_name = str(method or "GET").upper()
    path_name = str(path or "/").strip() or "/"

    def _denied(reason: str, *, weight: Optional[float] = None, **extra: Any) -> Dict[str, Any]:
        payload = {"allowed": False, "scope": scope, "client_ip": trusted_client, "reason": reason, **extra}
        if protection_feedback_manager is not None and scope == "remote" and reason in {"remote_protection_cooldown", "remote_cookie_replay_detected", "remote_origin_invalid", "remote_token_required", "remote_token_invalid", "remote_invite_invalid"}:
            protection_state = protection_feedback_manager.record_denial(
                client_ip=trusted_client,
                reason=reason,
                path=path_name,
                weight=float(weight if weight is not None else _protection_weight_for_reason(reason)),
            )
            payload["protection"] = protection_state
        return payload

    if scope in {"loopback", "unknown"}:
        return {"allowed": True, "scope": scope, "client_ip": trusted_client, "reason": "local"}
    if scope == "lan":
        if access.get("lan_enabled") or access.get("remote_enabled"):
            return {"allowed": True, "scope": scope, "client_ip": trusted_client, "reason": "lan_enabled"}
        return {"allowed": False, "scope": scope, "client_ip": trusted_client, "reason": "lan_disabled"}
    if not access.get("remote_enabled"):
        return {"allowed": False, "scope": scope, "client_ip": trusted_client, "reason": "remote_disabled"}
    if protection_feedback_manager is not None:
        protection_state = protection_feedback_manager.evaluate(trusted_client)
        if not protection_state.get("allowed", True):
            return _denied(
                "remote_protection_cooldown",
                weight=0.25,
                cooldown_until=float(protection_state.get("cooldown_until") or 0.0),
                cooldown_remaining_s=float(protection_state.get("cooldown_remaining_s") or 0.0),
            )
    if path_name == "/join":
        return {"allowed": True, "scope": scope, "client_ip": trusted_client, "reason": "join_allowed"}
    if remote_share_manager is not None:
        share_cookie = ""
        for cookie_name in remote_share_cookie_names():
            share_cookie = str(dict(cookies or {}).get(cookie_name) or "").strip()
            if share_cookie:
                break
        if share_cookie:
            auth_result = remote_share_manager.authenticate_cookie(
                share_cookie,
                remote_ip=trusted_client,
                user_agent=str(dict(headers or {}).get("user-agent") or ""),
            )
            if auth_result.session is not None:
                if (method_name in _CSRF_PROTECTED_METHODS or method_name == "WEBSOCKET") and not is_same_origin_request(current, headers, url_scheme=url_scheme):
                    return _denied("remote_origin_invalid")
                if protection_feedback_manager is not None:
                    protection_feedback_manager.record_allow(client_ip=trusted_client, reason="remote_share_cookie_valid", path=path_name)
                return {
                    "allowed": True,
                    "scope": scope,
                    "client_ip": trusted_client,
                    "reason": "remote_share_cookie_valid",
                    "replacement_cookie": auth_result.replacement_cookie,
                    "share_session_id": auth_result.session.token_id,
                    "share_session_name": auth_result.session.name,
                }
            if auth_result.replay_detected:
                return _denied("remote_cookie_replay_detected")
    expected_token = resolve_effective_remote_token(access, data_root=data_root)
    if not expected_token:
        return {"allowed": False, "scope": scope, "client_ip": trusted_client, "reason": "remote_token_missing"}
    presented = remote_token_from_inputs(headers=headers, query_params=query_params, cookies=cookies)
    if not presented:
        return _denied("remote_token_required")
    cookie_map = dict(cookies or {})
    cookie_only_auth = any(bool(str(cookie_map.get(cookie_name) or "").strip()) for cookie_name in remote_cookie_names()) and not bool(str(dict(headers or {}).get("authorization") or "").strip()) and not bool(str(dict(headers or {}).get("x-blackboard-remote-token") or "").strip()) and not bool(dict(query_params or {}).get("token") or dict(query_params or {}).get("access_token"))
    if cookie_only_auth and (method_name in _CSRF_PROTECTED_METHODS or method_name == "WEBSOCKET") and not is_same_origin_request(current, headers, url_scheme=url_scheme):
        return _denied("remote_origin_invalid")
    if not secrets.compare_digest(presented, expected_token):
        return _denied("remote_token_invalid")
    if protection_feedback_manager is not None:
        protection_feedback_manager.record_allow(client_ip=trusted_client, reason="remote_token_valid", path=path_name)
    return {"allowed": True, "scope": scope, "client_ip": trusted_client, "reason": "remote_token_valid"}


def remote_cookie_name(*, secure: bool = False) -> str:
    return _REMOTE_SECURE_COOKIE_NAME if secure else _REMOTE_COOKIE_NAME


def remote_cookie_names() -> tuple[str, str]:
    return (_REMOTE_SECURE_COOKIE_NAME, _REMOTE_COOKIE_NAME)
