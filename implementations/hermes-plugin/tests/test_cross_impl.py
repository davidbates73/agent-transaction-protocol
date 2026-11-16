"""Cross-implementation verify: Node (bin/atp.js) and Python (atp_cli.py) must print
byte-identical --json verdicts and exit codes on the same logs; and both must reproduce
the checked-in vectors exactly. Requires `node` (18+) on PATH or $NODE."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support as S  # noqa: E402

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
        }

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
        for name in ("valid", "multi", "swap-lines"):
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
        v = json.loads(run("python", self.path("valid"), self.pub, "--json")[1])
        self.assertEqual([w["kind"] for w in v["warnings"]], ["unmatched-request"])  # crash session

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
