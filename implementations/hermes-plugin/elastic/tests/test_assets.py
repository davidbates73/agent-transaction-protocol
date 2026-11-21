"""Static checks for the Elastic stack assets: template, roles, compose/.env, Kibana objects, docs."""

from __future__ import annotations

import filecmp
import glob
import json
import os
import re
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ELASTIC = os.path.dirname(HERE)
PLUGIN = os.path.dirname(ELASTIC)
IMPL = os.path.dirname(PLUGIN)
VECTORS = os.path.join(PLUGIN, "vectors")
sys.path.insert(0, os.path.join(ELASTIC, "exporter"))
sys.path.insert(0, os.path.join(ELASTIC, "kibana"))

import hermes_atp_export as hx  # noqa: E402
import atp_log  # noqa: E402
import build_saved_objects  # noqa: E402
import build_lifecycle_dashboard as lifecycle  # noqa: E402

def slurp(path, mode="r", **kw):
    with open(path, mode, **kw) as f:
        return f.read()



def read(*p):
    with open(os.path.join(ELASTIC, *p), encoding="utf-8") as f:
        return f.read()


def mapped_paths(props, pre=""):
    out = {}
    for k, v in props.items():
        out[pre + k] = v
        if "properties" in v:
            out.update(mapped_paths(v["properties"], pre + k + "."))
    return out


def flat(doc, pre=""):
    out = set()
    for k, v in doc.items():
        if pre + k == "body":
            out.add("body")
        elif isinstance(v, dict):
            out |= flat(v, pre + k + ".")
        elif isinstance(v, list) and v and isinstance(v[0], dict):
            for x in v:
                out |= flat(x, pre + k + ".")
        else:
            out.add(pre + k)
    return out


TEMPLATE = json.loads(read("setup", "index-template.json"))
MAPPING = TEMPLATE["template"]["mappings"]
FIELDS = mapped_paths(MAPPING["properties"])


class VendoredVerifierTests(unittest.TestCase):
    def test_atp_log_is_byte_identical(self):
        self.assertTrue(filecmp.cmp(os.path.join(PLUGIN, "atp_log.py"), os.path.join(IMPL, "python", "atp_log.py"),
                                    shallow=False),
                        "hermes-plugin/atp_log.py must be a byte-identical copy of implementations/python/atp_log.py")

    def test_exporter_uses_the_vendored_copies(self):
        self.assertEqual(os.path.dirname(os.path.abspath(atp_log.__file__)), PLUGIN)
        self.assertEqual(os.path.dirname(os.path.abspath(hx.atp.__file__)), PLUGIN)


class TemplateTests(unittest.TestCase):
    def all_docs(self):
        docs = []
        pub = atp_log.load_public_key(slurp(os.path.join(VECTORS, "TEST-ONLY-key.pub")))
        for path in glob.glob(os.path.join(VECTORS, "cases", "*.jsonl")) + [os.path.join(VECTORS, "sample-log.jsonl")]:
            chain = {}
            for i, raw in enumerate(slurp(path, "rb").split(b"\n")[:-1]):
                checked = atp_log.check_line(raw.decode("utf-8", "replace"), pub)
                cp = atp_log.chain_step(chain, checked["node"], None) if checked["scope"] else []
                docs.append(hx.build_document(raw, checked, cp, index_prefix="hermes-atp", source=path, offset=0,
                                              line=i + 1, ship_bodies=True, prefix_sha256="0" * 64))
        return docs

    def test_settings(self):
        self.assertEqual(TEMPLATE["index_patterns"], ["hermes-atp-records-*"])
        s = TEMPLATE["template"]["settings"]
        self.assertEqual((s["number_of_shards"], s["number_of_replicas"]), (1, 0))
        self.assertIs(MAPPING["dynamic"], False)

    def test_explicit_mappings_for_key_fields(self):
        want = {"session_id": "keyword", "parent_session_id": "keyword", "turn_id": "keyword", "event": "keyword",
                "model": "keyword", "tool_name": "keyword", "duration_ms": "long", "api_duration_ms": "long",
                "tokens.total": "long", "tokens.prompt": "long", "tokens.completion": "long",
                "verify.status": "keyword", "verify.valid": "boolean", "@timestamp": "date", "seq": "long",
                "is_session_start": "boolean", "node_id": "keyword"}
        for f, t in want.items():
            self.assertEqual(FIELDS[f]["type"], t, f)

    def test_bodies_are_never_indexed(self):
        self.assertEqual(FIELDS["body"], {"type": "object", "enabled": False})

    def test_every_emitted_field_is_mapped(self):
        seen = set()
        for index, _id, doc in self.all_docs():
            self.assertTrue(index.startswith("hermes-atp-records-"))
            seen |= flat(doc)
        missing = sorted(f for f in seen if f not in FIELDS)
        self.assertEqual(missing, [])
        self.assertIn("body", seen, "vectors include a bodies-logged record")

    def test_every_allowlisted_field_is_mapped(self):
        for dst in hx.HERMES_FIELDS.values():
            if dst:
                self.assertIn(dst, FIELDS)
        for dst in hx.USAGE_FIELDS.values():
            self.assertIn("tokens." + dst, FIELDS)

    def test_recorder_fields_are_all_handled(self):
        """Every hermes.* key the real recorder emits (driven through the fake-Hermes harness in
        every mode) is either shipped by the exporter or deliberately dropped."""
        import tempfile
        sys.path.insert(0, os.path.join(PLUGIN, "tests"))
        import support as S
        emitted, top = set(), set()
        for kw, env_extra in (({}, {}), ({"real_subagents": True}, {}), ({"crash": True}, {}),
                              ({}, {"HERMES_ATP_LOG_BODIES": "true"})):
            with tempfile.TemporaryDirectory() as d:
                S.run_fake_session({"HERMES_ATP_HOME": d, **env_extra}, clock=S.TickClock(), **kw)
                for line in slurp(os.path.join(d, "records.jsonl"), encoding="utf-8").splitlines():
                    node = json.loads(line)
                    top |= set(node)
                    emitted |= set(node.get("hermes", {}))
        self.assertGreater(len(emitted), 30, "harness produced a realistic spread of fields")
        dropped = {"sessionId",                      # == scope, shipped as session_id
                   "usage", "inputBody", "outputBody"}  # tokens.* / body.* (gated)
        handled = {k for k, v in hx.HERMES_FIELDS.items() if v} | dropped
        self.assertEqual(sorted(emitted - handled), [], "new recorder field: map it or drop it explicitly")
        self.assertEqual(sorted(top - {"nodeId", "timestamp", "scope", "issuer", "agent", "action", "parents",
                                       "profile", "hermes", "signature"}), [])


