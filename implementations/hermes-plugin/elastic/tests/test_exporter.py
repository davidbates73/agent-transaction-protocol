"""Exporter tests against a fake Elasticsearch (stdlib only, no network beyond 127.0.0.1)."""

from __future__ import annotations

import glob
import hashlib
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
ELASTIC = os.path.dirname(HERE)
PLUGIN = os.path.dirname(ELASTIC)
VECTORS = os.path.join(PLUGIN, "vectors")
sys.path.insert(0, os.path.join(ELASTIC, "exporter"))
sys.path.insert(0, HERE)

import hermes_atp_export as hx  # noqa: E402
import atp_log  # noqa: E402  (vendored copy, via the exporter's sys.path)
from fake_es import FakeES  # noqa: E402

# Keep expected retry warnings off the test output (SecretsTests attaches its own handler).
_lg = logging.getLogger("hermes-atp-export")
_lg.addHandler(logging.NullHandler())
_lg.propagate = False

PUB = os.path.join(VECTORS, "TEST-ONLY-key.pub")
SAMPLE = os.path.join(VECTORS, "sample-log.jsonl")
PASSWORD = "s3cr3t-PW-do-not-log"
API_KEY = "QUtFWS1kby1ub3QtbG9nOnNlY3JldA=="
AUTH = "Basic " + __import__("base64").b64encode(f"hermes_exporter:{PASSWORD}".encode()).decode()


def lines_of(path):
    with open(path, "rb") as f:
        return f.read().splitlines(keepends=True)

def slurp(path, mode="r", **kw):
    with open(path, mode, **kw) as f:
        return f.read()



class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hx-test-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.es = FakeES(auth=AUTH).__enter__()
        self.addCleanup(self.es.__exit__)
        self.log_path = os.path.join(self.tmp, "records.jsonl")
        self.sleeps = []

    def cfg(self, **kw):
        values = {"es_url": self.es.url, "allow_insecure_http": True, "username": "hermes_exporter",
                  "password": PASSWORD, "pub": PUB, "logs": [self.log_path],
                  "state_dir": os.path.join(self.tmp, "state"), "mode": "copy", "backoff_initial": 0.01,
                  "backoff_max": 0.05, "poll_interval": 0.01, "timeout": 5}
        values.update(kw)
        return hx.Config.load(env={}, overrides=values)

    def exporter(self, sleep=None, **kw):
        def default_sleep(s):
            self.sleeps.append(s)
        return hx.Exporter(self.cfg(**kw), sleep=sleep or default_sleep)

    def write(self, data: bytes, path=None, mode="wb"):
        with open(path or self.log_path, mode) as f:
            f.write(data)

    def ids(self):
        return sorted(k[1] for k in self.es.docs)


