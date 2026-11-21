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


# --- root_session_id ---------------------------------------------------------------------

sys.path.insert(0, os.path.join(PLUGIN, "tests"))
import hermes_atp_restamp as hr  # noqa: E402


def record_sessions(log_path, script, *, start=None):
    """Record sessions with the REAL recorder (fake-Hermes host, TEST-ONLY key, so the exporter's
    PUB verifies them). script: [(session, parent or None), ...]; a child gets its link the way
    Hermes v0.23.1 delivers it (subagent_start, so only on the child's first record). Every
    session makes two model calls and a tool call."""
    import support as S
    import fake_hermes
    work = os.path.dirname(log_path)
    key = os.path.join(work, "TEST-ONLY-key.json")
    if not os.path.exists(key):
        with open(key, "w") as f:
            json.dump({"warning": "TEST ONLY", "alg": "Ed25519", "seedHex": S.TEST_SEED_HEX}, f)
        os.chmod(key, 0o600)
    env = {"HERMES_ATP_HOME": work, "HERMES_ATP_KEY": key, "HERMES_ATP_PUB": key + ".pub",
           "HERMES_ATP_LOG": log_path}
    mod = fake_hermes.load_plugin(S.PLUGIN)
    rec_mod = S.recorder_module(mod)
    mod._recorder = rec_mod.Recorder(rec_mod.Config.from_environment(env), clock=S.TickClock(start or S.VECTOR_EPOCH))
    host = fake_hermes.FakeHermes()
    host.load(mod)
    for sid, parent in script:
        if parent:
            host.subagent_start(parent, "turn-1", sid, "sa-" + sid, secrets=False)
        host.api_call(sid, "turn-1", "r1-" + sid, 1, tool_calls=1, task="sa-" + sid if parent else None,
                      secrets=False)
        host.tool_call(sid, "turn-1", "r1-" + sid, "c1-" + sid, "read_file", {"path": "x"}, '{"ok": 1}')
        host.api_call(sid, "turn-1", "r2-" + sid, 2, secrets=False)
    with open(log_path, "rb") as f:
        return f.read()


TREE = [("req-p", None), ("sub-a", "req-p"), ("sub-b", "req-p"), ("sub-a1", "sub-a"), ("sub-a1x", "sub-a1"),
        ("orphan-o", None), ("req-t", None)]
WANT = {"req-p": "req-p", "sub-a": "req-p", "sub-b": "req-p", "sub-a1": "req-p", "sub-a1x": "req-p",
        "orphan-o": "orphan-o", "req-t": "req-t"}


