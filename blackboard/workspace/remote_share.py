from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import secrets
import socket
import time
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from cryptography.fernet import Fernet

from blackboard.kernel.atomic_files import append_text_atomically, write_text_atomically
from blackboard.kernel.logger import get_logger
from blackboard.workspace.redaction import sanitize_inline_text, sanitize_mapping

logger = get_logger("workspace.remote_share")

try:
    import miniupnpc  # type: ignore[import-not-found]
    _UPNP_AVAILABLE = True
except Exception:
    miniupnpc = None
    _UPNP_AVAILABLE = False

_REMOTE_SHARE_COOKIE_NAME = "bb_remote_share"
_REMOTE_SHARE_SECURE_COOKIE_NAME = "__Host-bb_remote_share"
_REMOTE_SHARE_COOKIE_MAX_AGE = 60 * 60 * 24 * 30
_REMOTE_SHARE_COOKIE_ROTATE_AFTER_SECONDS = 60 * 60 * 12
_REMOTE_SHARE_COOKIE_REPLAY_GRACE_SECONDS = 90
_REMOTE_SHARE_ANOMALY_WINDOW_SECONDS = 60 * 60
_REMOTE_SHARE_MAX_UNIQUE_IPS = 3
_REMOTE_SHARE_MAX_REQUESTS_PER_WINDOW = 120


@dataclass(slots=True)
class RemoteInviteSession:
    token_id: str
    name: str
    created_at: float
    expires_at: float
    token_hash: str
    token_value: str = ""
    remote_ip: str = ""
    active: bool = True
    last_seen: float = 0.0


@dataclass(slots=True)
class RotatingCookieSession:
    cookie_id: str
    family_id: str
    subject_id: str
    created_at: float
    expires_at: float
    replaced_by_cookie_id: str = ""
    grace_until: float = 0.0
    revoked: bool = False
    last_seen: float = 0.0
    remote_ip: str = ""
    user_agent_hash: str = ""


@dataclass(slots=True)
class RemoteCookieAuthResult:
    session: Optional[RemoteInviteSession]
    replacement_cookie: str = ""
    replay_detected: bool = False


class UPnPManager:
    def __init__(self) -> None:
        self.upnp = None
        self.external_ip: str = ""
        self.lan_ip: str = ""
        self.mapped_port: int = 0
        self.internal_port: int = 0
        self.lease_duration: int = 3600
        self.last_renewal: float = 0.0
        self.initialized = False

    async def discover(self) -> bool:
        if not _UPNP_AVAILABLE or miniupnpc is None:
            return False
        loop = asyncio.get_running_loop()
        try:
            found = await loop.run_in_executor(None, self._discover_sync)
        except Exception as exc:
            logger.warning("[remote_share] upnp discovery failed: %s", exc)
            return False
        self.initialized = bool(found)
        return self.initialized

    def _discover_sync(self) -> bool:
        upnp = miniupnpc.UPnP()
        upnp.discoverdelay = 2000
        devices = upnp.discover()
        if devices == 0:
            return False
        upnp.selectigd()
        self.upnp = upnp
        self.external_ip = str(upnp.externalipaddress() or "")
        self.lan_ip = str(getattr(upnp, "lanaddr", "") or "")
        return True

    async def add_port_mapping(self, internal_port: int, external_port: Optional[int] = None, protocol: str = "TCP", description: str = "Blackboard Remote Share") -> bool:
        if not self.initialized or self.upnp is None:
            return False
        external = int(external_port or internal_port)
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(
                None,
                lambda: self.upnp.addportmapping(external, protocol, self.lan_ip or self.get_local_ip(), int(internal_port), description, "", self.lease_duration),
            )
        except Exception as exc:
            logger.warning("[remote_share] upnp map failed: %s", exc)
            return False
        if result:
            self.mapped_port = external
            self.internal_port = int(internal_port)
            self.last_renewal = time.time()
            return True
        return False

    async def remove_port_mapping(self, external_port: Optional[int] = None, protocol: str = "TCP") -> bool:
        if not self.initialized or self.upnp is None:
            return False
        port = int(external_port or self.mapped_port or 0)
        if not port:
            return False
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(None, lambda: self.upnp.deleteportmapping(port, protocol))
        except Exception as exc:
            logger.debug("[remote_share] upnp unmap failed: %s", exc)
            return False
        if port == self.mapped_port:
            self.mapped_port = 0
            self.internal_port = 0
        return True

    async def renew_mapping(self) -> bool:
        if not self.mapped_port or not self.internal_port:
            return False
        if self.last_renewal and (time.time() - self.last_renewal) < (self.lease_duration / 2):
            return True
        return await self.add_port_mapping(self.internal_port, self.mapped_port)

    def get_local_ip(self) -> str:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.connect(("8.8.8.8", 80))
                return str(sock.getsockname()[0] or "127.0.0.1")
            finally:
                sock.close()
        except Exception:
            return "127.0.0.1"

    def status(self) -> Dict[str, Any]:
        return {
            "available": _UPNP_AVAILABLE,
            "initialized": self.initialized,
            "external_ip": self.external_ip,
            "lan_ip": self.lan_ip,
            "mapped_port": self.mapped_port,
            "internal_port": self.internal_port,
            "lease_expires": (self.last_renewal + self.lease_duration) if self.last_renewal else 0.0,
        }


