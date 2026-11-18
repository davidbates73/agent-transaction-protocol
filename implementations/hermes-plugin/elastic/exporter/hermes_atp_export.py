#!/usr/bin/env python3
"""hermes-atp-export: ship Hermes ATP recorder logs to Elasticsearch (stdlib only).

Reads records.jsonl READ-ONLY (never writes, locks or truncates it), verifies every record
with the ATP reference verifier (atp_log.check_line / chain_step, the same code as
`atp_cli.py verify`), and bulk-indexes one document per line into Elasticsearch.

  * Idempotent: document _id = the record's nodeId (intact records; anything else: sha256 of
    the raw line), sent as a bulk `create`; a 409 "already exists" counts as delivered. Combined with a per-file
    checkpoint (offset + sha256 of everything already shipped) a restart, a re-copied file, an appended
    file or a replaced/truncated file can never produce duplicates.
  * Resilient: if Elasticsearch is down or overloaded the batch is retried with capped
    exponential backoff. Nothing is read past the unacknowledged batch, so the log itself is
    the buffer and memory stays bounded. Hermes is never touched.
  * Modes: "push" (default) follows the live log forever; "copy" processes copied files to
    EOF once and exits (for logs rsync'ed from the Hermes host).

Usage:
    hermes_atp_export.py [--config FILE] [--mode push|copy] [--log PATH ...] [--once]
    hermes_atp_export.py --check-config      # print the resolved config (secrets masked)

See implementations/hermes-plugin/elastic/README.md.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import logging
import os
import random
import signal
import socket
import ssl
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN_DIR = os.path.dirname(os.path.dirname(HERE))  # implementations/hermes-plugin
if PLUGIN_DIR not in sys.path:
    sys.path.insert(0, PLUGIN_DIR)
import atp  # noqa: E402,F401  (vendored ATP reference implementation; atp_log imports it)
import atp_log  # noqa: E402  (vendored record-log verifier)

VERSION = "0.1.0"
log = logging.getLogger("hermes-atp-export")

INVALID_INDEX_SUFFIX = "invalid"
USAGE_FIELDS = {
    "prompt_tokens": "prompt", "completion_tokens": "completion", "total_tokens": "total",
    "input_tokens": "input", "output_tokens": "output", "reasoning_tokens": "reasoning",
    "cached_tokens": "cached", "cache_read_tokens": "cache_read", "cache_write_tokens": "cache_write",
}
# hermes.<field> -> document field. Anything not listed is never shipped.
HERMES_FIELDS = {
    "event": "event", "seq": "seq", "schema": "schema", "sessionId": None, "taskId": "task_id",
    "turnId": "turn_id", "apiRequestId": "api_request_id", "apiCallCount": "api_call_count",
    "toolCallId": "tool_call_id", "toolName": "tool_name", "parentSessionId": "parent_session_id",
    "parentTurnId": "parent_turn_id", "parentSubagentId": "parent_subagent_id",
    "conversationId": "conversation_id", "platform": "platform", "model": "model",
    "responseModel": "response_model", "provider": "provider", "apiMode": "api_mode",
    "baseUrl": "base_url", "status": "status", "statusCode": "status_code", "retryCount": "retry_count",
    "maxRetries": "max_retries", "retryable": "retryable", "reason": "reason", "errorType": "error_type",
    "finishReason": "finish_reason", "messageCount": "message_count", "toolCount": "tool_count",
    "durationMs": "duration_ms", "apiDurationMs": "api_duration_ms",
    "assistantToolCallCount": "assistant_tool_call_count", "inputLength": "input_length",
    "outputLength": "output_length", "inputRedacted": "input_redacted", "outputRedacted": "output_redacted",
    "hermesVersion": "hermes_version", "hermesCommit": "hermes_commit", "requestNodeId": "request_node_id",
    "bodiesLogged": "bodies_logged", "schemaMismatch": "schema_mismatch",
}


# ---------------------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------------------

class ConfigError(Exception):
    pass


def _truthy(v) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def _read_secret_file(path: str) -> str:
    with open(os.path.expanduser(path), encoding="utf-8") as f:
        return f.read().strip()


class Config:
    """Precedence: command line, then environment (HERMES_ES_* / HERMES_ATP_EXPORT_*), then the
    JSON config file, then defaults. Secrets may come from env vars or *_file paths."""

    DEFAULTS = {
        "mode": "push", "es_url": None, "ca_cert": None, "allow_insecure_http": False,
        "username": None, "password": None, "password_file": None, "api_key": None, "api_key_file": None,
        "logs": ["~/.hermes/atp/records.jsonl"], "pub": None, "state_dir": "~/.hermes/atp/export-state",
        "index_prefix": "hermes-atp", "ship_bodies": False, "batch_size": 500, "batch_bytes": 5_000_000,
        "poll_interval": 2.0, "backoff_initial": 1.0, "backoff_max": 60.0, "timeout": 30.0,
    }
    ENV = {
        "mode": "HERMES_ATP_EXPORT_MODE", "es_url": "HERMES_ES_URL", "ca_cert": "HERMES_ES_CA_CERT",
        "allow_insecure_http": "HERMES_ES_ALLOW_INSECURE_HTTP", "username": "HERMES_ES_USERNAME",
        "password": "HERMES_ES_PASSWORD", "password_file": "HERMES_ES_PASSWORD_FILE",
        "api_key": "HERMES_ES_API_KEY", "api_key_file": "HERMES_ES_API_KEY_FILE",
        "logs": "HERMES_ATP_EXPORT_LOGS", "pub": "HERMES_ATP_EXPORT_PUB", "state_dir": "HERMES_ATP_EXPORT_STATE",
        "index_prefix": "HERMES_ATP_EXPORT_INDEX_PREFIX", "ship_bodies": "HERMES_ATP_EXPORT_SHIP_BODIES",
        "batch_size": "HERMES_ATP_EXPORT_BATCH_SIZE", "poll_interval": "HERMES_ATP_EXPORT_POLL_INTERVAL",
    }
    SECRET_KEYS = ("password", "api_key")

    def __init__(self, values: Dict[str, Any]):
        self.__dict__.update(values)

    @classmethod
    def load(cls, config_path: Optional[str] = None, env=None, overrides: Optional[Dict[str, Any]] = None):
        env = os.environ if env is None else env
        v = dict(cls.DEFAULTS)
        path = config_path or env.get("HERMES_ATP_EXPORT_CONFIG")
        if path:
            with open(os.path.expanduser(path), encoding="utf-8") as f:
                doc = json.load(f)
            unknown = set(doc) - set(cls.DEFAULTS)
            if unknown:
                raise ConfigError(f"unknown config keys: {', '.join(sorted(unknown))}")
            v.update(doc)
        for key, name in cls.ENV.items():
            if env.get(name) not in (None, ""):
                v[key] = env[name].split(os.pathsep) if key == "logs" else env[name]
        for key, val in (overrides or {}).items():
            if val is not None:
                v[key] = val
        if v.get("password_file") and not v.get("password"):
            v["password"] = _read_secret_file(v["password_file"])
        if v.get("api_key_file") and not v.get("api_key"):
            v["api_key"] = _read_secret_file(v["api_key_file"])
        for key in ("allow_insecure_http", "ship_bodies"):
            v[key] = _truthy(v[key])
        for key in ("batch_size", "batch_bytes"):
            v[key] = int(v[key])
        for key in ("poll_interval", "backoff_initial", "backoff_max", "timeout"):
            v[key] = float(v[key])
        if isinstance(v["logs"], str):
            v["logs"] = [v["logs"]]
        v["logs"] = [os.path.abspath(os.path.expanduser(p)) for p in v["logs"]]
        for key in ("state_dir", "pub", "ca_cert"):
            if v.get(key):
                v[key] = os.path.abspath(os.path.expanduser(v[key]))
        cfg = cls(v)
        cfg.validate()
        return cfg

    def validate(self):
        if self.mode not in ("push", "copy"):
            raise ConfigError("mode must be 'push' or 'copy'")
        if not self.es_url:
            raise ConfigError("es_url is required (HERMES_ES_URL)")
        if self.es_url.startswith("http://"):
            if not self.allow_insecure_http:
                raise ConfigError("refusing plain http:// to Elasticsearch; use https:// (or set "
                                  "allow_insecure_http, see README 'Plain HTTP')")
        elif not self.es_url.startswith("https://"):
            raise ConfigError("es_url must start with https://")
        if not self.pub:
            raise ConfigError("pub (the recorder's key.pub) is required to verify records")
        if self.api_key and (self.username or self.password):
            raise ConfigError("set either api_key or username/password, not both")
        if not self.api_key and not (self.username and self.password):
            raise ConfigError("credentials required: api_key, or username + password")
        if self.batch_size < 1:
            raise ConfigError("batch_size must be >= 1")

    def masked(self) -> Dict[str, Any]:
        out = {}
        for k in self.DEFAULTS:
            val = getattr(self, k)
            if k in self.SECRET_KEYS:
                val = "(set)" if val else None
            out[k] = val
        return out

    def auth_header(self) -> str:
        if self.api_key:
            return "ApiKey " + self.api_key
        raw = f"{self.username}:{self.password}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")


# ---------------------------------------------------------------------------------------
# Elasticsearch bulk client
# ---------------------------------------------------------------------------------------

class FatalError(Exception):
    """Retrying cannot help (bad credentials, missing privileges, untrusted certificate)."""


class TransientError(Exception):
    """Elasticsearch unreachable or answered 429/5xx: retry the whole batch later."""


class ESClient:
    def __init__(self, cfg: Config):
        self.url = cfg.es_url.rstrip("/")
        self.timeout = cfg.timeout
        self._auth = cfg.auth_header()  # never logged
        self.ctx = None
        if self.url.startswith("https://"):
            self.ctx = ssl.create_default_context(cafile=cfg.ca_cert) if cfg.ca_cert else ssl.create_default_context()
            self.ctx.minimum_version = ssl.TLSVersion.TLSv1_2

    def bulk(self, body: bytes) -> dict:
        req = urllib.request.Request(self.url + "/_bulk", data=body, method="POST", headers={
            "Content-Type": "application/x-ndjson", "Accept": "application/json",
            "Authorization": self._auth, "User-Agent": f"hermes-atp-export/{VERSION}",
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout, context=self.ctx) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            status = e.code
            try:
                e.read()
            finally:
                e.close()
            if status in (401, 403):
                raise FatalError(f"Elasticsearch refused the credentials or privileges (HTTP {status})") from None
            if status == 429 or status >= 500:
                raise TransientError(f"HTTP {status}") from None
            raise FatalError(f"bulk request rejected (HTTP {status})") from None
        except (urllib.error.URLError, socket.timeout, ConnectionError, ssl.SSLError, OSError) as e:
            reason = getattr(e, "reason", e)
            if isinstance(reason, ssl.SSLCertVerificationError):
                raise FatalError(f"TLS certificate not trusted ({reason.verify_message}); check ca_cert") from None
            raise TransientError(f"{type(reason).__name__}: {str(reason)[:200]}") from None


# ---------------------------------------------------------------------------------------
# records -> documents
# ---------------------------------------------------------------------------------------

def index_for(prefix: str, timestamp: Optional[str]) -> str:
    """Monthly index from the record's own (signed) timestamp: deterministic, so a record
    always lands in the same index and _id dedup holds across re-ingests."""
    if isinstance(timestamp, str) and len(timestamp) >= 7 and timestamp[4] == "-":
        return f"{prefix}-records-{timestamp[:4]}.{timestamp[5:7]}"
    return f"{prefix}-records-{INVALID_INDEX_SUFFIX}"


def build_document(raw: bytes, checked: dict, chain_problems: List[tuple], *, index_prefix: str, source: str,
                   offset: int, line: int, ship_bodies: bool, prefix_sha256: str) -> Tuple[str, str, dict]:
    """(index, _id, document) for one log line. Only allowlisted fields are shipped."""
    node = checked["node"]
    problems = list(checked["problems"]) + list(chain_problems)
    record_hash = checked["record_hash"]
    # A record that passes every check is keyed by its nodeId. Anything else (tampered, forged
    # signature, duplicate, out of order, unparseable) is keyed by sha256(prefix hash || line): still
    # deterministic for the same file content (idempotent), but it can never take -- and so shadow,
    # or be swallowed by -- a genuine record's _id, and a duplicated line stays visible.
    if not problems and checked["intact"] and record_hash:
        doc_id = record_hash
    else:
        doc_id = "anomaly-" + hashlib.sha256(prefix_sha256.encode("ascii") + b"\n" + raw).hexdigest()
    doc: Dict[str, Any] = {
        "record_hash": record_hash,
        "verify": {
            "status": problems[0][0] if problems else "valid",
            "valid": not problems,
            "authentic": bool(checked["intact"]),
            "errors": [{"kind": k, "reason": r[:512]} for k, r in problems],
            "verifier": "atp_log.check_line+chain_step (incremental)",
        },
        "source": {"path": source, "offset": offset, "line": line, "bytes": len(raw)},
        "exporter": {"name": "hermes-atp-export", "version": VERSION},
        "ingested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    ts = None
    if isinstance(node, dict):
        ts = node.get("timestamp") if isinstance(node.get("timestamp"), str) else None
        doc["node_id"] = node.get("nodeId") if isinstance(node.get("nodeId"), str) else None
        doc["session_id"] = node.get("scope") if isinstance(node.get("scope"), str) else None
        parents = node.get("parents")
        if isinstance(parents, list):
            doc["parents"] = [p for p in parents if isinstance(p, str)]
        if isinstance(node.get("profile"), str):
            doc["profile"] = node["profile"]
        issuer = node.get("issuer") if isinstance(node.get("issuer"), dict) else {}
        agent = node.get("agent") if isinstance(node.get("agent"), dict) else {}
        action = node.get("action") if isinstance(node.get("action"), dict) else {}
        for src, dst, obj in (("issuerId", "issuer_id", issuer), ("keyId", "key_id", issuer),
                              ("agentId", "agent_id", agent), ("version", "agent_version", agent),
                              ("type", "action_type", action), ("subtype", "action_subtype", action),
                              ("inputHash", "input_hash", action), ("outputHash", "output_hash", action)):
            if isinstance(obj.get(src), str):
                doc[dst] = obj[src][:256]
        h = node.get("hermes") if isinstance(node.get("hermes"), dict) else {}
        for src, dst in HERMES_FIELDS.items():
            if dst is None or src not in h:
                continue
            val = h[src]
            if isinstance(val, (str, int, float, bool)) and not (isinstance(val, str) and len(val) > 1024):
                doc[dst] = val
        usage = h.get("usage") if isinstance(h.get("usage"), dict) else {}
        tokens = {USAGE_FIELDS[k]: v for k, v in usage.items()
                  if k in USAGE_FIELDS and isinstance(v, int) and not isinstance(v, bool)}
        if tokens:
            doc["tokens"] = tokens
        if isinstance(doc.get("seq"), (int, float)) and not isinstance(doc.get("seq"), bool):
            doc["is_session_start"] = doc["seq"] == 0
        # Bodies: only when the recorder logged them AND the exporter is explicitly told to ship.
        if ship_bodies and h.get("bodiesLogged") is True:
            body = {k: h[f"{k}Body"] for k in ("input", "output") if f"{k}Body" in h}
            if body:
                doc["body"] = body
    doc["@timestamp"] = ts or doc["ingested_at"]
    return index_for(index_prefix, ts), doc_id, doc


# ---------------------------------------------------------------------------------------
# checkpointed, read-only file source
# ---------------------------------------------------------------------------------------

def _hash_prefix(fd: int, length: int):
    """sha256 object over bytes [0, length) of fd, or None if the file is shorter."""
    h = hashlib.sha256()
    pos = 0
    while pos < length:
        chunk = os.pread(fd, min(1 << 20, length - pos), pos)
        if not chunk:
            return None
        h.update(chunk)
        pos += len(chunk)
    return h


class Source:
    """One log file. Opened O_RDONLY; never written, locked or truncated by the exporter."""

    def __init__(self, path: str, state_dir: str):
        self.path = path
        key = hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]
        self.state_path = os.path.join(state_dir, f"source-{key}.json")
        self.fd: Optional[int] = None
        self.ident: Optional[Tuple[int, int]] = None
        self.prefix = hashlib.sha256()  # running sha256 of bytes [0, state["offset"])
        self.state = self._load_state()

    def _fresh_state(self) -> dict:
        return {"path": self.path, "offset": 0, "line": 0, "prefix_sha256": hashlib.sha256().hexdigest(),
                "chain": {}, "resets": 0}

    def _load_state(self) -> dict:
        try:
            with open(self.state_path, encoding="utf-8") as f:
                st = json.load(f)
            if st.get("path") == self.path and isinstance(st.get("offset"), int):
                return st
        except FileNotFoundError:
            pass
        except Exception:  # noqa: BLE001 -- unreadable checkpoint: start over (dedup by _id)
            log.warning("checkpoint %s unreadable; re-reading %s from the start", self.state_path, self.path)
        return self._fresh_state()

    def save_state(self):
        tmp = self.state_path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.state, f, separators=(",", ":"))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.state_path)

    def open(self) -> bool:
        """(Re)open the path if needed. Returns False when the file doesn't exist (yet)."""
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return False
        if self.fd is not None:
            # Same open file: catch in-place truncation (e.g. logrotate copytruncate).
            if os.fstat(self.fd).st_size < self.state["offset"]:
                self.validate_checkpoint()
            return True
        self.fd = os.open(self.path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        st = os.fstat(self.fd)
        self.ident = (st.st_dev, st.st_ino)
        self.validate_checkpoint()
        return True

    def path_replaced(self) -> bool:
        """True when the path now names a different file (rotation / re-copy with a new inode)."""
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            return False
        return self.ident is not None and (st.st_dev, st.st_ino) != self.ident

    def validate_checkpoint(self):
        """Keep the offset only if the file still holds exactly the bytes already shipped (sha256
        of the whole prefix [0, offset)); otherwise re-read from 0 -- safe, as _ids are
        deterministic. Catches truncation, replacement and same-length rewrites."""
        st = self.state
        off = st["offset"]
        reason = None
        size = os.fstat(self.fd).st_size
        if size < off:
            reason = f"file is shorter than the checkpoint ({size} < {off}): truncated or replaced"
        else:
            h = _hash_prefix(self.fd, off)
            if h is None or h.hexdigest() != st.get("prefix_sha256"):
                reason = "already-shipped bytes changed: file replaced or rewritten"
            else:
                self.prefix = h
        if reason:
            log.warning("%s: %s; re-reading from the start (no duplicates: deterministic _ids)", self.path, reason)
            resets = st.get("resets", 0) + 1
            self.state = self._fresh_state()
            self.state["resets"] = resets
            self.prefix = hashlib.sha256()

    def read_lines(self, max_lines: int, max_bytes: int) -> List[Tuple[int, bytes]]:
        """Complete lines (ending in \\n) from the checkpoint offset: [(offset, line_bytes)].
        A partial last line is left for later (the writer may still be appending)."""
        out: List[Tuple[int, bytes]] = []
        off = self.state["offset"]
        buf = b""
        pos = off
        while len(out) < max_lines:
            chunk = os.pread(self.fd, 65536, pos)
            if not chunk:
                break
            pos += len(chunk)
            buf += chunk
            while len(out) < max_lines:
                nl = buf.find(b"\n")
                if nl < 0:
                    break
                line, buf = buf[:nl], buf[nl + 1:]
                out.append((off, line))
                off += nl + 1
            if sum(len(l) for _, l in out) >= max_bytes:
                break
        return out

    def commit(self, new_offset: int, lines_done: int, chain: dict, prefix):
        st = self.state
        st["offset"] = new_offset
        st["line"] += lines_done
        st["chain"] = chain
        st["prefix_sha256"] = prefix.hexdigest()
        self.prefix = prefix
        self.save_state()

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


# ---------------------------------------------------------------------------------------
# the exporter
# ---------------------------------------------------------------------------------------

class Exporter:
    def __init__(self, cfg: Config, client: Optional[ESClient] = None, sleep=time.sleep):
        self.cfg = cfg
        self.client = client or ESClient(cfg)
        self.sleep = sleep
        with open(cfg.pub, encoding="utf-8") as f:
            self.public_key = atp_log.load_public_key(f.read())
        os.makedirs(cfg.state_dir, mode=0o700, exist_ok=True)
        self.sources = [Source(p, cfg.state_dir) for p in cfg.logs]
        self.dead_letter = os.path.join(cfg.state_dir, "dead-letter.jsonl")
        self.stopping = False
        self.stats = {"shipped": 0, "duplicates": 0, "dead_lettered": 0, "batches": 0, "retries": 0,
                      "invalid": 0}

    # -- one batch ------------------------------------------------------------------
    def _docs(self, src: Source, lines: List[Tuple[int, bytes]]):
        chain = json.loads(json.dumps(src.state.get("chain", {})))  # work on a copy until committed
        prefix = src.prefix.copy()
        items = []
        line_no = src.state["line"]
        for off, raw in lines:
            line_no += 1
            text = raw.decode("utf-8", errors="replace")
            checked = atp_log.check_line(text, self.public_key)
            chain_problems = []
            if checked["node"] is not None and checked["scope"] is not None:
                chain_problems = atp_log.chain_step(chain, checked["node"], None)
            index, doc_id, doc = build_document(raw, checked, chain_problems, index_prefix=self.cfg.index_prefix,
                                                source=src.path, offset=off, line=line_no,
                                                ship_bodies=self.cfg.ship_bodies, prefix_sha256=prefix.hexdigest())
            prefix.update(raw + b"\n")
            if not doc["verify"]["valid"]:
                self.stats["invalid"] += 1
            items.append((index, doc_id, doc, off, len(raw) + 1))
        return items, chain, prefix

    @staticmethod
    def bulk_body(items) -> bytes:
        parts = []
        for index, doc_id, doc, _off, _n in items:
            parts.append(json.dumps({"create": {"_index": index, "_id": doc_id}}, separators=(",", ":")))
            parts.append(json.dumps(doc, ensure_ascii=False, separators=(",", ":")))
        return ("\n".join(parts) + "\n").encode("utf-8")

    def _ship(self, items) -> None:
        """Ship until every item is delivered (201), already present (409) or dead-lettered."""
        pending = items
        delay = self.cfg.backoff_initial
        failed_attempts = 0
        while pending:
            if self.stopping:
                raise KeyboardInterrupt
            try:
                resp = self.client.bulk(self.bulk_body(pending))
                self.stats["batches"] += 1
            except TransientError as e:
                failed_attempts += 1
                self.stats["retries"] += 1
                log.warning("Elasticsearch unavailable (%s); retrying %d record(s) in %.1fs", e, len(pending), delay)
                self.sleep(delay + random.uniform(0, delay / 4))
                delay = min(delay * 2, self.cfg.backoff_max)
                continue
            if failed_attempts:
                log.info("Elasticsearch reachable again after %d failed attempt(s)", failed_attempts)
                failed_attempts = 0
            if len(resp.get("items", [])) != len(pending):
                raise FatalError("bulk response item count does not match the request")
            retry = []
            for item, res in zip(pending, resp.get("items", [])):
                r = res.get("create") or res.get("index") or {}
                status = r.get("status", 0)
                if status in (200, 201):
                    self.stats["shipped"] += 1
                elif status == 409:
                    self.stats["duplicates"] += 1
                elif status == 429 or status >= 500:
                    retry.append(item)
                else:
                    self._dead_letter(item, status, r.get("error"))
            if retry:
                self.stats["retries"] += 1
                log.warning("%d record(s) throttled/failed transiently; retrying in %.1fs", len(retry), delay)
                self.sleep(delay)
                delay = min(delay * 2, self.cfg.backoff_max)
            pending = retry

    def _dead_letter(self, item, status, error):
        index, doc_id, _doc, off, _n = item
        err = error if isinstance(error, dict) else {}
        entry = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "index": index, "_id": doc_id,
                 "offset": off, "status": status, "error_type": str(err.get("type", ""))[:200],
                 "error_reason": str(err.get("reason", ""))[:500]}
        fd = os.open(self.dead_letter, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        self.stats["dead_lettered"] += 1
        log.error("record at offset %d rejected by Elasticsearch (HTTP %s %s); written to %s",
                  off, status, entry["error_type"], self.dead_letter)

    def process_source(self, src: Source) -> int:
        """Ship everything currently available in one source. Returns lines processed."""
        if not src.open():
            return 0
        total = 0
        while not self.stopping:
            lines = src.read_lines(self.cfg.batch_size, self.cfg.batch_bytes)
            if not lines:
                break
            items, chain, prefix = self._docs(src, lines)
            self._ship(items)
            last_off, last_raw = lines[-1]
            src.commit(last_off + len(last_raw) + 1, len(lines), chain, prefix)
            total += len(lines)
        return total

    def run(self) -> int:
        once = self.cfg.mode == "copy"
        log.info("hermes-atp-export %s: mode=%s, %d log(s), es=%s", VERSION, self.cfg.mode, len(self.sources),
                 self.cfg.es_url)
        try:
            while not self.stopping:
                for src in self.sources:
                    if not once and src.fd is not None and src.path_replaced():
                        self.rotate(src)
                    self.process_source(src)
                if once:
                    break
                self.sleep(self.cfg.poll_interval)
        except KeyboardInterrupt:
            log.info("stopping")
        finally:
            for src in self.sources:
                src.close()
        log.info("done: %s", json.dumps(self.stats))
        return 0

    def rotate(self, src: Source):
        """The path now names a new file (rotation). The old file is still open: ship its
        remainder, then follow the new file from offset 0. Session chain state carries over,
        because a rotated log continues the same sessions."""
        self.process_source(src)
        chain = src.state.get("chain", {})
        log.info("%s was rotated/replaced; following the new file", src.path)
        src.close()
        src.state = src._fresh_state()
        src.state["chain"] = chain
        src.prefix = hashlib.sha256()
        src.ident = None


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Ship Hermes ATP recorder logs to Elasticsearch.")
    ap.add_argument("--config", help="JSON config file (or HERMES_ATP_EXPORT_CONFIG)")
    ap.add_argument("--mode", choices=("push", "copy"))
    ap.add_argument("--log", action="append", dest="logs", help="log file to ship (repeatable)")
    ap.add_argument("--once", action="store_true", help="process to EOF once and exit (same as --mode copy)")
    ap.add_argument("--ship-bodies", action="store_true", default=None,
                    help="also ship inputBody/outputBody when the recorder logged them")
    ap.add_argument("--check-config", action="store_true", help="print the resolved config (secrets masked) and exit")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        cfg = Config.load(a.config, overrides={"mode": "copy" if a.once else a.mode, "logs": a.logs,
                                               "ship_bodies": a.ship_bodies})
    except (ConfigError, OSError, ValueError) as e:
        print(f"hermes-atp-export: config error: {e}", file=sys.stderr)
        return 1
    if a.check_config:
        print(json.dumps(cfg.masked(), indent=2))
        return 0
    exporter = Exporter(cfg)

    def stop(_sig, _frm):
        exporter.stopping = True
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        return exporter.run()
    except FatalError as e:
        log.error("fatal: %s", e)
        return 3


if __name__ == "__main__":
    sys.exit(main())
