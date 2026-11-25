#!/usr/bin/env python3
"""hermes-atp-restamp: set root_session_id on documents already in Elasticsearch (stdlib only).

Run on the Elasticsearch host (us-solebay) with admin credentials (e.g. `elastic`), never on
the Hermes host: the exporter's own role can only create documents, by design, so it can't
correct documents it already shipped. This tool:

  1. adds the root_session_id keyword field to the mapping of every existing
     <prefix>-records-* index (the index template only applies to indices created after it);
  2. reads the subagent links (parent_session_id on authentic records) from Elasticsearch;
  3. works out each session's root with the exporter's own resolve_root (same cycle and orphan
     rules), and
  4. runs _update_by_query for the documents whose root_session_id is missing or different.

It is idempotent: a second run finds nothing to change. Use it to backfill indices written
before root_session_id existed, and whenever the exporter logs that roots changed after
shipping (then run `hermes_atp_export.py --restamp-done` on the exporter host).

Usage:
    hermes_atp_restamp.py --es-url https://<SOLEBAY_IP>:9200 --ca-cert certs/ca/ca.crt [--dry-run]
(Elasticsearch on us-solebay listens only on the LAN IP, ES_BIND_ADDR, not on localhost.)
The password is read from HERMES_ES_PASSWORD, --password-file, or prompted for.
"""

from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import sys
from typing import Dict, Iterable, List, Optional, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import hermes_atp_export as hx  # noqa: E402

log = logging.getLogger("hermes-atp-restamp")
MAPPING = {"properties": {"root_session_id": {"type": "keyword", "ignore_above": 1024}}}
PAGE = 1000
TERMS_CHUNK = 1000
SCRIPT = {"lang": "painless", "source": "ctx._source.root_session_id = params.root", "params": {}}


def plan(links: Iterable[Tuple[str, str]], current: Iterable[Tuple[str, Optional[str]]]) -> Dict[str, List[str]]:
    """{root: [session ids whose documents need root_session_id = root]}.

    links: (session, parent) pairs from authentic records; a session with two parents keeps the
    smaller id, as the exporter does. current: (session, root_session_id or None) pairs present in
    the index (one per distinct combination)."""
    parents: Dict[str, str] = {}
    for child, parent in links:
        parents[child] = min(parents[child], parent) if child in parents else parent
    out: Dict[str, set] = {}
    for sid, root_now in current:
        want = hx.resolve_root(sid, parents)
        if root_now != want:
            out.setdefault(want, set()).add(sid)
    return {root: sorted(s) for root, s in sorted(out.items())}


def composite(client, index: str, query: dict, sources: list) -> Iterable[dict]:
    after = None
    while True:
        agg = {"size": PAGE, "sources": sources}
        if after:
            agg["after"] = after
        body = {"size": 0, "query": query, "aggs": {"c": {"composite": agg}}}
        resp = client.request("POST", f"/{index}/_search?allow_no_indices=true", json.dumps(body).encode())
        c = resp.get("aggregations", {}).get("c", {})
        buckets = c.get("buckets", [])
        yield from (b["key"] for b in buckets)
        after = c.get("after_key")
        if not buckets or not after:
            return


def read_links(client, index: str) -> List[Tuple[str, str]]:
    q = {"bool": {"filter": [{"exists": {"field": "parent_session_id"}}, {"term": {"verify.authentic": True}}]}}
    return [(k["s"], k["p"]) for k in composite(client, index, q, [
        {"s": {"terms": {"field": "session_id"}}}, {"p": {"terms": {"field": "parent_session_id"}}}])]


def read_current(client, index: str) -> List[Tuple[str, Optional[str]]]:
    q = {"exists": {"field": "session_id"}}
    return [(k["s"], k["r"]) for k in composite(client, index, q, [
        {"s": {"terms": {"field": "session_id"}}},
        {"r": {"terms": {"field": "root_session_id", "missing_bucket": True}}}])]


def apply(client, index: str, todo: Dict[str, List[str]]) -> int:
    updated = 0
    for root, sessions in todo.items():
        for i in range(0, len(sessions), TERMS_CHUNK):
            chunk = sessions[i:i + TERMS_CHUNK]
            body = {"query": {"bool": {"filter": [{"terms": {"session_id": chunk}}],
                                       "must_not": [{"term": {"root_session_id": root}}]}},
                    "script": dict(SCRIPT, params={"root": root})}
            resp = client.request("POST", f"/{index}/_update_by_query?conflicts=proceed&refresh=true"
                                          "&allow_no_indices=true", json.dumps(body).encode())
            updated += int(resp.get("updated", 0))
            if resp.get("failures"):
                raise hx.FatalError(f"_update_by_query reported {len(resp['failures'])} failure(s)")
    return updated


def run(client, index_prefix: str, dry_run: bool = False) -> dict:
    index = f"{index_prefix}-records-*"
    if not dry_run:
        client.request("PUT", f"/{index}/_mapping?allow_no_indices=true", json.dumps(MAPPING).encode())
    links = read_links(client, index)
    todo = plan(links, read_current(client, index))
    sessions = sum(len(v) for v in todo.values())
    result = {"links": len(links), "roots_to_fix": len(todo), "sessions_to_fix": sessions, "updated": 0,
              "dry_run": dry_run}
    if not dry_run and todo:
        result["updated"] = apply(client, index, todo)
    return result


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--es-url", default=os.environ.get("HERMES_ES_URL"),
                    help="e.g. https://<SOLEBAY_IP>:9200 (Elasticsearch binds only the LAN IP, not localhost)")
    ap.add_argument("--ca-cert", default=os.environ.get("HERMES_ES_CA_CERT"))
    ap.add_argument("--username", default=os.environ.get("HERMES_ES_USERNAME", "elastic"))
    ap.add_argument("--password-file", help="file holding the password (else HERMES_ES_PASSWORD, else a prompt)")
    ap.add_argument("--index-prefix", default="hermes-atp")
    ap.add_argument("--allow-insecure-http", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="report what would change; change nothing")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if not a.es_url:
        print("hermes-atp-restamp: --es-url is required (e.g. https://<SOLEBAY_IP>:9200; Elasticsearch binds only "
              "the LAN IP)", file=sys.stderr)
        return 1
    if a.password_file:
        password = hx._read_secret_file(a.password_file)
    else:
        password = os.environ.get("HERMES_ES_PASSWORD") or getpass.getpass(f"password for {a.username}: ")
    if a.es_url.startswith("http://") and not a.allow_insecure_http:
        print("hermes-atp-restamp: refusing plain http:// (use https://)", file=sys.stderr)
        return 1
    cfg = hx.Config(dict(hx.Config.DEFAULTS, es_url=a.es_url, ca_cert=a.ca_cert and os.path.abspath(a.ca_cert),
                         username=a.username, password=password, timeout=300.0))
    try:
        result = run(hx.ESClient(cfg), a.index_prefix, a.dry_run)
    except (hx.FatalError, hx.TransientError) as e:
        print(f"hermes-atp-restamp: {e}", file=sys.stderr)
        return 3
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
