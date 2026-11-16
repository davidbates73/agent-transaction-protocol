#!/usr/bin/env python3
"""Regenerate implementations/hermes-plugin/vectors/ deterministically.

    python3 tools/make_vectors.py [outdir]

Uses the fake Hermes harness, a fixed clock and the TEST-ONLY key, so the output is
byte-identical on every run (Ed25519 is deterministic). Expected verdicts are computed
by the Python verifier; the test suite checks that the Node verifier agrees exactly.
"""

import base64
import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "tests"))
import support as S  # noqa: E402
import atp_log  # noqa: E402  (implementations/python, via support)


def build(outdir):
    os.makedirs(os.path.join(outdir, "cases"), exist_ok=True)
    work = tempfile.mkdtemp(prefix="atp-vectors-")
    try:
        seed = bytes.fromhex(S.TEST_SEED_HEX)
        key_doc = {
            "warning": "TEST ONLY - public test vector key. NEVER use for real records; the recorder never defaults to it.",
            "alg": "Ed25519",
            "seedHex": S.TEST_SEED_HEX,
        }
        key_path = os.path.join(work, "TEST-ONLY-key.json")
        with open(key_path, "w") as f:
            json.dump(key_doc, f)
        os.chmod(key_path, 0o600)

        def session(name, crash):
            env = {"HERMES_ATP_HOME": work, "HERMES_ATP_KEY": key_path,
                   "HERMES_ATP_PUB": os.path.join(work, "TEST-ONLY-key.pub"),
                   "HERMES_ATP_LOG": os.path.join(work, name)}
            _host, rec, _mod, _ = S.run_fake_session(env, clock=S.TickClock(), crash=crash,
                                                    hermes_version=S.VECTOR_HERMES_VERSION)
            with open(os.path.join(work, name), "rb") as f:
                return f.read(), rec.public_key

        sample, pub = session("sample.jsonl", crash=False)
        crash, _ = session("crash.jsonl", crash=True)
        import atp  # noqa: E402  (implementations/python)
        wrong_pub = atp.ed25519_public_key(bytes(32))

        cases = [
            ("valid", "Unmodified scripted session (API, tool, subagent, API error + retry)", sample, "test"),
            ("unmatched-pre", "Scripted session plus a session whose pre_tool_call never got a post (crash): pass with WARNING", crash, "test"),
            ("edit-one-byte", "One hex digit of line 3's inputHash changed", S.edit_one_byte(sample, 2), "test"),
            ("delete-line", "Line 4 (post_tool_call call-1) deleted", S.delete_line(sample, 3), "test"),
            ("swap-lines", "Lines 13 and 14 (same session) swapped", S.swap_lines(sample, 12, 13), "test"),
            ("swap-across-sessions", "Lines 11 and 12 (different sessions) swapped: per-session order intact, so pass", S.swap_lines(sample, 10, 11), "test"),
            ("duplicate-line", "Line 6 repeated as line 7", S.duplicate_line(sample, 5), "test"),
            ("truncate-tail", "Last line removed: undetectable by the chain alone, surfaces as an unmatched-request WARNING", S.delete_line(sample, 15), "test"),
            ("blank-line", "Blank line inserted before line 5", S.insert_line(sample, 4, b""), "test"),
            ("garbage-line", "Non-JSON line inserted before line 5", S.insert_line(sample, 4, b"not json"), "test"),
            ("wrong-key", "Valid log checked against a different public key", sample, "wrong"),
        ]
        pubs = {"test": pub, "wrong": wrong_pub}
        with open(os.path.join(outdir, "TEST-ONLY-key.json"), "w") as f:
            f.write(json.dumps(key_doc, indent=2) + "\n")
        for label, raw in pubs.items():
            name = "TEST-ONLY-key.pub" if label == "test" else "wrong-key.pub"
            with open(os.path.join(outdir, name), "w") as f:
                f.write(base64.b64encode(raw).decode() + "\n")
        with open(os.path.join(outdir, "sample-log.jsonl"), "wb") as f:
            f.write(sample)
        manifest = {"description": "ATP Hermes recorder log vectors (slice 1). Each case: verify <log> --pub <pub> --json must print exactly `expected` (as JSON) and exit 0 for pass, 2 for fail.",
                    "cases": []}
        for cid, desc, data, keylabel in cases:
            path = f"cases/{cid}.jsonl"
            with open(os.path.join(outdir, path), "wb") as f:
                f.write(data)
            expected = atp_log.verify_log(data, pubs[keylabel])
            manifest["cases"].append({"id": cid, "description": desc, "log": path,
                                      "pub": "TEST-ONLY-key.pub" if keylabel == "test" else "wrong-key.pub",
                                      "expected": expected})
        with open(os.path.join(outdir, "hermes-log-vectors.json"), "w") as f:
            f.write(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    build(sys.argv[1] if len(sys.argv) > 1 else S.VECTORS)