class RemoteShareManager:
    def __init__(self, data_root: Path, server_port: int, event_hook: Any = None) -> None:
        self.data_root = Path(data_root)
        self.server_port = int(server_port)
        self.base_dir = self.data_root / "server"
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.master_key_path = self.base_dir / "remote_share.master.key"
        self.state_path = self.base_dir / "remote_share_state.enc"
        self.audit_path = self.base_dir / "remote_share_audit.jsonl"
        self._fernet = Fernet(self._load_or_create_master_key())
        self._event_hook = event_hook
        self.upnp = UPnPManager()
        self.enabled = False
        self.server_secret = b""
        self.invites: Dict[str, RemoteInviteSession] = {}
        self.cookie_sessions: Dict[str, RotatingCookieSession] = {}
        self.cookie_hashes: Dict[str, str] = {}
        self._usage: Dict[str, list[Dict[str, Any]]] = {}
        self._load_state()

    def _record_audit(self, event: str, payload: Optional[Dict[str, Any]] = None, *, outcome: str = "ok") -> Dict[str, Any]:
        entry = {
            "id": secrets.token_hex(8),
            "ts": time.time(),
            "event": sanitize_inline_text(str(event or "remote_share.event"), max_chars=120),
            "outcome": sanitize_inline_text(str(outcome or "ok"), max_chars=60),
            "payload": sanitize_mapping(dict(payload or {}), max_chars=400),
        }
        try:
            append_text_atomically(self.audit_path, json.dumps(entry, default=str) + "\n")
        except Exception as exc:
            logger.debug("[remote_share] audit write failed: %s", exc)
        if callable(self._event_hook):
            try:
                self._event_hook(dict(entry))
            except Exception as exc:
                logger.debug("[remote_share] event hook failed: %s", exc)
        return entry

    def _status_guidance(self, public_base_url: str, upnp_status: Dict[str, Any]) -> list[str]:
        guidance: list[str] = []
        public_url = sanitize_inline_text(str(public_base_url or "").strip(), max_chars=300)
        if public_url.startswith("https://"):
            guidance.append("HTTPS public base URL detected; secure __Host- cookies are preferred.")
        elif public_url:
            guidance.append("Public base URL is not HTTPS; secure __Host- cookie hardening is unavailable.")
        else:
            guidance.append("Set a public base URL when using a reverse proxy so invite URLs stay copy-ready and cookie policy can be inferred.")
        if upnp_status.get("mapped_port"):
            guidance.append("UPnP mapping is active; direct public access is available on the mapped port.")
        elif public_url:
            guidance.append("No direct UPnP mapping is active; reverse-proxy access is expected from the configured public base URL.")
        else:
            guidance.append("No UPnP mapping or public base URL detected yet; remote share links may not be reachable externally.")
        return guidance

    def _prune_usage(self, cookie_id: str) -> list[Dict[str, Any]]:
        current_time = time.time()
        recent = [
            item for item in list(self._usage.get(cookie_id) or [])
            if current_time - float(item.get("ts") or 0.0) < _REMOTE_SHARE_ANOMALY_WINDOW_SECONDS
        ]
        self._usage[cookie_id] = recent
        return recent

    def _auto_revoke(self, cookie_session: RotatingCookieSession, invite: Optional[RemoteInviteSession], reason: str, remote_ip: str = "") -> None:
        self._revoke_cookie_family(cookie_session.family_id)
        if invite is not None:
            invite.active = False
        self._save_state()
        self._record_audit(
            "remote_share.auto_revoked",
            {
                "reason": str(reason or "anomaly"),
                "token_id": str(cookie_session.subject_id or ""),
                "cookie_id": str(cookie_session.cookie_id or ""),
                "remote_ip": str(remote_ip or cookie_session.remote_ip or ""),
            },
            outcome="revoked",
        )

    def _record_usage_event(self, cookie_session: RotatingCookieSession, remote_ip: str = "", user_agent: str = "") -> tuple[bool, str]:
        cookie_id = str(cookie_session.cookie_id or "")
        if not cookie_id:
            return False, ""
        recent = self._prune_usage(cookie_id)
        current_hash = hashlib.sha256(str(user_agent or "").encode("utf-8")).hexdigest() if user_agent else ""
        recent.append({"ts": time.time(), "ip": sanitize_inline_text(str(remote_ip or ""), max_chars=120), "ua": current_hash})
        self._usage[cookie_id] = recent[-256:]
        unique_ips = {str(item.get("ip") or "") for item in recent if str(item.get("ip") or "")}
        if len(unique_ips) > _REMOTE_SHARE_MAX_UNIQUE_IPS:
            return True, "multiple_ips_detected"
        if len(recent) > _REMOTE_SHARE_MAX_REQUESTS_PER_WINDOW:
            return True, "excessive_requests_detected"
        unique_uas = {str(item.get("ua") or "") for item in recent if str(item.get("ua") or "")}
        if len(unique_uas) > 1:
            return True, "multiple_user_agents_detected"
        return False, ""

    def _load_or_create_master_key(self) -> bytes:
        if self.master_key_path.exists():
            return self.master_key_path.read_bytes().strip()
        key = Fernet.generate_key()
        write_text_atomically(self.master_key_path, key.decode("utf-8"))
        return key

    def _read_state(self) -> Dict[str, Any]:
        if not self.state_path.exists():
            return {}
        try:
            encrypted_b64 = self.state_path.read_text(encoding="utf-8").strip()
            if not encrypted_b64:
                return {}
            payload = base64.urlsafe_b64decode(encrypted_b64.encode("utf-8"))
            decoded = self._fernet.decrypt(payload)
            data = json.loads(decoded.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception as exc:
            logger.warning("[remote_share] failed to read encrypted state: %s", exc)
            return {}

    def _write_state(self, state: Dict[str, Any]) -> None:
        payload = json.dumps(state, indent=2, sort_keys=True).encode("utf-8")
        encrypted = self._fernet.encrypt(payload)
        write_text_atomically(self.state_path, base64.urlsafe_b64encode(encrypted).decode("utf-8"))

    def _load_state(self) -> None:
        state = self._read_state()
        secret_hex = str(state.get("server_secret") or "").strip()
        if secret_hex:
            try:
                self.server_secret = bytes.fromhex(secret_hex)
            except Exception:
                self.server_secret = b""
        if not self.server_secret:
            self.server_secret = secrets.token_bytes(32)
        self.enabled = bool(state.get("enabled", False))
        self.invites = {}
        self.cookie_sessions = {}
        self.cookie_hashes = {}
        now = time.time()
        for item in list(state.get("invites") or []):
            try:
                invite = RemoteInviteSession(**item)
            except Exception:
                continue
            if not invite.active:
                continue
            if invite.expires_at > 0 and invite.expires_at <= now:
                continue
            self.invites[invite.token_id] = invite
        for item in list(state.get("cookie_sessions") or []):
            try:
                cookie_session = RotatingCookieSession(**item)
            except Exception:
                continue
            if cookie_session.revoked:
                continue
            if cookie_session.expires_at > 0 and cookie_session.expires_at <= now:
                continue
            if cookie_session.subject_id not in self.invites:
                continue
            self.cookie_sessions[cookie_session.cookie_id] = cookie_session
        raw_hashes = dict(state.get("cookie_hashes") or {})
        valid_cookie_ids = set(self.cookie_sessions.keys())
        self.cookie_hashes = {str(cookie_hash): str(cookie_id) for cookie_hash, cookie_id in raw_hashes.items() if str(cookie_id) in valid_cookie_ids}
        self._save_state()

    def _save_state(self) -> None:
        now = time.time()
        invites = []
        for invite in self.invites.values():
            if not invite.active:
                continue
            if invite.expires_at > 0 and invite.expires_at <= now:
                continue
            invites.append(asdict(invite))
        cookie_sessions = []
        for cookie_session in self.cookie_sessions.values():
            if cookie_session.revoked:
                continue
            if cookie_session.expires_at > 0 and cookie_session.expires_at <= now:
                continue
            if cookie_session.subject_id not in self.invites:
                continue
            cookie_sessions.append(asdict(cookie_session))
        valid_cookie_ids = {item["cookie_id"] for item in cookie_sessions}
        state = {
            "server_secret": self.server_secret.hex(),
            "enabled": self.enabled,
            "invites": invites,
            "cookie_sessions": cookie_sessions,
            "cookie_hashes": {cookie_hash: cookie_id for cookie_hash, cookie_id in self.cookie_hashes.items() if cookie_id in valid_cookie_ids},
        }
        self._write_state(state)

    async def restore(self, public_base_url: str = "") -> Dict[str, Any]:
        if self.enabled:
            await self.enable(public_base_url=public_base_url, persist=False)
        return self.status(public_base_url=public_base_url)

    async def enable(self, public_base_url: str = "", persist: bool = True) -> Dict[str, Any]:
        mapped = False
        await self.upnp.discover()
        if self.upnp.initialized:
            mapped = await self.upnp.add_port_mapping(self.server_port, self.server_port)
        self.enabled = True
        if persist:
            self._save_state()
        self._record_audit(
            "remote_share.enabled",
            {
                "public_base_url": str(public_base_url or ""),
                "external_ip": str(self.upnp.external_ip or ""),
                "mapped_port": int(self.upnp.mapped_port or 0),
                "upnp_mapped": bool(mapped),
            },
        )
        return self.status(public_base_url=public_base_url, upnp_mapped=mapped)

    async def disable(self) -> Dict[str, Any]:
        self.enabled = False
        for invite in self.invites.values():
            invite.active = False
        for cookie_session in self.cookie_sessions.values():
            cookie_session.revoked = True
        self.cookie_hashes.clear()
        await self.upnp.remove_port_mapping()
        self._save_state()
        self._record_audit("remote_share.disabled", {"invite_count": len(self.invites), "session_count": len(self.cookie_sessions)})
        return self.status()

    async def close(self) -> None:
        await self.upnp.remove_port_mapping()

    async def renew_if_needed(self) -> bool:
        if not self.enabled:
            return False
        return await self.upnp.renew_mapping()

    def _public_url(self, public_base_url: str = "") -> str:
        base = str(public_base_url or "").strip().rstrip("/")
        if base:
            return base
        if self.upnp.external_ip and self.upnp.mapped_port:
            return f"http://{self.upnp.external_ip}:{self.upnp.mapped_port}"
        return ""

    def status(self, public_base_url: str = "", upnp_mapped: Optional[bool] = None) -> Dict[str, Any]:
        url = self._public_url(public_base_url)
        now = time.time()
        active_invites = sum(1 for invite in self.invites.values() if invite.active and (invite.expires_at <= 0 or invite.expires_at > now))
        active_sessions = sum(1 for cookie_session in self.cookie_sessions.values() if not cookie_session.revoked and (cookie_session.expires_at <= 0 or cookie_session.expires_at > now))
        upnp_status = self.upnp.status()
        secure_preferred = secure_cookie_preferred(url)
        transport_mode = "https_reverse_proxy" if secure_preferred else "upnp_direct" if bool(upnp_status.get("mapped_port")) else "local_only"
        return {
            "enabled": self.enabled,
            "cookie_name": remote_share_cookie_name(secure=secure_preferred),
            "cookie_name_insecure": remote_share_cookie_name(secure=False),
            "cookie_name_secure": remote_share_cookie_name(secure=True),
            "secure_cookie_preferred": secure_preferred,
            "cookie_rotation_seconds": _REMOTE_SHARE_COOKIE_ROTATE_AFTER_SECONDS,
            "cookie_max_age": _REMOTE_SHARE_COOKIE_MAX_AGE,
            "invite_count": active_invites,
            "session_count": active_sessions,
            "public_url": url,
            "transport_mode": transport_mode,
            "audit_path": str(self.audit_path),
            "guidance": self._status_guidance(url, upnp_status),
            "upnp": {
                **upnp_status,
                "mapped": bool(upnp_status.get("mapped_port")),
                "last_enable_mapped": bool(upnp_mapped) if upnp_mapped is not None else bool(upnp_status.get("mapped_port")),
            },
        }

    def create_invite(self, name: str = "Remote User", hours: int = 24, public_base_url: str = "") -> Dict[str, Any]:
        token_id = secrets.token_hex(4)
        safe_name = sanitize_inline_text(str(name or "Remote User").replace("|", "-"), max_chars=80) or "Remote User"
        created = time.time()
        expires = 0 if int(hours or 0) <= 0 else created + (int(hours) * 3600)
        payload = f"{token_id}|{safe_name}|{int(created)}|{int(expires)}"
        signature = hmac.new(self.server_secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()[:16]
        token = f"{payload}|{signature}"
        token_b64 = base64.urlsafe_b64encode(token.encode("utf-8")).decode("utf-8").rstrip("=")
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        self.invites[token_id] = RemoteInviteSession(
            token_id=token_id,
            name=safe_name,
            created_at=created,
            expires_at=expires,
            token_hash=token_hash,
            token_value=token_b64,
        )
        self._save_state()
        self._record_audit(
            "remote_share.invite_created",
            {
                "token_id": token_id,
                "name": safe_name,
                "expires_in_hours": int(hours or 0),
            },
        )
        public_url = self._public_url(public_base_url)
        return {
            "token_id": token_id,
            "token": token_b64,
            "name": safe_name,
            "expires_in_hours": int(hours or 0),
            "url": f"{public_url}/join?t={urllib.parse.quote(token_b64, safe='')}" if public_url else "",
        }

    def validate_invite(self, token_b64: str) -> Optional[RemoteInviteSession]:
        token_b64 = str(token_b64 or "").strip()
        if not token_b64:
            return None
        try:
            padded = token_b64 + "=" * ((4 - len(token_b64) % 4) % 4)
            raw_token = base64.urlsafe_b64decode(padded.encode("utf-8")).decode("utf-8")
            token_id, name, created_str, expires_str, signature = raw_token.split("|", 4)
        except Exception:
            return None
        payload = f"{token_id}|{name}|{created_str}|{expires_str}"
        expected = hmac.new(self.server_secret, payload.encode("utf-8"), hashlib.sha256).hexdigest()[:16]
        if not hmac.compare_digest(signature, expected):
            return None
        invite = self.invites.get(str(token_id or ""))
        if invite is None or not invite.active:
            return None
        if invite.expires_at > 0 and time.time() > invite.expires_at:
            return None
        if invite.token_hash != hashlib.sha256(raw_token.encode("utf-8")).hexdigest():
            return None
        return invite

    def _cookie_hash(self, cookie_value: str) -> str:
        return hashlib.sha256(str(cookie_value or "").encode("utf-8")).hexdigest()

    def _cookie_expired(self, cookie_session: RotatingCookieSession) -> bool:
        return cookie_session.expires_at > 0 and time.time() > cookie_session.expires_at

    def _issue_cookie(self, subject_id: str, remote_ip: str = "", user_agent: str = "", family_id: str = "") -> str:
        created = time.time()
        expires = created + _REMOTE_SHARE_COOKIE_MAX_AGE
        cookie_id = secrets.token_hex(8)
        cookie_value = secrets.token_urlsafe(48)
        self.cookie_sessions[cookie_id] = RotatingCookieSession(
            cookie_id=cookie_id,
            family_id=str(family_id or secrets.token_hex(8)),
            subject_id=str(subject_id or ""),
            created_at=created,
            expires_at=expires,
            last_seen=created,
            remote_ip=str(remote_ip or ""),
            user_agent_hash=hashlib.sha256(str(user_agent or "").encode("utf-8")).hexdigest() if user_agent else "",
        )
        self.cookie_hashes[self._cookie_hash(cookie_value)] = cookie_id
        return cookie_value

    def _revoke_cookie_family(self, family_id: str) -> None:
        family_id = str(family_id or "")
        if not family_id:
            return
        for cookie_session in self.cookie_sessions.values():
            if cookie_session.family_id == family_id:
                cookie_session.revoked = True

    def _rotate_cookie(self, cookie_session: RotatingCookieSession, remote_ip: str = "", user_agent: str = "") -> str:
        replacement = self._issue_cookie(cookie_session.subject_id, remote_ip=remote_ip or cookie_session.remote_ip, user_agent=user_agent, family_id=cookie_session.family_id)
        replacement_cookie_id = self.cookie_hashes.get(self._cookie_hash(replacement), "")
        cookie_session.replaced_by_cookie_id = replacement_cookie_id
        cookie_session.grace_until = time.time() + _REMOTE_SHARE_COOKIE_REPLAY_GRACE_SECONDS
        return replacement

    def register_join(self, invite: RemoteInviteSession, remote_ip: str = "", user_agent: str = "") -> str:
        invite.remote_ip = sanitize_inline_text(str(remote_ip or invite.remote_ip or ""), max_chars=120)
        invite.last_seen = time.time()
        cookie_value = self._issue_cookie(invite.token_id, remote_ip=remote_ip, user_agent=user_agent)
        self._save_state()
        self._record_audit(
            "remote_share.join_registered",
            {
                "token_id": invite.token_id,
                "name": invite.name,
                "remote_ip": invite.remote_ip,
            },
        )
        return cookie_value

    def authenticate_cookie(self, cookie_value: str, remote_ip: str = "", user_agent: str = "") -> RemoteCookieAuthResult:
        cookie_id = self.cookie_hashes.get(self._cookie_hash(cookie_value))
        if not cookie_id:
            return RemoteCookieAuthResult(session=None)
        cookie_session = self.cookie_sessions.get(cookie_id)
        if not cookie_session or cookie_session.revoked or self._cookie_expired(cookie_session):
            return RemoteCookieAuthResult(session=None)
        if cookie_session.replaced_by_cookie_id and time.time() > float(cookie_session.grace_until or 0.0):
            self._revoke_cookie_family(cookie_session.family_id)
            self._save_state()
            return RemoteCookieAuthResult(session=None, replay_detected=True)
        if cookie_session.user_agent_hash and user_agent:
            incoming_hash = hashlib.sha256(str(user_agent).encode("utf-8")).hexdigest()
            if incoming_hash != cookie_session.user_agent_hash:
                self._auto_revoke(cookie_session, self.invites.get(cookie_session.subject_id), "user_agent_changed", remote_ip=remote_ip)
                return RemoteCookieAuthResult(session=None, replay_detected=True)
        invite = self.invites.get(cookie_session.subject_id)
        if invite is None or not invite.active or (invite.expires_at > 0 and invite.expires_at <= time.time()):
            return RemoteCookieAuthResult(session=None)
        anomaly, reason = self._record_usage_event(cookie_session, remote_ip=remote_ip, user_agent=user_agent)
        if anomaly:
            self._auto_revoke(cookie_session, invite, reason or "anomaly", remote_ip=remote_ip)
            return RemoteCookieAuthResult(session=None, replay_detected=True)
        replacement_cookie = ""
        if not cookie_session.replaced_by_cookie_id and (time.time() - float(cookie_session.created_at or 0.0)) >= _REMOTE_SHARE_COOKIE_ROTATE_AFTER_SECONDS:
            replacement_cookie = self._rotate_cookie(cookie_session, remote_ip=remote_ip, user_agent=user_agent)
            self._record_audit(
                "remote_share.cookie_rotated",
                {
                    "token_id": invite.token_id,
                    "cookie_id": cookie_session.cookie_id,
                    "remote_ip": str(remote_ip or cookie_session.remote_ip or ""),
                },
            )
        cookie_session.last_seen = time.time()
        if remote_ip:
            cookie_session.remote_ip = sanitize_inline_text(str(remote_ip), max_chars=120)
            invite.remote_ip = sanitize_inline_text(str(remote_ip), max_chars=120)
        invite.last_seen = time.time()
        if replacement_cookie or remote_ip:
            self._save_state()
        return RemoteCookieAuthResult(session=invite, replacement_cookie=replacement_cookie)

    def revoke_invite(self, token_id: str) -> bool:
        invite = self.invites.get(str(token_id or ""))
        if invite is None:
            return False
        invite.active = False
        for cookie_session in self.cookie_sessions.values():
            if cookie_session.subject_id == invite.token_id:
                cookie_session.revoked = True
        self._save_state()
        self._record_audit("remote_share.invite_revoked", {"token_id": invite.token_id, "name": invite.name})
        return True

    def list_invites(self, public_base_url: str = "") -> list[Dict[str, Any]]:
        public_url = self._public_url(public_base_url)
        now = time.time()
        items: list[Dict[str, Any]] = []
        for invite in self.invites.values():
            if not invite.active:
                continue
            if invite.expires_at > 0 and invite.expires_at <= now:
                continue
            items.append({
                "token_id": invite.token_id,
                "name": sanitize_inline_text(invite.name, max_chars=80),
                "created_at": invite.created_at,
                "expires_at": invite.expires_at,
                "remote_ip": sanitize_inline_text(invite.remote_ip, max_chars=120),
                "last_seen": invite.last_seen,
                "join_url": f"{public_url}/join?t={urllib.parse.quote(invite.token_value, safe='')}" if public_url and invite.token_value else "",
            })
        items.sort(key=lambda item: float(item.get("created_at") or 0.0), reverse=True)
        return items

    def audit_events(self, limit: int = 50) -> list[Dict[str, Any]]:
        if not self.audit_path.exists():
            return []
        lines = self.audit_path.read_text(encoding="utf-8").splitlines()
        events: list[Dict[str, Any]] = []
        for line in lines[-max(1, int(limit or 50)):]:
            line = str(line or "").strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except Exception:
                continue
            if isinstance(parsed, dict):
                events.append(parsed)
        return events


def secure_cookie_preferred(public_base_url: str) -> bool:
    return str(public_base_url or "").strip().lower().startswith("https://")


def remote_share_cookie_name(*, secure: bool = False) -> str:
    return _REMOTE_SHARE_SECURE_COOKIE_NAME if secure else _REMOTE_SHARE_COOKIE_NAME


def remote_share_cookie_names() -> tuple[str, str]:
    return (_REMOTE_SHARE_SECURE_COOKIE_NAME, _REMOTE_SHARE_COOKIE_NAME)
