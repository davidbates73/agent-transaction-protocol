"""ATP Core reference implementation (Python 3.9+, standard library only).

Independent of the Node.js implementation: its own RFC 8785 canonicalizer
(including ECMAScript number formatting) and a pure-Python Ed25519 taken
from the RFC 8032 Section 6 reference algorithm, so agreement between the
two implementations is meaningful interoperability evidence.

The pure-Python Ed25519 is NOT constant-time. It is fine for test vectors
and verification demos; do not use it to sign production nodes.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
from datetime import datetime
from decimal import Decimal
from typing import Callable, Dict, Iterable, List, Optional

# ---------------------------------------------------------------------------
# Section 9: ATP null omission + RFC 8785 JCS
# ---------------------------------------------------------------------------


def omit_nulls(value):
    """ATP §9: drop null-valued object members (recursively; array nulls kept)."""
    if isinstance(value, list):
        return [omit_nulls(v) for v in value]
    if isinstance(value, dict):
        return {k: omit_nulls(v) for k, v in value.items() if v is not None}
    return value


_ESC = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _jcs_string(s: str) -> str:
    out = ['"']
    for ch in s:
        o = ord(ch)
        if ch in _ESC:
            out.append(_ESC[ch])
        elif o < 0x20 or 0xD800 <= o <= 0xDFFF:  # control chars and lone surrogates
            out.append("\\u%04x" % o)
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _jcs_number(x) -> str:
    """ECMAScript Number::toString, as required by RFC 8785 §3.2.2.3."""
    if isinstance(x, int):
        if abs(x) <= 2 ** 53:
            return str(x)
        x = float(x)
    if math.isnan(x) or math.isinf(x):
        raise ValueError("JCS: non-finite number")
    if x == 0:
        return "0"
    sign = "-" if x < 0 else ""
    digits_t, exp = Decimal(repr(abs(x))).as_tuple()[1:]
    digits = "".join(map(str, digits_t)).rstrip("0") or "0"
    exp += len(digits_t) - len(digits)
    k = len(digits)
    n = k + exp  # value = 0.digits * 10^n
    if k <= n <= 21:
        s = digits + "0" * (n - k)
    elif 0 < n <= 21:
        s = digits[:n] + "." + digits[n:]
    elif -6 < n <= 0:
        s = "0." + "0" * (-n) + digits
    else:
        e = n - 1
        mant = digits if k == 1 else digits[0] + "." + digits[1:]
        s = mant + "e" + ("+" if e >= 0 else "-") + str(abs(e))
    return sign + s


def jcs(value) -> str:
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (int, float)):
        return _jcs_number(value)
    if isinstance(value, str):
        return _jcs_string(value)
    if isinstance(value, list):
        return "[" + ",".join(jcs(v) for v in value) + "]"
    if isinstance(value, dict):
        keys = sorted(value.keys(), key=lambda k: k.encode("utf-16-be", "surrogatepass"))
        return "{" + ",".join(_jcs_string(k) + ":" + jcs(value[k]) for k in keys) + "}"
    raise TypeError(f"JCS: unsupported type {type(value).__name__}")


def canonicalize(value) -> str:
    return jcs(omit_nulls(value))


# ---------------------------------------------------------------------------
# Section 9 / RFC 7493 I-JSON gate
# ---------------------------------------------------------------------------

_MAX_SAFE = (1 << 53) - 1  # 9007199254740991


def has_unpaired_surrogate(s: str) -> bool:
    """True if s contains an unpaired UTF-16 surrogate code unit."""
    i = 0
    while i < len(s):
        o = ord(s[i])
        if 0xD800 <= o <= 0xDBFF:
            if i + 1 < len(s) and 0xDC00 <= ord(s[i + 1]) <= 0xDFFF:
                i += 2
                continue
            return True
        if 0xDC00 <= o <= 0xDFFF:
            return True
        i += 1
    return False


def ijson_error(value) -> Optional[str]:
    """Deep I-JSON check; return reason or None."""
    if isinstance(value, str):
        return "I-JSON: unpaired surrogate in string" if has_unpaired_surrogate(value) else None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        if abs(value) > _MAX_SAFE:
            return "I-JSON: integer outside ±(2^53-1)"
        return None
    if isinstance(value, float):
        if not math.isfinite(value):
            return "I-JSON: non-finite number"
        # Integer-valued floats outside the safe range (e.g. 1e21)
        if value.is_integer() and abs(value) > _MAX_SAFE:
            return "I-JSON: integer outside ±(2^53-1)"
        return None
    if isinstance(value, list):
        for v in value:
            e = ijson_error(v)
            if e:
                return e
        return None
    if isinstance(value, dict):
        for k, v in value.items():
            if has_unpaired_surrogate(k):
                return "I-JSON: unpaired surrogate in member name"
            e = ijson_error(v)
            if e:
                return e
    return None


def _reject_bad_number_tokens(text: str) -> None:
    """Scan raw JSON number tokens (outside strings) for I-JSON violations."""
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            i += 1
            while i < n:
                if text[i] == "\\":
                    i += 2
                    continue
                if text[i] == '"':
                    i += 1
                    break
                i += 1
            continue
        if c == "-" or "0" <= c <= "9":
            if i > 0 and text[i - 1].isalnum():
                i += 1
                continue
            start = i
            if text[i] == "-":
                i += 1
            while i < n and "0" <= text[i] <= "9":
                i += 1
            is_int = True
            if i < n and text[i] == ".":
                is_int = False
                i += 1
                while i < n and "0" <= text[i] <= "9":
                    i += 1
            if i < n and text[i] in "eE":
                is_int = False
                i += 1
                if i < n and text[i] in "+-":
                    i += 1
                while i < n and "0" <= text[i] <= "9":
                    i += 1
            tok = text[start:i]
            try:
                # Non-finite / out of double range
                f = float(tok)
            except OverflowError:
                raise ValueError(f"I-JSON: number outside IEEE-754 double: {tok}") from None
            if not math.isfinite(f):
                raise ValueError(f"I-JSON: number outside IEEE-754 double: {tok}")
            # Underflow: non-zero mantissa literal that converts to +0.0/-0.0 as a double.
            # Digits in the mantissa only (before e/E), so 0e-400 / 0E5 stay accepted.
            if f == 0.0:
                mant = tok[1:] if tok.startswith("-") else tok
                for sep in ("e", "E"):
                    if sep in mant:
                        mant = mant.split(sep, 1)[0]
                        break
                if any(ch in mant for ch in "123456789"):
                    raise ValueError(f"I-JSON: number underflows to zero: {tok}")
            if is_int:
                bi = int(tok)
                if abs(bi) > _MAX_SAFE:
                    raise ValueError(f"I-JSON: integer outside ±(2^53-1): {tok}")
            continue
        i += 1


def parse_atp_json(text: str):
    """Parse JSON as I-JSON for ATP: reject duplicate names, unpaired surrogates, bad numbers."""
    if not isinstance(text, str):
        raise TypeError("parse_atp_json: text must be a string")

    def check_pairs(pairs):
        seen = set()
        out = {}
        for k, v in pairs:
            if k in seen:
                raise ValueError(f"I-JSON: duplicate member name: {json.dumps(k)}")
            seen.add(k)
            out[k] = v
        return out

    _reject_bad_number_tokens(text)
    # parse_constant catches Infinity/NaN literals if ever emitted
    try:
        value = json.loads(text, object_pairs_hook=check_pairs, parse_constant=lambda c: (_ for _ in ()).throw(
            ValueError(f"I-JSON: non-finite number: {c}")
        ))
    except json.JSONDecodeError as e:
        raise ValueError(f"JSON: {e}") from e
    err = ijson_error(value)
    if err:
        raise ValueError(err)
    return value


# ---------------------------------------------------------------------------
# Section 10.1: node identity
# ---------------------------------------------------------------------------


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def node_content(node: dict) -> dict:
    return {k: v for k, v in node.items() if k not in ("nodeId", "signature")}


def canonical_bytes(node: dict) -> bytes:
    return canonicalize(node_content(node)).encode("utf-8")


def compute_node_id(node: dict) -> str:
    return sha256_hex(canonical_bytes(node))


def payload_hash(payload) -> str:
    if isinstance(payload, bytes):
        data = payload
    elif isinstance(payload, str):
        data = payload.encode("utf-8")
    else:
        data = canonicalize(payload).encode("utf-8")
    return "sha256:" + sha256_hex(data)


# ---------------------------------------------------------------------------
# Ed25519, RFC 8032 Section 6 reference algorithm
# ---------------------------------------------------------------------------

_p = 2 ** 255 - 19
_q = 2 ** 252 + 27742317777372353535851937790883648493
_d = -121665 * pow(121666, _p - 2, _p) % _p
_SQRT_M1 = pow(2, (_p - 1) // 4, _p)


def _sha512(b: bytes) -> bytes:
    return hashlib.sha512(b).digest()


def _sha512_modq(b: bytes) -> int:
    return int.from_bytes(_sha512(b), "little") % _q


def _add(P, Q):
    A = (P[1] - P[0]) * (Q[1] - Q[0]) % _p
    B = (P[1] + P[0]) * (Q[1] + Q[0]) % _p
    C = P[3] * 2 * _d * Q[3] % _p
    D = P[2] * 2 * Q[2] % _p
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F % _p, G * H % _p, F * G % _p, E * H % _p)


def _mul(s: int, P):
    Q = (0, 1, 1, 0)
    while s > 0:
        if s & 1:
            Q = _add(Q, P)
        P = _add(P, P)
        s >>= 1
    return Q


def _equal(P, Q) -> bool:
    return (P[0] * Q[2] - Q[0] * P[2]) % _p == 0 and (P[1] * Q[2] - Q[1] * P[2]) % _p == 0


def _recover_x(y: int, sign: int) -> Optional[int]:
    if y >= _p:
        return None
    x2 = (y * y - 1) * pow(_d * y * y + 1, _p - 2, _p) % _p
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_p + 3) // 8, _p)
    if (x * x - x2) % _p != 0:
        x = x * _SQRT_M1 % _p
    if (x * x - x2) % _p != 0:
        return None
    if (x & 1) != sign:
        x = _p - x
    return x


_gy = 4 * pow(5, _p - 2, _p) % _p
_gx = _recover_x(_gy, 0)
_G = (_gx, _gy, 1, _gx * _gy % _p)


def _compress(P) -> bytes:
    zinv = pow(P[2], _p - 2, _p)
    x, y = P[0] * zinv % _p, P[1] * zinv % _p
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _decompress(b: bytes):
    if len(b) != 32:
        return None
    y = int.from_bytes(b, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    x = _recover_x(y, sign)
    return None if x is None else (x, y, 1, x * y % _p)


def _expand(seed: bytes):
    if len(seed) != 32:
        raise ValueError("Ed25519 seed must be 32 bytes")
    h = _sha512(seed)
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    return a, h[32:]


def ed25519_public_key(seed: bytes) -> bytes:
    a, _ = _expand(seed)
    return _compress(_mul(a, _G))


def ed25519_sign(seed: bytes, msg: bytes) -> bytes:
    a, prefix = _expand(seed)
    A = _compress(_mul(a, _G))
    r = _sha512_modq(prefix + msg)
    Rs = _compress(_mul(r, _G))
    h = _sha512_modq(Rs + A + msg)
    s = (r + h * a) % _q
    return Rs + s.to_bytes(32, "little")


def ed25519_verify(public: bytes, msg: bytes, sig: bytes) -> bool:
    if len(public) != 32 or len(sig) != 64:
        return False
    A = _decompress(public)
    R = _decompress(sig[:32])
    if A is None or R is None:
        return False
    # Reject small-order A and R: [8]P equals the identity.
    _identity = (0, 1, 1, 0)
    if _equal(_mul(8, A), _identity) or _equal(_mul(8, R), _identity):
        return False
    s = int.from_bytes(sig[32:], "little")
    if s >= _q:
        return False
    h = _sha512_modq(sig[:32] + public + msg)
    return _equal(_mul(s, _G), _add(R, _mul(h, A)))


# ---------------------------------------------------------------------------
# Section 10.3: sign / verify nodeId (32 raw bytes, base64 signature)
# ---------------------------------------------------------------------------


# Strict ATP signature base64: RFC 4648 §4, exactly 88 chars, padded, no whitespace.
_ATP_SIG_B64_RE = re.compile(r"^[A-Za-z0-9+/]{86}==$")


def _decode_atp_signature_b64(signature_b64: str):
    if not isinstance(signature_b64, str) or _ATP_SIG_B64_RE.fullmatch(signature_b64) is None:
        return None
    try:
        sig = base64.b64decode(signature_b64, validate=True)
    except Exception:
        return None
    if len(sig) != 64:
        return None
    # Reject non-canonical encodings (e.g. non-zero trailing bits in the last quartet).
    if base64.b64encode(sig).decode("ascii") != signature_b64:
        return None
    return sig


def sign_node_id(node_id_hex: str, seed: bytes) -> str:
    return base64.b64encode(ed25519_sign(seed, bytes.fromhex(node_id_hex))).decode("ascii")


def verify_node_id(node_id_hex: str, signature_b64: str, public_key: bytes) -> bool:
    try:
        sig = _decode_atp_signature_b64(signature_b64)
        if sig is None:
            return False
        return ed25519_verify(public_key, bytes.fromhex(node_id_hex), sig)
    except Exception:  # malformed input is a verification failure
        return False


def create_node(fields: dict, seed: bytes) -> dict:
    normalized = dict(fields)
    ts = normalized.get("timestamp")
    if isinstance(ts, str) and not is_atp_timestamp(ts):
        normalized["timestamp"] = format_atp_timestamp(ts)
    content = omit_nulls(node_content(normalized))
    node_id = compute_node_id(content)
    return {"nodeId": node_id, **content, "signature": sign_node_id(node_id, seed)}


# ---------------------------------------------------------------------------
# Section 8: Timestamp format (exactly 20 chars: YYYY-MM-DDTHH:MM:SSZ)
# ---------------------------------------------------------------------------

_ATP_TIMESTAMP_RE = re.compile(r"^([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})Z$")
_DAYS_IN_MONTH = (0, 31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)


def _is_leap(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def is_atp_timestamp(s: str) -> bool:
    """True iff s is exactly the ATP timestamp form (no Date/fromisoformat)."""
    if not isinstance(s, str) or len(s) != 20:
        return False
    m = _ATP_TIMESTAMP_RE.fullmatch(s)
    if not m:
        return False
    Y, Mo, D, H, Mi, S = (int(m.group(i)) for i in range(1, 7))
    if Mo < 1 or Mo > 12 or H > 23 or Mi > 59 or S > 59:
        return False
    dim = 29 if Mo == 2 and _is_leap(Y) else _DAYS_IN_MONTH[Mo]
    return 1 <= D <= dim


def format_atp_timestamp(value=None) -> str:
    """Emit an ATP timestamp; truncate (do not round) to whole UTC seconds."""
    from datetime import timezone
    if value is None:
        dt = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        dt = value if value.tzinfo is None else value.astimezone(timezone.utc)
    elif isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(value, tz=timezone.utc)
    elif isinstance(value, str):
        # Emitter convenience only — validators must use is_atp_timestamp, not this.
        s = value
        if s.endswith("Z") or s.endswith("z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is not None:
            dt = dt.astimezone(timezone.utc)
    else:
        raise TypeError("format_atp_timestamp: unsupported type")
    # Truncate fractional seconds (datetime already exposes whole .second)
    return f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}T{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}Z"


# ---------------------------------------------------------------------------
# Structural checks and Section 13 validation
# ---------------------------------------------------------------------------

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_PAYLOAD_HASH = re.compile(r"^sha256:[0-9a-f]{64}$")


def is_atp_payload_hash(s: str) -> bool:
    return isinstance(s, str) and bool(_PAYLOAD_HASH.fullmatch(s))



def structural_error(node) -> Optional[str]:
    if not isinstance(node, dict):
        return "node is not an object"
    for f in ("timestamp", "scope"):
        if not isinstance(node.get(f), str):
            return f"missing or non-string {f}"
    if not is_atp_timestamp(node["timestamp"]):
        return "timestamp must be YYYY-MM-DDTHH:MM:SSZ (20 chars, uppercase T/Z, no fraction)"
    iss, ag, act = node.get("issuer"), node.get("agent"), node.get("action")
    if not (isinstance(iss, dict) and isinstance(iss.get("issuerId"), str) and isinstance(iss.get("keyId"), str)):
        return "issuer.issuerId/keyId required"
    if not (isinstance(ag, dict) and isinstance(ag.get("agentId"), str) and isinstance(ag.get("version"), str)):
        return "agent.agentId/version required"
    if not (isinstance(act, dict) and isinstance(act.get("type"), str)):
        return "action.type required"
    if "parents" not in node:
        return "parents is required"
    parents = node.get("parents")
    if not isinstance(parents, list):
        return "parents must be an array"
    for p in parents:
        if not (isinstance(p, str) and _HEX64.fullmatch(p)):
            return f"parent is not a lowercase hex SHA-256 nodeId: {p}"
    if len(set(parents)) != len(parents):
        return "duplicate parent nodeId"
    if not isinstance(node.get("signature"), str):
        return "signature required"
    action = node.get("action")
    if isinstance(action, dict):
        for f in ("inputHash", "outputHash"):
            if f in action and action[f] is not None:
                if not is_atp_payload_hash(action[f]):
                    return f"{f} must be sha256: + 64 lowercase hex"
    if "nodeId" in node and node["nodeId"] is not None:
        if not (isinstance(node["nodeId"], str) and _HEX64.fullmatch(node["nodeId"])):
            return "nodeId must be 64 lowercase hex characters (unprefixed)"
    return None


def _ts(s: str) -> str:
    """Boundary compare key: ATP timestamps sort lexicographically == chronologically."""
    if not is_atp_timestamp(s):
        raise ValueError(f"not an ATP timestamp: {s!r}")
    return s


def validate(
    targets: Iterable[str],
    nodes: Dict[str, dict],
    resolve_key: Callable[[str, str], Optional[bytes]],
    mode: str = "full",
    boundary: Optional[dict] = None,
    withheld: Iterable[str] = (),
    known_profiles: Iterable[str] = (),
    strict_profiles: bool = False,
) -> dict:
    """Return a §13.6 validation result. Semantics mirror the Node implementation."""
    if mode not in ("full", "bounded", "tip", "redacted"):
        raise ValueError(f"unknown mode {mode}")
    if mode == "bounded" and not boundary:
        raise ValueError("bounded mode requires a boundary")
    withheld, known = set(withheld), set(known_profiles)
    cats = ["verified", "invalid", "unresolved", "withheld", "outOfHorizon", "keyUnresolved", "profileUnresolved", "lineageIncomplete"]
    S = {c: set() for c in cats}
    relay: Dict[str, str] = {}
    details: Dict[str, List[str]] = {}
    node_ok: Dict[str, bool] = {}
    lineage_ok: Dict[str, bool] = {}

    def note(i, m):
        details.setdefault(i, []).append(m)

    def check_node(i, n) -> bool:
        if i in node_ok:
            return node_ok[i]
        ok = True
        err = structural_error(n)
        if err:
            S["invalid"].add(i); note(i, f"structural: {err}"); ok = False
        if ok:
            ij = ijson_error(n)
            if ij:
                S["invalid"].add(i); note(i, ij); ok = False
        if ok:
            try:
                rid = compute_node_id(n)
            except Exception as e:  # noqa: BLE001
                S["invalid"].add(i); note(i, f"canonicalization failed: {e}"); ok = False
            else:
                if rid != i:
                    S["invalid"].add(i); note(i, f"nodeId mismatch: recomputed {rid}"); ok = False
        if ok and "nodeId" in n and n["nodeId"] != i:
            S["invalid"].add(i); note(i, "embedded nodeId differs from reference"); ok = False
        if ok and "profile" in n and n["profile"] not in known:
            S["profileUnresolved"].add(i); note(i, f"profile not recognized: {n['profile']}")
            if strict_profiles:
                S["invalid"].add(i); ok = False
        if ok:
            key = resolve_key(n["issuer"]["issuerId"], n["issuer"]["keyId"])
            if key is None:
                S["keyUnresolved"].add(i); note(i, "no public key"); ok = False
            elif not verify_node_id(i, n["signature"], key):
                S["invalid"].add(i); note(i, "signature verification failed"); ok = False
        node_ok[i] = ok
        return ok

    def within(n, depth) -> bool:
        if mode != "bounded":
            return True
        if "depth" in boundary and depth > boundary["depth"]:
            return False
        if "sinceTimestamp" in boundary:
            since = boundary["sinceTimestamp"]
            if not is_atp_timestamp(since) or not is_atp_timestamp(n["timestamp"]):
                return False
            if n["timestamp"] < since:  # lexical == chronological for ATP form
                return False
        return True

    def relay_state(n) -> str:
        a = n["action"]
        claims = "inputHash" in a and a.get("inputHash") == a.get("outputHash")
        for p in n["parents"]:
            par = nodes.get(p)
            if par is not None and node_ok.get(p) and "outputHash" in par.get("action", {}):
                return "Verified" if claims and par["action"]["outputHash"] == a["inputHash"] else "Contradicted"
        return "Asserted"

    def visit(i, depth, stack) -> bool:
        if i in stack:
            S["invalid"].add(i); note(i, "cycle detected"); return False
        if i in lineage_ok:
            return lineage_ok[i]
        if i in withheld and (mode == "redacted" or i not in nodes):
            S["withheld"].add(i)
            lineage_ok[i] = mode == "redacted"
            return lineage_ok[i]
        n = nodes.get(i)
        if n is None:
            S["unresolved"].add(i); lineage_ok[i] = False; return False
        if not within(n, depth):
            S["outOfHorizon"].add(i); return True
        ok = check_node(i, n)
        if mode == "tip":
            lineage_ok[i] = ok; return ok
        stack.add(i)
        parents_ok = True
        # MUST NOT default missing/non-array parents to [] before/during validation.
        parents = n.get("parents") if "parents" in n else None
        if not isinstance(parents, list):
            parents_ok = False
            note(i, "parents is absent or not an array")
        else:
            for p in parents:
                if not visit(p, depth + 1, stack):
                    parents_ok = False
        stack.discard(i)
        if ok and n["action"]["type"] == "atp:relay":
            relay[i] = relay_state(n)
        lineage_ok[i] = ok and parents_ok
        return lineage_ok[i]

    for t in targets:
        visit(t, 0, set())

    for i, ok in node_ok.items():
        if not ok:
            continue
        if mode == "tip" or lineage_ok.get(i):
            S["verified"].add(i)
        else:
            S["lineageIncomplete"].add(i)

    result: dict = {"mode": mode}
    if mode == "bounded":
        result["boundary"] = dict(boundary)
    for c in cats:
        result[c] = sorted(S[c])
    if relay:
        result["relayFidelity"] = relay
    result["details"] = details
    return result


def key_resolver(mapping: Dict[str, str]) -> Callable[[str, str], Optional[bytes]]:
    """mapping: {"issuerId|keyId": base64 raw public key}"""
    def resolve(issuer_id: str, key_id: str) -> Optional[bytes]:
        v = mapping.get(f"{issuer_id}|{key_id}")
        return base64.b64decode(v) if v else None
    return resolve


def index_nodes(nodes: Iterable[dict]) -> Dict[str, dict]:
    return {n.get("nodeId") or compute_node_id(n): n for n in nodes}


def tips(nodes: List[dict]) -> List[str]:
    referenced = {p for n in nodes for p in (n["parents"] if isinstance(n.get("parents"), list) else [])}
    return [i for i in (n.get("nodeId") or compute_node_id(n) for n in nodes) if i not in referenced]


def load_json(path: str):
    with open(path, encoding="utf-8") as f:
        return json.load(f)
