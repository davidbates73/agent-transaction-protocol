"""ATP record builder / signer / append-only writer for the Hermes recorder plugin.

Standard library only. Signing uses the vendored ATP Python reference
implementation (atp.py, byte-identical to implementations/python/atp.py).

Each Hermes hook event becomes exactly one ATP node (draft-bates-atp §8):

  timestamp  now, whole UTC seconds (§8.1)
  scope      Hermes session_id (one chain per session, §12)
  issuer     {issuerId: config issuer_id, keyId: "ed25519:" + first 16 hex of sha256(pub)}
  agent      {agentId: "hermes-agent", version: detected Hermes version}
  action     {type: atp:request | atp:completion | atp:failure (§16.2, §17),
              subtype: the Hermes hook name, inputHash / outputHash (§8.2)}
  parents    [previous record in this session] (+ the paired request record, when
              it is not already the previous record) -- [] for a session's first record
  profile    PROFILE_ID (harness fields below are profile-defined, §20.3/§20.4)
  hermes     allowlisted correlation/metadata fields; seq = position in session
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
from typing import Any, Dict, Optional
from urllib.parse import urlsplit, urlunsplit

from . import atp

logger = logging.getLogger("atp_recorder")

PROFILE_ID = "https://davidbates73.github.io/agent-transaction-protocol/atp/profile/hermes-recorder/0.1.0"
AGENT_ID = "hermes-agent"
DEFAULT_ISSUER = "hermes-atp-recorder.local"
TARGET_SCHEMA = "hermes.observer.v1"
MAX_SAFE = (1 << 53) - 1
MAX_STR = 256
MAX_DEPTH = 64

ACTION_TYPE = {
    "pre_api_request": "atp:request",
    "post_api_request": "atp:completion",
    "api_request_error": "atp:failure",
    "pre_tool_call": "atp:request",
    "post_tool_call": "atp:completion",  # atp:failure when status != "ok"
}
FAMILY = {
    "pre_api_request": "api", "post_api_request": "api", "api_request_error": "api",
    "pre_tool_call": "tool", "post_tool_call": "tool",
}

# kwarg -> (record field, kind). Anything not listed here is never written.
ALLOWLIST = {
    "telemetry_schema_version": ("schema", "str"),
    "session_id": ("sessionId", "str"),
    "task_id": ("taskId", "str"),
    "turn_id": ("turnId", "str"),
    "api_request_id": ("apiRequestId", "str"),
    "api_call_count": ("apiCallCount", "int"),
    "tool_call_id": ("toolCallId", "str"),
    "tool_name": ("toolName", "str"),
    "parent_session_id": ("parentSessionId", "str"),
    "parent_turn_id": ("parentTurnId", "str"),
    "parent_subagent_id": ("parentSubagentId", "str"),
    "conversation_id": ("conversationId", "str"),
    "platform": ("platform", "str"),
    "model": ("model", "str"),
    "response_model": ("responseModel", "str"),
    "provider": ("provider", "str"),
    "api_mode": ("apiMode", "str"),
    "base_url": ("baseUrl", "url"),
    "status": ("status", "str"),
    "status_code": ("statusCode", "int"),
    "retry_count": ("retryCount", "int"),
    "max_retries": ("maxRetries", "int"),
    "retryable": ("retryable", "bool"),
    "reason": ("reason", "str"),
    "error_type": ("errorType", "str"),
    "finish_reason": ("finishReason", "str"),
    "message_count": ("messageCount", "int"),
    "tool_count": ("toolCount", "int"),
    "duration_ms": ("durationMs", "int"),
    "api_duration": ("apiDurationMs", "seconds_to_ms"),
    "assistant_tool_call_count": ("assistantToolCallCount", "int"),
}
USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens", "input_tokens", "output_tokens",
              "reasoning_tokens", "cached_tokens", "cache_read_tokens", "cache_write_tokens")

_SENSITIVE_EXACT = {
    "authorization", "proxyauthorization", "apikey", "xapikey", "accesstoken", "refreshtoken", "idtoken",
    "token", "bearer", "secret", "clientsecret", "password", "passwd", "cookie", "setcookie",
    "privatekey", "headers", "credentials", "credential",
}
_SENSITIVE_SUFFIX = ("apikey", "secret", "password", "token_value", "authtoken")
_SCRUB = [
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{6,}"), r"\1 [REDACTED]"),
    (re.compile(r"\b(sk|xai|pk|rk)-[A-Za-z0-9_-]{8,}"), "[REDACTED]"),
]
REDACTED = "[REDACTED]"


def _truthy(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _is_sensitive_key(k: str) -> bool:
    n = re.sub(r"[^a-z0-9]", "", k.lower())
    return n in _SENSITIVE_EXACT or n.endswith(_SENSITIVE_SUFFIX)


def _clean_str(s: str) -> str:
    # Replace unpaired surrogates so every record is I-JSON (RFC 7493).
    return s.encode("utf-8", "replace").decode("utf-8") if atp.has_unpaired_surrogate(s) else s


def normalize(value, depth: int = 0, hits=None):
    """JSON-normalise and redact a payload (sensitive keys -> "[REDACTED]").
    Each key redaction appends to *hits* when a list is passed."""
    if depth > MAX_DEPTH:
        return "[TRUNCATED]"
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value if abs(value) <= MAX_SAFE else str(value)
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return _clean_str(value)
    if isinstance(value, bytes):
        return _clean_str(value.decode("utf-8", "replace"))
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            ks = _clean_str(str(k))
            if _is_sensitive_key(ks):
                out[ks] = REDACTED
                if hits is not None:
                    hits.append(ks)
            else:
                out[ks] = normalize(v, depth + 1, hits)
        return out
    if isinstance(value, (list, tuple)):
        return [normalize(v, depth + 1, hits) for v in value]
    for attr in ("model_dump", "to_dict", "dict"):
        fn = getattr(value, attr, None)
        if callable(fn):
            try:
                return normalize(fn(), depth + 1, hits)
            except Exception:  # noqa: BLE001
                break
    return _clean_str(str(value))


def scrub(value):
    """Scrub credential-shaped substrings (Bearer/Basic, sk-/xai-/pk-/rk- keys) from every string.
    Applied to every payload before hashing, so inputHash/outputHash cover exactly the bytes a
    stored inputBody/outputBody would hold."""
    if isinstance(value, str):
        for rx, rep in _SCRUB:
            value = rx.sub(rep, value)
        return value
    if isinstance(value, list):
        return [scrub(v) for v in value]
    if isinstance(value, dict):
        return {k: scrub(v) for k, v in value.items()}
    return value


def payload_bytes(normalized) -> bytes:
    """Bytes covered by inputHash/outputHash: UTF-8 for strings, JCS (RFC 8785) otherwise."""
    if isinstance(normalized, str):
        return normalized.encode("utf-8")
    try:
        return atp.canonicalize(normalized).encode("utf-8")
    except Exception:  # noqa: BLE001
        return json.dumps(normalized, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")


def _safe_url(u: str) -> str:
    try:
        p = urlsplit(u)
        host = p.hostname or ""
        if p.port:
            host = f"{host}:{p.port}"
        return urlunsplit((p.scheme, host, p.path, "", ""))
    except Exception:  # noqa: BLE001
        return ""


def detect_hermes_version() -> tuple:
    """Best-effort (version, commit). Hermes v0.23.1's authority is
    hermes_cli.version_info.get_version_info(); package metadata reports a "0.0.0"
    placeholder on source installs. v0.23.1 publishes no version accessor in its plugin API
    (hermes_cli/data/plugin_api.json), so this one guarded private import remains; it is the
    only one, and a failure just falls back to metadata, then "unknown"."""
    try:
        from hermes_cli.version_info import get_version_info
        info = get_version_info()
        v = getattr(info, "derived_version", None) or getattr(info, "base_version", None)
        if isinstance(v, str) and v and v != "unknown":
            c = getattr(info, "commit", None)
            return v, (c if isinstance(c, str) and c else None)
    except Exception:  # noqa: BLE001
        pass
    try:
        from importlib import metadata
        for dist in ("hermes-agent", "hermes_agent"):
            try:
                v = metadata.version(dist)
                if v and v != "0.0.0":
                    return v, None
            except Exception:  # noqa: BLE001
                pass
    except Exception:  # noqa: BLE001
        pass
    return "unknown", None


def key_id_for(pub: bytes) -> str:
    return "ed25519:" + hashlib.sha256(pub).hexdigest()[:16]


class Config:
    def __init__(self, atp_home: str, log_path: str, key_path: str, pub_path: str,
                 log_bodies: bool = False, issuer_id: str = DEFAULT_ISSUER):
        self.atp_home = atp_home
        self.log_path = log_path
        self.key_path = key_path
        self.pub_path = pub_path
        self.log_bodies = log_bodies
        self.issuer_id = issuer_id

    @classmethod
    def from_environment(cls, env=None) -> "Config":
        env = os.environ if env is None else env
        hermes_home = env.get("HERMES_HOME") or os.path.join(os.path.expanduser("~"), ".hermes")
        atp_home = env.get("HERMES_ATP_HOME") or os.path.join(hermes_home, "atp")
        cfg_path = env.get("HERMES_ATP_CONFIG") or os.path.join(atp_home, "config.json")
        file_cfg: Dict[str, Any] = {}
        if os.path.isfile(cfg_path):
            try:
                with open(cfg_path, encoding="utf-8") as f:
                    loaded = json.load(f)
                if isinstance(loaded, dict):
                    file_cfg = loaded
            except Exception:  # noqa: BLE001
                logger.warning("atp-recorder: ignoring unreadable config %s", cfg_path)

        def pick(env_name, key, default):
            if env.get(env_name) not in (None, ""):
                return env[env_name]
            if file_cfg.get(key) not in (None, ""):
                return file_cfg[key]
            return default

        def path(p):
            return os.path.abspath(os.path.expanduser(str(p)))

        key_path = path(pick("HERMES_ATP_KEY", "key_path", os.path.join(atp_home, "key")))
        return cls(
            atp_home=path(atp_home),
            log_path=path(pick("HERMES_ATP_LOG", "log_path", os.path.join(atp_home, "records.jsonl"))),
            key_path=key_path,
            pub_path=path(pick("HERMES_ATP_PUB", "pub_path", key_path + ".pub")),
            log_bodies=_truthy(pick("HERMES_ATP_LOG_BODIES", "log_bodies", False)),
            issuer_id=str(pick("HERMES_ATP_ISSUER", "issuer_id", DEFAULT_ISSUER)),
        )


class Recorder:
    def __init__(self, config: Config, clock=None, hermes_version: Optional[str] = None):
        self.config = config
        self._clock = clock or time.time
        self._hermes_version = hermes_version
        self._hermes_commit: Optional[str] = None
        self._lock = threading.RLock()
        self._seed: Optional[bytes] = None
        self._pub: Optional[bytes] = None
        self._key_id: Optional[str] = None
        self._sessions: Dict[str, dict] = {}
        self._requests: Dict[tuple, dict] = {}
        self._indexed = False

    @classmethod
    def from_environment(cls, env=None) -> "Recorder":
        return cls(Config.from_environment(env))

    # -- key management -------------------------------------------------
    def _ensure_key(self):
        if self._seed is not None:
            return
        kp = self.config.key_path
        if not os.path.exists(kp):
            os.makedirs(os.path.dirname(kp), mode=0o700, exist_ok=True)
            seed = os.urandom(32)
            pub = atp.ed25519_public_key(seed)
            doc = {
                "warning": "PRIVATE Ed25519 signing key for the Hermes ATP recorder. Keep secret.",
                "alg": "Ed25519",
                "keyId": key_id_for(pub),
                "seedHex": seed.hex(),
                "publicKeyBase64": base64.b64encode(pub).decode("ascii"),
            }
            try:
                fd = os.open(kp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass  # another process won the race; load theirs below
            else:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(json.dumps(doc, indent=2) + "\n")
                os.chmod(kp, 0o600)
        self._load_key(kp)
        if not os.path.exists(self.config.pub_path):
            with open(self.config.pub_path, "w", encoding="utf-8") as f:
                f.write(base64.b64encode(self._pub).decode("ascii") + "\n")

    def _load_key(self, kp: str):
        try:
            mode = os.stat(kp).st_mode
            if mode & 0o077:
                logger.warning("atp-recorder: key file %s is readable by group/others; chmod 600 it", kp)
        except OSError:
            pass
        with open(kp, encoding="utf-8") as f:
            text = f.read().strip()
        if re.fullmatch(r"[0-9a-fA-F]{64}", text):
            seed = bytes.fromhex(text)
        else:
            doc = json.loads(text)
            seed = bytes.fromhex(doc["seedHex"])
        if len(seed) != 32:
            raise ValueError("Ed25519 seed must be 32 bytes")
        self._seed = seed
        self._pub = atp.ed25519_public_key(seed)
        self._key_id = key_id_for(self._pub)

    @property
    def public_key(self) -> bytes:
        with self._lock:
            self._ensure_key()
            return self._pub

    # -- log index (resume chains across restarts) ------------------------
    def _ensure_index(self):
        if self._indexed:
            return
        self._indexed = True
        lp = self.config.log_path
        if not os.path.exists(lp):
            return
        with open(lp, "rb") as f:
            data = f.read()
        for raw in data.split(b"\n"):
            try:
                n = json.loads(raw.decode("utf-8"))
                scope, seq, nid = n["scope"], n["hermes"]["seq"], n["nodeId"]
            except Exception:  # noqa: BLE001
                continue
            if isinstance(scope, str) and isinstance(seq, int) and isinstance(nid, str):
                st = self._sessions.get(scope)
                if st is None or seq > st["seq"]:
                    self._sessions[scope] = {"id": nid, "seq": seq}
        if data and not data.endswith(b"\n"):
            # A torn final line (crash mid-write): terminate it so it stays visible
            # as evidence instead of corrupting the next record. Append-only.
            self._append(b"\n")

    def _append(self, data: bytes):
        lp = self.config.log_path
        d = os.path.dirname(lp)
        if d:
            os.makedirs(d, mode=0o700, exist_ok=True)
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(lp, flags, 0o600)
        try:
            try:
                import fcntl  # POSIX advisory lock for cross-process appends
                fcntl.flock(fd, fcntl.LOCK_EX)
            except Exception:  # noqa: BLE001
                pass
            view = memoryview(data)
            while view:
                n = os.write(fd, view)
                view = view[n:]
            os.fsync(fd)
        finally:
            os.close(fd)

    def hermes_version(self) -> str:
        if self._hermes_version is None:
            self._hermes_version, self._hermes_commit = detect_hermes_version()
        return self._hermes_version

    # -- record construction ----------------------------------------------
    def build(self, event: str, kw: Dict[str, Any]) -> tuple:
        family = FAMILY[event]
        session = kw.get("session_id") or kw.get("task_id") or "unknown-session"
        scope = _clean_str(str(session))
        corr_key = "api_request_id" if family == "api" else "tool_call_id"
        corr = kw.get(corr_key)
        corr = _clean_str(str(corr)) if corr not in (None, "") else None

        st = self._sessions.get(scope)
        seq = 0 if st is None else st["seq"] + 1
        parents = [] if st is None else [st["id"]]

        ext: Dict[str, Any] = {"event": event, "seq": seq}
        for src, (dst, kind) in ALLOWLIST.items():
            v = kw.get(src)
            if v is None or v == "":
                continue
            if kind == "str" and isinstance(v, (str, int)) and not isinstance(v, bool):
                ext[dst] = _clean_str(str(v))[:MAX_STR]
            elif kind == "int" and isinstance(v, int) and not isinstance(v, bool) and abs(v) <= MAX_SAFE:
                ext[dst] = v
            elif kind == "bool" and isinstance(v, bool):
                ext[dst] = v
            elif kind == "url" and isinstance(v, str):
                ext[dst] = _safe_url(v)[:MAX_STR]
            elif kind == "seconds_to_ms" and isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v):
                ext[dst] = int(round(v * 1000))
        ext["sessionId"] = scope
        err = kw.get("error")
        if isinstance(err, dict) and isinstance(err.get("type"), str):
            ext.setdefault("errorType", _clean_str(err["type"])[:MAX_STR])
        usage = kw.get("usage")
        if isinstance(usage, dict):
            u = {k: usage[k] for k in USAGE_KEYS
                 if isinstance(usage.get(k), int) and not isinstance(usage.get(k), bool) and 0 <= usage[k] <= MAX_SAFE}
            if u:
                ext["usage"] = u
        if seq == 0:
            ext["hermesVersion"] = self.hermes_version()
            if self._hermes_commit:
                ext["hermesCommit"] = self._hermes_commit[:64]
        schema = kw.get("telemetry_schema_version")
        if schema is not None and schema != TARGET_SCHEMA:
            ext["schemaMismatch"] = True
        ext["bodiesLogged"] = bool(self.config.log_bodies)

        if event == "pre_api_request":
            inp, out = kw.get("request"), None
        elif event == "post_api_request":
            inp, out = None, kw.get("response")
        elif event == "api_request_error":
            inp, out = kw.get("request"), kw.get("error")
        elif event == "pre_tool_call":
            inp, out = kw.get("args"), None
        else:
            inp, out = kw.get("args"), kw.get("result")

        action: Dict[str, Any] = {"type": ACTION_TYPE[event], "subtype": event}
        if event == "post_tool_call" and kw.get("status") not in (None, "ok"):
            action["type"] = "atp:failure"

        def digest(value, which):
            # Hash, length and (optional) stored body all cover the SAME redacted payload, so
            # sha256(payload_bytes(<which>Body)) == <which>Hash and verify can recompute it.
            hits: list = []
            raw = normalize(value, hits=hits)
            n = scrub(raw)
            b = payload_bytes(n)
            ext[which + "Length"] = len(b)
            if hits or n != raw:
                ext[which + "Redacted"] = True
            if self.config.log_bodies:
                ext[which + "Body"] = n
            return "sha256:" + hashlib.sha256(b).hexdigest()

        if inp is not None:
            action["inputHash"] = digest(inp, "input")
        if out is not None:
            action["outputHash"] = digest(out, "output")

        req = None
        if event.startswith("pre_"):
            pass
        elif corr is not None:
            req = self._requests.get((scope, family, corr))
        if req is not None:
            if "inputHash" not in action and req.get("inputHash"):
                action["inputHash"] = req["inputHash"]  # §16.2: completion SHOULD repeat inputHash
            if req["nodeId"] not in parents:
                parents.append(req["nodeId"])
            ext["requestNodeId"] = req["nodeId"]

        fields = {
            "timestamp": atp.format_atp_timestamp(self._clock()),
            "scope": scope,
            "issuer": {"issuerId": self.config.issuer_id, "keyId": self._key_id},
            "agent": {"agentId": AGENT_ID, "version": self.hermes_version()},
            "action": action,
            "parents": parents,
            "profile": PROFILE_ID,
            "hermes": ext,
        }
        node = atp.create_node(fields, self._seed)
        return node, scope, family, corr, seq

    def record(self, event: str, kw: Dict[str, Any]) -> Optional[dict]:
        if event not in FAMILY:
            return None
        with self._lock:
            self._ensure_key()
            self._ensure_index()
            node, scope, family, corr, seq = self.build(event, kw)
            line = json.dumps(node, ensure_ascii=False, separators=(",", ":")) + "\n"
            self._append(line.encode("utf-8"))
            self._sessions[scope] = {"id": node["nodeId"], "seq": seq}
            if corr is not None:
                if event.startswith("pre_"):
                    self._requests[(scope, family, corr)] = {
                        "nodeId": node["nodeId"], "inputHash": node["action"].get("inputHash")}
                else:
                    self._requests.pop((scope, family, corr), None)
            return node
