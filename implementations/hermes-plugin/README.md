# ATP recorder for Hermes Agent (slice 1)

An **observe-only** [Hermes Agent](https://hermes-agent.nousresearch.com/) plugin that
turns every main-loop provider API request / response / error and every tool call into
one signed [ATP](../../working/draft-bates-atp-01.txt) record, appended to a local JSONL log.
`verify` in both reference implementations checks the log.

**Tested with Hermes Agent v0.23.1** (tag `v0.23.1`, commit `3b421578588243f920886e6fe79a6faa87ea026e`),
observer schema `hermes.observer.v1`. See [Compatibility](#compatibility).

Standard library only (Python ≥ 3.9; Hermes v0.23.1 itself needs ≥ 3.11).

## Install (one step)

```sh
hermes plugins install davidbates73/agent-transaction-protocol/implementations/hermes-plugin --enable
```

That clones only this folder into `~/.hermes/plugins/atp-recorder/` and adds `atp-recorder`
to `plugins.enabled` (Hermes plugins are opt-in). Start a new session (or restart the
gateway) for it to load. The shorthand resolves to the repository's default branch, so it
works once this directory is on `main`; to pin a reviewed commit add
`--ref <full 40-char sha>`. Before it is pushed, the same command works from a local
checkout: `hermes plugins install "file:///path/to/agent-transaction-protocol#implementations/hermes-plugin" --enable`
(Hermes warns about `file://` sources).

Check it: `hermes plugins doctor atp-recorder` (or `hermes plugins validate <dir>`).
Remove it: `hermes plugins remove atp-recorder`.

## What it does

| Hermes hook | ATP `action.type` | `inputHash` covers | `outputHash` covers |
|---|---|---|---|
| `pre_api_request` | `atp:request` | sanitized `request` | (absent, §16.2) |
| `post_api_request` | `atp:completion` | same as its request | sanitized `response` |
| `api_request_error` | `atp:failure` | sanitized `request` | structured `error` |
| `pre_tool_call` | `atp:request` | `args` | (absent) |
| `post_tool_call` | `atp:completion`, or `atp:failure` if `status` ≠ `ok` | `args` | `result` |

* **One record per hook event**; pre and post are separate records. A pre with no post
  is evidence of a crash or hang and is reported by `verify` as a WARNING.
* **Lineage:** `scope` is the Hermes `session_id`. A session's first record has
  `parents: []` and `hermes.seq: 0`; every later record has `parents[0]` = the previous
  record in that session and `hermes.seq` + 1. A completion also lists its request as a
  parent when the request is not already `parents[0]` (draft §16.2). Each subagent has its
  own session, so it starts its own chain.
* **Observe only:** each callback is wrapped in `try/except` and always returns `None`.
  This matters most for `pre_tool_call`: in Hermes a callback that raises, times out or
  returns a directive **blocks or modifies the tool call** (fail-closed). The recorder
  never does any of those; a recorder failure is logged as a warning and the record is
  lost, never the turn.
* **Signing:** Ed25519 over the nodeId, JCS canonicalization, `sha256:` payload hashes,
  using the vendored ATP Python reference implementation (`atp.py`, kept byte-identical to
  `implementations/python/atp.py` by a test). Note that the reference Ed25519 is pure
  Python and not constant-time (about 2–3 ms per record on a laptop), which is fine for a
  local audit log but is not a hardened signer.
* **Log:** append-only JSONL, opened `O_APPEND`, `flock`ed, written and `fsync`ed for each
  record, never rewritten. Writes are serialized by a lock because hooks can run
  concurrently. On startup the recorder reads the existing log so a resumed session
  continues its chain. If the last line was cut off by a crash, it appends a newline so
  that line stays visible as a tampered line rather than corrupting the next one.

## Configuration

Every setting is optional. Precedence: environment variable, then `config.json`, then the default.

| Setting | Env var | `config.json` key | Default |
|---|---|---|---|
| Recorder home | `HERMES_ATP_HOME` | n/a | `$HERMES_HOME/atp`, else `~/.hermes/atp` |
| Config file | `HERMES_ATP_CONFIG` | n/a | `<recorder home>/config.json` |
| Log path | `HERMES_ATP_LOG` | `log_path` | `<recorder home>/records.jsonl` |
| Signing key | `HERMES_ATP_KEY` | `key_path` | `<recorder home>/key` (generated on first run, mode 600) |
| Public key | `HERMES_ATP_PUB` | `pub_path` | `<key path>.pub` |
| Log full bodies | `HERMES_ATP_LOG_BODIES` | `log_bodies` | `false` |
| Issuer id | `HERMES_ATP_ISSUER` | `issuer_id` | `hermes-atp-recorder.local` |

Example `~/.hermes/atp/config.json`:

```json
{ "log_path": "~/audit/hermes-atp.jsonl", "key_path": "~/.keys/hermes-atp", "log_bodies": false }
```

**Bring your own key:** set `key_path` to a file that holds either a 64-hex-character
Ed25519 seed or `{"seedHex": "..."}`, and `chmod 600` it. If `<key path>.pub` is missing,
the recorder writes it. The `keyId` in each record is `ed25519:` followed by the first
16 hex characters of sha256(public key).

## Verify

```sh
python3 implementations/python/atp_cli.py verify ~/.hermes/atp/records.jsonl --pub ~/.hermes/atp/key.pub [--json]
node    implementations/node/bin/atp.js   verify ~/.hermes/atp/records.jsonl --pub ~/.hermes/atp/key.pub [--json]
```

Both implementations print byte-identical output and exit 0 on pass, 2 on fail and 1 on a
usage or key error. Signatures are only checked against the `--pub` file you supply. The
`keyId` in the log is informational and is never used to find a key. Each problem is
reported with its 1-based line number:

* `tampered`: invalid JSON, a blank line, not a structurally valid ATP node, a nodeId that
  doesn't match the content, a bad signature, or a `parents[0]` that isn't the previous
  record in the session.
* `missing`: a gap in `hermes.seq`, for example after a deleted line.
* `out-of-order`: a seq that appears too early, or not after the previous one (swapped or
  duplicated lines).
* `WARNING unmatched-request`: a `pre_*` with no matching post or error (same session and
  `api_request_id` / `tool_call_id`). This is a warning, not a failure.

```json
{ "verdict": "fail", "records": 16, "sessions": 2,
  "errors": [ { "line": 13, "kind": "out-of-order", "reason": "session sess-main: expected seq 6, found 7; seq 6 appears later in the log" } ],
  "warnings": [] }
```

Limitations: order is checked within a session, not across sessions (ATP does not
require a global order, §7). Removing the last records of a session can't be detected
from the chain alone (it usually shows up as an unmatched-request warning). Detecting
that needs external anchoring such as a transparency log (§14.12), which is out of scope
for slice 1. The default `hermes-atp-recorder.local` issuer is self-asserted.

## What is logged, and what isn't

Fields are **allowlisted**, so nothing outside this list is ever written:
`event, seq, schema, sessionId, taskId, turnId, apiRequestId, apiCallCount, toolCallId,
toolName, parentSessionId, parentTurnId, parentSubagentId, conversationId, platform,
model, responseModel, provider, apiMode, baseUrl, status, statusCode, retryCount,
maxRetries, retryable, reason, errorType, finishReason, messageCount, toolCount,
durationMs, apiDurationMs, assistantToolCallCount, usage (token counts only),
inputLength, outputLength, hermesVersion and hermesCommit (on a session's first record),
requestNodeId, bodiesLogged, schemaMismatch`. Strings are capped at 256 characters.

* **Never logged:** API keys, `Authorization` and other headers, cookies, raw
  `request_messages`, `conversation_history`, `user_message`, `system_prompt`,
  `assistant_message`, `error_message`. `base_url` is reduced to scheme, host and path,
  so credentials and query strings are removed.
* **Bodies are off by default.** Prompts, responses, tool args and results are reduced to
  `sha256:` plus a byte length. Before hashing, the payload is JSON-normalized and keys
  that look sensitive (`authorization`, `api_key`, `token`, `password`, `headers`, …)
  are replaced with `[REDACTED]`. Strings are hashed as UTF-8 and everything else as JCS.
* With `log_bodies: true`, the redacted bodies are written as `hermes.inputBody` and
  `hermes.outputBody`, with an extra pass that scrubs `Bearer …` and `sk-…`/`xai-…`
  key patterns. Bodies can still contain personal or confidential data, so treat the log
  accordingly. Even short hashes can be guessable (§15.1).

## Profile fields

Records carry `profile: https://davidbates73.github.io/agent-transaction-protocol/atp/profile/hermes-recorder/0.1.0`.
The `hermes` object and `action.subtype` are fields defined by that profile (§20.3/§20.4),
and they are covered by the nodeId and signature. The profile is not yet a published
specification: this README is its working definition. A generic ATP `validate()` will
list these nodes under `profileUnresolved`, which is expected (§20.4 permissive mode).

## Tests

```sh
python3 -m unittest discover -s implementations/hermes-plugin/tests -p "test_*.py"   # also run by implementations/run-all.sh
```

* `tests/fake_hermes.py` is a fake Hermes host. It follows the documented contract,
  including fail-open callbacks, fail-closed `pre_tool_call` and directive returns. Its
  scripted session covers API calls, tool calls, a delegated subagent, a 429 API error
  with a retry, an errored tool and a crash session with no post. It also tries to leak a
  fake API key and an `Authorization` header.
* `tests/test_cross_impl.py` runs Node and Python `verify --json` on the same valid and
  tampered logs (edited byte, deleted line, swapped lines, …), checks the output is
  byte-identical, and replays `vectors/`.
* `tests/test_real_hermes.py` is opt-in and runs against a real Hermes install:
  `HERMES_REAL_PYTHON=/path/to/hermes/venv/bin/python python3 -m unittest discover -s implementations/hermes-plugin/tests -p test_real_hermes.py`.
  It loads the plugin through Hermes' own discovery, fires the API hooks through
  `hermes_cli.lifecycle.invoke_hook`, runs a real `read_file` through
  `model_tools.handle_function_call`, does it again with the recorder forced to fail
  (the tool must not be blocked), then verifies the log with both implementations.

## Test vectors

`vectors/hermes-log-vectors.json` lists each case's log, public key and expected verdict.
`vectors/sample-log.jsonl` is a valid sample log. Regenerate them with
`python3 tools/make_vectors.py`, which is deterministic; a test checks that the
checked-in files are current.

> **`vectors/TEST-ONLY-key.json` is a published test key.** Never sign real records with it.
> The recorder never uses it by default; it always generates its own key.

## Compatibility

Checked against Hermes **v0.23.1** source (`hermes_cli/plugins*.py`, `agent/turn_api_request.py`,
`agent/turn_response_intake.py`, `agent/api_request_hooks.py`, `model_tools.py`):

* All five hooks are in `VALID_HOOKS`. `telemetry_schema_version` is `hermes.observer.v1`.
  Callback exceptions and timeouts on `pre_tool_call` fail closed. `plugins.enabled`
  gates user plugins. `hermes plugins install owner/repo/subdir` installs only that
  subdirectory, under the manifest `name`.
* The call sites pass the fields in the current docs. v0.23.1 also passes some extra
  fields (`first_chunk_at`, `context_length`, `moa_references` on `post_api_request`;
  `system_prompt` on `pre_api_request`; `middleware_trace` on tool hooks). They are not
  allowlisted, so they are never logged.
* **None of the five hooks receives `parent_session_id` in v0.23.1.** It is only on
  `pre_llm_call` and `subagent_*`. Subagent records therefore form their own chain under
  the child `session_id`, but don't name their parent session. Linking them through
  `subagent_start` is planned for slice 2.
* Version detection uses `hermes_cli.version_info.get_version_info()`. Package metadata
  and `hermes_cli.__version__` both report a `0.0.0` placeholder on source installs.

Out of scope for slice 1: Elastic exporter, Kibana, outbound webhooks, auxiliary-call hooks.