class RoleTests(unittest.TestCase):
    def test_exporter_role_is_write_only(self):
        r = json.loads(read("setup", "role-hermes-exporter.json"))
        self.assertEqual(r["cluster"], [])
        self.assertEqual(r["run_as"], [])
        self.assertEqual(len(r["indices"]), 1)
        self.assertEqual(r["indices"][0]["names"], ["hermes-atp-records-*"])
        self.assertEqual(sorted(r["indices"][0]["privileges"]), ["auto_configure", "create_doc"])
        self.assertFalse(r["indices"][0]["allow_restricted_indices"])

    def test_viewer_role_is_read_only(self):
        r = json.loads(read("setup", "role-hermes-viewer.kibana.json"))
        es = r["elasticsearch"]
        self.assertEqual(es["cluster"], [])
        self.assertEqual([i["names"] for i in es["indices"]], [["hermes-atp-records-*"]])
        self.assertEqual(sorted(es["indices"][0]["privileges"]), ["read", "view_index_metadata"])
        (k,) = r["kibana"]
        self.assertEqual(k["base"], [])
        self.assertEqual(set(k["feature"]), {"dashboard_v2", "discover_v2", "visualize_v2"})
        self.assertTrue(all(v == ["read"] for v in k["feature"].values()))


class ComposeTests(unittest.TestCase):
    compose = read("docker-compose.yml")
    env_example = read(".env.example")

    def test_no_logstash_no_paid_features(self):
        low = self.compose.lower()
        self.assertNotIn("logstash", low.replace("no logstash", ""))
        self.assertIn("xpack.license.self_generated.type: basic", self.compose)
        self.assertIn('xpack.ml.enabled: "false"', self.compose)
        self.assertIn('xpack.watcher.enabled: "false"', self.compose)
        self.assertNotIn("trial", low.replace("needs a trial", ""))
        for paid in ("saml", "oidc", "kerberos", "xpack.security.authc.realms", "authc.anonymous"):
            self.assertNotIn(paid, low)

    def test_security_and_tls_on(self):
        for s in ('xpack.security.enabled: "true"', 'xpack.security.http.ssl.enabled: "true"',
                  'xpack.security.transport.ssl.enabled: "true"', 'SERVER_SSL_ENABLED: "true"'):
            self.assertIn(s, self.compose)

    def test_memory_and_persistence(self):
        self.assertIn("-Xms${ES_HEAP:-1g} -Xmx${ES_HEAP:-1g}", self.compose)
        self.assertIn("mem_limit: ${ES_MEM_LIMIT:-2g}", self.compose)
        self.assertIn("esdata:/usr/share/elasticsearch/data", self.compose)

    def test_every_variable_is_documented_and_secrets_blank(self):
        used = set(re.findall(r"\$\{([A-Z_][A-Z0-9_]*)", self.compose))
        documented = dict(re.findall(r"^([A-Z_][A-Z0-9_]*)=(.*)$", self.env_example, re.M))
        self.assertEqual(sorted(used - set(documented)), [])
        for secret in ("ELASTIC_PASSWORD", "KIBANA_PASSWORD", "HERMES_EXPORTER_PASSWORD", "HERMES_VIEWER_PASSWORD",
                       "KIBANA_ENCRYPTION_KEY", "ES_BIND_ADDR"):
            self.assertEqual(documented[secret], "", f"{secret} must be blank in .env.example")
            self.assertIn("${%s:?" % secret, self.compose, f"{secret} must be required")

    def test_single_stack_version_pin(self):
        images = re.findall(r"^\s*(?:x-es-image: &es-image|image:)\s*(.+?)\s*$", self.compose, re.M)
        images = [i for i in images if not i.startswith("*")]
        self.assertEqual(sorted(i.split(":", 1)[0] for i in images),
                         ["docker.elastic.co/elasticsearch/elasticsearch", "docker.elastic.co/kibana/kibana"])
        for img in images:
            self.assertTrue(img.endswith(":${STACK_VERSION:?set STACK_VERSION in .env}"), img)
        self.assertEqual(self.compose.count("image: *es-image"), 2, "setup and es01 share the ES image anchor")
        self.assertNotIn("9.5.6", self.compose, "the version lives only in .env")
        self.assertEqual(re.findall(r"^STACK_VERSION=(.*)$", self.env_example, re.M), ["9.5.6"])

    def test_ports(self):
        self.assertIn('"${ES_BIND_ADDR:?set ES_BIND_ADDR in .env}:9200:9200"', self.compose)
        self.assertIn('"${KIBANA_BIND_ADDR:-127.0.0.1}:5601:5601"', self.compose)
        self.assertNotIn("9300:9300", self.compose)

    def test_env_and_certs_gitignored(self):
        gi = read(".gitignore").split()
        for p in (".env", "certs/", "secrets/"):
            self.assertIn(p, gi)
        r = subprocess.run(["git", "check-ignore", "-q", os.path.join(ELASTIC, ".env")], cwd=ELASTIC)
        if r.returncode != 128:  # 128: not a git checkout
            self.assertEqual(r.returncode, 0, ".env must be ignored by git")
        tracked = subprocess.run(["git", "ls-files", ELASTIC], cwd=ELASTIC, capture_output=True, text=True).stdout
        self.assertNotRegex(tracked, r"(^|/)\.env$|/certs/|\.key$|\.p12$", )

    def test_setup_script_never_prints_secrets(self):
        sh = read("setup", "setup.sh")
        self.assertNotRegex(sh, r"set -[a-z]*x")
        for var in ("ELASTIC_PASSWORD", "KIBANA_PASSWORD", "HERMES_EXPORTER_PASSWORD", "HERMES_VIEWER_PASSWORD"):
            for line in sh.splitlines():
                if "$" + var in line or "${" + var in line:
                    self.assertTrue(re.search(r"json_str|local pw=|\[ -n|printf 'user", line), line)
        self.assertNotIn(" -u ", sh, "credentials must not go on curl's command line")


