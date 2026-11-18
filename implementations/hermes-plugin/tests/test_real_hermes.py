"""Opt-in: run tests/real_hermes_check.py and tests/real_hermes_subagent_check.py under a REAL
Hermes interpreter (the subagent check needs a Hermes source checkout for its loopback LLM fake).

    HERMES_REAL_PYTHON=/path/to/hermes/venv/bin/python python3 -m unittest tests.test_real_hermes

Skipped when HERMES_REAL_PYTHON is unset (CI has no Hermes install)."""

import json
import os
import shutil
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support as S  # noqa: E402

REAL = os.environ.get("HERMES_REAL_PYTHON")
NODE = os.environ.get("NODE") or shutil.which("node")


@unittest.skipUnless(REAL, "set HERMES_REAL_PYTHON to a Hermes venv python to run")
class RealHermesTests(unittest.TestCase):
    def test_real_hermes(self):
        p = subprocess.run([REAL, os.path.join(S.HERE, "real_hermes_check.py")], capture_output=True, text=True,
                           cwd=os.path.dirname(S.PLUGIN))
        summary = json.loads(p.stdout)
        self.assertEqual(p.returncode, 0, json.dumps(summary["checks"], indent=1))
        pub = os.path.join(os.path.dirname(summary["log"]), "key.pub")
        outs = [subprocess.run([sys.executable, S.PY_CLI, "verify", summary["log"], "--pub", pub, "--json"],
                               capture_output=True).stdout]
        if NODE:
            outs.append(subprocess.run([NODE, S.NODE_CLI, "verify", summary["log"], "--pub", pub, "--json"],
                                       capture_output=True).stdout)
        self.assertEqual(json.loads(outs[0])["verdict"], "pass")
        self.assertEqual(len(set(outs)), 1)
        shutil.rmtree(summary["home"], ignore_errors=True)

    def test_real_hermes_subagent(self):
        p = subprocess.run([REAL, os.path.join(S.HERE, "real_hermes_subagent_check.py")], capture_output=True,
                           text=True, cwd=os.path.dirname(S.PLUGIN), timeout=300)
        summary = json.loads(p.stdout[p.stdout.index("{"):])
        if p.returncode == 3:
            self.skipTest(summary["skipped"])
        self.assertEqual(p.returncode, 0, json.dumps(summary.get("checks"), indent=1))
        pub = os.path.join(os.path.dirname(summary["log"]), "key.pub")
        outs = [subprocess.run([sys.executable, S.PY_CLI, "verify", summary["log"], "--pub", pub, "--json"],
                               capture_output=True).stdout]
        if NODE:
            outs.append(subprocess.run([NODE, S.NODE_CLI, "verify", summary["log"], "--pub", pub, "--json"],
                                       capture_output=True).stdout)
        self.assertEqual(len(set(outs)), 1)
        v = json.loads(outs[0])
        self.assertEqual((v["verdict"], v["errors"], v["warnings"]), ("pass", [], []))
        self.assertEqual([(l["parent"], l["child"]) for l in v["links"]],
                         [(summary["parent_session"], summary["child_session"])])
        shutil.rmtree(summary["home"], ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
