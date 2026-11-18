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
