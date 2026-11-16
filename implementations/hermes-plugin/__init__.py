"""ATP recorder plugin for Hermes Agent (observe only).

Registers the five observer hooks and turns every event into one signed ATP
record. Also observes subagent_start, which writes no record of its own: it only
links the child session's first record to its parent session. Every callback is wrapped so that it ALWAYS returns None and never
raises: a pre_tool_call callback that raises (or returns a directive) would
block or modify the tool call in Hermes, and this plugin must not change
runtime behaviour.
"""

import logging

logger = logging.getLogger("atp_recorder")

HOOKS = ("pre_api_request", "post_api_request", "api_request_error", "pre_tool_call", "post_tool_call")
LINK_HOOKS = ("subagent_start",)

_recorder = None


def _get_recorder():
    global _recorder
    if _recorder is None:
        from .recorder import Recorder  # lazy: keep register() cheap and import-safe
        _recorder = Recorder.from_environment()
    return _recorder


def _make_callback(event):
    def callback(*args, **kwargs):
        try:
            if event in LINK_HOOKS:
                _get_recorder().note_subagent(kwargs)
            else:
                _get_recorder().record(event, kwargs)
        except Exception:  # noqa: BLE001 -- observe only: never propagate
            try:
                logger.warning("atp-recorder: failed to record %s", event, exc_info=True)
            except Exception:  # noqa: BLE001
                pass
        return None

    callback.__name__ = f"atp_recorder_{event}"
    return callback


def register(ctx):
    try:
        for event in HOOKS + LINK_HOOKS:
            ctx.register_hook(event, _make_callback(event))
    except Exception:  # noqa: BLE001
        try:
            logger.warning("atp-recorder: hook registration failed", exc_info=True)
        except Exception:  # noqa: BLE001
            pass
