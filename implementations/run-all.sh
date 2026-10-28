#!/usr/bin/env sh
# Runs every ATP check: vectors (Node + Python), Appendix A demo, cross-implementation interop.
set -e
cd "$(dirname "$0")"
command -v node >/dev/null 2>&1 || { echo "Node.js 18+ is required (https://nodejs.org)"; exit 1; }
PY=$(command -v python3 || command -v python || true)
[ -n "$PY" ] || { echo "Python 3.9+ is required (https://python.org)"; exit 1; }

echo "== 1/4  Node.js vectors ==";   node node/bin/run-vectors.js
echo; echo "== 2/4  Python vectors =="; "$PY" python/run_vectors.py
echo; echo "== 3/4  Appendix A demo (Node.js) =="; node node/bin/demo.js
echo; echo "== 4/4  Cross-implementation interop =="; "$PY" python/atp_cli.py interop
echo; echo "ALL CHECKS PASSED"