class RootSessionTests(Base):
    def docs(self, es=None):
        return {k[1]: d for k, d in (es or self.es).docs.items()}

    def roots(self, es=None):
        out = {}
        for d in self.docs(es).values():
            if d.get("session_id") is not None:
                out.setdefault(d["session_id"], set()).add(d.get("root_session_id"))
        return out

    def assert_roots(self, want, es=None):
        self.assertEqual(self.roots(es), {s: {r} for s, r in want.items()})

    def test_parent_with_two_subagents_nested_and_orphan(self):
        data = record_sessions(self.log_path, TREE)
        ex = self.exporter()
        ex.run()
        self.assertEqual(len(self.es.docs), data.count(b"\n"))
        self.assert_roots(WANT)
        # the link is only on the child's first record, yet every record of the child is stamped
        firsts = [d for d in self.docs().values() if d.get("parent_session_id")]
        self.assertEqual(sorted(d["session_id"] for d in firsts), ["sub-a", "sub-a1", "sub-a1x", "sub-b"])
        self.assertTrue(all(d["seq"] == 0 for d in firsts))
        self.assertEqual(ex.stats["restamp_pending"], 0)

    def test_export_time_only_records_ids_and_verify_unchanged(self):
        data = record_sessions(self.log_path, TREE)
        before = hashlib.sha256(data).hexdigest()
        self.exporter().run()
        self.assertEqual(hashlib.sha256(slurp(self.log_path, "rb")).hexdigest(), before, "log untouched")
        ref = atp_log.verify_log(data, atp_log.load_public_key(slurp(PUB)))
        self.assertEqual(ref["errors"], [], "the recorder's log still verifies")
        for doc_id, d in self.docs().items():
            self.assertEqual(doc_id, d["record_hash"], "_id is still the record hash")
            self.assertTrue(d["verify"]["valid"])

    def test_reexport_is_idempotent(self):
        data = record_sessions(self.log_path, TREE)
        self.exporter().run()
        first = {k: dict(v) for k, v in self.docs().items()}
        ex = self.exporter()
        ex.run()  # same state: nothing to send
        self.assertEqual(ex.stats["shipped"] + ex.stats["duplicates"], 0)
        shutil.rmtree(os.path.join(self.tmp, "state"))
        ex = self.exporter()
        ex.run()  # cleared state: everything again, all 409
        self.assertEqual((ex.stats["shipped"], ex.stats["duplicates"]), (0, data.count(b"\n")))
        os.replace(self.log_path, self.log_path + ".old")  # copy mode: a re-copy (new inode)
        shutil.copy(self.log_path + ".old", self.log_path)
        ex = self.exporter()
        ex.run()
        self.assertEqual(ex.stats["shipped"], 0)
        self.assertEqual(set(self.docs()), set(first))
        for k, d in self.docs().items():
            self.assertEqual(d["root_session_id"], first[k]["root_session_id"])
        # and a fresh index gets identical _ids and roots from a cleared state
        with FakeES(auth=AUTH) as es2:
            shutil.rmtree(os.path.join(self.tmp, "state"))
            self.exporter(es_url=es2.url).run()
            self.assertEqual(set(self.docs(es2)), set(first))
            self.assert_roots(WANT, es2)

    def split_logs(self):
        """log1: req-p with sub-a and sub-b. log2 (another Hermes home, same key): sub-a1 (child of
        sub-a, whose link is only in log1) and sub-a1x (child of sub-a1)."""
        d2 = os.path.join(self.tmp, "home2")
        os.makedirs(d2)
        log2 = os.path.join(d2, "records.jsonl")
        record_sessions(self.log_path, TREE[:3])
        record_sessions(log2, [("sub-a1", "sub-a"), ("sub-a1x", "sub-a1")], start=1794790000)
        return log2

    def test_child_log_before_parent_log_in_one_run(self):
        log2 = self.split_logs()
        ex = self.exporter(logs=[log2, self.log_path])  # the child's log is processed first
        ex.run()
        self.assert_roots({s: WANT[s] for s in ("req-p", "sub-a", "sub-b", "sub-a1", "sub-a1x")})
        self.assertEqual(ex.stats["restamp_pending"], 0)

    def test_parent_link_arriving_in_a_later_run_is_flagged_and_restamped(self):
        log2 = self.split_logs()
        ex = self.exporter(logs=[log2])  # copy mode: only the child log has arrived
        ex.run()
        self.assert_roots({"sub-a1": "sub-a", "sub-a1x": "sub-a"})  # best known: stops at sub-a
        ex = self.exporter(logs=[log2, self.log_path])  # parent log copied in later
        ex.run()
        self.assertEqual(ex.stats["restamp_pending"], 2)
        sm = hx.SessionMap(os.path.join(self.tmp, "state"))
        self.assertEqual(sm.restamp, {"sub-a1": "req-p", "sub-a1x": "req-p"})
        # the restamp tool's plan, from what is in the index (links + current roots) ...
        docs = list(self.docs().values())
        links = {(d["session_id"], d["parent_session_id"]) for d in docs
                 if d.get("parent_session_id") and d["verify"]["authentic"]}
        current = {(d["session_id"], d.get("root_session_id")) for d in docs}
        todo = hr.plan(links, current)
        self.assertEqual(todo, {"req-p": ["sub-a1", "sub-a1x"]})
        for d in docs:  # ... applied like its _update_by_query
            for root, sessions in todo.items():
                if d["session_id"] in sessions:
                    d["root_session_id"] = root
        self.assert_roots({s: WANT[s] for s in ("req-p", "sub-a", "sub-b", "sub-a1", "sub-a1x")})
        current = {(d["session_id"], d.get("root_session_id")) for d in docs}
        self.assertEqual(hr.plan(links, current), {}, "a second restamp changes nothing")
        # deterministic: same final state as one in-order run into a fresh index
        with FakeES(auth=AUTH) as es2:
            hx.Exporter(self.cfg(es_url=es2.url, logs=[self.log_path, log2],
                                 state_dir=os.path.join(self.tmp, "s2")), sleep=lambda s: None).run()
            self.assertEqual({k: d["root_session_id"] for k, d in self.docs(es2).items()},
                             {k: d["root_session_id"] for k, d in self.docs().items()})
        # --restamp-done clears the list
        cfgfile = os.path.join(self.tmp, "c.json")
        with open(cfgfile, "w") as f:
            json.dump({"es_url": self.es.url, "allow_insecure_http": True, "username": "hermes_exporter",
                       "password": PASSWORD, "pub": PUB, "state_dir": os.path.join(self.tmp, "state")}, f)
        with redirect_stdout(io.StringIO()) as out:
            self.assertEqual(hx.main(["--config", cfgfile, "--restamp-done"]), 0)
        self.assertIn("cleared 2", out.getvalue())
        self.assertEqual(hx.SessionMap(os.path.join(self.tmp, "state")).restamp, {})

    def test_session_map_persists_across_restarts(self):
        data = record_sessions(self.log_path, TREE)
        lines = data.splitlines(keepends=True)
        # cut right after sub-a's first record: its later records arrive after a restart
        cut = next(i for i, l in enumerate(lines) if b'"scope":"sub-a"' in l and b'"parentSessionId"' in l) + 1
        self.write(b"".join(lines[:cut]))
        self.exporter().run()
        state = os.path.join(self.tmp, "state", "sessions.json")
        self.assertEqual(os.stat(state).st_mode & 0o777, 0o600)
        self.assertEqual(json.loads(slurp(state))["parents"], {"sub-a": "req-p"})
        self.write(b"".join(lines))  # appended
        self.exporter().run()        # new process: the link comes from sessions.json only
        self.assert_roots(WANT)
        # a lost session map is relearned from the logs (already-shipped part included)
        os.remove(state)
        with open(self.log_path, "ab") as f:
            f.write(record_sessions(os.path.join(self.tmp, "more.jsonl"), [("sub-c", "req-p")],
                                    start=1794795000))
        self.exporter().run()
        roots = self.roots()
        self.assertEqual(roots["sub-c"], {"req-p"})
        self.assertEqual(json.loads(slurp(state))["parents"]["sub-a1x"], "sub-a1")

    def test_only_authentic_links_count_and_unparseable_lines_get_no_root(self):
        import support as S
        data = record_sessions(self.log_path, TREE[:2])
        lines = data.split(b"\n")[:-1]
        i = next(i for i, l in enumerate(lines) if b'"parentSessionId"' in l)
        forged = S.edit_one_byte(data, i)  # the child's first record no longer verifies
        self.write(forged + b"{not json\n")
        self.exporter().run()
        roots = self.roots()
        self.assertEqual(roots["req-p"], {"req-p"})
        self.assertEqual(roots["sub-a"], {"sub-a"}, "a forged link is not followed: own root")
        broken = [d for d in self.docs().values() if d.get("session_id") is None]
        self.assertEqual(len(broken), 1)
        self.assertNotIn("root_session_id", broken[0])

    def test_resolve_root_cycles_orphans_and_conflicts(self):
        cyc = {"a": "b", "b": "c", "c": "a", "d": "b", "s": "s"}
        for sid in ("a", "b", "c", "d"):
            self.assertEqual(hx.resolve_root(sid, cyc), "a", sid)
        self.assertEqual(hx.resolve_root("s", cyc), "s", "self link")
        self.assertEqual(hx.resolve_root("x", cyc), "x", "no link: own root")
        self.assertEqual(hx.resolve_root("k", {"k": "gone"}), "gone", "stops at the last known session")
        sm = hx.SessionMap(self.tmp)
        sm.learn("k", "p2")
        sm.learn("k", "p1")
        sm.learn("k", "p3")
        self.assertEqual(sm.parents, {"k": "p1"}, "two parents: the smaller id, whatever the order")
        self.assertEqual(hr.plan([("k", "p2"), ("k", "p1")], [("k", None), ("p1", "p1")]), {"p1": ["k"]})


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
                    es_docs = dict(es.docs)
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
                for (_idx, doc_id), d in es_docs.items():
                    self.assertEqual(d["anomaly"], doc_id.startswith("anomaly-"))
                    self.assertEqual(d["anomaly"], not d["verify"]["valid"], (path, doc_id))


if __name__ == "__main__":
    unittest.main()
