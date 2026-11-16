#!/usr/bin/env sh
# Runs every ATP check: vectors (Node + Python), Appendix A demo, cross-implementation interop,
# and the Hermes recorder plugin suite (fake Hermes harness, log vectors, Node/Python verify parity).
set -e
cd "$(dirname "$0")"
command -v node >/dev/null 2>&1 || { echo "Node.js 18+ is required (https://nodejs.org)"; exit 1; }
PY=$(command -v python3 || command -v python || true)
[ -n "$PY" ] || { echo "Python 3.9+ is required (https://python.org)"; exit 1; }

echo "== 1/5  Node.js vectors ==";   node node/bin/run-vectors.js
echo; echo "== 2/5  Python vectors =="; "$PY" python/run_vectors.py
echo; echo "== 3/5  Appendix A demo (Node.js) =="; node node/bin/demo.js
echo; echo "== 4/5  Cross-implementation interop =="; "$PY" python/atp_cli.py interop
echo; echo "== 5/5  Hermes recorder plugin (implementations/hermes-plugin) =="; "$PY" -m unittest discover -s hermes-plugin/tests -p "test_*.py"
echo; echo "ALL CHECKS PASSED"
