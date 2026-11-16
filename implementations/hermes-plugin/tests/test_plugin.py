"""Recorder plugin tests against the fake Hermes harness (stdlib unittest)."""

import base64
import filecmp
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import support as S  # noqa: E402
import fake_hermes as FH  # noqa: E402
import atp  # noqa: E402  (implementations/python)
import atp_log  # noqa: E402

HOOKS = {"pre_api_request", "post_api_request", "api_request_error", "pre_tool_call", "post_tool_call"}


def read_records(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f]


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="atp-hermes-test-")
        self.env = {"HERMES_HOME": os.path.join(self.dir, "hermes")}
        self.atp_home = os.path.join(self.dir, "hermes", "atp")
        self.log = os.path.join(self.atp_home, "records.jsonl")

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def run_session(self, **kw):
        return S.run_fake_session(self.env, **kw)

    def verify(self, data=None):
        if data is None:
            with open(self.log, "rb") as f:
                data = f.read()
        with open(os.path.join(self.atp_home, "key.pub")) as f:
            pub = atp_log.load_public_key(f.read())
        return atp_log.verify_log(data, pub)


class RegistrationTests(Base):
    def test_registers_exactly_the_five_observer_hooks(self):
        host, *_ = self.run_session()
        self.assertEqual(set(host.hooks), HOOKS)
        self.assertTrue(all(len(v) == 1 for v in host.hooks.values()))

    def test_callbacks_always_return_none_and_never_raise(self):
        host, *_ = self.run_session()
        self.assertEqual(host.returns, [])
        self.assertEqual(host.callback_errors, [])
        self.assertEqual(host.blocked_tools, [])
        self.assertEqual(host.modified_tools, [])

    def test_register_does_no_io(self):
        mod = FH.load_plugin(S.PLUGIN)
        host = FH.FakeHermes()
        host.load(mod)
        self.assertFalse(os.path.exists(self.atp_home))

    def test_vendored_atp_is_identical_to_reference(self):
        self.assertTrue(filecmp.cmp(os.path.join(S.PLUGIN, "atp.py"), os.path.join(S.PY_IMPL, "atp.py"), shallow=False),
                        "hermes-plugin/atp.py must be a byte-identical copy of implementations/python/atp.py")