class KibanaObjectsTests(unittest.TestCase):
    ndjson = read("kibana", "hermes-atp-dashboard.ndjson")
    objs = [json.loads(l) for l in ndjson.splitlines()]

    def test_generated_file_is_up_to_date(self):
        self.assertEqual(self.ndjson, build_saved_objects.build(),
                         "run: python3 kibana/build_saved_objects.py")

    def test_references_resolve(self):
        ids = {(o["type"], o["id"]) for o in self.objs}
        for o in self.objs:
            for r in o["references"]:
                self.assertIn((r["type"], r["id"]), ids, f"{o['id']} -> {r}")
        (dash,) = [o for o in self.objs if o["type"] == "dashboard"]
        panels = json.loads(dash["attributes"]["panelsJSON"])
        names = {r["name"] for r in dash["references"]}
        for p in panels:
            self.assertIn(f"{p['panelIndex']}:{p['panelRefName']}", names)
        self.assertEqual(len(panels), len([o for o in self.objs if o["type"] == "lens"]))

    def test_data_view(self):
        (dv,) = [o for o in self.objs if o["type"] == "index-pattern"]
        self.assertEqual(dv["attributes"]["title"], TEMPLATE["index_patterns"][0])
        self.assertEqual(dv["attributes"]["timeFieldName"], "@timestamp")

    def test_lens_fields_exist_and_are_aggregatable(self):
        for o in self.objs:
            if o["type"] != "lens":
                continue
            st = o["attributes"]["state"]
            for layer in st["datasourceStates"]["formBased"]["layers"].values():
                for col in layer["columns"].values():
                    f = col["sourceField"]
                    if f == "___records___":
                        continue
                    self.assertIn(f, FIELDS, o["id"])
                    self.assertIn(FIELDS[f]["type"], ("keyword", "long", "date", "boolean"), (o["id"], f))
            for f in re.findall(r"([A-Za-z_@][\w.]*)\s*:", st["query"]["query"]):
                self.assertIn(f, FIELDS, (o["id"], f))

    def test_required_panels(self):
        titles = " | ".join(o["attributes"]["title"].lower() for o in self.objs if o["type"] == "lens")
        for want in ("sessions over time", "model calls", "latency", "tool calls by name", "subagent links",
                     "verify failures", "anomalies", "anomaly records by kind, reason, session and line"):
            self.assertIn(want, titles)

    def test_anomaly_panels(self):
        by_id = {o["id"]: o for o in self.objs}
        metric = by_id["hermes-atp-anomalies-count"]["attributes"]
        table = by_id["hermes-atp-anomalies"]["attributes"]
        self.assertEqual(metric["visualizationType"], "lnsMetric")
        self.assertEqual(table["visualizationType"], "lnsDatatable")
        for a in (metric, table):
            self.assertEqual(a["state"]["query"]["query"], "anomaly : true")
        cols = table["state"]["datasourceStates"]["formBased"]["layers"]["layer1"]["columns"]
        self.assertEqual({c["sourceField"] for c in cols.values()},
                         {"verify.errors.kind", "verify.errors.reason", "session_id", "source.path", "source.line",
                          "___records___"})
        self.assertTrue(all(c["params"]["missingBucket"] for c in cols.values() if c["operationType"] == "terms"),
                        "unparseable lines (no session_id) must still get a row")
        self.assertEqual(FIELDS["anomaly"]["type"], "boolean")
        self.assertEqual(FIELDS["verify.errors.reason"]["type"], "keyword")

    def test_pinned_migration_versions(self):
        for o in self.objs:
            self.assertEqual(o["coreMigrationVersion"], "8.8.0")
            self.assertIn("typeMigrationVersion", o)


