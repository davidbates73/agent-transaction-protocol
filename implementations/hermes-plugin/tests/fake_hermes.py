"""A fake Hermes Agent host for tests (David's real Hermes runs on his own machine).

It mimics the documented plugin contract (hermes.observer.v1):
  * plugins are imported as a package and register(ctx) is called;
  * ctx.register_hook(name, cb) only accepts known hook names;
  * every callback receives keyword arguments plus telemetry_schema_version;
  * callback exceptions are caught and logged (fail-open) -- EXCEPT pre_tool_call,
    where a raising callback blocks the tool (fail-closed), as the Hermes docs state;
  * pre_tool_call directive returns ({"action": "block"|"approve"|"modify"}) are honoured.

The scripted session exercises API requests, tool calls, an API error with retry,
and a delegated subagent session.
"""

from __future__ import annotations

import importlib.util
import itertools
import os
import sys

VALID_HOOKS = {
    "pre_tool_call", "post_tool_call", "pre_llm_call", "post_llm_call", "pre_api_request",
    "post_api_request", "api_request_error", "pre_auxiliary_call", "post_auxiliary_call",
    "on_session_start", "on_session_end", "subagent_start", "subagent_stop",
}
SCHEMA = "hermes.observer.v1"

FAKE_API_KEY = "sk-FAKE-hermes-test-key-0123456789abcdef"
FAKE_AUTH = "Bearer FAKE-auth-token-abcdef0123456789"

_counter = itertools.count()


def load_plugin(plugin_dir: str):
    name = f"hermes_plugins_atp_recorder_{next(_counter)}"
    spec = importlib.util.spec_from_file_location(
        name, os.path.join(plugin_dir, "__init__.py"), submodule_search_locations=[plugin_dir])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


class FakeContext:
    def __init__(self, host):
        self.host = host

    def register_hook(self, name, cb):
        if name not in VALID_HOOKS:
            raise ValueError(f"unknown hook {name}")
        self.host.hooks.setdefault(name, []).append(cb)


