#!/usr/bin/env python3
"""Run vectors/atp-core-test-vectors.json against the Python implementation.

    python3 run_vectors.py [path/to/vectors.json] [--published]
"""

import base64
import json
import os
import platform
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import atp  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
args = [a for a in sys.argv[1:] if not a.startswith("--")]
path = args[0] if args else os.path.join(HERE, "..", "..", "vectors", "atp-core-test-vectors.json")
only_published = "--published" in sys.argv
V = atp.load_json(path)

tty = sys.stdout.isatty()
G, R, D, X = ("\x1b[32m", "\x1b[31m", "\x1b[2m", "\x1b[0m") if tty else ("", "", "", "")
results = {"pass": 0, "fail": 0}


def check(vid, status, label, ok, info=""):
    if only_published and status != "published-00":
        return
    results["pass" if ok else "fail"] += 1
    tag = f"{G}PASS{X}" if ok else f"{R}FAIL{X}"
    print(f"  {tag}  {vid:<4} {D}[{status}]{X} {label}" + (f"  -> {info}" if info else ""))


print(f"ATP Core vectors (Python {platform.python_version()})\n  file: {os.path.relpath(path)}\n")

print("Canonicalization (§9 + RFC 8785)")
for c in V["canonicalization"]:
    got = atp.canonicalize(json.loads(c["inputJson"]))
    check(c["id"], c["status"], c["description"],
          got == c["canonical"] and len(got.encode("utf-8")) == c["length"], "" if got == c["canonical"] else got)

print("\nnodeId computation (§10.1)")
for n in V["nodeId"]:
    node = json.loads(n["inputJson"])
    got = atp.compute_node_id(node)
    check(n["id"], n["status"], n["description"],
          atp.canonicalize(atp.node_content(node)) == n["canonical"] and got == n["nodeId"], "" if got == n["nodeId"] else got)

print("\nEd25519 (§10.2–10.3)")
seeds, pubs = {}, {}
for name, k in V["keys"].items():
    seed = bytes.fromhex(k["seedHex"])
    pub = atp.ed25519_public_key(seed)
    seeds[name], pubs[name] = seed, pub
    check(name.replace("test-key-", "K"), k["status"], f"public key from seed ({name})",
          pub.hex() == k["publicKeyHex"] and base64.b64encode(pub).decode() == k["publicKeyBase64"])
for s in V["signatures"]:
    sig = atp.sign_node_id(s["messageNodeIdHex"], seeds[s["key"]])
    ok = (sig == s["signatureBase64"] and base64.b64decode(sig).hex() == s["signatureHex"]
          and atp.verify_node_id(s["messageNodeIdHex"], s["signatureBase64"], pubs[s["key"]]))
    check(s["id"], s["status"], s["description"], ok)

print("\nNegative cases (MUST reject)")
for n in V["negative"]:
    got = atp.verify_node_id(n["messageNodeIdHex"], n["signatureBase64"], pubs[n["key"]])
    check(n["id"], n["status"], n["description"], got == n["expectVerify"])

print("\nValidation modes and §13.6 result format")
cats = ["verified", "invalid", "unresolved", "withheld", "outOfHorizon", "keyUnresolved", "profileUnresolved"]
all_keys = {f"{k['issuerId']}|{name}": k["publicKeyBase64"] for name, k in V["keys"].items()}
for r in V["validation"]:
    keys = {k: v for k, v in all_keys.items() if k.split("|")[1] in r["keys"]}
    nodes = atp.index_nodes(V["signedNodes"][ref] for ref in r["nodes"])
    got = atp.validate(r["targets"], nodes, atp.key_resolver(keys), mode=r["mode"], boundary=r.get("boundary"),
                       withheld=r.get("withheld", []), strict_profiles=r.get("strictProfiles", False))
    ok = got["mode"] == r["mode"] and all(sorted(got[c]) == sorted(r["expected"][c]) for c in cats)
    if r.get("boundary"):
        ok = ok and got["boundary"] == r["expected"]["boundary"]
    if r.get("expectedExtension"):
        ok = ok and sorted(got["lineageIncomplete"]) == sorted(r["expectedExtension"]["lineageIncomplete"])
    bad = "" if ok else " ".join(f"{c}={[x[:8] for x in got[c]]}" for c in cats if sorted(got[c]) != sorted(r["expected"][c]))
    check(r["id"], r["status"], r["description"], ok, bad)

colour = G if results["fail"] == 0 else R
print(f"\n{colour}{results['pass']} passed, {results['fail']} failed{X}" + (" (published -00 vectors only)" if only_published else ""))
sys.exit(0 if results["fail"] == 0 else 1)
