#!/usr/bin/env python3
"""hermes-atp-audit: read-only consistency audit of the Hermes ATP indices in Elasticsearch (stdlib only).

Run on the Elasticsearch host (us-solebay). It only reads: every request is a GET, or a POST to
_search / _count / _search/scroll (searches with a body), and the client refuses anything else
before it reaches the network. It never changes a document, mapping, setting or index.

Checks (each prints PASS, WARN or FAIL with a short reason):
  cluster-health   cluster status; yellow on a single node is a WARN (replicas can't be placed)
  index-health     status and unassigned shards of every <prefix>-records-* index
  doc-counts       documents per index, by event, by verify status (anomalies are a WARN)
  duplicate-ids    the same node_id (the document _id of an intact record) in more than one document
  lifecycle        pre_* requests with no post_* / api_request_error (WARN once older than
                   --pending-grace), completions whose request is missing, records whose
                   ATP parent record is missing, sessions with no first record (seq 0),
                   subagent links to a parent session with no documents
  root-session     documents missing root_session_id, roots that name no session in the index,
                   roots that disagree with the links (what hermes_atp_restamp.py would fix)
  timestamps       future @timestamp (beyond --max-skew), a completion earlier than its request,
                   seq order vs time within a session, ingestion gaps longer than --max-gap
  freshness        age of the newest document (ingested_at) vs --max-age
  expected-count   with --expected-count N: total documents vs the exporter's shipped total

Output has ids and counts only: no document bodies, no credentials. Exit code 0 = no FAIL,
2 = at least one FAIL, 3 = Elasticsearch unreachable or refused.

Usage:
    hermes_atp_audit.py --es-url https://<SOLEBAY_IP>:9200 --ca-cert certs/ca/ca.crt [--json]
The password is read from HERMES_ES_PASSWORD, --password-file, or prompted for.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import hermes_atp_export as hx  # noqa: E402
import hermes_atp_restamp as rs  # noqa: E402  (read-only helpers: plan)

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"
RANK = {PASS: 0, WARN: 1, FAIL: 2}
PRE_EVENTS = {"pre_api_request": "api", "pre_tool_call": "tool"}
POST_EVENTS = {"post_api_request": "api", "api_request_error": "api", "post_tool_call": "tool"}
SOURCE_FIELDS = ["node_id", "session_id", "root_session_id", "parent_session_id", "request_node_id", "parents",
                 "event", "seq", "@timestamp", "ingested_at", "anomaly", "verify.status", "verify.authentic",
                 "api_request_id", "tool_call_id"]
PAGE = 1000
SCROLL = "2m"
SAMPLE = 5

# The only requests this tool may make. Checked on the path before the query string.
READ_ONLY = [
    ("GET", re.compile(r"^/$")),
    ("GET", re.compile(r"^/_cluster/health(/[^/]+)?$")),
    ("GET", re.compile(r"^/_cat/[a-z_]+(/[^/]+)?$")),
    ("GET", re.compile(r"^/[^/_][^/]*/_(search|count)$")),
    ("POST", re.compile(r"^/[^/_][^/]*/_(search|count)$")),
    ("POST", re.compile(r"^/_search/scroll$")),
]


class WriteRefused(Exception):
    """A request that is not on the read-only list (a bug in this tool, never sent)."""


def is_read_only(method: str, path: str) -> bool:
    p = path.split("?", 1)[0]
    return any(method == m and rx.match(p) for m, rx in READ_ONLY)


class ReadOnlyClient:
    """Wraps hx.ESClient (or a test fake) and refuses every request that isn't a read."""

    def __init__(self, inner):
        self.inner = inner

    def get(self, path: str) -> dict:
        return self._req("GET", path, None)

    def post(self, path: str, body: dict) -> dict:
        return self._req("POST", path, json.dumps(body).encode())

    def _req(self, method, path, body):
        if not is_read_only(method, path):
            raise WriteRefused(f"refusing {method} {path.split('?')[0]}: not a read")
        return self.inner.request(method, path, body)


# ---------------------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------------------

