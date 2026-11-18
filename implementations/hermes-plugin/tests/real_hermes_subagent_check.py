"""Slice 2 integration check: a REAL subagent delegation through Hermes Agent's own code path.

Run with the Python interpreter of a Hermes SOURCE checkout (it needs Hermes' test fakes):

    HERMES_REAL_PYTHON=/path/to/hermes/venv/bin/python
    $HERMES_REAL_PYTHON tests/real_hermes_subagent_check.py

No LLM is involved. Hermes' own loopback provider (tests/fakes/fake_llm_provider.FakeLLMServer,
OpenAI Chat Completions wire format on 127.0.0.1) answers the model calls, so everything else
is real: run_agent.AIAgent.run_conversation -> the model asks for delegate_task -> Hermes'
real delegate_task builds a child AIAgent (tools/delegate_tool.py) -> real subagent_start
hook -> the child runs its own conversation loop (background delegation, the v0.23.1
default) -> real pre/post_api_request hooks in the child session. Prints a JSON summary;
exit 0 only if every check passed, exit 3 if this Hermes install has no test fakes (skip).
"""

import json
import os
import pathlib
import shutil
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.dirname(HERE)
PARENT_SID = "real-parent-1"
GOAL = "Say hello from the child"
SECRET = "sk-FAKE-goal-secret-0123456789abcdef"


def main():
    import hermes_cli
    root = os.path.dirname(os.path.dirname(os.path.abspath(hermes_cli.__file__)))
    sys.path.insert(0, root)
    try:
        from tests.fakes.fake_llm_provider import FakeLLMServer, Text, ToolCall, write_hermes_home
        from tests.e2e.core.history._helpers import NO_BACKGROUND_REVIEW, OFFLINE_CONFIG
    except Exception as e:  # noqa: BLE001
        print(json.dumps({"skipped": f"Hermes test fakes not importable from {root}: {e!r}"}))
        return 3

    home = pathlib.Path(tempfile.mkdtemp(prefix="hermes-real-sub-"))
    os.environ["HERMES_HOME"] = str(home)
    for k in [k for k in os.environ if k.startswith("HERMES_ATP_")]:
        os.environ.pop(k)
    shutil.copytree(PLUGIN, home / "plugins" / "atp-recorder",
                    ignore=shutil.ignore_patterns("__pycache__", "tests", "vectors", "tools"))
    checks = {}
    log = home / "atp" / "records.jsonl"

    script = [ToolCall("delegate_task", {"goal": f"{GOAL} (key {SECRET})"}), Text("first text"), Text("second text")]
    with FakeLLMServer(script) as srv:
        write_hermes_home(home, srv.base_url, extra_config=OFFLINE_CONFIG + NO_BACKGROUND_REVIEW
                          + "plugins:\n  enabled:\n    - atp-recorder\n")
        import hermes_cli.plugins as hp
        from hermes_cli import lifecycle
        hp.discover_plugins(force=True)
        checks["plugin_hooks_registered"] = all(lifecycle.has_hook(h) for h in (
            "pre_api_request", "post_api_request", "api_request_error", "pre_tool_call", "post_tool_call",
            "subagent_start"))

        from hermes_state import SessionDB
        from run_agent import AIAgent
        db = SessionDB(db_path=home / "state.db")
        db.ensure_session(PARENT_SID, source="cli")
        agent = AIAgent(provider="custom", base_url=srv.base_url, api_key="sk-fake-e2e", model="fake-model",
                        session_db=db, session_id=PARENT_SID, quiet_mode=True, platform="cli")
        out = agent.run_conversation("please delegate a greeting", conversation_history=[], task_id="t")
        checks["parent_turn_completed"] = bool(out.get("final_response"))

        # Background child: wait until its completion record lands.
        rows, deadline = [], time.time() + 90
        while time.time() < deadline:
            if log.exists():
                rows = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]
                if any(r["scope"] != PARENT_SID and r["hermes"]["event"] == "post_api_request" for r in rows):
                    break
            time.sleep(0.2)
        time.sleep(1.0)  # let subagent_stop / stragglers finish
        rows = [json.loads(l) for l in log.read_text(encoding="utf-8").splitlines() if l.strip()]
        agent.close()
        requests_seen = len(srv.main_requests())

    parent = [r for r in rows if r["scope"] == PARENT_SID]
    children = sorted({r["scope"] for r in rows} - {PARENT_SID})
    child = children[0] if len(children) == 1 else None
    crow = [r for r in rows if r["scope"] == child]
    checks["one_child_session"] = child is not None
    checks["parent_recorded_delegate_task"] = [r["hermes"]["event"] for r in parent
                                               if r["hermes"].get("toolName") == "delegate_task"] == [
        "pre_tool_call", "post_tool_call"]
    checks["child_api_records"] = [r["hermes"]["event"] for r in crow][:2] == ["pre_api_request", "post_api_request"]
    checks["child_is_platform_subagent"] = bool(crow) and crow[0]["hermes"].get("platform") == "subagent"
    first = crow[0]["hermes"] if crow else {}
    parent_turn = next((r["hermes"].get("turnId") for r in parent if r["hermes"].get("toolName") == "delegate_task"), None)
    checks["child_first_record_links_parent"] = first.get("seq") == 0 and first.get("parentSessionId") == PARENT_SID
    checks["child_first_record_parent_turn"] = parent_turn is not None and first.get("parentTurnId") == parent_turn
    checks["only_first_child_record_linked"] = all("parentSessionId" not in r["hermes"] for r in crow[1:])
    checks["parent_records_unlinked"] = all("parentSessionId" not in r["hermes"] for r in parent)
    checks["no_subagent_records"] = all(not r["hermes"]["event"].startswith("subagent") for r in rows)
    text = log.read_text(encoding="utf-8")
    checks["no_goal_or_secret_in_log"] = all(s not in text for s in (SECRET, GOAL, "sk-fake-e2e"))

    summary = {"hermes_src": root, "home": str(home), "log": str(log), "parent_session": PARENT_SID,
               "child_session": child, "parent_turn": parent_turn, "records": len(rows),
               "provider_requests": requests_seen,
               "events": [[r["scope"], r["hermes"]["seq"], r["hermes"]["event"]] for r in rows],
               "child_first_record": {k: first.get(k) for k in ("seq", "event", "parentSessionId", "parentTurnId",
                                                                 "parentSubagentId", "taskId", "platform",
                                                                 "hermesVersion")},
               "checks": checks}
    print(json.dumps(summary, indent=2))
    return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
