"""hermes_atp_audit.py tests: a read-only audit over documents shipped by the real exporter.

The documents come from the REAL recorder (fake-Hermes host) shipped by the REAL exporter into
the in-process fake _bulk API; an in-memory fake then answers the audit's read requests
(cluster health, _cat/indices, _count, scroll searches). No network beyond 127.0.0.1.
"""

from __future__ import annotations

import copy
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ELASTIC = os.path.dirname(HERE)
PLUGIN = os.path.dirname(ELASTIC)
sys.path.insert(0, os.path.join(ELASTIC, "exporter"))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(PLUGIN, "tests"))

import hermes_atp_export as hx  # noqa: E402
import hermes_atp_audit as au  # noqa: E402
from fake_es import FakeES  # noqa: E402
from test_exporter import AUTH, PASSWORD, PUB, TREE, record_sessions  # noqa: E402
import support as S  # noqa: E402

NOW = S.VECTOR_EPOCH + 1200  # twenty minutes after the first record


def shipped_docs():
    """{(index, _id): doc} for the TREE sessions, recorded and shipped by the real code. ingested_at
    is pinned to @timestamp + 2 s so the results don't depend on the wall clock."""
    tmp = tempfile.mkdtemp(prefix="hx-audit-")
    try:
        log_path = os.path.join(tmp, "records.jsonl")
        record_sessions(log_path, TREE)
        with FakeES(auth=AUTH) as es:
            cfg = hx.Config.load(env={}, overrides={
                "es_url": es.url, "allow_insecure_http": True, "username": "hermes_exporter",
                "password": PASSWORD, "pub": PUB, "logs": [log_path], "state_dir": os.path.join(tmp, "state"),
                "mode": "copy", "timeout": 5})
            hx.Exporter(cfg, sleep=lambda s: None).run()
            docs = copy.deepcopy(es.docs)
    finally:
        shutil.rmtree(tmp, True)
    for d in docs.values():
        d["ingested_at"] = au.iso(au.parse_ts(d["@timestamp"]) + 2)
    return docs


class FakeAuditES:
    """Answers the audit's read requests from {(index, _id): doc}. Records every call."""

    def __init__(self, docs, health="green", nodes=1, unassigned=0, health_status=200, cat_status=200):
        self.docs = docs
        self.calls = []
        self.health = {"status": health, "number_of_nodes": nodes, "unassigned_shards": unassigned,
                       "active_shards_percent_as_number": 100.0 if not unassigned else 50.0}
        self.health_status = health_status
        self.cat_status = cat_status
        self.scrolls = {}

    def indices(self):
        return sorted({i for i, _ in self.docs})

    def _source(self, doc, fields):
        top = {f.split(".")[0] for f in fields}
        return {k: copy.deepcopy(v) for k, v in doc.items() if k in top}

    def request(self, method, path, body=None):
        self.calls.append((method, path))
        p = path.split("?")[0]
        b = json.loads(body) if body else {}
        if p == "/_cluster/health":
            if self.health_status in (401, 403):
                raise hx.FatalError(f"Elasticsearch refused the credentials or privileges (HTTP {self.health_status})")
            return dict(self.health)
        if p.startswith("/_cluster/health/"):
            return {"indices": {i: {"status": "green", "unassigned_shards": 0} for i in self.indices()}}
        if p.startswith("/_cat/indices/"):
            if self.cat_status in (401, 403):
                raise hx.FatalError(f"Elasticsearch refused the credentials or privileges (HTTP {self.cat_status})")
            return [{"index": i, "health": "green", "status": "open", "pri": "1", "rep": "0",
                     "docs.count": str(sum(1 for x, _ in self.docs if x == i))} for i in self.indices()]
        if p.endswith("/_count"):
            return {"count": len(self.docs)}
        if p.endswith("/_search") and "scroll=" in path:
            hits = [{"_index": i, "_id": _id, "_source": self._source(d, b["_source"])}
                    for (i, _id), d in sorted(self.docs.items())]
            sid = f"scroll-{len(self.scrolls)}"
            self.scrolls[sid] = (hits, b["size"])
            return self._page(sid, 0)
        if p == "/_search/scroll":
            hits, size = self.scrolls[b["scroll_id"]]
            pos = self.scrolls.get(b["scroll_id"] + ":pos", size)
            self.scrolls[b["scroll_id"] + ":pos"] = pos + size
            return self._page(b["scroll_id"], pos)
        raise AssertionError(f"unexpected request {method} {path}")

    def _page(self, sid, pos):
        hits, size = self.scrolls[sid]
        return {"_scroll_id": sid, "hits": {"hits": hits[pos:pos + size]}}