def parse_ts(v) -> Optional[float]:
    if not isinstance(v, str) or not v:
        return None
    s = v.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def fmt_age(seconds: float) -> str:
    s = int(abs(seconds))
    if s < 120:
        return f"{s}s"
    if s < 7200:
        return f"{s // 60}m"
    if s < 172800:
        return f"{s / 3600:.1f}".rstrip("0").rstrip(".") + "h"
    return f"{s / 86400:.1f}".rstrip("0").rstrip(".") + "d"


def parse_duration(v: str) -> float:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*", str(v))
    if not m:
        raise argparse.ArgumentTypeError(f"bad duration {v!r} (use e.g. 90s, 30m, 6h, 2d)")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def iso(ts: Optional[float]) -> Optional[str]:
    return None if ts is None else time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


def sample(items) -> List[str]:
    return sorted(str(i) for i in items)[:SAMPLE]


def get_field(src: dict, dotted: str):
    if dotted in src:
        return src[dotted]
    cur: Any = src
    for part in dotted.split("."):
        if not isinstance(cur, dict):
            return None
        cur = cur.get(part)
    return cur


class Result:
    def __init__(self):
        self.checks: List[dict] = []

    def add(self, name: str, status: str, reason: str, /, **details):
        self.checks.append({"name": name, "status": status, "reason": reason,
                            "details": {k: v for k, v in details.items() if v not in (None, [], {})}})

    def worst(self) -> str:
        return max((c["status"] for c in self.checks), key=RANK.get, default=PASS)

    def summary(self) -> Dict[str, int]:
        out = {PASS: 0, WARN: 0, FAIL: 0}
        for c in self.checks:
            out[c["status"]] += 1
        return out


# ---------------------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------------------

def scan(client: ReadOnlyClient, index: str, max_docs: int) -> Tuple[List[dict], bool]:
    """Slim copies of every document (only SOURCE_FIELDS, plus _index), via a scroll. Returns
    (docs, truncated). The scroll context expires on its own after SCROLL."""
    body = {"size": PAGE, "_source": SOURCE_FIELDS, "sort": ["_doc"], "query": {"match_all": {}}}
    resp = client.post(f"/{index}/_search?scroll={SCROLL}&allow_no_indices=true&ignore_unavailable=true", body)
    docs: List[dict] = []
    while True:
        hits = resp.get("hits", {}).get("hits", [])
        for h in hits:
            d = dict(h.get("_source") or {})
            d["_index"] = h.get("_index")
            docs.append(d)
            if len(docs) >= max_docs:
                return docs, True
        sid = resp.get("_scroll_id")
        if not hits or not sid:
            return docs, False
        resp = client.post("/_search/scroll", {"scroll": SCROLL, "scroll_id": sid})


# ---------------------------------------------------------------------------------------
# checks
# ---------------------------------------------------------------------------------------