class BulkFormatTests(Base):
    def test_bulk_ndjson_create_with_record_hash_ids(self):
        shutil.copy(SAMPLE, self.log_path)
        self.exporter().run()
        self.assertTrue(self.es.requests)
        body = self.es.requests[0].decode()
        self.assertTrue(body.endswith("\n"))
        rows = body.split("\n")[:-1]
        self.assertEqual(len(rows) % 2, 0)
        actions = [json.loads(r) for r in rows[0::2]]
        self.assertTrue(all(list(a) == ["create"] for a in actions))
        node_ids = [json.loads(l)["nodeId"] for l in lines_of(SAMPLE)]
        self.assertEqual(self.ids(), sorted(node_ids))
        self.assertEqual({k[0] for k in self.es.docs}, {"hermes-atp-records-2026.11"})
        h = self.es.headers[0]
        self.assertEqual(h["Content-Type"], "application/x-ndjson")
        self.assertEqual(h["Authorization"], AUTH)
        for doc in self.es.docs.values():
            self.assertTrue(doc["verify"]["valid"], doc["verify"])
            self.assertEqual(doc["verify"]["status"], "valid")
            self.assertEqual(doc["record_hash"], doc["node_id"])

    def test_document_fields(self):
        shutil.copy(SAMPLE, self.log_path)
        self.exporter().run()
        docs = sorted(self.es.docs.values(), key=lambda d: d["source"]["line"])
        first = docs[0]
        self.assertEqual(first["event"], "pre_api_request")
        self.assertEqual(first["session_id"], "sess-main")
        self.assertEqual(first["model"], "grok-4")
        self.assertIs(first["is_session_start"], True)
        self.assertEqual(first["@timestamp"], "2026-11-16T00:00:00Z")
        self.assertEqual(first["source"], {"path": self.log_path, "offset": 0, "line": 1,
                                           "bytes": len(lines_of(SAMPLE)[0]) - 1})
        self.assertTrue(any("tokens" in d for d in docs))
        self.assertTrue(any(d.get("tool_name") for d in docs))
        self.assertFalse(any("sessionId" in d or "hermes" in d for d in docs))

    def test_batching(self):
        shutil.copy(SAMPLE, self.log_path)
        self.exporter(batch_size=2).run()
        n = len(lines_of(SAMPLE))
        self.assertEqual(len(self.es.requests), (n + 1) // 2)
        self.assertEqual(len(self.es.docs), n)

    def test_invalid_lines_get_line_hash_ids_and_invalid_index(self):
        good = lines_of(SAMPLE)
        self.write(good[0] + b"\n" + b"not json\n" + good[1])
        self.exporter().run()
        self.assertEqual(len(self.es.docs), 4)
        bad = [d for (i, _), d in self.es.docs.items() if i.endswith("-invalid")]
        self.assertEqual(len(bad), 2)
        for d in bad:
            self.assertFalse(d["verify"]["valid"])
            self.assertEqual(d["verify"]["status"], "tampered")
        ids = {k[1] for k in self.es.docs if k[0].endswith("-invalid")}
        self.assertTrue(all(i.startswith("anomaly-") for i in ids))


class IdempotencyTests(Base):
    def test_restart_does_not_resend_or_duplicate(self):
        shutil.copy(SAMPLE, self.log_path)
        self.exporter().run()
        n_req = len(self.es.requests)
        before = dict(self.es.docs)
        self.exporter().run()
        self.assertEqual(len(self.es.requests), n_req, "checkpoint: nothing re-sent")
        self.assertEqual(self.es.docs, before)

    def test_lost_checkpoint_is_harmless(self):
        shutil.copy(SAMPLE, self.log_path)
        self.exporter().run()
        before = dict(self.es.docs)
        shutil.rmtree(os.path.join(self.tmp, "state"))
        ex = self.exporter()
        ex.run()
        self.assertEqual(self.es.docs, before)
        self.assertEqual(ex.stats["duplicates"], len(before))
        self.assertEqual(ex.stats["shipped"], 0)

    def test_corrupt_checkpoint_is_harmless(self):
        shutil.copy(SAMPLE, self.log_path)
        ex = self.exporter()
        ex.run()
        state = ex.sources[0].state_path
        self.write(b"{garbage", path=state)
        ex = self.exporter()
        ex.run()
        self.assertEqual(ex.stats["duplicates"], len(lines_of(SAMPLE)))

    def test_checkpoint_is_written_only_after_ack(self):
        shutil.copy(SAMPLE, self.log_path)
        self.es.auth = "wrong"  # every bulk -> 401 -> fatal
        ex = self.exporter()
        with self.assertRaises(hx.FatalError):
            ex.run()
        self.assertFalse(os.path.exists(ex.sources[0].state_path))


class CopyModeTests(Base):
    """Copy mode: logs rsync'ed/scp'ed from the Hermes host, processed to EOF per run."""

    def setUp(self):
        super().setUp()
        self.all = lines_of(SAMPLE)

    def copy_in(self, data: bytes):
        # Like rsync/scp: write a temp file, then rename over the target (new inode).
        tmp = self.log_path + ".part"
        self.write(data, path=tmp)
        os.replace(tmp, self.log_path)

    def test_recopied_identical_file(self):
        self.copy_in(b"".join(self.all))
        self.exporter().run()
        n_req, before = len(self.es.requests), dict(self.es.docs)
        self.copy_in(b"".join(self.all))
        ex = self.exporter()
        ex.run()
        self.assertEqual(len(self.es.requests), n_req)
        self.assertEqual(self.es.docs, before)

    def test_appended_file(self):
        self.copy_in(b"".join(self.all[:4]))
        self.exporter().run()
        self.copy_in(b"".join(self.all))
        ex = self.exporter()
        ex.run()
        self.assertEqual(ex.stats["shipped"], len(self.all) - 4)
        self.assertEqual(ex.stats["duplicates"], 0)
        self.assertEqual(len(self.es.docs), len(self.all))
        self.assertTrue(all(d["verify"]["valid"] for d in self.es.docs.values()),
                        "chain state carried across runs: appended records still verify")

    def test_truncated_file(self):
        self.copy_in(b"".join(self.all))
        self.exporter().run()
        self.copy_in(b"".join(self.all[:3]))
        ex = self.exporter()
        ex.run()
        self.assertEqual(ex.stats["shipped"], 0)
        self.assertEqual(ex.stats["duplicates"], 3)
        self.assertEqual(len(self.es.docs), len(self.all))
        self.assertEqual(ex.sources[0].state["resets"], 1)

    def test_replaced_with_different_content(self):
        self.copy_in(b"".join(self.all))
        self.exporter().run()
        other = lines_of(os.path.join(VECTORS, "cases", "subagent-linked.jsonl"))
        self.assertGreater(len(b"".join(other)), len(b"".join(self.all)) // 2)
        self.copy_in(b"".join(other) * 2)  # different head, longer than the checkpoint
        ex = self.exporter()
        ex.run()
        self.assertEqual(ex.sources[0].state["resets"], 1)
        mine = {json.loads(l)["nodeId"] for l in self.all}
        theirs = {json.loads(l)["nodeId"] for l in other}
        overlap = len(mine & theirs)  # these vectors share one genuine record
        self.assertEqual(ex.stats["duplicates"], overlap)
        self.assertEqual(ex.stats["shipped"], 2 * len(other) - overlap)
        self.assertTrue(all(self.es.docs[("hermes-atp-records-2026.11", g)]["verify"]["valid"] for g in theirs))
        flagged = [d for d in self.es.docs.values() if not d["verify"]["valid"]]
        self.assertEqual(len(flagged), len(other), "the repeated copy is flagged, not swallowed by dedup")
        self.assertEqual(len(self.es.docs), len(mine | theirs) + len(other))

    def rewrite_one_byte(self, find: bytes, replace: bytes):
        data = b"".join(self.all)
        self.copy_in(data)
        self.exporter().run()
        last = self.all[len(self.all) // 2]   # well inside the already-shipped region
        start = data.index(last)
        i = data.index(find, start)
        new = data[:i] + replace + data[i + len(find):]
        self.assertEqual(len(new), len(data))
        self.copy_in(new)
        ex = self.exporter()
        ex.run()
        self.assertEqual(ex.sources[0].state["resets"], 1, "prefix hash caught the rewrite")
        tampered = [d for d in self.es.docs.values() if not d["verify"]["valid"]]
        self.assertEqual(len(tampered), 1)
        self.assertEqual(tampered[0]["verify"]["status"], "tampered")
        self.assertEqual(tampered[0]["source"]["line"], len(self.all) // 2 + 1)
        self.assertEqual(len(self.es.docs), len(self.all) + 1, "genuine doc kept, forged copy added beside it")
        genuine = json.loads(last)["nodeId"]
        self.assertTrue(self.es.docs[("hermes-atp-records-2026.11", genuine)]["verify"]["valid"])

    def test_rewritten_content_same_length(self):
        mid = json.loads(self.all[len(self.all) // 2])
        ts = mid["timestamp"].encode()
        self.rewrite_one_byte(ts, ts[:-2] + (b"9Z" if ts[-2:] != b"9Z" else b"8Z"))

    def test_forged_signature_cannot_shadow_genuine_record(self):
        sig = json.loads(self.all[len(self.all) // 2])["signature"].encode()
        self.rewrite_one_byte(sig[:8], (b"A" if sig[:1] != b"A" else b"B") + sig[1:8])

    def test_partial_last_line_waits(self):
        self.copy_in(b"".join(self.all[:2]) + self.all[2][:-10])
        ex = self.exporter()
        ex.run()
        self.assertEqual(len(self.es.docs), 2)
        self.copy_in(b"".join(self.all[:3]))
        self.exporter().run()
        self.assertEqual(len(self.es.docs), 3)
        self.assertTrue(all(d["verify"]["valid"] for d in self.es.docs.values()))

    def test_multiple_logs(self):
        second = os.path.join(self.tmp, "child.jsonl")
        shutil.copy(SAMPLE, self.log_path)
        shutil.copy(os.path.join(VECTORS, "cases", "subagent-linked.jsonl"), second)
        self.exporter(logs=[self.log_path, second]).run()
        expected = {json.loads(l)["nodeId"] for l in lines_of(SAMPLE) +
                    lines_of(os.path.join(VECTORS, "cases", "subagent-linked.jsonl"))}
        self.assertEqual(set(self.ids()), expected)


class FollowModeTests(Base):
    """Push mode follows the live file; script actions run in the (fake) poll sleep."""

    def run_follow(self, actions):
        ex = None
        actions = list(actions)

        def sleep(s):
            self.sleeps.append(s)
            if actions:
                actions.pop(0)()
            else:
                ex.stopping = True
        ex = self.exporter(sleep=sleep, mode="push")
        ex.run()
        return ex

    def test_follows_appends(self):
        all_ = lines_of(SAMPLE)
        self.write(b"".join(all_[:2]))
        ex = self.run_follow([lambda: self.write(b"".join(all_[2:5]), mode="ab"),
                              lambda: self.write(b"".join(all_[5:]), mode="ab")])
        self.assertEqual(len(self.es.docs), len(all_))
        self.assertEqual(ex.stats["shipped"], len(all_))
        self.assertTrue(all(d["verify"]["valid"] for d in self.es.docs.values()))

    def test_rotation_drains_old_file_then_follows_new(self):
        all_ = lines_of(SAMPLE)
        self.write(b"".join(all_[:3]))

        def rotate():
            self.write(all_[3], mode="ab")               # written just before rotation
            os.rename(self.log_path, self.log_path + ".1")
            self.write(b"".join(all_[4:]))               # new file continues the sessions
        ex = self.run_follow([rotate])
        self.assertEqual(len(self.es.docs), len(all_))
        self.assertEqual(ex.stats["duplicates"], 0)
        self.assertTrue(all(d["verify"]["valid"] for d in self.es.docs.values()),
                        "chain state carries over a rotation")

    def test_copytruncate_in_place(self):
        all_ = lines_of(SAMPLE)
        self.write(b"".join(all_))

        def truncate():
            with open(self.log_path, "r+b") as f:
                f.truncate(0)
            self.write(all_[0] + all_[1], mode="ab")
        ex = self.run_follow([truncate])
        self.assertEqual(len(self.es.docs), len(all_))
        self.assertEqual(ex.stats["duplicates"], 2)
        self.assertEqual(ex.sources[0].state["resets"], 1)

    def test_missing_file_waits(self):
        all_ = lines_of(SAMPLE)
        self.run_follow([lambda: None, lambda: self.write(b"".join(all_))])
        self.assertEqual(len(self.es.docs), len(all_))


class ResilienceTests(Base):
    def test_http_503_outage_retries_with_backoff(self):
        shutil.copy(SAMPLE, self.log_path)
        self.es.fail_http = 4
        ex = self.exporter()
        ex.run()
        self.assertEqual(len(self.es.docs), len(lines_of(SAMPLE)))
        self.assertEqual(ex.stats["retries"], 4)
        self.assertEqual(len(self.sleeps), 4)
        base = [s for s in self.sleeps]
        self.assertLess(base[0], base[-1] + 1e-9)
        self.assertTrue(all(s <= 0.05 * 1.25 + 1e-9 for s in base), "capped at backoff_max (+jitter)")

    def test_connection_refused_until_es_comes_back(self):
        shutil.copy(SAMPLE, self.log_path)
        import socket
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        dead = f"http://127.0.0.1:{s.getsockname()[1]}"
        s.close()
        ex = None

        def sleep(t):
            self.sleeps.append(t)
            if len(self.sleeps) == 3:
                ex.client.url = self.es.url  # ES is back
        ex = self.exporter(sleep=sleep, es_url=dead)
        ex.run()
        self.assertEqual(len(self.sleeps), 3)
        self.assertEqual(len(self.es.docs), len(lines_of(SAMPLE)))
        self.assertFalse(os.path.exists(ex.dead_letter))

    def test_outage_mid_stream_resumes_without_loss_or_duplicates(self):
        shutil.copy(SAMPLE, self.log_path)
        ex = None

        def sleep(t):
            self.sleeps.append(t)
        ex = self.exporter(sleep=sleep, batch_size=3)
        orig = ex.client.bulk
        calls = {"n": 0}

        def flaky(body):
            calls["n"] += 1
            if calls["n"] in (2, 3):
                raise hx.TransientError("ConnectionResetError: simulated")
            return orig(body)
        ex.client.bulk = flaky
        ex.run()
        self.assertEqual(len(self.es.docs), len(lines_of(SAMPLE)))
        self.assertEqual(ex.stats["duplicates"], 0)

    def test_per_item_429_is_retried(self):
        shutil.copy(SAMPLE, self.log_path)
        first = json.loads(lines_of(SAMPLE)[0])["nodeId"]
        self.es.throttle_ids[first] = 2
        ex = self.exporter()
        ex.run()
        self.assertEqual(len(self.es.docs), len(lines_of(SAMPLE)))
        self.assertEqual(ex.stats["retries"], 2)

    def test_per_item_rejection_is_dead_lettered_without_content(self):
        shutil.copy(SAMPLE, self.log_path)
        first = json.loads(lines_of(SAMPLE)[0])["nodeId"]
        self.es.reject_ids.add(first)
        ex = self.exporter()
        ex.run()
        self.assertEqual(len(self.es.docs), len(lines_of(SAMPLE)) - 1)
        with open(ex.dead_letter) as f:  # noqa: SIM115
            entries = [json.loads(l) for l in f]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["_id"], first)
        self.assertEqual(set(entries[0]), {"at", "index", "_id", "offset", "status", "error_type", "error_reason"})
        self.assertEqual(oct(os.stat(ex.dead_letter).st_mode & 0o777), "0o600")
        # advanced past it: a rerun sends nothing
        n = len(self.es.requests)
        self.exporter().run()
        self.assertEqual(len(self.es.requests), n)

    def test_bad_credentials_are_fatal_not_retried(self):
        shutil.copy(SAMPLE, self.log_path)
        self.es.auth = "nope"
        with self.assertRaises(hx.FatalError):
            self.exporter().run()
        self.assertEqual(self.sleeps, [])


class SourceSafetyTests(Base):
    def test_source_is_only_read(self):
        shutil.copy(SAMPLE, self.log_path)
        os.chmod(self.log_path, 0o444)
        st = os.stat(self.log_path)
        digest = hashlib.sha256(slurp(self.log_path, "rb")).hexdigest()
        self.exporter().run()
        st2 = os.stat(self.log_path)
        self.assertEqual((st.st_size, st.st_mtime_ns, st.st_ino), (st2.st_size, st2.st_mtime_ns, st2.st_ino))
        self.assertEqual(hashlib.sha256(slurp(self.log_path, "rb")).hexdigest(), digest)
        self.assertEqual(len(self.es.docs), len(lines_of(SAMPLE)))
        self.assertEqual(sorted(os.listdir(self.tmp)), ["records.jsonl", "state"], "nothing written beside the log")

    def test_never_locks_or_opens_for_write(self):
        src = slurp(os.path.join(ELASTIC, "exporter", "hermes_atp_export.py"))
        self.assertNotIn("fcntl", src)
        self.assertNotIn("flock", src)
        self.assertIn("os.O_RDONLY", src)

    def test_state_files_are_private(self):
        shutil.copy(SAMPLE, self.log_path)
        ex = self.exporter()
        ex.run()
        self.assertEqual(oct(os.stat(ex.cfg.state_dir).st_mode & 0o777), "0o700")
        self.assertEqual(oct(os.stat(ex.sources[0].state_path).st_mode & 0o777), "0o600")


class BodiesTests(Base):
    BODIES = os.path.join(VECTORS, "cases", "bodies-valid.jsonl")

    def body_texts(self):
        out = []
        for l in lines_of(self.BODIES):
            h = json.loads(l).get("hermes", {})
            out += [h[k] for k in ("inputBody", "outputBody") if isinstance(h.get(k), str) and len(h[k]) > 8]
        self.assertTrue(out)
        return out

    def test_bodies_not_shipped_by_default(self):
        shutil.copy(self.BODIES, self.log_path)
        self.exporter().run()
        wire = b"".join(self.es.requests).decode()
        for t in self.body_texts():
            self.assertNotIn(json.dumps(t)[1:-1], wire)
        self.assertFalse(any("body" in d for d in self.es.docs.values()))

    def test_bodies_shipped_only_with_flag(self):
        shutil.copy(self.BODIES, self.log_path)
        self.exporter(ship_bodies=True).run()
        with_body = [d for d in self.es.docs.values() if "body" in d]
        self.assertTrue(with_body)
        self.assertTrue(all(d["bodies_logged"] is True for d in with_body))

    def test_flag_without_logged_bodies_ships_nothing(self):
        shutil.copy(SAMPLE, self.log_path)
        self.exporter(ship_bodies=True).run()
        self.assertFalse(any("body" in d for d in self.es.docs.values()))


class SecretsTests(Base):
    def capture(self, fn):
        buf = io.StringIO()
        handler = logging.StreamHandler(buf)
        lg = logging.getLogger("hermes-atp-export")
        lg.addHandler(handler)
        lg.setLevel(logging.DEBUG)
        out, err = io.StringIO(), io.StringIO()
        try:
            with redirect_stdout(out), redirect_stderr(err):
                fn()
        finally:
            lg.removeHandler(handler)
        return buf.getvalue() + out.getvalue() + err.getvalue()

    def assert_clean(self, text):
        for secret in (PASSWORD, API_KEY, AUTH, AUTH.split()[1]):
            self.assertNotIn(secret, text)
        self.assertNotIn("Authorization", text)

    def test_logs_never_contain_credentials(self):
        shutil.copy(SAMPLE, self.log_path)
        self.es.fail_http = 1

        def go():
            self.exporter().run()
            self.es.auth = "x"
            try:
                self.write(lines_of(SAMPLE)[0], mode="ab")
                self.exporter().run()
            except hx.FatalError as e:
                logging.getLogger("hermes-atp-export").error("%s", e)
        self.assert_clean(self.capture(go))

    def test_check_config_masks_secrets(self):
        cfgfile = os.path.join(self.tmp, "export.json")
        keyfile = os.path.join(self.tmp, "api_key")
        self.write(API_KEY.encode() + b"\n", path=keyfile)
        with open(cfgfile, "w") as f:
            json.dump({"es_url": "https://es.example:9200", "api_key_file": keyfile, "pub": PUB}, f)
        out = self.capture(lambda: hx.main(["--config", cfgfile, "--check-config"]))
        self.assert_clean(out)
        self.assertIn('"api_key": "(set)"', out)

    def test_config_error_messages_do_not_echo_secrets(self):
        out = self.capture(lambda: hx.main(["--config", "/nonexistent"]))
        self.assert_clean(out)
        os.environ["HERMES_ES_PASSWORD"] = PASSWORD
        try:
            out = self.capture(lambda: hx.main(["--check-config"]))
        finally:
            del os.environ["HERMES_ES_PASSWORD"]
        self.assert_clean(out)

    def test_api_key_header(self):
        cfg = hx.Config.load(env={}, overrides={"es_url": "https://x:9200", "api_key": API_KEY, "pub": PUB})
        self.assertEqual(cfg.auth_header(), "ApiKey " + API_KEY)


class ConfigTests(unittest.TestCase):
    def load(self, **kw):
        base = {"es_url": "https://es:9200", "username": "u", "password": "p", "pub": PUB}
        base.update(kw)
        return hx.Config.load(env={}, overrides=base)

    def test_push_is_default(self):
        self.assertEqual(self.load().mode, "push")

    def test_plain_http_refused_unless_allowed(self):
        with self.assertRaises(hx.ConfigError):
            self.load(es_url="http://es:9200")
        self.assertTrue(self.load(es_url="http://es:9200", allow_insecure_http=True))

    def test_credentials_required_and_exclusive(self):
        with self.assertRaises(hx.ConfigError):
            hx.Config.load(env={}, overrides={"es_url": "https://es:9200", "pub": PUB})
        with self.assertRaises(hx.ConfigError):
            self.load(api_key="k")

    def test_env_and_file_precedence(self):
        with tempfile.TemporaryDirectory() as d:
            pw = os.path.join(d, "pw")
            with open(pw, "w") as f:
                f.write("from-file\n")
            cfgfile = os.path.join(d, "c.json")
            with open(cfgfile, "w") as f:
                json.dump({"es_url": "https://file:9200", "username": "u", "password_file": pw, "pub": PUB,
                           "batch_size": 7}, f)
            cfg = hx.Config.load(cfgfile, env={"HERMES_ES_URL": "https://env:9200"})
            self.assertEqual(cfg.es_url, "https://env:9200")
            self.assertEqual(cfg.password, "from-file")
            self.assertEqual(cfg.batch_size, 7)
            with open(cfgfile, "w") as f:
                json.dump({"es_url": "https://x:9200", "bogus": 1}, f)
            with self.assertRaises(hx.ConfigError):
                hx.Config.load(cfgfile, env={})

    def test_no_tls_verification_bypass_exists(self):
        src = slurp(os.path.join(ELASTIC, "exporter", "hermes_atp_export.py"))
        self.assertNotIn("CERT_NONE", src)
        self.assertNotIn("check_hostname = False", src)
        self.assertNotIn("_create_unverified_context", src)


@unittest.skipUnless(shutil.which("openssl"), "openssl CLI not available")
class TLSTests(Base):
    def setUp(self):
        super().setUp()
        d = self.tmp
        run = lambda *a: subprocess.run(a, check=True, capture_output=True, cwd=d)  # noqa: E731
        run("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", "ca.key", "-out", "ca.crt",
            "-days", "2", "-subj", "/CN=test-ca", "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign")
        run("openssl", "req", "-newkey", "rsa:2048", "-nodes", "-keyout", "es.key", "-out", "es.csr",
            "-subj", "/CN=localhost")
        with open(os.path.join(d, "ext"), "w") as f:
            f.write("subjectAltName=IP:127.0.0.1,DNS:localhost\n")
        run("openssl", "x509", "-req", "-in", "es.csr", "-CA", "ca.crt", "-CAkey", "ca.key", "-CAcreateserial",
            "-out", "es.crt", "-days", "2", "-extfile", "ext")
        self.tls = FakeES(auth=AUTH, certfile=os.path.join(d, "es.crt"), keyfile=os.path.join(d, "es.key")).__enter__()
        self.addCleanup(self.tls.__exit__)

    def test_https_with_ca_cert(self):
        shutil.copy(SAMPLE, self.log_path)
        self.exporter(es_url=self.tls.url, allow_insecure_http=False, ca_cert=os.path.join(self.tmp, "ca.crt")).run()
        self.assertEqual(len(self.tls.docs), len(lines_of(SAMPLE)))

    def test_untrusted_certificate_is_fatal(self):
        shutil.copy(SAMPLE, self.log_path)
        with self.assertRaises(hx.FatalError) as cm:
            self.exporter(es_url=self.tls.url, allow_insecure_http=False).run()
        self.assertIn("ca_cert", str(cm.exception))
        self.assertEqual(self.tls.docs, {})


class IncrementalVerifyTests(Base):
    """The per-document verify status agrees with `atp_cli.py verify` (atp_log.verify_log)."""

    def test_matches_verify_log_on_every_vector(self):
        pub = slurp(PUB)
        cases = sorted(glob.glob(os.path.join(VECTORS, "cases", "*.jsonl"))) + [SAMPLE]
        for path in cases:
            with self.subTest(case=os.path.basename(path)):
                es = FakeES(auth=AUTH).__enter__()
                try:
                    tmp = tempfile.mkdtemp(dir=self.tmp)
                    log_path = os.path.join(tmp, "r.jsonl")
                    shutil.copy(path, log_path)
                    hx.Exporter(self.cfg(es_url=es.url, logs=[log_path], state_dir=os.path.join(tmp, "s")),
                                sleep=lambda s: None).run()
                    docs = {d["source"]["line"]: d for d in es.docs.values()}
                finally:
                    es.__exit__()
                data = slurp(path, "rb")
                complete = data.count(b"\n")  # the exporter ships complete lines only
                ref = atp_log.verify_log(data, atp_log.load_public_key(pub))
                ref_lines = {}
                for e in ref["errors"]:
                    if 0 < e["line"] <= complete:
                        ref_lines.setdefault(e["line"], []).append(e["kind"])
                bad = {ln: [x["kind"] for x in d["verify"]["errors"]] for ln, d in docs.items()
                       if not d["verify"]["valid"]}
                self.assertEqual(sorted(bad), sorted(ref_lines))
                norm = lambda ks: sorted("gap" if k in ("missing", "out-of-order") else k for k in ks)  # noqa: E731
                for ln in bad:
                    self.assertEqual(norm(bad[ln]), norm(ref_lines[ln]), f"line {ln}")
                self.assertEqual(len(docs), complete)


if __name__ == "__main__":
    unittest.main()
