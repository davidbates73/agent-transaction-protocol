"""Shared helpers for the Hermes recorder tests and vector generator."""

import hashlib
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
IMPL = os.path.dirname(PLUGIN)
PY_IMPL = os.path.join(IMPL, "python")
NODE_CLI = os.path.join(IMPL, "node", "bin", "atp.js")
PY_CLI = os.path.join(PY_IMPL, "atp_cli.py")
VECTORS = os.path.join(PLUGIN, "vectors")

for p in (HERE, PY_IMPL):
    if p not in sys.path:
        sys.path.insert(0, p)

import fake_hermes  # noqa: E402

# Dedicated TEST-ONLY key (never the default): sha256("atp-hermes-recorder TEST ONLY key").
TEST_SEED_HEX = hashlib.sha256(b"atp-hermes-recorder TEST ONLY key").hexdigest()
VECTOR_EPOCH = 1794787200  # 2026-11-16T00:00:00Z; the fake clock ticks 1 s per record
VECTOR_HERMES_VERSION = "0.23.1"


class TickClock:
    def __init__(self, start=VECTOR_EPOCH):
        self.t = start - 1

    def __call__(self):
        self.t += 1
        return self.t


def recorder_module(plugin_mod):
    import importlib
    return importlib.import_module(plugin_mod.__name__ + ".recorder")


def run_fake_session(env, *, clock=None, hermes_version=None, crash=False, secrets=True):
    """Load the plugin into a FakeHermes with a recorder built from env; run the scripted session."""
    mod = fake_hermes.load_plugin(PLUGIN)
    rec_mod = recorder_module(mod)
    rec = rec_mod.Recorder(rec_mod.Config.from_environment(env), clock=clock, hermes_version=hermes_version)
    mod._recorder = rec
    host = fake_hermes.FakeHermes()
    host.load(mod)
    out = host.run_scripted_session(secrets=secrets)
    if crash:
        host.run_crash_session()
    return host, rec, mod, out


# -- tampering (operate on raw bytes; each changes exactly what its name says) --------

def lines_of(data: bytes):
    return data.split(b"\n")[:-1] if data.endswith(b"\n") else data.split(b"\n")


def join(lines):
    return b"".join(l + b"\n" for l in lines)


def edit_one_byte(data: bytes, line_index: int) -> bytes:
    """Change ONE hex digit of the first payload hash on the given line (JSON stays valid)."""
    lines = lines_of(data)
    line = lines[line_index]
    m = re.search(rb"sha256:([0-9a-f])", line)
    i = m.start(1)
    new = b"0" if line[i:i + 1] != b"0" else b"1"
    lines[line_index] = line[:i] + new + line[i + 1:]
    out = join(lines)
    assert sum(a != b for a, b in zip(out, data)) == 1 and len(out) == len(data)
    return out


def delete_line(data: bytes, line_index: int) -> bytes:
    lines = lines_of(data)
    del lines[line_index]
    return join(lines)


def swap_lines(data: bytes, i: int, j: int) -> bytes:
    lines = lines_of(data)
    lines[i], lines[j] = lines[j], lines[i]
    return join(lines)


def duplicate_line(data: bytes, i: int) -> bytes:
    lines = lines_of(data)
    lines.insert(i + 1, lines[i])
    return join(lines)


def insert_line(data: bytes, i: int, raw: bytes) -> bytes:
    lines = lines_of(data)
    lines.insert(i, raw)
    return join(lines)
