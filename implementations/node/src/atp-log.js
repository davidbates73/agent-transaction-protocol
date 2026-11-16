// Verify an append-only ATP JSONL record log (Hermes recorder, slice 1).
//
// One signed ATP node per line. Per-session lineage is carried by:
//   scope        -- the session (Hermes session_id)
//   parents[0]   -- nodeId of the previous record in the same scope ([] for the first)
//   hermes.seq   -- 0-based position of the record in its session (profile-defined field)
//
// When a record stores hermes.inputBody / hermes.outputBody (log_bodies on), the body must
// re-hash to action.inputHash / action.outputHash and match hermes.*Length ("body-mismatch").
// An empty log (no records) is a failure ("empty-log", line 0).
//
// Subagent links (slice 2): an intact first record (seq 0) carrying hermes.parentSessionId
// links that session to its parent; every link is listed in "links" (in line order). A link
// whose parent session has no record in the log is a WARNING ("missing-parent-session").
//
// Known limitation: deleting records from the END of a session (tail truncation) leaves a valid
// chain and cannot be detected from the log alone; it only shows up when it orphans a request
// (an "unmatched-request" warning). Anchor the last nodeId externally if that matters.
//
// The verdict format is shared byte-for-byte with implementations/python/atp_log.py;
// any change here MUST be mirrored there (the cross-implementation test diffs both).

import { createHash } from 'node:crypto';
import { parseAtpJson, structuralError, computeNodeId, verifyNodeId, publicKeyFromRaw, canonicalize } from './atp.js';

const HEX64 = /^[0-9a-f]{64}$/;
const BLANK = /^[ \t\r]*$/;
const MAX_SAFE = Number.MAX_SAFE_INTEGER;
const PRE_EVENTS = { pre_api_request: 'api', pre_tool_call: 'tool' };
const POST_EVENTS = { post_api_request: 'api', api_request_error: 'api', post_tool_call: 'tool' };
const PAIR_NAME = { pre_api_request: 'post_api_request or api_request_error', pre_tool_call: 'post_tool_call' };
const own = (o, k) => Object.prototype.hasOwnProperty.call(o, k);
const isObj = (v) => v !== null && typeof v === 'object' && !Array.isArray(v);

/** A .pub file is the base64 of the raw 32-byte Ed25519 public key (whitespace ignored). */
export function loadPublicKey(text) {
  const s = text.replace(/\s+/g, '');
  if (!/^[A-Za-z0-9+/]*={0,2}$/.test(s) || s.length % 4 !== 0) throw new Error('public key file is not base64');
  const raw = Buffer.from(s, 'base64');
  if (raw.length !== 32) throw new Error('public key must be 32 bytes');
  return raw;
}

function seqOf(node) {
  const h = isObj(node) ? node.hermes : undefined;
  if (!isObj(h)) return null;
  const v = h.seq;
  if (typeof v !== 'number' || !Number.isInteger(v) || v < 0 || v > MAX_SAFE) return null;
  return v;
}

function strField(node, key) {
  const h = node.hermes;
  return isObj(h) && typeof h[key] === 'string' && h[key] !== '' ? h[key] : null;
}
const eventOf = (node) => strField(node, 'event');
const corrOf = (node, fam) => strField(node, fam === 'api' ? 'apiRequestId' : 'toolCallId');

/** Reason text when a stored hermes.<which>Body disagrees with its hash/length, else null. */
function bodyProblem(node, which) {
  const h = node.hermes;
  if (!isObj(h) || !own(h, which + 'Body')) return null;
  const action = isObj(node.action) ? node.action : {};
  const want = action[which + 'Hash'];
  if (typeof want !== 'string') return `hermes.${which}Body present but action.${which}Hash is missing`;
  const body = h[which + 'Body'];
  let data;
  try {
    data = Buffer.from(typeof body === 'string' ? body : canonicalize(body), 'utf8');
  } catch {
    return `hermes.${which}Body cannot be canonicalized`;
  }
  if ('sha256:' + createHash('sha256').update(data).digest('hex') !== want) {
    return `hermes.${which}Body does not match action.${which}Hash`;
  }
  if (own(h, which + 'Length')) {
    const n = h[which + 'Length'];
    if (typeof n !== 'number' || n !== data.length) return `hermes.${which}Length does not match hermes.${which}Body`;
  }
  return null;
}

export function splitLines(buf) {
  const text = Buffer.isBuffer(buf) ? buf.toString('utf8') : String(buf);
  const lines = text.split('\n');
  if (lines.length && lines[lines.length - 1] === '') lines.pop();
  return lines;
}