def by_name(res):
    return {c["name"]: c for c in res.checks}


class AuditBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.base_docs = shipped_docs()

    def setUp(self):
        self.docs = copy.deepcopy(self.base_docs)

    def audit(self, fake=None, **kw):
        self.fake = fake or FakeAuditES(self.docs)
        kw.setdefault("now", NOW)
        return au.run(self.fake, **kw)

    def find(self, **match):
        return [(k, d) for k, d in sorted(self.docs.items()) if all(d.get(f) == v for f, v in match.items())]


class CleanDataTests(AuditBase):
    def test_real_export_passes_every_check(self):
        res = self.audit(expected_count=len(self.docs))
        self.assertEqual({c["name"]: c["status"] for c in res.checks}, {
            "cluster-health": "PASS", "index-health": "PASS", "doc-counts": "PASS", "duplicate-ids": "PASS",
            "lifecycle": "PASS", "root-session": "PASS", "timestamps": "PASS", "freshness": "PASS",
            "expected-count": "PASS"}, json.dumps(res.checks, indent=1))
        self.assertEqual(res.worst(), "PASS")

    def test_counts_by_index_event_and_verify_status(self):
        c = by_name(self.audit())["doc-counts"]["details"]
        self.assertEqual(c["total"], len(self.docs))
        self.assertEqual(sum(c["per_index"].values()), len(self.docs))
        self.assertEqual(set(c["by_event"]), {"pre_api_request", "post_api_request", "pre_tool_call",
                                              "post_tool_call"})
        self.assertEqual(c["by_event"]["pre_tool_call"], len(TREE))
        self.assertEqual(c["by_verify_status"], {"valid": len(self.docs)})

    def test_pagination(self):
        with mock.patch.object(au, "PAGE", 3):
            res = self.audit()
        self.assertEqual(by_name(res)["doc-counts"]["details"]["scanned"], len(self.docs))
        self.assertGreater(sum(1 for m, p in self.fake.calls if p == "/_search/scroll"), 3)

    def test_max_docs_truncates_with_a_warning(self):
        res = self.audit(max_docs=5)
        self.assertEqual(by_name(res)["doc-counts"]["status"], "WARN")
        self.assertIn("--max-docs", by_name(res)["doc-counts"]["reason"])

    def test_empty_index_fails(self):
        res = self.audit(FakeAuditES({}))
        self.assertEqual(by_name(res)["index-health"]["status"], "FAIL")
        self.assertEqual(by_name(res)["doc-counts"]["status"], "FAIL")

    def test_anomalies_warn(self):
        (k, d), = self.find(event="post_tool_call", session_id="req-t")
        d["anomaly"] = True
        d["verify"] = {"status": "bad-signature", "valid": False, "authentic": False}
        c = by_name(self.audit())["doc-counts"]
        self.assertEqual(c["status"], "WARN")
        self.assertEqual(c["details"]["by_verify_status"]["bad-signature"], 1)


