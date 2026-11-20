"""Verify an append-only ATP JSONL record log (Hermes recorder, slice 1).

One signed ATP node per line. Per-session lineage is carried by:
  * scope            -- the session (Hermes session_id)
  * parents[0]       -- the nodeId of the previous record in the same scope
                        (absent / [] for the first record of a session)
  * hermes.seq       -- 0-based position of the record in its session
                        (profile-defined field; see implementations/hermes-plugin/README.md)

When a record stores hermes.inputBody / hermes.outputBody (log_bodies on), the body must
re-hash to action.inputHash / action.outputHash and match hermes.*Length ("body-mismatch").
An empty log (no records) is a failure ("empty-log", line 0).

Subagent links (slice 2): an intact first record (seq 0) carrying hermes.parentSessionId
links that session to its parent; every link is listed in "links" (in line order). A link
whose parent session has no record in the log is a WARNING ("missing-parent-session").

Known limitation: deleting records from the END of a session (tail truncation) leaves a valid
chain and cannot be detected from the log alone; it only shows up when it orphans a request
(an "unmatched-request" warning). Anchor the last nodeId externally if that matters.

The verdict format is shared byte-for-byte with implementations/node/src/atp-log.js;
any change here MUST be mirrored there (the cross-implementation test diffs both).

check_line() / chain_step() expose verify_log's per-record and per-session checks for
incremental readers (the Elastic exporter); verify_log is built on them, so both agree.
hermes-plugin/atp_log.py is a byte-identical vendored copy of this file.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Dict, List, Optional

import atp

_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_BLANK = re.compile(r"^[ \t\r]*$")
MAX_SAFE = (1 << 53) - 1

PRE_EVENTS = {"pre_api_request": "api", "pre_tool_call": "tool"}
POST_EVENTS = {"post_api_request": "api", "api_request_error": "api", "post_tool_call": "tool"}
PAIR_NAME = {"pre_api_request": "post_api_request or api_request_error", "pre_tool_call": "post_tool_call"}


def load_public_key(text: str) -> bytes:
    """A .pub file is the base64 of the raw 32-byte Ed25519 public key (whitespace ignored)."""
    s = "".join(text.split())
    try:
        raw = base64.b64decode(s, validate=True)
    except Exception:
        raise ValueError("public key file is not base64") from None
    if len(raw) != 32:
        raise ValueError("public key must be 32 bytes")
    return raw


def _seq(node) -> Optional[int]:
    h = node.get("hermes") if isinstance(node, dict) else None
    if not isinstance(h, dict):
        return None
    v = h.get("seq")
    if isinstance(v, bool):
        return None
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    if isinstance(v, int) and 0 <= v <= MAX_SAFE:
        return v
    return None


def _str_field(node, key) -> Optional[str]:
    h = node.get("hermes")
    if isinstance(h, dict) and isinstance(h.get(key), str) and h.get(key) != "":
        return h[key]
    return None


def _event(node) -> Optional[str]:
    return _str_field(node, "event")


def _corr(node, family) -> Optional[str]:
    return _str_field(node, "apiRequestId" if family == "api" else "toolCallId")


def _body_problem(node, which) -> Optional[str]:
    """Reason text when a stored hermes.<which>Body disagrees with its hash/length, else None."""
    h = node.get("hermes")
    if not isinstance(h, dict) or (which + "Body") not in h:
        return None
    action = node.get("action") if isinstance(node.get("action"), dict) else {}
    want = action.get(which + "Hash")
    if not isinstance(want, str):
        return f"hermes.{which}Body present but action.{which}Hash is missing"
    body = h[which + "Body"]
    try:
        data = body.encode("utf-8") if isinstance(body, str) else atp.canonicalize(body).encode("utf-8")
    except Exception:
        return f"hermes.{which}Body cannot be canonicalized"
    if "sha256:" + atp.sha256_hex(data) != want:
        return f"hermes.{which}Body does not match action.{which}Hash"
    if (which + "Length") in h:
        n = h[which + "Length"]
        if isinstance(n, bool) or not isinstance(n, (int, float)) or n != len(data):
            return f"hermes.{which}Length does not match hermes.{which}Body"
    return None


def split_lines(data: bytes) -> List[str]:
    text = data.decode("utf-8", errors="replace")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def check_line(text: str, public_key: bytes, node=None, parsed: bool = False) -> dict:
    """Integrity checks for ONE log line, independent of the rest of the log.

    Returns {"node", "intact", "record_hash", "scope", "seq", "problems": [(kind, reason)]}.
    intact: structurally valid, nodeId matches content, signature verifies with public_key.
    record_hash: the recomputed content hash (== nodeId when intact), or None.
    scope/seq: set only when the line can take part in a session chain (see chain_step).
    Pass parsed=True with node=<dict or None> to reuse an earlier parse.
    """
    problems: List[tuple] = []
    out = {"node": None, "intact": False, "record_hash": None, "scope": None, "seq": None, "problems": problems}
    if _BLANK.match(text):
        problems.append(("tampered", "blank line"))
        return out
    if not parsed:
        try:
            node = atp.parse_atp_json(text)
        except Exception:
            node = None
        node = node if isinstance(node, dict) else None
    if node is None:
        problems.append(("tampered", "invalid JSON"))
        return out
    out["node"] = node
    intact = True
    if atp.structural_error(node) is not None:
        problems.append(("tampered", "not a structurally valid ATP node"))
        intact = False
    claimed = node.get("nodeId")
    if intact:
        try:
            rid = atp.compute_node_id(node)
        except Exception:
            rid = None
        out["record_hash"] = rid
        if not isinstance(claimed, str):
            problems.append(("tampered", "nodeId missing"))
            intact = False
        elif rid != claimed:
            problems.append(("tampered", "nodeId does not match record content"))
            intact = False
        elif not atp.verify_node_id(rid, node["signature"], public_key):
            problems.append(("tampered", "signature does not verify with the supplied public key"))
            intact = False
    if intact:
        for which in ("input", "output"):
            problem = _body_problem(node, which)
            if problem is not None:
                problems.append(("body-mismatch", problem))
    scope = node.get("scope")
    q = _seq(node)
    if not isinstance(scope, str) or q is None:
        if intact:
            problems.append(("tampered", "hermes.seq missing or not a non-negative integer"))
    else:
        out["scope"], out["seq"] = scope, q
    out["intact"] = intact
    return out


def chain_step(state: Dict[str, dict], node: dict, appears_later=None) -> List[tuple]:
    """Per-session lineage check for one record (scope + hermes.seq + parents[0]); updates state.

    state maps scope -> {"id": claimed nodeId, "seq": int}. appears_later(scope, seq) -> bool
    is the whole-log look-ahead used by verify_log to tell out-of-order from missing; an
    incremental reader (no look-ahead) passes None and a gap is reported as missing.
    """
    problems: List[tuple] = []
    scope = node["scope"]
    q = _seq(node)
    claimed = node.get("nodeId")
    cid = claimed if isinstance(claimed, str) and _HEX64.fullmatch(claimed) else None
    parents = node.get("parents") if isinstance(node.get("parents"), list) else []
    prev = parents[0] if parents else None
    st = state.get(scope)
    expected = 0 if st is None else st["seq"] + 1
    if q == expected:
        if st is None:
            if prev is not None:
                problems.append(("tampered", "first record of session must not link to a previous record"))
        elif prev != st["id"]:
            problems.append(("tampered", "parent link does not match the previous record in session"))
    elif q > expected:
        if appears_later is not None and appears_later(scope, expected):
            problems.append(("out-of-order", f"session {scope}: expected seq {expected}, found {q}; seq {expected} appears later in the log"))
        else:
            n = q - expected
            problems.append(("missing", f"session {scope}: expected seq {expected}, found {q} ({n} record(s) missing)"))
    else:
        problems.append(("out-of-order", f"session {scope}: seq {q} is not after seq {st['seq']}"))
    if st is None or q > st["seq"]:
        state[scope] = {"id": cid, "seq": q}
    return problems


def verify_log(data: bytes, public_key: bytes) -> dict:
    lines = split_lines(data)
    errors: List[dict] = []
    warnings: List[dict] = []

    def err(line, kind, reason):
        errors.append({"line": line, "kind": kind, "reason": reason})

    # Pass 1: parse every line once.
    parsed: List[Optional[dict]] = []
    for text in lines:
        try:
            node = atp.parse_atp_json(text) if not _BLANK.match(text) else None
        except Exception:
            node = None
        parsed.append(node if isinstance(node, dict) else None)

    # Index of (scope, seq) -> line positions, for "appears later" look-ahead.
    positions: Dict[str, Dict[int, List[int]]] = {}
    for pos, node in enumerate(parsed, 1):
        if node is None or not isinstance(node.get("scope"), str):
            continue
        q = _seq(node)
        if q is not None:
            positions.setdefault(node["scope"], {}).setdefault(q, []).append(pos)

    def appears_later(scope, seq, pos) -> bool:
        return any(p > pos for p in positions.get(scope, {}).get(seq, []))

    state: Dict[str, dict] = {}  # scope -> {"id": claimed nodeId, "seq": int}
    good: List[tuple] = []  # (pos, node) for records that passed integrity checks
    scopes = set()

    for pos, (text, node) in enumerate(zip(lines, parsed), 1):
        checked = check_line(text, public_key, node=node, parsed=True)
        for kind, reason in checked["problems"]:
            err(pos, kind, reason)
        node = checked["node"]
        if node is None or checked["scope"] is None:
            continue
        scopes.add(checked["scope"])
        if checked["intact"]:
            good.append((pos, node))
        for kind, reason in chain_step(state, node, lambda scope, seq, _p=pos: appears_later(scope, seq, _p)):
            err(pos, kind, reason)

    if not lines:
        err(0, "empty-log", "log contains no records")

    # Unmatched request (pre) events are WARNINGS, not failures (draft-bates-atp §16.2).
    closed = set()
    for _pos, node in good:
        ev = _event(node)
        if ev in POST_EVENTS:
            fam = POST_EVENTS[ev]
            c = _corr(node, fam)
            if c is not None:
                closed.add((node["scope"], fam, c))
    for pos, node in good:
        ev = _event(node)
        if ev not in PRE_EVENTS:
            continue
        fam = PRE_EVENTS[ev]
        c = _corr(node, fam)
        idname = "api_request_id" if fam == "api" else "tool_call_id"
        if c is None:
            warnings.append({"line": pos, "kind": "unmatched-request",
                             "reason": f"session {node['scope']}: {ev} has no {idname}; cannot be paired"})
        elif (node["scope"], fam, c) not in closed:
            warnings.append({"line": pos, "kind": "unmatched-request",
                             "reason": f"session {node['scope']}: {ev} {idname} {c} has no matching {PAIR_NAME[ev]}"})

    # Parent -> child session links, from each session's intact first record.
    links: List[dict] = []
    linked = set()
    for pos, node in good:
        if _seq(node) != 0 or node["scope"] in linked:
            continue
        parent = _str_field(node, "parentSessionId")
        if parent is None:
            continue
        child = node["scope"]
        linked.add(child)
        links.append({"line": pos, "parent": parent, "child": child})
        if parent not in scopes:
            warnings.append({"line": pos, "kind": "missing-parent-session",
                             "reason": f"session {child}: parent session {parent} has no records in this log"})
    warnings.sort(key=lambda w: w["line"])  # stable: same-line warnings keep their order

    return {
        "verdict": "pass" if not errors else "fail",
        "records": len(lines),
        "sessions": len(scopes),
        "errors": errors,
        "warnings": warnings,
        "links": links,
    }


def format_json(result: dict) -> str:
    return json.dumps(result, indent=2, ensure_ascii=False)


def format_text(result: dict) -> str:
    out = [f"{result['verdict'].upper()}  {result['records']} records, {result['sessions']} sessions, "
           f"{len(result['errors'])} errors, {len(result['warnings'])} warnings"]
    for e in result["errors"]:
        out.append(f"  line {e['line']}: {e['kind'].upper()}: {e['reason']}")
    for w in result["warnings"]:
        out.append(f"  line {w['line']}: WARNING {w['kind']}: {w['reason']}")
    for l in result.get("links", []):
        out.append(f"  line {l['line']}: LINK session {l['parent']} -> session {l['child']}")
    return "\n".join(out)