class RecordTests(Base):
    def test_one_record_per_hook_event_with_ids(self):
        host, *_ = self.run_session()
        recs = read_records(self.log)
        self.assertEqual(len(recs), len(host.fired))
        self.assertEqual(len(recs), 16)
        for (hook, kw), r in zip(host.fired, recs):
            h = r["hermes"]
            self.assertEqual(h["event"], hook)
            self.assertEqual(r["action"]["subtype"], hook)
            self.assertEqual(h["sessionId"], kw["session_id"])
            self.assertEqual(r["scope"], kw["session_id"])
            self.assertEqual(h["turnId"], kw["turn_id"])
            self.assertEqual(h["schema"], "hermes.observer.v1")
            if "tool" in hook:
                self.assertEqual(h["toolCallId"], kw["tool_call_id"])
                self.assertEqual(h["toolName"], kw["tool_name"])
            else:
                self.assertEqual(h["apiRequestId"], kw["api_request_id"])
            if kw.get("parent_session_id"):
                self.assertEqual(h["parentSessionId"], kw["parent_session_id"])
            self.assertTrue(atp.is_atp_timestamp(r["timestamp"]))
            self.assertIsNone(atp.structural_error(r))

    def test_action_types_follow_request_completion_pairing(self):
        self.run_session()
        types = {(r["hermes"]["event"], r["hermes"].get("status")): r["action"]["type"] for r in read_records(self.log)}
        self.assertEqual(types[("pre_api_request", None)], "atp:request")
        self.assertEqual(types[("post_api_request", None)], "atp:completion")
        self.assertEqual(types[("api_request_error", None)], "atp:failure")
        self.assertEqual(types[("pre_tool_call", None)], "atp:request")
        self.assertEqual(types[("post_tool_call", "ok")], "atp:completion")
        self.assertEqual(types[("post_tool_call", "error")], "atp:failure")
        for r in read_records(self.log):
            if r["action"]["type"] == "atp:request":
                self.assertNotIn("outputHash", r["action"])  # §16.2: absent, not zeroed

    def test_signatures_verify_with_generated_key(self):
        self.run_session()
        with open(os.path.join(self.atp_home, "key.pub")) as f:
            pub = base64.b64decode(f.read())
        for r in read_records(self.log):
            self.assertEqual(atp.compute_node_id(r), r["nodeId"])
            self.assertTrue(atp.verify_node_id(r["nodeId"], r["signature"], pub))
            self.assertEqual(r["issuer"]["keyId"], "ed25519:" + __import__("hashlib").sha256(pub).hexdigest()[:16])

    def test_lineage_per_session(self):
        self.run_session()
        recs = read_records(self.log)
        by = {}
        for r in recs:
            by.setdefault(r["scope"], []).append(r)
        self.assertEqual(set(by), {"sess-main", "sess-child"})
        for scope, rs in by.items():
            self.assertEqual(rs[0]["parents"], [], scope)
            self.assertEqual(rs[0]["hermes"]["seq"], 0)
            self.assertIn("hermesVersion", rs[0]["hermes"])
            for prev, cur in zip(rs, rs[1:]):
                self.assertEqual(cur["hermes"]["seq"], prev["hermes"]["seq"] + 1)
                self.assertEqual(cur["parents"][0], prev["nodeId"])
                self.assertNotIn("hermesVersion", cur["hermes"])
        # Recorded whenever the hook passes it. NOTE: real Hermes v0.23.1 does not pass
        # parent_session_id to these five hooks; the fake passes it on API hooks only.
        child_api = [r for r in by["sess-child"] if "api" in r["hermes"]["event"]]
        self.assertTrue(child_api and all(r["hermes"]["parentSessionId"] == "sess-main" for r in child_api))
        self.assertTrue(all("parentSessionId" not in r["hermes"] for r in by["sess-main"]))

    def test_completion_links_its_request(self):
        self.run_session()
        recs = read_records(self.log)
        ids = {r["nodeId"]: r for r in recs}
        post2 = [r for r in recs if r["hermes"]["event"] == "post_tool_call" and r["hermes"]["toolCallId"] == "call-2"][0]
        pre2 = [r for r in recs if r["hermes"]["event"] == "pre_tool_call" and r["hermes"]["toolCallId"] == "call-2"][0]
        # subagent records are in another scope, so the request is also the previous record
        self.assertEqual(post2["parents"], [pre2["nodeId"]])
        self.assertEqual(post2["hermes"]["requestNodeId"], pre2["nodeId"])
        self.assertEqual(post2["action"]["inputHash"], pre2["action"]["inputHash"])
        post_api = [r for r in recs if r["hermes"]["event"] == "post_api_request"][0]
        self.assertEqual(post_api["action"]["inputHash"], ids[post_api["parents"][0]]["action"]["inputHash"])

    def test_interleaved_tools_link_request_as_second_parent(self):
        mod = FH.load_plugin(S.PLUGIN)
        rec_mod = S.recorder_module(mod)
        mod._recorder = rec_mod.Recorder(rec_mod.Config.from_environment(self.env))
        host = FH.FakeHermes()
        host.load(mod)
        ids = dict(session_id="s", turn_id="t", api_request_id="r")
        host.invoke("pre_tool_call", **ids, tool_call_id="A", tool_name="x", args={"a": 1})
        host.invoke("pre_tool_call", **ids, tool_call_id="B", tool_name="x", args={"b": 2})
        host.invoke("post_tool_call", **ids, tool_call_id="A", tool_name="x", args={"a": 1}, result="ra", status="ok")
        host.invoke("post_tool_call", **ids, tool_call_id="B", tool_name="x", args={"b": 2}, result="rb", status="ok")
        pa, pb, qa, qb = read_records(self.log)
        self.assertEqual(qa["parents"], [pb["nodeId"], pa["nodeId"]])
        self.assertEqual(qb["parents"], [qa["nodeId"], pb["nodeId"]])
        self.assertEqual(qa["action"]["inputHash"], pa["action"]["inputHash"])
        self.assertEqual(self.verify()["verdict"], "pass")

    def test_hermes_version_detected_best_effort(self):
        self.run_session()
        first = read_records(self.log)[0]
        self.assertIsInstance(first["hermes"]["hermesVersion"], str)
        self.assertEqual(first["agent"]["version"], first["hermes"]["hermesVersion"])

    def test_verify_passes(self):
        self.run_session()
        r = self.verify()
        self.assertEqual((r["verdict"], r["records"], r["sessions"], r["errors"], r["warnings"]), ("pass", 16, 2, [], []))


