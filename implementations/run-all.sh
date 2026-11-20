#!/usr/bin/env sh
# Runs every ATP check: vectors (Node + Python), Appendix A demo, cross-implementation interop,
# the Hermes recorder plugin suite (fake Hermes harness, log vectors, Node/Python verify parity),
# and the Elastic reporting exporter/assets suite (fake Elasticsearch; no Docker needed).
set -e
cd "$(dirname "$0")"
command -v node >/dev/null 2>&1 || { echo "Node.js 18+ is required (https://nodejs.org)"; exit 1; }
PY=$(command -v python3 || command -v python || true)
[ -n "$PY" ] || { echo "Python 3.9+ is required (https://python.org)"; exit 1; }

echo "== 1/6  Node.js vectors ==";   node node/bin/run-vectors.js
echo; echo "== 2/6  Python vectors =="; "$PY" python/run_vectors.py
echo; echo "== 3/6  Appendix A demo (Node.js) =="; node node/bin/demo.js
echo; echo "== 4/6  Cross-implementation interop =="; "$PY" python/atp_cli.py interop
echo; echo "== 5/6  Hermes recorder plugin (implementations/hermes-plugin) =="; "$PY" -m unittest discover -s hermes-plugin/tests -p "test_*.py"
echo; echo "== 6/6  Hermes ATP -> Elasticsearch exporter (implementations/hermes-plugin/elastic) =="; "$PY" -m unittest discover -s hermes-plugin/elastic/tests -p "test_*.py"
echo; echo "ALL CHECKS PASSED"