def es_fields(obj, out=None):
    """Every ES field name an aggregation / sort / _source / term query in a Vega body refers to."""
    out = set() if out is None else out
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == "field" and isinstance(v, str):
                out.add(v)
            elif k in ("term", "range") and isinstance(v, dict):
                out |= set(v)
            elif k == "sort" and isinstance(v, list):
                for item in v:
                    out |= set(item) if isinstance(item, dict) else {item}
            elif k in ("_source", "includes") and isinstance(v, list):
                out |= set(v)
            else:
                es_fields(v, out)
    elif isinstance(obj, list):
        for x in obj:
            es_fields(x, out)
    return out


def es_urls(spec):
    data = spec["data"] if isinstance(spec["data"], list) else [spec["data"]]
    return [d["url"] for d in data if isinstance(d.get("url"), dict)]


class LifecycleDashboardTests(unittest.TestCase):
    """The "Hermes ATP request lifecycle" dashboard (kibana/hermes-atp-lifecycle.ndjson)."""
    ndjson = read("kibana", "hermes-atp-lifecycle.ndjson")
    objs = [json.loads(l) for l in ndjson.splitlines()]
    by_id = {o["id"]: o for o in objs}
    (dash,) = [o for o in objs if o["type"] == "dashboard"]
    panels = json.loads(dash["attributes"]["panelsJSON"])

    def vega(self, oid):
        vis = json.loads(self.by_id[oid]["attributes"]["visState"])
        self.assertEqual(vis["type"], "vega")
        return json.loads(vis["params"]["spec"])

    def test_generated_file_is_up_to_date(self):
        self.assertEqual(self.ndjson, lifecycle.build(), "run: python3 kibana/build_lifecycle_dashboard.py")

    def test_stable_unique_ids(self):
        ids = [(o["type"], o["id"]) for o in self.objs]
        self.assertEqual(len(ids), len(set(ids)))
        for _, i in ids:
            self.assertTrue(i.startswith("hermes-atp-"), i)
        self.assertEqual(self.dash["id"], "hermes-atp-lifecycle")
        self.assertEqual(self.dash["attributes"]["title"], "Hermes ATP request lifecycle")
        self.assertEqual(lifecycle.build(), lifecycle.build(), "deterministic output")
        overview = [json.loads(l) for l in read("kibana", "hermes-atp-dashboard.ndjson").splitlines()]
        clash = {(o["type"], o["id"]) for o in overview} & set(ids)
        self.assertEqual(clash, {("index-pattern", "hermes-atp-records")}, "only the shared data view overlaps")
        (dv_a,) = [o for o in overview if o["type"] == "index-pattern"]
        self.assertEqual(dv_a, self.by_id["hermes-atp-records"], "same data view object in both files")

    def test_references_resolve_within_the_file(self):
        ids = {(o["type"], o["id"]) for o in self.objs}
        for o in self.objs:
            for r in o["references"]:
                self.assertIn((r["type"], r["id"]), ids, f"{o['id']} -> {r}")
        names = {r["name"] for r in self.dash["references"]}
        for p in self.panels:
            self.assertIn(f"{p['panelIndex']}:{p['panelRefName']}", names)
        self.assertEqual(len(self.panels), len(self.objs) - 2, "every object but the data view and dashboard is a panel")

    def test_panel_types_and_layout(self):
        kinds = sorted(p["type"] for p in self.panels)
        self.assertEqual(kinds, ["lens"] * 7 + ["search"] + ["visualization"] * 2)
        for p in self.panels:
            g = p["gridData"]
            self.assertLessEqual(g["x"] + g["w"], 48, p)
        cells = set()
        for p in self.panels:
            g = p["gridData"]
            for x in range(g["x"], g["x"] + g["w"]):
                for y in range(g["y"], g["y"] + g["h"]):
                    self.assertNotIn((x, y), cells, f"panels overlap at {(x, y)}")
                    cells.add((x, y))

    def test_no_paid_or_external_features(self):
        self.assertEqual({o["type"] for o in self.objs}, {"index-pattern", "lens", "visualization", "search", "dashboard"})
        for o in self.objs:
            if o["type"] == "visualization":
                spec = self.vega(o["id"])
                self.assertIn(spec["$schema"], ("https://vega.github.io/schema/vega/v5.json",
                                                "https://vega.github.io/schema/vega-lite/v5.json"))
                text = json.dumps({k: v for k, v in spec.items() if k != "$schema"})
                self.assertNotRegex(text, r"https?://", "no external data or images")
        text = self.ndjson.lower()
        for paid in ("esql", "\"ml\"", "anomaly_detection", "alert", "reporting", "canvas"):
            self.assertNotIn(paid, text.replace("alerting", ""))

    def test_vega_queries_follow_the_dashboard(self):
        for oid in ("hermes-atp-lc-session-tree", "hermes-atp-lc-waterfall"):
            urls = es_urls(self.vega(oid))
            self.assertTrue(urls, oid)
            for u in urls:
                if u is lifecycle.LINKS_URL or u == lifecycle.LINKS_URL:
                    continue  # label links: time range only, checked in test_session_labels
                self.assertIs(u["%context%"], True, f"{oid}: dashboard query/filters/controls must apply")
                self.assertEqual(u["%timefield%"], "@timestamp", f"{oid}: dashboard time range must apply")
                self.assertEqual(u["index"], TEMPLATE["index_patterns"][0])
                self.assertNotIn("query", u["body"], "a body query would replace the dashboard context")
                for f in es_fields(u["body"]):
                    self.assertIn(f, FIELDS, (oid, f))
                    self.assertIn(FIELDS[f]["type"], ("keyword", "long", "date", "boolean"), (oid, f))

    def test_session_tree_uses_the_recorder_links(self):
        spec = self.vega("hermes-atp-lc-session-tree")
        self.assertEqual(spec["$schema"], "https://vega.github.io/schema/vega/v5.json")
        (u,) = [x for x in es_urls(spec) if x != lifecycle.LINKS_URL]
        fields = es_fields(u["body"])
        for f in ("session_id", "parent_session_id", "parent_turn_id", "parent_subagent_id", "is_session_start",
                  "tokens.total", "api_duration_ms", "duration_ms", "model"):
            self.assertIn(f, fields)
        kinds = [t["type"] for d in spec["data"] for t in d.get("transform", [])]
        for k in ("stratify", "tree", "treelinks", "linkpath", "lookup"):
            self.assertIn(k, kinds)
        names = [d["name"] for d in spec["data"]]
        self.assertEqual(len(names), len(set(names)))
        self.assertNotIn("root", names, "'root' is reserved in Vega")
        marks = {m["type"] for m in spec["marks"]}
        self.assertTrue({"symbol", "path", "text"} <= marks)

    def test_waterfall(self):
        spec = self.vega("hermes-atp-lc-waterfall")
        self.assertEqual(spec["$schema"], "https://vega.github.io/schema/vega-lite/v5.json")
        (u,) = es_urls(spec)
        self.assertNotIn("format", spec["data"], "whole response: hits plus the label-links aggregation")
        flat_ = [t for t in spec["transform"] if "flatten" in t]
        self.assertEqual(flat_, [{"flatten": ["hits.hits"], "as": ["hit"]}])
        src = set(u["body"]["_source"])
        for f in ("@timestamp", "session_id", "seq", "event", "duration_ms", "api_duration_ms", "tool_name", "model",
                  "status", "tokens.total", "tool_call_id", "api_request_id"):
            self.assertIn(f, src)
        layers = [l["mark"]["type"] for l in spec["layer"]]
        self.assertEqual(layers, ["bar", "point"])
        self.assertEqual(spec["layer"][0]["encoding"]["x2"], {"field": "end"})
        tips = {t["title"] for t in spec["encoding"]["tooltip"]}
        self.assertTrue({"tool / model", "status", "tokens", "duration ms"} <= tips)
        self.assertEqual(set(spec["encoding"]["color"]["scale"]["domain"]) - {"started, no completion"},
                         {"post_api_request", "post_tool_call", "api_request_error"})

    def test_session_labels(self):
        tree = self.vega("hermes-atp-lc-session-tree")
        wf = self.vega("hermes-atp-lc-waterfall")
        tree_text = json.dumps(tree, ensure_ascii=False)
        wf_text = json.dumps(wf, ensure_ascii=False)
        self.assertIn(json.dumps(lifecycle.label_expr("datum.id", "datum.level"), ensure_ascii=False), tree_text)
        self.assertIn(json.dumps(lifecycle.label_expr("datum.session", "datum.level"), ensure_ascii=False)[1:-1],
                      wf_text)
        (label_mark,) = [m for m in tree["marks"] if m["type"] == "text" and m.get("from")]
        self.assertTrue(label_mark["encode"]["update"]["text"]["signal"].startswith("datum.label"))
        for spec_text in (tree_text, wf_text):
            self.assertNotIn("slice(datum.id, -14)", spec_text)
            self.assertNotIn("slice(datum.session, -12)", spec_text)
        # the full id stays in the tooltips
        self.assertIn("'session': datum.id", tree_text)
        self.assertIn({"field": "session", "title": "session"}, wf["encoding"]["tooltip"])
        # links for the labels: subagent first records in the time range, NOT the dashboard filters
        links = [d for d in tree["data"] if d["name"] == "linkdocs"][0]["url"]
        self.assertEqual(links, lifecycle.LINKS_URL)
        self.assertNotIn("%context%", links)
        self.assertIn({"range": {"@timestamp": {"%timefilter%": True}}}, links["body"]["query"]["bool"]["filter"])
        agg = wf["data"]["url"]["body"]["aggs"]["links"]
        self.assertEqual(agg["global"], {})
        self.assertEqual(agg["aggs"]["f"]["filter"], lifecycle.LINKS_BODY["query"])
        # role levels: same number of hops in both specs
        self.assertEqual([t["as"] for t in wf["transform"] if re.fullmatch(r"p\d", str(t.get("as", "")))],
                         ["p1", "p2", "p3", "p4"])
        self.assertEqual(sum(1 for d in tree["data"] for t in d.get("transform", []) if t["type"] == "lookup"
                             and t["from"] == "linkdocs"), lifecycle.MAX_LABEL_DEPTH)

    def test_fixture_sessions_get_distinct_labels(self):
        """Real-recorder session ids (fake-Hermes harness + a real-Hermes run's shapes)."""
        parents = {"sess-child": "sess-main", "sess-grandchild": "sess-child",
                   "20261120_203515_d7e34b": "req-demo-1", "20261120_203515_4b0023": "req-demo-1",
                   "20261120_203600_aa11bb": "20261120_203515_d7e34b",
                   "20261120_203601_cc22dd": "20261120_203600_aa11bb",
                   "20261120_203602_ee33ff": "20261120_203601_cc22dd"}
        sessions = sorted(set(parents) | set(parents.values()) | {"sess-crash", "req-demo-2"})
        labels = {sid: lifecycle.session_label(sid, parents) for sid in sessions}
        self.assertEqual(len(set(labels.values())), len(labels), labels)
        self.assertEqual(labels["req-demo-1"], "parent · …demo-1")
        self.assertEqual(labels["20261120_203515_d7e34b"], "sub · …d7e34b")
        self.assertEqual(labels["20261120_203515_4b0023"], "sub · …4b0023")
        self.assertEqual(labels["sess-grandchild"], "sub² · …dchild")
        self.assertEqual(labels["20261120_203600_aa11bb"], "sub² · …aa11bb")
        self.assertEqual(labels["20261120_203601_cc22dd"], "sub³ · …cc22dd")
        self.assertEqual(labels["20261120_203602_ee33ff"], "sub⁴⁺ · …ee33ff")
        self.assertEqual(lifecycle.session_label("abc", {}), "parent · abc")
        cyc = lifecycle.session_label("a-loop-1", {"a-loop-1": "a-loop-2", "a-loop-2": "a-loop-1"})
        self.assertEqual(cyc, "sub⁴⁺ · …loop-1", "a link cycle is capped, never loops")

    def test_step_table(self):
        a = self.by_id["hermes-atp-lifecycle-steps"]["attributes"]
        (tab,) = a["tabs"]
        self.assertEqual(tab["attributes"]["sort"], [["session_id", "asc"], ["seq", "asc"]])
        self.assertEqual(tab["attributes"]["columns"][:2], ["session_id", "seq"])
        for c in ("event", "tool_name", "model", "status", "duration_ms", "tokens.total", "verify.status"):
            self.assertIn(c, tab["attributes"]["columns"])
        for c in tab["attributes"]["columns"] + ["session_id"]:
            self.assertIn(c, FIELDS)
        ss = json.loads(tab["attributes"]["kibanaSavedObjectMeta"]["searchSourceJSON"])
        self.assertEqual(ss["indexRefName"], "kibanaSavedObjectMeta.searchSourceJSON.index")

    def test_rollup_tiles(self):
        tiles = [o for o in self.objs if o["type"] == "lens"]
        self.assertEqual([t["attributes"]["title"] for t in tiles],
                         ["Tokens", "Elapsed (first → last record)", "Model time", "Tool time", "Failed steps",
                          "Retries", "Verify pass rate"])
        for t in tiles:
            st = t["attributes"]["state"]
            self.assertEqual(t["attributes"]["visualizationType"], "lnsMetric")
            cols = st["datasourceStates"]["formBased"]["layers"][lifecycle.LAYER]["columns"]
            self.assertIn(st["visualization"]["metricAccessor"], cols)
            for cid, col in cols.items():
                for ref in col.get("references", []):
                    self.assertIn(ref, cols, (t["id"], cid))
                if col.get("sourceField") not in (None, "___records___"):
                    self.assertIn(col["sourceField"], FIELDS, t["id"])
                if "filter" in col:
                    for f in re.findall(r"([A-Za-z_@][\w.]*)\s*[:>]", col["filter"]["query"]):
                        self.assertIn(f, FIELDS, (t["id"], f))
            self.assertEqual(set(st["datasourceStates"]["formBased"]["layers"][lifecycle.LAYER]["columnOrder"]), set(cols))
        verify = self.by_id["hermes-atp-lc-verify"]["attributes"]["state"]["datasourceStates"]["formBased"]
        self.assertEqual(verify["layers"][lifecycle.LAYER]["columns"]["v"]["params"]["formula"],
                         "count(kql='verify.valid : true') / count()")

    def test_controls(self):
        group = self.dash["attributes"]["controlGroupInput"]
        controls = json.loads(group["panelsJSON"])
        self.assertEqual([c["explicitInput"]["fieldName"] for c in sorted(controls.values(), key=lambda c: c["order"])],
                         ["session_id", "turn_id", "event"])
        refs = {r["name"]: r for r in self.dash["references"]}
        for cid, c in controls.items():
            self.assertEqual(c["type"], "optionsListControl")
            self.assertEqual(c["explicitInput"]["id"], cid)
            self.assertEqual(FIELDS[c["explicitInput"]["fieldName"]]["type"], "keyword")
            r = refs[f"controlGroup_{cid}:optionsListDataView"]
            self.assertEqual((r["type"], r["id"]), ("index-pattern", "hermes-atp-records"))
        ignore = json.loads(group["ignoreParentSettingsJSON"])
        self.assertFalse(any(ignore.values()), "controls follow the dashboard query, filters and time")

    def test_viewer_role_can_open_every_panel_type(self):
        need = {"dashboard": "dashboard_v2", "lens": "visualize_v2", "visualization": "visualize_v2",
                "search": "discover_v2"}
        role = json.loads(read("setup", "role-hermes-viewer.kibana.json"))
        (k,) = role["kibana"]
        for o in self.objs:
            if o["type"] in need:
                self.assertEqual(k["feature"].get(need[o["type"]]), ["read"], o["type"])

    def test_setup_imports_both_files(self):
        sh = read("setup", "setup.sh")
        self.assertIn("for nd in hermes-atp-dashboard.ndjson hermes-atp-lifecycle.ndjson; do", sh)
        self.assertIn("_import?overwrite=true", sh)

    def test_panels_match_real_record_shapes(self):
        """Drive the real recorder (fake-Hermes harness, v0.23.1-shaped subagents) through the
        exporter's build_document and check what the panels assume about the documents."""
        import tempfile
        sys.path.insert(0, os.path.join(PLUGIN, "tests"))
        import support as S
        with tempfile.TemporaryDirectory() as d:
            S.run_fake_session({"HERMES_ATP_HOME": d}, clock=S.TickClock(), real_subagents=True, crash=True,
                               secrets=False)
            path = os.path.join(d, "records.jsonl")
            pub = atp_log.load_public_key(slurp(os.path.join(d, "key.pub")))
            chain, docs = {}, []
            for i, raw in enumerate(slurp(path, "rb").split(b"\n")[:-1]):
                checked = atp_log.check_line(raw.decode("utf-8"), pub)
                cp = atp_log.chain_step(chain, checked["node"], None) if checked["scope"] else []
                docs.append(hx.build_document(raw, checked, cp, index_prefix="hermes-atp", source=path, offset=0,
                                              line=i + 1, ship_bodies=False, prefix_sha256="0" * 64)[2])
        events = {doc["event"] for doc in docs}
        self.assertTrue({"pre_api_request", "post_api_request", "pre_tool_call", "post_tool_call"} <= events)
        wf = self.vega("hermes-atp-lc-waterfall")
        self.assertLessEqual(set(wf["encoding"]["color"]["scale"]["domain"]) - {"started, no completion"},
                             set(build_document_events()))
        self.assertIn("api_request_error", build_document_events())
        by_session = {}
        for doc in docs:
            by_session.setdefault(doc["session_id"], []).append(doc)
        children = {s: ds for s, ds in by_session.items() if any("parent_session_id" in x for x in ds)}
        self.assertEqual(set(children), {"sess-child", "sess-grandchild"})
        for s, ds in children.items():
            first = min(ds, key=lambda x: x["seq"])
            self.assertTrue(first["is_session_start"])
            self.assertIn("parent_session_id", first, "the tree reads the link from the seq-0 record")
            self.assertIn(first["parent_session_id"], by_session, "parent is a recorded session")
        for doc in docs:
            if doc["event"] == "post_tool_call":
                self.assertIn("duration_ms", doc)
                self.assertIn("tool_call_id", doc)
            if doc["event"] in ("post_api_request", "api_request_error"):
                self.assertIn("api_duration_ms", doc)
                self.assertIn("api_request_id", doc)
            if doc["event"] == "post_api_request":
                self.assertIn("total", doc["tokens"])
            self.assertIn("valid", doc["verify"])
        crash = [x["event"] for x in by_session["sess-crash"]]
        self.assertEqual(crash[-1], "pre_tool_call", "an unfinished step the waterfall marks")