def check_cluster(client: ReadOnlyClient, index: str, res: Result, scanned: Optional[Dict[str, int]] = None) -> None:
    """scanned: {index: documents} from the scan, the fallback when _cat/indices is refused."""
    try:
        h = client.get("/_cluster/health")
    except hx.FatalError as e:
        res.add("cluster-health", WARN, f"cannot read cluster health ({e}); the user needs the monitor privilege")
        h = None
    if h is not None:
        status, nodes = h.get("status"), h.get("number_of_nodes")
        unassigned = h.get("unassigned_shards", 0)
        d = {"status": status, "nodes": nodes, "unassigned_shards": unassigned,
             "active_shards_percent": h.get("active_shards_percent_as_number")}
        if status == "green":
            res.add("cluster-health", PASS, f"green, {nodes} node(s)", **d)
        elif status == "yellow" and nodes == 1:
            res.add("cluster-health", WARN, f"yellow on a single node: {unassigned} replica shard(s) can't be placed "
                    "(normal for one node; the Hermes template uses 0 replicas)", **d)
        elif status == "yellow":
            res.add("cluster-health", WARN, f"yellow: {unassigned} replica shard(s) unassigned", **d)
        else:
            res.add("cluster-health", FAIL, f"cluster status {status}: primary shard(s) unassigned", **d)

    try:
        ih = client.get(f"/_cluster/health/{index}?level=indices&expand_wildcards=all")
        per = ih.get("indices", {}) or {}
    except hx.FatalError:
        per = None
    try:
        cat = client.get(f"/_cat/indices/{index}?format=json&h=index,health,status,pri,rep,docs.count"
                         "&expand_wildcards=all")
    except hx.FatalError as e:
        if "HTTP 403" not in str(e):
            res.add("index-health", FAIL, f"cannot list {index} ({e})")
            return
        seen = dict(sorted((scanned or {}).items()))
        if not seen:
            res.add("index-health", WARN, f"cannot read index health: the user lacks the monitor privilege "
                    f"(HTTP 403 on _cat/indices), and the scan found no {index} index")
        else:
            res.add("index-health", WARN, f"cannot read index health: the user lacks the monitor privilege "
                    f"(HTTP 403 on _cat/indices); the scan read {len(seen)} index(es): {', '.join(seen)}",
                    indices={k: {"documents": v} for k, v in seen.items()})
        return
    if not cat:
        res.add("index-health", FAIL, f"no index matches {index}")
        return
    bad, warn, rows = [], [], {}
    for row in cat:
        name = row.get("index")
        info = (per or {}).get(name, {})
        health = row.get("health") or info.get("status")
        rows[name] = {"health": health, "state": row.get("status"), "primaries": row.get("pri"),
                      "replicas": row.get("rep"), "unassigned_shards": info.get("unassigned_shards")}
        if row.get("status") == "close" or health == "red":
            bad.append(name)
        elif health != "green":
            warn.append(name)
    if bad:
        res.add("index-health", FAIL, f"{len(bad)} index(es) red or closed: {', '.join(sorted(bad))}", indices=rows)
    elif warn:
        res.add("index-health", WARN, f"{len(warn)} index(es) not green: {', '.join(sorted(warn))}", indices=rows)
    else:
        res.add("index-health", PASS, f"{len(rows)} index(es), all green and open", indices=rows)


def check_counts(client: ReadOnlyClient, index: str, docs: List[dict], truncated: bool, res: Result,
                 prefix: str) -> int:
    total = int(client.post(f"/{index}/_count?allow_no_indices=true&ignore_unavailable=true",
                            {"query": {"match_all": {}}}).get("count", 0))
    per_index: Dict[str, int] = {}
    by_event: Dict[str, int] = {}
    by_verify: Dict[str, int] = {}
    anomalies = 0
    for d in docs:
        per_index[d["_index"]] = per_index.get(d["_index"], 0) + 1
        ev = d.get("event") if isinstance(d.get("event"), str) else "(none)"
        by_event[ev] = by_event.get(ev, 0) + 1
        vs = get_field(d, "verify.status") or "(none)"
        by_verify[vs] = by_verify.get(vs, 0) + 1
        anomalies += bool(d.get("anomaly"))
    det = dict(total=total, scanned=len(docs), per_index=dict(sorted(per_index.items())),
               by_event=dict(sorted(by_event.items())), by_verify_status=dict(sorted(by_verify.items())),
               anomalies=anomalies)
    invalid = per_index.get(f"{prefix}-records-{hx.INVALID_INDEX_SUFFIX}", 0)
    if total == 0:
        res.add("doc-counts", FAIL, "no documents in " + index, **det)
    elif truncated:
        res.add("doc-counts", WARN, f"{total} documents; scan stopped at --max-docs {len(docs)}, so the checks "
                "below cover only part of the data", **det)
    elif len(docs) != total:
        res.add("doc-counts", WARN, f"_count says {total} but the scan read {len(docs)} (documents arrived "
                "during the audit?)", **det)
    elif anomalies or invalid:
        res.add("doc-counts", WARN, f"{total} documents in {len(per_index)} index(es); {anomalies} failed ingest "
                f"verification (anomaly), {invalid} in the -invalid index", **det)
    else:
        res.add("doc-counts", PASS, f"{total} documents in {len(per_index)} index(es), all verified at ingest",
                **det)
    return total