class ReadOnlyTests(AuditBase):
    def test_every_request_is_a_read(self):
        self.audit(expected_count=1)
        self.assertTrue(self.fake.calls)
        for method, path in self.fake.calls:
            self.assertIn(method, ("GET", "POST"))
            self.assertTrue(au.is_read_only(method, path), path)
            p = path.split("?")[0]
            if method == "POST":
                self.assertRegex(p, r"/_(search|count)$|^/_search/scroll$")

    def test_writes_are_refused_before_the_network(self):
        inner = mock.Mock()
        ro = au.ReadOnlyClient(inner)
        for method, path in [("PUT", "/hermes-atp-records-*/_mapping"), ("DELETE", "/hermes-atp-records-2026.11"),
                             ("POST", "/hermes-atp-records-*/_update_by_query"), ("POST", "/_bulk"),
                             ("POST", "/hermes-atp-records-*/_delete_by_query"), ("POST", "/hermes-atp-records-*/_pit"),
                             ("POST", "/_security/user/elastic/_password"), ("PUT", "/_cluster/settings"),
                             ("POST", "/hermes-atp-records-*/_doc"), ("DELETE", "/_search/scroll"),
                             ("POST", "/_cluster/reroute"), ("POST", "/_cat/indices")]:
            with self.assertRaises(au.WriteRefused, msg=f"{method} {path}"):
                ro._req(method, path, b"{}")
        inner.request.assert_not_called()

    def test_reads_are_allowed(self):
        for method, path in [("GET", "/_cluster/health"), ("GET", "/_cluster/health/hermes-atp-records-*?level=indices"),
                             ("GET", "/_cat/indices/hermes-atp-records-*?format=json"),
                             ("POST", "/hermes-atp-records-*/_search?scroll=2m"), ("POST", "/hermes-atp-records-*/_count"),
                             ("POST", "/_search/scroll"), ("GET", "/")]:
            self.assertTrue(au.is_read_only(method, path), path)

    def test_source_has_no_body_field(self):
        self.assertNotIn("body", {f.split(".")[0] for f in au.SOURCE_FIELDS})


class ClusterHealthTests(AuditBase):
    def test_yellow_single_node_warns_with_explanation(self):
        c = by_name(self.audit(FakeAuditES(self.docs, health="yellow", nodes=1, unassigned=3)))["cluster-health"]
        self.assertEqual(c["status"], "WARN")
        self.assertIn("single node", c["reason"])

    def test_red_fails(self):
        res = self.audit(FakeAuditES(self.docs, health="red"))
        self.assertEqual(by_name(res)["cluster-health"]["status"], "FAIL")
        self.assertEqual(res.worst(), "FAIL")

    def test_no_monitor_privilege_warns(self):
        c = by_name(self.audit(FakeAuditES(self.docs, health_status=403)))["cluster-health"]
        self.assertEqual(c["status"], "WARN")
        self.assertIn("monitor", c["reason"])


    def test_viewer_without_monitor_warns_and_falls_back_to_scanned_indices(self):
        res = self.audit(FakeAuditES(self.docs, health_status=403, cat_status=403), expected_count=len(self.docs))
        c = by_name(res)
        self.assertEqual(c["cluster-health"]["status"], "WARN")
        self.assertEqual(c["index-health"]["status"], "WARN")
        self.assertIn("monitor privilege", c["index-health"]["reason"])
        want = sorted({i for i, _ in self.docs})
        self.assertEqual(sorted(c["index-health"]["details"]["indices"]), want)
        self.assertEqual(sum(v["documents"] for v in c["index-health"]["details"]["indices"].values()),
                         len(self.docs))
        self.assertEqual(res.worst(), "WARN", "every data check still passes: no FAIL, exit 0")
        for name in ("doc-counts", "duplicate-ids", "lifecycle", "root-session", "timestamps", "freshness",
                     "expected-count"):
            self.assertEqual(c[name]["status"], "PASS", name)

    def test_other_cat_errors_still_fail(self):
        class Broken(FakeAuditES):
            def request(self, method, path, body=None):
                if path.startswith("/_cat/indices/"):
                    raise hx.FatalError("GET /_cat/indices/x rejected (HTTP 400)")
                return super().request(method, path, body)
        self.assertEqual(by_name(self.audit(Broken(self.docs)))["index-health"]["status"], "FAIL")


