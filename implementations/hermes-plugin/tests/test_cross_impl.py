"""Cross-implementation verify: Node (bin/atp.js) and Python (atp_cli.py) must print
byte-identical --json verdicts and exit codes on the same logs; and both must reproduce
the checked-in vectors exactly. Requires `node` (18+) on PATH or $NODE."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support as S  # noqa: E402
import atp_log  # noqa: E402  (implementations/python, via support)

NODE = os.environ.get("NODE") or shutil.which("node")


def run(cli, log, pub, *extra):
    cmd = ([NODE, S.NODE_CLI] if cli == "node" else [sys.executable, S.PY_CLI]) + ["verify", log, "--pub", pub, *extra]
    p = subprocess.run(cmd, capture_output=True)
    return p.returncode, p.stdout, p.stderr


@unittest.skipUnless(NODE, "node not found")
class CrossImplTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="atp-xi-")
        env = {"HERMES_ATP_HOME": cls.dir}
        S.run_fake_session(env, crash=True)
        cls.pub = os.path.join(cls.dir, "key.pub")
        with open(os.path.join(cls.dir, "records.jsonl"), "rb") as f:
            cls.data = f.read()
        n = len(S.lines_of(cls.data))
        cls.cases = {
            "valid": cls.data,
            "edit-byte": S.edit_one_byte(cls.data, 4),
            "delete-line": S.delete_line(cls.data, 2),
            "swap-lines": S.swap_lines(cls.data, 12, 13),
            "swap-first-two": S.swap_lines(cls.data, 0, 1),
            "duplicate": S.duplicate_line(cls.data, 3),
            "delete-first": S.delete_line(cls.data, 0),
            "truncate": S.delete_line(cls.data, n - 1),
            "blank": S.insert_line(cls.data, 2, b""),
            "garbage": S.insert_line(cls.data, 2, b"{not json"),
            "multi": S.swap_lines(S.delete_line(S.edit_one_byte(cls.data, 1), 5), 10, 11),
            "empty": b"",
            "newline-only": b"\n",
            "crash-tail-deleted": S.delete_line(cls.data, n - 1),
        }
        bdir = os.path.join(cls.dir, "bodies")
        S.run_fake_session({"HERMES_ATP_HOME": bdir, "HERMES_ATP_KEY": os.path.join(cls.dir, "key"),
                            "HERMES_ATP_PUB": cls.pub, "HERMES_ATP_LOG_BODIES": "true"}, secrets=True)
        with open(os.path.join(bdir, "records.jsonl"), "rb") as f:
            bodies = f.read()
        with open(os.path.join(cls.dir, "key")) as f:
            seed_hex = json.load(f)["seedHex"]
        nb = len(S.lines_of(bodies))

        def alter_out(node):
            node["hermes"]["outputBody"] = {"altered": True}

        def alter_in(node):
            node["hermes"]["inputBody"] = "altered"

        def alter_len(node):
            node["hermes"]["outputLength"] = node["hermes"]["outputLength"] + 1
        sdir = os.path.join(cls.dir, "subagents")
        S.run_fake_session({"HERMES_ATP_HOME": sdir, "HERMES_ATP_KEY": os.path.join(cls.dir, "key"),
                            "HERMES_ATP_PUB": cls.pub}, real_subagents=True)
        with open(os.path.join(sdir, "records.jsonl"), "rb") as f:
            subs = f.read()
        cls.cases.update({
            "subagents": subs,
            "subagents-no-parent": S.join([l for l in S.lines_of(subs) if b'"scope":"sess-main"' not in l]),
            "subagents-no-child": S.join([l for l in S.lines_of(subs) if b'"scope":"sess-child"' not in l]),
            "subagents-mixed": subs + cls.data,
        })
        cls.cases.update({
            "bodies-valid": bodies,
            "body-mismatch-out": S.resign_line(bodies, nb - 1, alter_out, seed_hex),
            "body-mismatch-in": S.resign_line(bodies, 0, alter_in, seed_hex),
            "body-length-mismatch": S.resign_line(bodies, nb - 1, alter_len, seed_hex),
        })

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def path(self, name):
        p = os.path.join(self.dir, name + ".jsonl")
        with open(p, "wb") as f:
            f.write(self.cases[name])
        return p

    def test_identical_json_and_exit_codes(self):
        for name in self.cases:
            with self.subTest(case=name):
                log = self.path(name)
                pc, pout, perr = run("python", log, self.pub, "--json")
                nc, nout, nerr = run("node", log, self.pub, "--json")
                self.assertEqual(pout, nout, f"{name}: verdict JSON differs")
                self.assertEqual(pc, nc)
                v = json.loads(pout)
                self.assertEqual(pc, 0 if v["verdict"] == "pass" else 2)

    def test_identical_text_output(self):
        for name in ("valid", "multi", "swap-lines", "empty", "body-mismatch-out", "subagents",
                     "subagents-no-parent", "subagents-no-child"):
            log = self.path(name)
            self.assertEqual(run("python", log, self.pub)[1], run("node", log, self.pub)[1])

    def test_expected_verdicts_for_required_tampers(self):
        def kinds(name):
            v = json.loads(run("node", self.path(name), self.pub, "--json")[1])
            return v["verdict"], [(e["line"], e["kind"]) for e in v["errors"]]
        self.assertEqual(kinds("valid"), ("pass", []))
        self.assertEqual(kinds("edit-byte"), ("fail", [(5, "tampered")]))
        self.assertEqual(kinds("delete-line"), ("fail", [(3, "missing")]))
        self.assertEqual(kinds("swap-lines"), ("fail", [(13, "out-of-order"), (14, "out-of-order")]))
        self.assertEqual(kinds("empty"), ("fail", [(0, "empty-log")]))
        self.assertEqual(kinds("newline-only"), ("fail", [(1, "tampered")]))  # one blank line
        self.assertEqual(kinds("bodies-valid"), ("pass", []))
        nb = len(S.lines_of(self.cases["bodies-valid"]))
        self.assertEqual(kinds("body-mismatch-out"), ("fail", [(nb, "body-mismatch")]))
        # Line 1 changed nodeId, so line 2's parent link breaks as well: both reported.
        self.assertEqual(kinds("body-mismatch-in")[1][0], (1, "body-mismatch"))
        self.assertEqual(kinds("body-length-mismatch"), ("fail", [(nb, "body-mismatch")]))
        # Known limitation (H1-F3): deleting a crash tail's unmatched pre leaves a valid chain.
        self.assertEqual(kinds("crash-tail-deleted"), ("pass", []))
        v = json.loads(run("python", self.path("valid"), self.pub, "--json")[1])
        self.assertEqual([w["kind"] for w in v["warnings"]], ["unmatched-request"])  # crash session

    def test_subagent_links_and_missing_parent(self):
        def out(name):
            return json.loads(run("node", self.path(name), self.pub, "--json")[1])
        v = out("subagents")
        self.assertEqual((v["verdict"], v["errors"], v["warnings"]), ("pass", [], []))
        self.assertEqual([(l["parent"], l["child"]) for l in v["links"]],
                         [("sess-main", "sess-child"), ("sess-child", "sess-grandchild")])
        v = out("subagents-no-parent")
        self.assertEqual(v["verdict"], "pass")
        self.assertEqual([(w["line"], w["kind"]) for w in v["warnings"]], [(1, "missing-parent-session")])
        # Removing the middle session orphans the grandchild (a warning, not a failure).
        v = out("subagents-no-child")
        self.assertEqual(v["verdict"], "pass")
        self.assertEqual([w["kind"] for w in v["warnings"]], ["missing-parent-session"])
        self.assertEqual([(l["parent"], l["child"]) for l in v["links"]], [("sess-child", "sess-grandchild")])
        text = run("python", self.path("subagents"), self.pub)[1].decode()
        self.assertIn("LINK session sess-main -> session sess-child", text)

    def test_wrong_key_and_bad_pub(self):
        log = self.path("valid")
        other = os.path.join(S.VECTORS, "TEST-ONLY-key.pub")
        self.assertEqual(run("python", log, other, "--json")[1], run("node", log, other, "--json")[1])
        self.assertEqual(run("python", log, other)[0], 2)
        bad = os.path.join(self.dir, "bad.pub")
        with open(bad, "w") as f:
            f.write("not-a-key")
        self.assertEqual(run("python", log, bad)[0], 1)
        self.assertEqual(run("node", log, bad)[0], 1)


# Slice 1 contract, pinned from main 494c0c2: the 19 slice-1 vector files (logs, keys) are
# byte-identical, and every slice-1 case's expected verdict is unchanged apart from the
# additive "links" key added in slice 2.
SLICE1_IDS = ["valid", "unmatched-pre", "edit-one-byte", "delete-line", "swap-lines", "swap-across-sessions",
              "duplicate-line", "truncate-tail", "blank-line", "garbage-line", "wrong-key", "empty-log",
              "truncate-crash-tail", "bodies-valid", "body-mismatch"]
SLICE1_EXPECTED_SHA256 = "7e0ac737633132cfffa9ea5f0cf5bd275e7ca533dd70beda71969dcfa933f96d"
SLICE1_FILES_SHA256 = "1ea5431e1162951cd4a188d4157c473da4c7893059e3bfc5bd2b18f07b4cb732"
SLICE1_FILES = ["TEST-ONLY-key.json", "TEST-ONLY-key.pub", "sample-log.jsonl", "wrong-key.pub"] + \
    [f"cases/{i}.jsonl" for i in SLICE1_IDS]


class Slice1CompatTests(unittest.TestCase):
    def test_slice1_vector_files_unchanged(self):
        rels = sorted("vectors/" + f for f in SLICE1_FILES)
        blob = b"".join(r.encode() + b"\0" + hashlib.sha256(open(os.path.join(S.PLUGIN, r), "rb").read())
                        .hexdigest().encode() + b"\n" for r in rels)
        self.assertEqual(hashlib.sha256(blob).hexdigest(), SLICE1_FILES_SHA256)

    def test_slice1_verdicts_unchanged_except_links(self):
        with open(os.path.join(S.VECTORS, "hermes-log-vectors.json"), encoding="utf-8") as f:
            by = {c["id"]: c for c in json.load(f)["cases"]}
        got = []
        for i in SLICE1_IDS:
            c = by[i]
            with open(os.path.join(S.VECTORS, c["log"]), "rb") as f, \
                    open(os.path.join(S.VECTORS, c["pub"])) as k:
                v = atp_log.verify_log(f.read(), atp_log.load_public_key(k.read()))
            self.assertEqual(v, c["expected"], i)
            got.append([i, {k2: x for k2, x in v.items() if k2 != "links"}])
        blob = json.dumps(got, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
        self.assertEqual(hashlib.sha256(blob).hexdigest(), SLICE1_EXPECTED_SHA256)


class VectorTests(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(S.VECTORS, "hermes-log-vectors.json"), encoding="utf-8") as f:
            self.manifest = json.load(f)

    def check(self, cli):
        for c in self.manifest["cases"]:
            with self.subTest(cli=cli, case=c["id"]):
                code, out, _ = run(cli, os.path.join(S.VECTORS, c["log"]), os.path.join(S.VECTORS, c["pub"]), "--json")
                self.assertEqual(json.loads(out), c["expected"])
                self.assertEqual(code, 0 if c["expected"]["verdict"] == "pass" else 2)

    def test_python_reproduces_vectors(self):
        self.check("python")

    @unittest.skipUnless(NODE, "node not found")
    def test_node_reproduces_vectors(self):
        self.check("node")

    def test_vectors_regenerate_byte_identical(self):
        out = tempfile.mkdtemp(prefix="atp-vec-")
        try:
            subprocess.run([sys.executable, os.path.join(S.PLUGIN, "tools", "make_vectors.py"), out], check=True,
                           capture_output=True)
            for root, _dirs, files in os.walk(S.VECTORS):
                for fn in files:
                    rel = os.path.relpath(os.path.join(root, fn), S.VECTORS)
                    with open(os.path.join(S.VECTORS, rel), "rb") as a, open(os.path.join(out, rel), "rb") as b:
                        self.assertEqual(a.read(), b.read(), f"vectors/{rel} is stale; run tools/make_vectors.py")
        finally:
            shutil.rmtree(out, ignore_errors=True)

    def test_sample_log_is_the_valid_case(self):
        with open(os.path.join(S.VECTORS, "sample-log.jsonl"), "rb") as a, \
                open(os.path.join(S.VECTORS, "cases", "valid.jsonl"), "rb") as b:
            self.assertEqual(a.read(), b.read())


if __name__ == "__main__":
    unittest.main()