def check_duplicates(docs: List[dict], res: Result) -> None:
    seen: Dict[str, int] = {}
    for d in docs:
        nid = d.get("node_id")
        if isinstance(nid, str) and nid:
            seen[nid] = seen.get(nid, 0) + 1
    dups = {k: v for k, v in seen.items() if v > 1}
    if dups:
        extra = sum(v - 1 for v in dups.values())
        res.add("duplicate-ids", FAIL, f"{len(dups)} node_id(s) appear in more than one document ({extra} extra "
                "document(s)); an intact record is stored once, so this is a re-ingest or a tampered/duplicated "
                "line", duplicates=len(dups), extra_documents=extra, sample=sample(dups))
    else:
        res.add("duplicate-ids", PASS, f"{len(seen)} distinct node_id(s), none repeated")


def check_lifecycle(docs: List[dict], now: float, grace: float, res: Result) -> None:
    node_ids = {d["node_id"] for d in docs if isinstance(d.get("node_id"), str)}
    sessions: Dict[str, List[dict]] = {}
    for d in docs:
        if isinstance(d.get("session_id"), str):
            sessions.setdefault(d["session_id"], []).append(d)
    closed = set()
    for d in docs:
        fam = POST_EVENTS.get(d.get("event"))
        if fam:
            c = d.get("api_request_id" if fam == "api" else "tool_call_id")
            if c is not None:
                closed.add((d.get("session_id"), fam, c))
            if isinstance(d.get("request_node_id"), str):
                closed.add(("node", d["request_node_id"]))
    open_old, open_recent = [], []
    pre_keys = set()
    for d in docs:
        fam = PRE_EVENTS.get(d.get("event"))
        if not fam:
            continue
        c = d.get("api_request_id" if fam == "api" else "tool_call_id")
        pre_keys.add((d.get("session_id"), fam, c))
        if (d.get("session_id"), fam, c) in closed or ("node", d.get("node_id")) in closed:
            continue
        ts = parse_ts(d.get("@timestamp"))
        (open_recent if ts is not None and now - ts < grace else open_old).append(d.get("node_id") or "?")
    orphans, unlinked = [], []
    for d in docs:
        fam = POST_EVENTS.get(d.get("event"))
        if not fam:
            continue
        rn = d.get("request_node_id")
        if isinstance(rn, str):
            if rn not in node_ids:
                orphans.append(d.get("node_id") or "?")
        else:
            c = d.get("api_request_id" if fam == "api" else "tool_call_id")
            if (d.get("session_id"), fam, c) not in pre_keys:
                unlinked.append(d.get("node_id") or "?")
    missing_parent = []
    for d in docs:
        for p in d.get("parents") or []:
            if p not in node_ids:
                missing_parent.append(d.get("node_id") or "?")
                break
    no_start = [s for s, ds in sessions.items() if not any(x.get("seq") == 0 for x in ds)]
    known_sessions = set(sessions)
    dangling_links = sorted({d["parent_session_id"] for d in docs
                             if isinstance(d.get("parent_session_id"), str)
                             and d["parent_session_id"] not in known_sessions})
    det = dict(sessions=len(sessions), open_requests_older_than_grace=len(open_old),
               open_requests_in_flight=len(open_recent), completions_missing_request=len(orphans),
               completions_without_request_link=len(unlinked), records_missing_parent=len(missing_parent),
               sessions_without_first_record=len(no_start), links_to_missing_parent_session=len(dangling_links),
               sample_open_requests=sample(open_old), sample_completions_missing_request=sample(orphans),
               sample_records_missing_parent=sample(missing_parent), sample_sessions_without_first_record=
               sample(no_start), sample_missing_parent_sessions=sample(dangling_links))
    fails, warns = [], []
    if orphans:
        fails.append(f"{len(orphans)} completion(s) whose request record is not in the index")
    if missing_parent:
        fails.append(f"{len(missing_parent)} record(s) whose previous/parent record is missing (lost documents, "
                     "or an index deleted by retention)")
    if open_old:
        warns.append(f"{len(open_old)} request(s) started more than {fmt_age(grace)} ago with no end "
                     "(crashed or killed sessions)")
    if unlinked:
        warns.append(f"{len(unlinked)} completion(s) with no matching request")
    if no_start:
        warns.append(f"{len(no_start)} session(s) with no first record (seq 0)")
    if dangling_links:
        warns.append(f"{len(dangling_links)} subagent link(s) to a parent session with no documents")
    if fails:
        res.add("lifecycle", FAIL, "; ".join(fails + warns), **det)
    elif warns:
        res.add("lifecycle", WARN, "; ".join(warns), **det)
    else:
        extra = f", {len(open_recent)} in flight" if open_recent else ""
        res.add("lifecycle", PASS, f"{len(sessions)} session(s): every request has its end{extra}, every "
                "parent record present", **det)