class DuplicateTests(AuditBase):
    def test_same_node_id_in_two_documents_fails(self):
        (k, d), = self.find(event="pre_tool_call", session_id="sub-b")
        self.docs[("hermes-atp-records-invalid", "anomaly-x")] = dict(d, anomaly=True)
        c = by_name(self.audit())["duplicate-ids"]
        self.assertEqual(c["status"], "FAIL")
        self.assertEqual(c["details"]["sample"], [d["node_id"]])


class LifecycleTests(AuditBase):
    def test_request_without_end_warns_once_past_grace(self):
        (k, d), = self.find(event="post_tool_call", session_id="req-t")
        self.docs.pop(k)
        c = by_name(self.audit())["lifecycle"]
        self.assertEqual(c["status"], "FAIL")  # the next record's parent (the deleted one) is gone too
        self.assertEqual(c["details"]["open_requests_older_than_grace"], 1)

    def test_crashed_tail_is_a_warning_and_in_flight_is_fine(self):
        last = max(self.find(session_id="req-t"), key=lambda kd: kd[1]["seq"])
        pre = [kd for kd in self.find(session_id="req-t") if kd[1]["event"] == "pre_api_request"][-1]
        for k, d in self.find(session_id="req-t"):
            if d["seq"] > pre[1]["seq"]:
                self.docs.pop(k)
        self.assertNotEqual(last[0], pre[0])
        c = by_name(self.audit())["lifecycle"]
        self.assertEqual(c["status"], "WARN")
        self.assertEqual(c["details"]["sample_open_requests"], [pre[1]["node_id"]])
        ts = au.parse_ts(pre[1]["@timestamp"])
        c = by_name(self.audit(now=ts + 60))["lifecycle"]
        self.assertEqual(c["status"], "PASS")
        self.assertEqual(c["details"]["open_requests_in_flight"], 1)

    def test_completion_whose_request_is_missing_fails(self):
        (k, d), = self.find(event="pre_tool_call", session_id="sub-a")
        self.docs.pop(k)
        c = by_name(self.audit())["lifecycle"]
        self.assertEqual(c["status"], "FAIL")
        self.assertEqual(c["details"]["completions_missing_request"], 1)
        self.assertGreaterEqual(c["details"]["records_missing_parent"], 1)

    def test_session_without_first_record_and_dangling_link(self):
        for k, d in self.find(session_id="req-p"):
            self.docs.pop(k)
        c = by_name(self.audit())["lifecycle"]
        self.assertEqual(c["status"], "WARN")
        self.assertEqual(c["details"]["sample_missing_parent_sessions"], ["req-p"])
        (k, d), = self.find(session_id="sub-b", seq=0)
        self.docs.pop(k)
        c = by_name(self.audit())["lifecycle"]
        self.assertEqual(c["status"], "FAIL")
        self.assertIn("sub-b", c["details"]["sample_sessions_without_first_record"])


class RootSessionAuditTests(AuditBase):
    def test_missing_root_warns_and_points_at_restamp(self):
        for k, d in self.find(session_id="sub-a1"):
            d.pop("root_session_id")
        c = by_name(self.audit())["root-session"]
        self.assertEqual(c["status"], "WARN")
        self.assertIn("hermes_atp_restamp.py", c["reason"])
        self.assertEqual(c["details"]["sample_sessions_missing_root"], ["sub-a1"])

    def test_root_naming_no_session_and_disagreeing_with_links(self):
        for k, d in self.find(session_id="orphan-o"):
            d["root_session_id"] = "ghost"
        c = by_name(self.audit())["root-session"]
        self.assertEqual(c["status"], "WARN")
        self.assertEqual(c["details"]["sample_roots_without_session"], ["ghost"])
        self.assertEqual(c["details"]["sample_outdated_sessions"], ["orphan-o"])

    def test_subagent_whose_parent_was_never_exported(self):
        for k, d in self.find(session_id="req-p"):
            self.docs.pop(k)
        c = by_name(self.audit())["root-session"]
        self.assertEqual(c["status"], "WARN")
        self.assertEqual(c["details"]["sample_roots_without_session"], ["req-p"])
        self.assertEqual(c["details"]["sessions_with_outdated_root"], 0)