class SecretTests(Base):
    SECRETS = [FH.FAKE_API_KEY, FH.FAKE_AUTH, FH.FAKE_AUTH.split()[1], "hunter2", "user:pw"]

    def assert_no_secrets(self):
        with open(self.log, encoding="utf-8") as f:
            text = f.read()
        for s in self.SECRETS:
            self.assertNotIn(s, text)
        self.assertNotIn('"Authorization":"Bearer', text)
        self.assertNotIn("api_key\":\"sk-", text)
        return text

    def test_no_secrets_and_no_bodies_by_default(self):
        self.run_session(secrets=True)
        text = self.assert_no_secrets()
        self.assertNotIn("Body", text)
        self.assertNotIn("turn turn-1 attempt", text)  # prompt text
        for r in read_records(self.log):
            self.assertFalse(r["hermes"]["bodiesLogged"])
            if "inputHash" in r["action"] and r["action"]["type"] == "atp:request":
                self.assertIsInstance(r["hermes"]["inputLength"], int)
            self.assertNotIn("inputBody", r["hermes"])
            self.assertNotIn("outputBody", r["hermes"])

    def test_bodies_with_flag_still_redacted(self):
        self.env["HERMES_ATP_LOG_BODIES"] = "true"
        self.run_session(secrets=True)
        text = self.assert_no_secrets()
        self.assertIn("turn turn-1 attempt", text)
        self.assertIn("[REDACTED]", text)
        self.assertEqual(self.verify()["verdict"], "pass")

    def test_bodies_rehash_to_their_hashes_lockstep(self):
        """QA H1-F1: every stored body must re-hash to the hash in the same record, including
        bodies whose secrets were scrubbed (the hash covers the redacted form)."""
        self.env["HERMES_ATP_LOG_BODIES"] = "true"
        self.run_session(secrets=True)
        checked, scrubbed_key = 0, 0
        for r in read_records(self.log):
            h = r["hermes"]
            for which in ("input", "output"):
                if which + "Body" not in h:
                    continue
                body = h[which + "Body"]
                data = body.encode("utf-8") if isinstance(body, str) else atp.canonicalize(body).encode("utf-8")
                self.assertEqual(r["action"][which + "Hash"], atp.payload_hash(body), (h["event"], which))
                self.assertEqual(h[which + "Length"], len(data))
                checked += 1
                if "sk-" not in data.decode() and "[REDACTED]" in data.decode() and h.get(which + "Redacted"):
                    scrubbed_key += 1
        self.assertGreaterEqual(checked, 10)
        self.assertGreaterEqual(scrubbed_key, 1)
        # The 429 error carried the fake key in free text: its body is scrubbed and hash-consistent.
        err = [r for r in read_records(self.log) if r["hermes"]["event"] == "api_request_error"]
        self.assertTrue(err and all(r["hermes"].get("outputRedacted") for r in err))
        self.assertEqual(self.verify()["verdict"], "pass")

    def test_hashes_identical_with_bodies_on_and_off(self):
        """Hashing is over the scrubbed payload in every case, so the flag never changes a hash."""
        def hashes(bodies):
            d = tempfile.mkdtemp(prefix="atp-hh-", dir=self.dir)
            env = {"HERMES_ATP_HOME": d}
            if bodies:
                env["HERMES_ATP_LOG_BODIES"] = "true"
            S.run_fake_session(env, clock=S.TickClock(), secrets=True)
            out = []
            for r in read_records(os.path.join(d, "records.jsonl")):
                out.append((r["action"].get("inputHash"), r["action"].get("outputHash"),
                            r["hermes"].get("inputLength"), r["hermes"].get("outputLength"),
                            r["hermes"].get("inputRedacted"), r["hermes"].get("outputRedacted")))
            return out
        self.assertEqual(hashes(False), hashes(True))

    def test_verify_flags_body_hash_mismatch(self):
        self.env["HERMES_ATP_LOG_BODIES"] = "true"
        _h, rec, _m, _o = self.run_session()
        with open(self.log, "rb") as f:
            data = f.read()
        n = len(S.lines_of(data))
        seed = rec._seed if hasattr(rec, "_seed") else None
        self.assertIsNotNone(seed, "recorder seed attribute")

        def alter(node):
            node["hermes"]["outputBody"] = "something else"
        bad = S.resign_line(data, n - 1, alter, seed_hex=seed.hex())
        v = self.verify(bad)
        self.assertEqual(v["verdict"], "fail")
        self.assertEqual([(e["line"], e["kind"]) for e in v["errors"]], [(n, "body-mismatch")])

    def test_verify_empty_log_fails(self):
        v = atp_log.verify_log(b"", bytes(32))
        self.assertEqual((v["verdict"], v["errors"][0]["kind"], v["errors"][0]["line"]), ("fail", "empty-log", 0))

    def test_bodies_flag_from_config_file(self):
        os.makedirs(self.atp_home)
        with open(os.path.join(self.atp_home, "config.json"), "w") as f:
            json.dump({"log_bodies": True, "log_path": os.path.join(self.dir, "custom.jsonl")}, f)
        self.log = os.path.join(self.dir, "custom.jsonl")
        self.run_session()
        self.assertTrue(all(r["hermes"]["bodiesLogged"] for r in read_records(self.log)))