def check_roots(docs: List[dict], res: Result) -> None:
    with_session = [d for d in docs if isinstance(d.get("session_id"), str)]
    sessions = {d["session_id"] for d in with_session}
    missing = [d for d in with_session if not d.get("root_session_id")]
    roots = {d["root_session_id"] for d in with_session if d.get("root_session_id")}
    dangling = sorted(r for r in roots if r not in sessions)
    links = sorted({(d["session_id"], d["parent_session_id"]) for d in with_session
                    if isinstance(d.get("parent_session_id"), str) and get_field(d, "verify.authentic") is True})
    current = sorted({(d["session_id"], d.get("root_session_id") or None) for d in with_session},
                     key=lambda t: (t[0], t[1] or ""))
    todo = rs.plan(links, current)
    stale = sorted({s for v in todo.values() for s in v} - {d["session_id"] for d in missing})
    det = dict(documents_with_session=len(with_session), missing_root=len(missing),
               sessions_missing_root=len({d["session_id"] for d in missing}), roots=len(roots),
               roots_without_session=len(dangling), sessions_with_outdated_root=len(stale),
               sample_roots_without_session=sample(dangling), sample_outdated_sessions=sample(stale),
               sample_sessions_missing_root=sample({d["session_id"] for d in missing}))
    problems = []
    if missing:
        problems.append(f"{len(missing)} document(s) missing root_session_id")
    if stale:
        problems.append(f"{len(stale)} session(s) with a root that disagrees with the links")
    if dangling:
        problems.append(f"{len(dangling)} root_session_id value(s) name a session with no documents (its parent "
                        "was never exported, or was deleted)")
    if not with_session:
        res.add("root-session", WARN, "no documents with a session_id", **det)
    elif missing or stale:
        res.add("root-session", WARN, "; ".join(problems) + "; run hermes_atp_restamp.py --dry-run, then without "
                "--dry-run", **det)
    elif dangling:
        res.add("root-session", WARN, "; ".join(problems), **det)
    else:
        res.add("root-session", PASS, f"all {len(with_session)} document(s) carry root_session_id; "
                f"{len(roots)} root(s), all consistent with the links", **det)