class TimestampTests(AuditBase):
    def test_future_record_fails(self):
        (k, d), = self.find(event="pre_tool_call", session_id="req-t")
        d["@timestamp"] = au.iso(NOW + 3600)
        c = by_name(self.audit())["timestamps"]
        self.assertEqual(c["status"], "FAIL")
        self.assertEqual(c["details"]["future_records"], 1)
        self.assertEqual(c["details"]["sessions_out_of_time_order"], 1)

    def test_completion_before_request_fails(self):
        (k, d), = self.find(event="post_tool_call", session_id="sub-b")
        d["@timestamp"] = au.iso(S.VECTOR_EPOCH - 100)
        c = by_name(self.audit())["timestamps"]
        self.assertEqual(c["status"], "FAIL")
        self.assertEqual(c["details"]["completions_before_request"], 1)

    def test_ingestion_gap_warns(self):
        for k, d in self.find(session_id="req-t"):
            d["ingested_at"] = au.iso(au.parse_ts(d["ingested_at"]) - 8 * 3600)
        c = by_name(self.audit(max_gap=3600))["timestamps"]
        self.assertEqual(c["status"], "WARN")
        self.assertEqual(c["details"]["ingestion_gaps"], 1)
        c = by_name(self.audit(max_gap=3600, lookback=3600))["timestamps"]
        self.assertEqual(c["status"], "PASS", "the gap is outside the lookback window")


class FreshnessAndExpectedTests(AuditBase):
    def test_stale_warns_or_fails_when_strict(self):
        self.assertEqual(by_name(self.audit(now=NOW + 7200))["freshness"]["status"], "WARN")
        self.assertEqual(by_name(self.audit(now=NOW + 7200, strict_freshness=True))["freshness"]["status"], "FAIL")
        self.assertEqual(by_name(self.audit(now=NOW + 7200, max_age=86400))["freshness"]["status"], "PASS")

    def test_expected_count(self):
        n = len(self.docs)
        self.assertEqual(by_name(self.audit(expected_count=n))["expected-count"]["status"], "PASS")
        self.assertEqual(by_name(self.audit(expected_count=n + 2))["expected-count"]["status"], "FAIL")
        self.assertEqual(by_name(self.audit(expected_count=n - 2))["expected-count"]["status"], "WARN")
        self.assertNotIn("expected-count", by_name(self.audit()))


