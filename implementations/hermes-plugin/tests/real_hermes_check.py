"""Integration check against a REAL Hermes Agent install (not the fake harness).

Run with the Python interpreter of a Hermes environment, e.g.

    HERMES_REAL_PYTHON=/path/to/hermes/venv/bin/python
    $HERMES_REAL_PYTHON tests/real_hermes_check.py

What it does, all inside a throwaway HERMES_HOME:
  1. copies this plugin to $HERMES_HOME/plugins/atp-recorder and enables it in config.yaml;
  2. loads it through Hermes' own PluginManager discovery;
  3. fires pre/post_api_request and api_request_error through hermes_cli.lifecycle.invoke_hook
     with the kwargs the v0.23.1 call sites pass;
  4. runs a real tool (read_file) through model_tools.handle_function_call, so pre_tool_call /
     post_tool_call go through Hermes' real directive resolution;
  5. repeats the tool call with the recorder forced to fail (log path is a directory) and checks
     the tool is NOT blocked and its result is unchanged;
  6. prints a JSON summary; exit 0 only if every check passed.
"""

import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)


def main():
    home = tempfile.mkdtemp(prefix="hermes-real-")
    os.environ["HERMES_HOME"] = home
    os.environ.pop("HERMES_ATP_LOG", None)
    os.makedirs(os.path.join(home, "plugins"))
    shutil.copytree(PLUGIN, os.path.join(home, "plugins", "atp-recorder"),
                    ignore=shutil.ignore_patterns("__pycache__", "tests", "vectors", "tools"))
    with open(os.path.join(home, "config.yaml"), "w") as f:
        f.write("plugins:\n  enabled:\n    - atp-recorder\n")
    checks = {}

    import hermes_cli.plugins as hp
    from hermes_cli import lifecycle
    try:
        hp._reset_plugin_managers_for_tests()
    except Exception:
        pass
    hp.discover_plugins(force=True)
    mgr = hp.get_plugin_manager()
    names = sorted(getattr(mgr, "_plugins", {}).keys()) if hasattr(mgr, "_plugins") else []
    checks["plugin_loaded"] = all(lifecycle.has_hook(h) for h in (
        "pre_api_request", "post_api_request", "api_request_error", "pre_tool_call", "post_tool_call"))

    sid, turn = "real-sess-1", "real-turn-1"
    common = dict(task_id=sid, turn_id=turn, session_id=sid, platform="cli", model="grok-4", provider="xai",
                  base_url="https://api.x.ai/v1", api_mode="chat_completions")
    req = {"model": "grok-4", "messages": [{"role": "user", "content": "hi"}]}
    r1 = lifecycle.invoke_hook("pre_api_request", **common, api_request_id="r1", api_call_count=1, retry_count=0,
                               user_message="hi", conversation_history=[], request_messages=req["messages"],
                               system_prompt="sys", message_count=1, tool_count=1, approx_input_tokens=3,
                               request_char_count=2, max_tokens=100, started_at=1.0, middleware_trace=[],
                               request=req)
    r2 = lifecycle.invoke_hook("api_request_error", **common, api_request_id="r1", api_call_count=1,
                               api_duration=0.1, started_at=1.0, ended_at=1.1, status_code=429, retry_count=0,
                               max_retries=3, retryable=True, reason="rate_limit",
                               error={"type": "RateLimitError", "message": "slow down"}, request=req)
    r3 = lifecycle.invoke_hook("pre_api_request", **common, api_request_id="r2", api_call_count=2, retry_count=1,
                               request=req, started_at=2.0)
    r4 = lifecycle.invoke_hook("post_api_request", **common, api_request_id="r2", api_call_count=2,
                               api_duration=0.2, started_at=2.0, ended_at=2.2, first_chunk_at=None,
                               finish_reason="tool_calls", message_count=2, response_model="grok-4",
                               response={"content": "calling read_file"}, usage={"prompt_tokens": 3},
                               context_length=None, assistant_message=None, assistant_content_chars=0,
                               assistant_tool_call_count=1, moa_references=None)
    checks["api_hooks_return_nothing"] = (r1, r2, r3, r4) == ([], [], [], [])

    import model_tools
    target = os.path.join(home, "hello.txt")
    with open(target, "w") as f:
        f.write("hello from the real hermes check\n")
    out1 = model_tools.handle_function_call("read_file", {"path": target}, task_id=sid, tool_call_id="tc-1",
                                            session_id=sid, turn_id=turn, api_request_id="r2")
    checks["tool_ran_with_recorder"] = "hello from the real hermes check" in out1

    log = os.path.join(home, "atp", "records.jsonl")
    with open(log, encoding="utf-8") as f:
        rows = [json.loads(line) for line in f]
    events = [r["hermes"]["event"] for r in rows]
    checks["records_written"] = events == ["pre_api_request", "api_request_error", "pre_api_request",
                                           "post_api_request", "pre_tool_call", "post_tool_call"]
    checks["schema_is_observer_v1"] = all(r["hermes"].get("schema") == "hermes.observer.v1" for r in rows)
    checks["tool_ids_recorded"] = [r["hermes"].get("toolCallId") for r in rows[4:]] == ["tc-1", "tc-1"]
    checks["key_mode_600"] = (os.stat(os.path.join(home, "atp", "key")).st_mode & 0o777) == 0o600

    # Force a recorder failure: the log path becomes a directory.
    os.environ["HERMES_ATP_LOG"] = os.path.join(home, "not-a-file")
    os.makedirs(os.environ["HERMES_ATP_LOG"])
    plugin_mod = [m for k, m in sys.modules.items() if k.endswith("atp-recorder") or k.endswith("atp_recorder")]
    for m in plugin_mod:
        if hasattr(m, "_recorder"):
            m._recorder = None  # re-read config on next event
    target2 = os.path.join(home, "hello2.txt")
    with open(target2, "w") as f:
        f.write("second file, recorder broken\n")
    out2 = model_tools.handle_function_call("read_file", {"path": target2}, task_id=sid, tool_call_id="tc-2",
                                            session_id=sid, turn_id=turn, api_request_id="r2")
    checks["tool_not_blocked_when_recorder_fails"] = "second file, recorder broken" in out2 and "block" not in out2
    checks["recorder_really_failed"] = os.listdir(os.environ["HERMES_ATP_LOG"]) == [] and \
        sum(1 for _ in open(log, encoding="utf-8")) == len(rows)
    checks["plugin_module_found"] = bool(plugin_mod)

    import importlib.metadata as md
    summary = {"hermes_src": os.path.dirname(model_tools.__file__), "home": home, "log": log,
               "plugins_seen": names, "checks": checks, "records": len(rows)}
    print(json.dumps(summary, indent=2))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