def check_timestamps(docs: List[dict], now: float, skew: float, max_gap: float, lookback: float,
                     res: Result) -> None:
    future = [d.get("node_id") or "?" for d in docs
              if (parse_ts(d.get("@timestamp")) or 0) > now + skew]
    by_node = {d["node_id"]: d for d in docs if isinstance(d.get("node_id"), str)}
    before_request = []
    for d in docs:
        rn = d.get("request_node_id")
        if POST_EVENTS.get(d.get("event")) and isinstance(rn, str) and rn in by_node:
            t_end, t_start = parse_ts(d.get("@timestamp")), parse_ts(by_node[rn].get("@timestamp"))
            if t_end is not None and t_start is not None and t_end < t_start:
                before_request.append(d.get("node_id") or "?")
    sessions: Dict[str, List[Tuple[int, float]]] = {}
    for d in docs:
        seq, ts = d.get("seq"), parse_ts(d.get("@timestamp"))
        if isinstance(d.get("session_id"), str) and isinstance(seq, int) and not isinstance(seq, bool) and ts:
            sessions.setdefault(d["session_id"], []).append((seq, ts))
    seq_disorder = []
    for sid, rows in sessions.items():
        rows.sort()
        if any(b[1] < a[1] for a, b in zip(rows, rows[1:])):
            seq_disorder.append(sid)
    ingested = sorted(t for t in (parse_ts(d.get("ingested_at")) for d in docs) if t is not None and
                      t >= now - lookback)
    gaps = [(b - a, a, b) for a, b in zip(ingested, ingested[1:]) if b - a > max_gap]
    biggest = max(gaps, default=None)
    det = dict(future_records=len(future), completions_before_request=len(before_request),
               sessions_out_of_time_order=len(seq_disorder), ingestion_gaps=len(gaps),
               largest_gap=fmt_age(biggest[0]) if biggest else None,
               largest_gap_from=iso(biggest[1]) if biggest else None,
               largest_gap_to=iso(biggest[2]) if biggest else None,
               sample_future=sample(future), sample_completions_before_request=sample(before_request),
               sample_sessions_out_of_order=sample(seq_disorder))
    fails = []
    if future:
        fails.append(f"{len(future)} record(s) dated more than {fmt_age(skew)} in the future (clock skew on "
                     "nuc-hermes?)")
    if before_request:
        fails.append(f"{len(before_request)} completion(s) timestamped before their request")
    if seq_disorder:
        fails.append(f"{len(seq_disorder)} session(s) where a later seq has an earlier timestamp")
    gap_msg = (f"{len(gaps)} ingestion gap(s) over {fmt_age(max_gap)} in the last {fmt_age(lookback)}, largest "
               f"{fmt_age(biggest[0])} ({iso(biggest[1])} to {iso(biggest[2])}); Hermes idle, or the exporter "
               "was down") if gaps else None
    if fails:
        res.add("timestamps", FAIL, "; ".join(fails + ([gap_msg] if gap_msg else [])), **det)
    elif gaps:
        res.add("timestamps", WARN, gap_msg, **det)
    else:
        res.add("timestamps", PASS, f"no future records, requests before their ends, seq in time order, no "
                f"ingestion gap over {fmt_age(max_gap)} in the last {fmt_age(lookback)}", **det)


def check_freshness(docs: List[dict], now: float, max_age: float, strict: bool, res: Result) -> None:
    newest_ing = max((t for t in (parse_ts(d.get("ingested_at")) for d in docs) if t), default=None)
    newest_rec = max((t for t in (parse_ts(d.get("@timestamp")) for d in docs) if t), default=None)
    if newest_ing is None:
        res.add("freshness", FAIL, "no document has an ingested_at time")
        return
    age = now - newest_ing
    det = dict(newest_ingested_at=iso(newest_ing), newest_record_at=iso(newest_rec), age_seconds=int(age),
               max_age_seconds=int(max_age))
    if age <= max_age:
        res.add("freshness", PASS, f"newest document ingested {fmt_age(age)} ago (limit {fmt_age(max_age)})", **det)
    else:
        res.add("freshness", FAIL if strict else WARN,
                f"newest document ingested {fmt_age(age)} ago, over {fmt_age(max_age)}: Hermes idle, or the "
                "exporter on nuc-hermes stopped (check `systemctl status hermes-atp-export`)", **det)


def check_expected(total: int, expected: Optional[int], res: Result) -> None:
    if expected is None:
        return
    det = dict(in_elasticsearch=total, expected=expected, difference=total - expected)
    if total == expected:
        res.add("expected-count", PASS, f"{total} documents, as expected", **det)
    elif total < expected:
        res.add("expected-count", FAIL, f"{expected - total} fewer documents than the exporter shipped "
                f"({total} vs {expected})", **det)
    else:
        res.add("expected-count", WARN, f"{total - expected} more documents than expected ({total} vs {expected}): "
                "other runs or sources, or a stale expected total", **det)


def run(client, *, index_prefix: str = "hermes-atp", now: Optional[float] = None, max_age: float = 1800,
        max_gap: float = 6 * 3600, lookback: float = 7 * 86400, max_skew: float = 300,
        pending_grace: float = 900, expected_count: Optional[int] = None, max_docs: int = 2_000_000,
        strict_freshness: bool = False) -> Result:
    ro = client if isinstance(client, ReadOnlyClient) else ReadOnlyClient(client)
    now = time.time() if now is None else now
    index = f"{index_prefix}-records-*"
    res = Result()
    docs, truncated = scan(ro, index, max_docs)
    scanned: Dict[str, int] = {}
    for d in docs:
        scanned[d["_index"]] = scanned.get(d["_index"], 0) + 1
    check_cluster(ro, index, res, scanned)
    total = check_counts(ro, index, docs, truncated, res, index_prefix)
    if docs:
        check_duplicates(docs, res)
        check_lifecycle(docs, now, pending_grace, res)
        check_roots(docs, res)
        check_timestamps(docs, now, max_skew, max_gap, lookback, res)
        check_freshness(docs, now, max_age, strict_freshness, res)
    check_expected(total, expected_count, res)
    return res