class KeyAndLogTests(Base):
    def test_key_generated_600_with_pub(self):
        self.run_session()
        kp = os.path.join(self.atp_home, "key")
        self.assertEqual(stat.S_IMODE(os.stat(kp).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(self.log).st_mode), 0o600)
        doc = json.load(open(kp))
        pub = base64.b64decode(open(kp + ".pub").read())
        self.assertEqual(atp.ed25519_public_key(bytes.fromhex(doc["seedHex"])), pub)

    def test_own_key_via_env(self):
        own = os.path.join(self.dir, "mykey")
        with open(own, "w") as f:
            f.write(S.TEST_SEED_HEX + "\n")
        os.chmod(own, 0o600)
        self.env["HERMES_ATP_KEY"] = own
        self.run_session()
        pub = atp.ed25519_public_key(bytes.fromhex(S.TEST_SEED_HEX))
        self.assertEqual(base64.b64decode(open(own + ".pub").read()), pub)
        self.assertFalse(os.path.exists(os.path.join(self.atp_home, "key")))
        r = read_records(self.log)[0]
        self.assertTrue(atp.verify_node_id(r["nodeId"], r["signature"], pub))

    def test_append_only_and_chain_resumes_after_restart(self):
        self.run_session()
        with open(self.log, "rb") as f:
            before = f.read()
        self.run_session()  # a brand-new recorder instance (Hermes restart), same sessions resumed
        with open(self.log, "rb") as f:
            after = f.read()
        self.assertTrue(after.startswith(before))
        r = self.verify(after)
        self.assertEqual((r["verdict"], r["records"]), ("pass", 32))

    def test_torn_last_line_is_terminated_not_merged(self):
        self.run_session()
        with open(self.log, "ab") as f:
            f.write(b'{"partial":')
        self.run_session()
        r = self.verify()
        self.assertEqual([(e["line"], e["kind"], e["reason"]) for e in r["errors"]], [(17, "tampered", "invalid JSON")])

    def test_concurrent_hooks_are_serialized(self):
        mod = FH.load_plugin(S.PLUGIN)
        rec_mod = S.recorder_module(mod)
        mod._recorder = rec_mod.Recorder(rec_mod.Config.from_environment(self.env))
        host = FH.FakeHermes()
        host.load(mod)

        def worker(n):
            for i in range(10):
                host.tool_call(f"sess-{n % 2}", "t", "r", f"c-{n}-{i}", "terminal", {"i": i}, "{}")

        ts = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
        [t.start() for t in ts]
        [t.join() for t in ts]
        r = self.verify()
        self.assertEqual((r["verdict"], r["records"], r["sessions"], r["warnings"]), ("pass", 120, 2, []))