class CliTests(AuditBase):
    def cli(self, argv, env=None, fake=None):
        self.fake = fake or FakeAuditES(self.docs)
        made = {}

        def factory(cfg):
            made["auth"] = cfg.auth_header()
            return self.fake
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env or {"HERMES_ES_PASSWORD": PASSWORD}, clear=True), \
                redirect_stdout(out), redirect_stderr(err), \
                mock.patch("getpass.getpass", side_effect=AssertionError("prompted")):
            code = au.main(argv, client_factory=factory, now=NOW)
        return code, out.getvalue(), err.getvalue(), made

    def test_text_output_exit_zero_no_secrets(self):
        code, out, err, made = self.cli(["--es-url", "https://es.example:9200", "--ca-cert", "ca.crt"])
        self.assertEqual(code, 0, out + err)
        self.assertIn("summary: 8 pass, 0 warn, 0 fail -> PASS", out)
        for secret in (PASSWORD, made["auth"], made["auth"].split()[1], "Authorization"):
            self.assertNotIn(secret, out + err)

    def test_json_output_and_fail_exit_code(self):
        (k, d), = self.find(event="pre_tool_call", session_id="sub-b")
        self.docs[("hermes-atp-records-invalid", "anomaly-x")] = dict(d, anomaly=True)
        code, out, err, made = self.cli(["--es-url", "https://es.example:9200", "--json",
                                         "--expected-count", str(len(self.docs))])
        self.assertEqual(code, 2)
        doc = json.loads(out)
        self.assertEqual(doc["result"], "FAIL")
        self.assertEqual({c["name"]: c["status"] for c in doc["checks"]}["duplicate-ids"], "FAIL")
        self.assertNotIn(PASSWORD, out + err)
        for c in doc["checks"]:
            self.assertNotIn("body", json.dumps(c["details"]))

    def test_no_document_bodies_in_output(self):
        for d in self.docs.values():
            d["body"] = {"input": "PROMPT-TEXT-do-not-print"}
        code, out, err, _ = self.cli(["--es-url", "https://es.example:9200", "--json"])
        self.assertNotIn("PROMPT-TEXT", out + err)

    def test_refusals(self):
        code, out, err, _ = self.cli([], env={"HERMES_ES_PASSWORD": PASSWORD})
        self.assertEqual(code, 1)
        self.assertIn("binds only the LAN IP", err)
        code, out, err, _ = self.cli(["--es-url", "http://es.example:9200"])
        self.assertEqual(code, 1)
        self.assertIn("refusing plain http://", err)

    def test_password_file_and_unreachable(self):
        pw = os.path.join(tempfile.mkdtemp(), "pw")
        self.addCleanup(shutil.rmtree, os.path.dirname(pw), True)
        with open(pw, "w") as f:
            f.write(PASSWORD + "\n")

        class Down:
            def request(self, *a):
                raise hx.TransientError("ConnectionRefusedError: [Errno 111] Connection refused")
        code, out, err, made = self.cli(["--es-url", "https://es.example:9200", "--password-file", pw],
                                        env={}, fake=Down())
        self.assertEqual(code, 3)
        self.assertIn("Connection refused", err)
        self.assertNotIn(PASSWORD, out + err)
        self.assertTrue(made["auth"].startswith("Basic "))

    def test_viewer_cli_exits_zero(self):
        code, out, err, _ = self.cli(["--es-url", "https://es.example:9200", "--username", "hermes_viewer",
                                      "--expected-count", str(len(self.docs))],
                                     fake=FakeAuditES(self.docs, health_status=403, cat_status=403))
        self.assertEqual(code, 0, out + err)
        self.assertIn("summary: 7 pass, 2 warn, 0 fail -> WARN", out)

    def test_restamp_requires_es_url(self):
        import hermes_atp_restamp as rs
        err = io.StringIO()
        with mock.patch.dict(os.environ, {"HERMES_ES_PASSWORD": PASSWORD}, clear=True), redirect_stderr(err), \
                mock.patch.object(rs.hx, "ESClient", side_effect=AssertionError("connected")), \
                mock.patch("getpass.getpass", side_effect=AssertionError("prompted")):
            self.assertEqual(rs.main([]), 1)
        self.assertIn("--es-url is required", err.getvalue())
        self.assertIn("binds only the LAN IP", err.getvalue())
        out = io.StringIO()
        with mock.patch.dict(os.environ, {"HERMES_ES_PASSWORD": PASSWORD, "HERMES_ES_URL": "https://es.example:9200"},
                             clear=True), redirect_stdout(out), \
                mock.patch.object(rs, "run", return_value={"dry_run": True}) as run:
            self.assertEqual(rs.main(["--dry-run"]), 0)
        self.assertEqual(run.call_args.args[0].url, "https://es.example:9200", "HERMES_ES_URL is used")
        self.assertNotIn(PASSWORD, out.getvalue())

    def test_real_client_refuses_writes(self):
        cfg = hx.Config(dict(hx.Config.DEFAULTS, es_url="https://127.0.0.1:1", username="elastic", password="x"))
        ro = au.ReadOnlyClient(hx.ESClient(cfg))
        with self.assertRaises(au.WriteRefused):
            ro._req("PUT", "/hermes-atp-records-*/_mapping", b"{}")


if __name__ == "__main__":
    unittest.main()