def build_document_events():
    """The hook events the recorder turns into records (keys of recorder.ACTION_TYPE)."""
    src = slurp(os.path.join(PLUGIN, "recorder.py"), encoding="utf-8")
    start = src.index("ACTION_TYPE = {")
    return re.findall(r'"([a-z_]+)":', src[start:src.index("}", start)])


class DocsTests(unittest.TestCase):
    readme = read("README.md")

    def test_no_mac_or_launchd(self):
        for path in glob.glob(os.path.join(ELASTIC, "**", "*"), recursive=True):
            if os.path.isfile(path) and not path.endswith((".pyc", ".png")) and \
                    os.path.abspath(path) != os.path.abspath(__file__):
                text = slurp(path, encoding="utf-8", errors="replace")
                for bad in ("launchd", "launchctl", "macOS", "MacBook", " Mac ", "Mac's", ".plist"):
                    self.assertNotIn(bad, text, f"{path}: {bad}")

    def test_network_section(self):
        net = self.readme[self.readme.index("## Network"):]
        for want in ("OPNsense", "9200", "5601", "<NUC_IP>", "<SOLEBAY_IP>", "ufw", "iptables", "alias",
                     "stateful", "Flynn"):
            self.assertIn(want, net)

    def test_david_applies_the_rules_not_the_agent(self):
        who = self.readme[self.readme.index("### Who applies these rules"):self.readme.index("## Tests")]
        self.assertIn("David reviews and applies these rules himself", who)
        for rule in ("OPNsense", "DOCKER-USER", "ufw"):
            self.assertIn(rule, who)
        self.assertIn("Flynn", who)
        self.assertIn("must not write, apply or change", who)

    def test_backlog_priority_order(self):
        backlog = self.readme[self.readme.index("## Backlog (priority order)"):self.readme.index("## Limitations")]
        items = re.findall(r"^(\d+)\. \*\*([^*]+)\*\*", backlog, re.M)
        self.assertEqual([t.split(":")[0].rstrip(".") for _, t in items],
                         ["Index retention", "Scheduled verify", "Snapshots", "Rotated files in copy mode"])
        self.assertIn("NEXT", items[0][1])
        self.assertIn("shared", backlog.split("\n2.")[0])

    def test_no_real_ip_addresses(self):
        for path in [os.path.join(ELASTIC, "README.md"), os.path.join(ELASTIC, ".env.example")] + \
                glob.glob(os.path.join(ELASTIC, "examples", "*")):
            text = slurp(path, encoding="utf-8")
            ips = set(re.findall(r"\b\d{1,3}(?:\.\d{1,3}){3}\b", text)) - {"127.0.0.1", "0.0.0.0"}
            self.assertEqual(ips, set(), path)

    def test_push_is_documented_as_default(self):
        self.assertIn("push", self.readme.lower())
        self.assertRegex(self.readme, r"(?i)push mode \(default\)|default.{0,40}push")


if __name__ == "__main__":
    unittest.main()