class FailureIsolationTests(Base):
    def test_recorder_exception_never_reaches_hermes(self):
        os.makedirs(self.atp_home)
        os.makedirs(self.log)  # log path is a directory: every write fails
        host, rec, mod, out = self.run_session()
        self.assertTrue(out["turn_completed"])
        self.assertEqual(host.callback_errors, [])
        self.assertEqual(host.blocked_tools, [])  # pre_tool_call would fail closed if we raised
        self.assertEqual(host.returns, [])

    def test_broken_build_never_reaches_hermes(self):
        mod = FH.load_plugin(S.PLUGIN)
        rec_mod = S.recorder_module(mod)
        rec = rec_mod.Recorder(rec_mod.Config.from_environment(self.env))

        def boom(*a, **k):
            raise RuntimeError("forced plugin failure")

        rec.build = boom
        mod._recorder = rec
        host = FH.FakeHermes()
        host.load(mod)
        self.assertTrue(host.run_scripted_session()["turn_completed"])
        self.assertEqual((host.callback_errors, host.blocked_tools, host.returns), ([], [], []))

    def test_other_plugin_raising_does_not_affect_records(self):
        mod = FH.load_plugin(S.PLUGIN)
        host = FH.FakeHermes()
        rec_mod = S.recorder_module(mod)
        mod._recorder = rec_mod.Recorder(rec_mod.Config.from_environment(self.env))
        host.load(mod)
        for h in ("post_api_request", "post_tool_call"):
            host.hooks[h].append(lambda **kw: 1 / 0)
        self.assertTrue(host.run_scripted_session()["turn_completed"])
        self.assertTrue(host.callback_errors)
        self.assertEqual(self.verify()["verdict"], "pass")

    def test_fake_harness_really_fails_closed_on_raising_pre_tool_call(self):
        # Sanity check of the harness itself: a raising pre_tool_call blocks the tool, which is why
        # the recorder must never raise there.
        host = FH.FakeHermes()
        host.hooks["pre_tool_call"] = [lambda **kw: 1 / 0]
        self.assertTrue(host.tool_call("s", "t", "r", "c", "terminal", {}, "{}"))


class TamperTests(Base):
    def setUp(self):
        super().setUp()
        self.run_session()
        with open(self.log, "rb") as f:
            self.data = f.read()

    def kinds(self, data):
        r = self.verify(data)
        return r["verdict"], [(e["line"], e["kind"]) for e in r["errors"]]

    def test_edit_one_byte(self):
        self.assertEqual(self.kinds(S.edit_one_byte(self.data, 6)), ("fail", [(7, "tampered")]))

    def test_delete_one_line(self):
        self.assertEqual(self.kinds(S.delete_line(self.data, 1)), ("fail", [(2, "missing")]))

    def test_swap_two_lines(self):
        self.assertEqual(self.kinds(S.swap_lines(self.data, 0, 1)), ("fail", [(1, "out-of-order"), (2, "out-of-order")]))

    def test_unmatched_pre_is_warning_only(self):
        self.run_session(crash=True)
        r = self.verify()
        self.assertEqual(r["verdict"], "pass")
        self.assertEqual([w["kind"] for w in r["warnings"]], ["unmatched-request"])


if __name__ == "__main__":
    unittest.main()