def render_text(res: Result) -> str:
    out = []
    for c in res.checks:
        out.append(f"{c['status']:<4}  {c['name']:<15} {c['reason']}")
    s = res.summary()
    out.append(f"summary: {s[PASS]} pass, {s[WARN]} warn, {s[FAIL]} fail -> {res.worst()}")
    return "\n".join(out)


def main(argv=None, client_factory: Callable[[Any], Any] = None, now: Optional[float] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--es-url", default=os.environ.get("HERMES_ES_URL"),
                    help="e.g. https://<SOLEBAY_IP>:9200 (Elasticsearch binds only the LAN IP, not localhost)")
    ap.add_argument("--ca-cert", default=os.environ.get("HERMES_ES_CA_CERT"))
    ap.add_argument("--username", default=os.environ.get("HERMES_ES_USERNAME", "elastic"))
    ap.add_argument("--password-file", help="file holding the password (else HERMES_ES_PASSWORD, else a prompt)")
    ap.add_argument("--index-prefix", default="hermes-atp")
    ap.add_argument("--allow-insecure-http", action="store_true")
    ap.add_argument("--max-age", type=parse_duration, default="30m", help="freshness limit (default 30m)")
    ap.add_argument("--strict-freshness", action="store_true", help="a stale index is a FAIL, not a WARN")
    ap.add_argument("--max-gap", type=parse_duration, default="6h", help="ingestion gap limit (default 6h)")
    ap.add_argument("--gap-lookback", type=parse_duration, default="7d", help="window for gaps (default 7d)")
    ap.add_argument("--max-skew", type=parse_duration, default="5m", help="allowed future skew (default 5m)")
    ap.add_argument("--pending-grace", type=parse_duration, default="15m",
                    help="an open request younger than this is in flight, not missing (default 15m)")
    ap.add_argument("--expected-count", type=int, help="the exporter's shipped total to compare with")
    ap.add_argument("--max-docs", type=int, default=2_000_000, help="stop scanning after this many documents")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    a = ap.parse_args(argv)
    if not a.es_url:
        print("hermes-atp-audit: --es-url is required (e.g. https://<SOLEBAY_IP>:9200; Elasticsearch binds only "
              "the LAN IP)", file=sys.stderr)
        return 1
    if a.es_url.startswith("http://") and not a.allow_insecure_http:
        print("hermes-atp-audit: refusing plain http:// (use https://)", file=sys.stderr)
        return 1
    if a.password_file:
        password = hx._read_secret_file(a.password_file)
    else:
        password = os.environ.get("HERMES_ES_PASSWORD") or getpass.getpass(f"password for {a.username}: ")
    cfg = hx.Config(dict(hx.Config.DEFAULTS, es_url=a.es_url, ca_cert=a.ca_cert and os.path.abspath(a.ca_cert),
                         username=a.username, password=password, timeout=120.0))
    client = (client_factory or hx.ESClient)(cfg)
    try:
        res = run(client, index_prefix=a.index_prefix, now=now, max_age=a.max_age, max_gap=a.max_gap,
                  lookback=a.gap_lookback, max_skew=a.max_skew, pending_grace=a.pending_grace,
                  expected_count=a.expected_count, max_docs=a.max_docs, strict_freshness=a.strict_freshness)
    except (hx.FatalError, hx.TransientError, WriteRefused) as e:
        print(f"hermes-atp-audit: {e}", file=sys.stderr)
        return 3
    if a.json:
        print(json.dumps({"result": res.worst(), "summary": res.summary(), "checks": res.checks}, indent=2))
    else:
        print(render_text(res))
    return 2 if res.worst() == FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