export function verifyLog(data, publicKeyRaw) {
  const lines = splitLines(data);
  const publicKey = publicKeyFromRaw(Buffer.from(publicKeyRaw));
  const errors = [];
  const warnings = [];
  const err = (line, kind, reason) => errors.push({ line, kind, reason });

  const parsed = lines.map((text) => {
    if (BLANK.test(text)) return null;
    try {
      const n = parseAtpJson(text);
      return isObj(n) ? n : null;
    } catch {
      return null;
    }
  });

  const positions = new Map(); // scope -> Map(seq -> [pos])
  parsed.forEach((node, i) => {
    if (!node || typeof node.scope !== 'string') return;
    const q = seqOf(node);
    if (q === null) return;
    if (!positions.has(node.scope)) positions.set(node.scope, new Map());
    const m = positions.get(node.scope);
    if (!m.has(q)) m.set(q, []);
    m.get(q).push(i + 1);
  });
  const appearsLater = (scope, seq, pos) => ((positions.get(scope) ?? new Map()).get(seq) ?? []).some((p) => p > pos);

  const state = new Map();
  const good = [];
  const scopes = new Set();

  lines.forEach((text, i) => {
    const pos = i + 1;
    const node = parsed[i];
    if (BLANK.test(text)) { err(pos, 'tampered', 'blank line'); return; }
    if (!node) { err(pos, 'tampered', 'invalid JSON'); return; }
    let intact = true;
    if (structuralError(node) !== null) {
      err(pos, 'tampered', 'not a structurally valid ATP node');
      intact = false;
    }
    const claimed = node.nodeId;
    if (intact) {
      let rid = null;
      try { rid = computeNodeId(node); } catch { rid = null; }
      if (typeof claimed !== 'string') {
        err(pos, 'tampered', 'nodeId missing'); intact = false;
      } else if (rid !== claimed) {
        err(pos, 'tampered', 'nodeId does not match record content'); intact = false;
      } else if (!verifyNodeId(rid, node.signature, publicKey)) {
        err(pos, 'tampered', 'signature does not verify with the supplied public key'); intact = false;
      }
    }
    if (intact) {
      for (const which of ['input', 'output']) {
        const problem = bodyProblem(node, which);
        if (problem !== null) err(pos, 'body-mismatch', problem);
      }
    }
    const scope = node.scope;
    const q = seqOf(node);
    if (typeof scope !== 'string' || q === null) {
      if (intact) err(pos, 'tampered', 'hermes.seq missing or not a non-negative integer');
      return;
    }
    scopes.add(scope);
    if (intact) good.push([pos, node]);
    const cid = typeof claimed === 'string' && HEX64.test(claimed) ? claimed : null;
    const parents = Array.isArray(node.parents) ? node.parents : [];
    const prev = parents.length ? parents[0] : null;
    const st = state.get(scope);
    const expected = st === undefined ? 0 : st.seq + 1;
    if (q === expected) {
      if (st === undefined) {
        if (prev !== null) err(pos, 'tampered', 'first record of session must not link to a previous record');
      } else if (prev !== st.id) {
        err(pos, 'tampered', 'parent link does not match the previous record in session');
      }
    } else if (q > expected) {
      if (appearsLater(scope, expected, pos)) {
        err(pos, 'out-of-order', `session ${scope}: expected seq ${expected}, found ${q}; seq ${expected} appears later in the log`);
      } else {
        err(pos, 'missing', `session ${scope}: expected seq ${expected}, found ${q} (${q - expected} record(s) missing)`);
      }
    } else {
      err(pos, 'out-of-order', `session ${scope}: seq ${q} is not after seq ${st.seq}`);
    }
    if (st === undefined || q > st.seq) state.set(scope, { id: cid, seq: q });
  });

  if (lines.length === 0) err(0, 'empty-log', 'log contains no records');

  const closed = new Set();
  const key = (s, f, c) => JSON.stringify([s, f, c]);
  for (const [, node] of good) {
    const ev = eventOf(node);
    if (ev !== null && own(POST_EVENTS, ev)) {
      const fam = POST_EVENTS[ev];
      const c = corrOf(node, fam);
      if (c !== null) closed.add(key(node.scope, fam, c));
    }
  }
  for (const [pos, node] of good) {
    const ev = eventOf(node);
    if (ev === null || !own(PRE_EVENTS, ev)) continue;
    const fam = PRE_EVENTS[ev];
    const c = corrOf(node, fam);
    const idname = fam === 'api' ? 'api_request_id' : 'tool_call_id';
    if (c === null) {
      warnings.push({ line: pos, kind: 'unmatched-request', reason: `session ${node.scope}: ${ev} has no ${idname}; cannot be paired` });
    } else if (!closed.has(key(node.scope, fam, c))) {
      warnings.push({ line: pos, kind: 'unmatched-request', reason: `session ${node.scope}: ${ev} ${idname} ${c} has no matching ${PAIR_NAME[ev]}` });
    }
  }

  // Parent -> child session links, from each session's intact first record.
  const links = [];
  const linked = new Set();
  for (const [pos, node] of good) {
    if (seqOf(node) !== 0 || linked.has(node.scope)) continue;
    const parent = strField(node, 'parentSessionId');
    if (parent === null) continue;
    const child = node.scope;
    linked.add(child);
    links.push({ line: pos, parent, child });
    if (!scopes.has(parent)) {
      warnings.push({ line: pos, kind: 'missing-parent-session', reason: `session ${child}: parent session ${parent} has no records in this log` });
    }
  }
  warnings.sort((a, b) => a.line - b.line); // stable (ES2019): same-line warnings keep their order

  return {
    verdict: errors.length ? 'fail' : 'pass',
    records: lines.length,
    sessions: scopes.size,
    errors,
    warnings,
    links,
  };
}

export const formatJson = (r) => JSON.stringify(r, null, 2);

export function formatText(r) {
  const out = [`${r.verdict.toUpperCase()}  ${r.records} records, ${r.sessions} sessions, ${r.errors.length} errors, ${r.warnings.length} warnings`];
  for (const e of r.errors) out.push(`  line ${e.line}: ${e.kind.toUpperCase()}: ${e.reason}`);
  for (const w of r.warnings) out.push(`  line ${w.line}: WARNING ${w.kind}: ${w.reason}`);
  for (const l of r.links ?? []) out.push(`  line ${l.line}: LINK session ${l.parent} -> session ${l.child}`);
  return out.join('\n');
}