class FakeHermes:
    def __init__(self):
        self.hooks = {}
        self.fired = []          # (hook, kwargs) in order
        self.callback_errors = []
        self.returns = []        # non-None returns seen, per hook
        self.blocked_tools = []
        self.modified_tools = []

    def load(self, module):
        module.register(FakeContext(self))

    def invoke(self, hook, **kw):
        kw = dict(kw, telemetry_schema_version=SCHEMA)
        self.fired.append((hook, kw))
        blocked = False
        for cb in self.hooks.get(hook, []):
            try:
                r = cb(**kw)
            except Exception as e:  # Hermes: logged, skipped
                self.callback_errors.append((hook, repr(e)))
                if hook == "pre_tool_call":
                    blocked = True  # documented fail-closed behaviour
                continue
            if r is not None:
                self.returns.append((hook, r))
                if hook == "pre_tool_call" and isinstance(r, dict):
                    if r.get("action") in ("block", "approve"):
                        blocked = True
                    elif r.get("action") == "modify":
                        self.modified_tools.append(kw.get("tool_call_id"))
        if blocked:
            self.blocked_tools.append(kw.get("tool_call_id"))
        return blocked

    # ------------------------------------------------------------------
    def api_call(self, sid, turn, rid, n, *, fail=None, tool_calls=0, task=None, parent=None, secrets=True):
        common = dict(session_id=sid, task_id=task or sid, turn_id=turn, api_request_id=rid, api_call_count=n,
                      platform="cli", model="grok-4", provider="xai", api_mode="chat_completions",
                      base_url="https://user:pw@api.x.ai/v1?key=" + FAKE_API_KEY)
        if parent:
            common["parent_session_id"] = parent
        request = {"model": "grok-4", "messages": [{"role": "user", "content": f"turn {turn} attempt {n}"}]}
        extra = {}
        if secrets:  # things a careless host could pass; none may reach the log
            request["headers"] = {"Authorization": FAKE_AUTH}
            request["api_key"] = FAKE_API_KEY
            extra = dict(api_key=FAKE_API_KEY, headers={"Authorization": FAKE_AUTH}, authorization=FAKE_AUTH,
                         request_messages=[{"role": "system", "content": "key=" + FAKE_API_KEY}])
        self.invoke("pre_api_request", **common, **extra, request=request, message_count=1, tool_count=3,
                    approx_input_tokens=12, request_char_count=40, max_tokens=1024, started_at=1.0)
        if fail:
            self.invoke("api_request_error", **common, **extra, api_duration=0.25, started_at=1.0, ended_at=1.25,
                        status_code=fail, retry_count=0, max_retries=3, retryable=True, reason="rate_limit",
                        error={"type": "RateLimitError", "message": "429 for key " + FAKE_API_KEY}, request=request)
            return
        response = {"choices": [{"message": {"role": "assistant", "content": f"reply {rid}",
                                             "tool_calls": [{"id": f"tc-{rid}-{i}"} for i in range(tool_calls)]}}]}
        self.invoke("post_api_request", **common, **extra, api_duration=0.5, started_at=1.0, ended_at=1.5,
                    finish_reason="tool_calls" if tool_calls else "stop", message_count=2, response_model="grok-4",
                    response=response, usage={"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19},
                    assistant_message=response["choices"][0]["message"], assistant_content_chars=10,
                    assistant_tool_call_count=tool_calls)

    def tool_call(self, sid, turn, rid, cid, name, args, result, *, status="ok", run=None, task=None, post=True):
        common = dict(session_id=sid, task_id=task or sid, conversation_id=sid, turn_id=turn,
                      api_request_id=rid, tool_call_id=cid, tool_name=name, args=args)
        blocked = self.invoke("pre_tool_call", **common)
        if run and not blocked:
            run()
        if not post:
            return blocked  # simulated crash between pre and post
        if blocked:
            status, result = "blocked", '{"error": "blocked"}'
        self.invoke("post_tool_call", **common, result=result, duration_ms=12, status=status,
                    error_type=None if status == "ok" else "ToolError",
                    error_message=None if status == "ok" else "failed with " + FAKE_API_KEY)
        return blocked

    def subagent_start(self, parent_sid, parent_turn, child_sid, child_subagent_id, *, parent_subagent_id=None,
                       goal="", secrets=True):
        """subagent_start as Hermes v0.23.1 fires it (tools/delegate_tool.py, before the child runs)."""
        self.invoke("subagent_start", parent_session_id=parent_sid, parent_turn_id=parent_turn,
                    parent_subagent_id=parent_subagent_id, child_session_id=child_sid,
                    child_subagent_id=child_subagent_id, child_role="leaf",
                    child_goal=goal + (" (use key " + FAKE_API_KEY + ")" if secrets else ""))

    def subagent_stop(self, parent_sid, parent_turn, child_sid):
        self.invoke("subagent_stop", parent_session_id=parent_sid, parent_turn_id=parent_turn,
                    child_session_id=child_sid, child_role="leaf", child_summary="done", child_status="completed",
                    tool_call_history=[], duration_ms=40)

    def run_real_subagent_session(self, *, secrets=True, sid="sess-main", child="sess-child",
                                  grandchild="sess-grandchild"):
        """v0.23.1-shaped delegation: parent -> child -> grandchild. No parent_session_id on the five
        hooks; the link only arrives via subagent_start. Child task_id is its subagent id (as real)."""
        turn = "turn-1"
        self.api_call(sid, turn, "req-1", 1, tool_calls=1, secrets=secrets)
        ct, gt = "child-turn-1", "grandchild-turn-1"

        def grandchild_run():
            self.subagent_start(child, ct, grandchild, "sa-1-test", parent_subagent_id="sa-0-test",
                                goal="count the lines", secrets=secrets)
            self.api_call(grandchild, gt, "greq-1", 1, task="sa-1-test", secrets=secrets)
            self.subagent_stop(child, ct, grandchild)

        def child_run():
            self.subagent_start(sid, turn, child, "sa-0-test", goal="read the README", secrets=secrets)
            self.api_call(child, ct, "creq-1", 1, tool_calls=1, task="sa-0-test", secrets=secrets)
            self.tool_call(child, ct, "creq-1", "ccall-1", "delegate_task", {"goal": "count the lines"},
                           '{"summary": "3 lines"}', run=grandchild_run, task="sa-0-test")
            self.api_call(child, ct, "creq-2", 2, task="sa-0-test", secrets=secrets)
            self.subagent_stop(sid, turn, child)

        self.tool_call(sid, turn, "req-1", "call-1", "delegate_task", {"goal": "read the README"},
                       '{"summary": "done"}', run=child_run)
        self.api_call(sid, turn, "req-2", 2, secrets=secrets)
        return {"turn_completed": True}

    def run_scripted_session(self, *, secrets=True, sid="sess-main", child="sess-child"):
        """One user turn: API -> tool -> delegate (subagent) -> API error + retry -> final API."""
        turn = "turn-1"
        self.api_call(sid, turn, "req-1", 1, tool_calls=2, secrets=secrets)
        self.tool_call(sid, turn, "req-1", "call-1", "terminal", {"command": "ls", "password": "hunter2"},
                       '{"output": "README.md"}')

        def subagent():
            ct = "child-turn-1"
            self.api_call(child, ct, "creq-1", 1, tool_calls=1, parent=sid, secrets=secrets)
            self.tool_call(child, ct, "creq-1", "ccall-1", "read_file", {"path": "README.md"},
                           '{"content": "# ATP"}', status="error")
            self.api_call(child, ct, "creq-2", 2, parent=sid, secrets=secrets)

        self.tool_call(sid, turn, "req-1", "call-2", "delegate_task", {"goal": "read the README"},
                       '{"summary": "done"}', run=subagent)
        self.api_call(sid, turn, "req-2", 2, fail=429, secrets=secrets)
        self.api_call(sid, turn, "req-3", 3, secrets=secrets)
        return {"turn_completed": True}

    def run_crash_session(self, sid="sess-crash"):
        """A tool call whose post_tool_call never fires (emitter crash / hang)."""
        self.api_call(sid, "turn-1", "req-1", 1, tool_calls=1, secrets=False)
        self.tool_call(sid, "turn-1", "req-1", "call-1", "terminal", {"command": "sleep 999"}, None, post=False)
