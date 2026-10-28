// ATP Core reference implementation (Node.js, zero dependencies).
// Implements draft-bates-atp: canonicalization (§9), nodeId (§10.1),
// Ed25519 signing/verification (§10.2–10.4), and the four validation
// modes with the §13.6 result format.
//
// Where the -00 text is ambiguous, the choice made here is marked
// "INTERPRETATION" and is listed in docs/review-draft-00.md.

import crypto from 'node:crypto';

// ---------------------------------------------------------------------------
// §9 Canonicalization: ATP null-omission + RFC 8785 JCS
// ---------------------------------------------------------------------------

/**
 * ATP §9 pre-processing: remove null-valued object members.
 * INTERPRETATION: applied recursively to nested objects; null array
 * elements are preserved (the -00 text only speaks of "fields").
 */
export function omitNulls(value) {
  if (Array.isArray(value)) return value.map(omitNulls);
  if (value !== null && typeof value === 'object') {
    const out = {};
    for (const [k, v] of Object.entries(value)) {
      if (v === null) continue;
      out[k] = omitNulls(v);
    }
    return out;
  }
  return value;
}

/**
 * RFC 8785 JSON Canonicalization Scheme.
 * - Strings and numbers use ECMAScript serialization (JSON.stringify),
 *   which is exactly what RFC 8785 §3.2.2 specifies.
 * - Object keys are sorted by UTF-16 code units (RFC 8785 §3.2.3);
 *   Array.prototype.sort() with no comparator compares UTF-16 code units.
 */
export function jcs(value) {
  if (value === null || typeof value === 'boolean') return JSON.stringify(value);
  if (typeof value === 'number') {
    if (!Number.isFinite(value)) throw new Error('JCS: non-finite number');
    return JSON.stringify(value);
  }
  if (typeof value === 'string') return JSON.stringify(value);
  if (Array.isArray(value)) return '[' + value.map(jcs).join(',') + ']';
  if (typeof value === 'object') {
    const keys = Object.keys(value).sort();
    return '{' + keys.map((k) => JSON.stringify(k) + ':' + jcs(value[k])).join(',') + '}';
  }
  throw new Error(`JCS: unsupported type ${typeof value}`);
}

/** ATP canonical form: §9 null omission, then RFC 8785. */
export function canonicalize(value) {
  return jcs(omitNulls(value));
}

// ---------------------------------------------------------------------------
// §10.1 Node identity
// ---------------------------------------------------------------------------

export const sha256Hex = (data) => crypto.createHash('sha256').update(data).digest('hex');

/** Remove nodeId and signature before canonicalization (§10.1). */
export function nodeContent(node) {
  const { nodeId, signature, ...rest } = node; // eslint-disable-line no-unused-vars
  return rest;
}

export function canonicalBytes(node) {
  return Buffer.from(canonicalize(nodeContent(node)), 'utf8');
}

export function computeNodeId(node) {
  return sha256Hex(canonicalBytes(node));
}

/** Hash an arbitrary payload into ATP's "sha256:<hex>" form. */
export function payloadHash(payload) {
  const bytes = Buffer.isBuffer(payload)
    ? payload
    : Buffer.from(typeof payload === 'string' ? payload : canonicalize(payload), 'utf8');
  return 'sha256:' + sha256Hex(bytes);
}

// ---------------------------------------------------------------------------
// §10.2–10.3 Ed25519 keys and signatures
// ---------------------------------------------------------------------------

const PKCS8_ED25519_PREFIX = Buffer.from('302e020100300506032b657004220420', 'hex');
const SPKI_ED25519_PREFIX = Buffer.from('302a300506032b6570032100', 'hex');

/** Build an Ed25519 keypair from a raw 32-byte seed (RFC 8032). */
export function keyPairFromSeed(seedHex) {
  const seed = Buffer.from(seedHex, 'hex');
  if (seed.length !== 32) throw new Error('Ed25519 seed must be 32 bytes');
  const privateKey = crypto.createPrivateKey({
    key: Buffer.concat([PKCS8_ED25519_PREFIX, seed]), format: 'der', type: 'pkcs8',
  });
  const publicKey = crypto.createPublicKey(privateKey);
  const raw = publicKey.export({ format: 'der', type: 'spki' }).subarray(SPKI_ED25519_PREFIX.length);
  return { privateKey, publicKey, publicKeyRaw: Buffer.from(raw) };
}

export function publicKeyFromRaw(raw) {
  const buf = Buffer.isBuffer(raw) ? raw : Buffer.from(raw, 'base64');
  if (buf.length !== 32) throw new Error('Ed25519 public key must be 32 bytes');
  return crypto.createPublicKey({ key: Buffer.concat([SPKI_ED25519_PREFIX, buf]), format: 'der', type: 'spki' });
}

/**
 * §10.3: signature = Sign(privateKey, nodeId).
 * The message is the 32 raw bytes of the SHA-256 digest, NOT the
 * 64-character hex string (test-vectors §5.2 is explicit; Core is not).
 * Encoding: standard base64 with padding (RFC 4648 §4).
 */
export function signNodeId(nodeIdHex, privateKey) {
  return crypto.sign(null, Buffer.from(nodeIdHex, 'hex'), privateKey).toString('base64');
}

export function verifyNodeId(nodeIdHex, signatureB64, publicKey) {
  try {
    const sig = Buffer.from(signatureB64, 'base64');
    if (sig.length !== 64) return false;
    return crypto.verify(null, Buffer.from(nodeIdHex, 'hex'), publicKey, sig);
  } catch {
    return false;
  }
}

/** Construct and sign a node. Returns a new object with nodeId + signature. */
export function createNode(fields, privateKey) {
  const content = omitNulls(nodeContent(fields));
  const nodeId = computeNodeId(content);
  return { nodeId, ...content, signature: signNodeId(nodeId, privateKey) };
}

// ---------------------------------------------------------------------------
// Structural checks (§8, §13.3 "structural presence")
// ---------------------------------------------------------------------------

const HEX64 = /^[0-9a-f]{64}$/;

/** Returns null if structurally OK, otherwise a reason string. */
export function structuralError(node) {
  if (node === null || typeof node !== 'object' || Array.isArray(node)) return 'node is not an object';
  for (const f of ['timestamp', 'scope']) if (typeof node[f] !== 'string') return `missing or non-string ${f}`;
  if (typeof node.issuer?.issuerId !== 'string' || typeof node.issuer?.keyId !== 'string') return 'issuer.issuerId/keyId required';
  if (typeof node.agent?.agentId !== 'string' || typeof node.agent?.version !== 'string') return 'agent.agentId/version required';
  if (typeof node.action?.type !== 'string') return 'action.type required';
  if (!Array.isArray(node.parents)) return 'parents must be an array';
  for (const p of node.parents) if (typeof p !== 'string' || !HEX64.test(p)) return `parent is not a lowercase hex SHA-256 nodeId: ${p}`;
  if (new Set(node.parents).size !== node.parents.length) return 'duplicate parent nodeId';
  if (typeof node.signature !== 'string') return 'signature required';
  return null;
}

// ---------------------------------------------------------------------------
// §13 Validation
// ---------------------------------------------------------------------------

/**
 * Validate one or more target nodes.
 *
 * @param {object} opts
 * @param {string[]} opts.targets           nodeIds to validate
 * @param {Map<string,object>|object} opts.nodes  available nodes keyed by nodeId
 * @param {(issuerId:string,keyId:string)=>KeyObject|null} opts.resolveKey
 * @param {'full'|'bounded'|'tip'|'redacted'} [opts.mode='full']
 * @param {{depth?:number,sinceTimestamp?:string}} [opts.boundary]
 * @param {string[]} [opts.withheld]         nodeIds declared withheld (§13.4)
 * @param {string[]} [opts.knownProfiles]    profile ids this validator understands
 * @param {boolean} [opts.strictProfiles=false]
 * @returns {object} §13.6 result (+ implementation extension "details")
 */
export function validate(opts) {
  const mode = opts.mode ?? 'full';
  if (!['full', 'bounded', 'tip', 'redacted'].includes(mode)) throw new Error(`unknown mode ${mode}`);
  if (mode === 'bounded' && !opts.boundary) throw new Error('bounded mode requires a boundary');
  const store = opts.nodes instanceof Map ? opts.nodes : new Map(Object.entries(opts.nodes ?? {}));
  const withheld = new Set(opts.withheld ?? []);
  const knownProfiles = new Set(opts.knownProfiles ?? []);
  const strict = !!opts.strictProfiles;

  const sets = {
    verified: new Set(), invalid: new Set(), unresolved: new Set(), withheld: new Set(),
    outOfHorizon: new Set(), keyUnresolved: new Set(), profileUnresolved: new Set(),
  };
  const relayFidelity = {};
  const details = {};            // nodeId -> human-readable reason(s)
  const nodeOk = new Map();      // nodeId -> node-level integrity boolean
  const lineageOk = new Map();   // nodeId -> lineage validity (full/bounded)
  const note = (id, msg) => { (details[id] ??= []).push(msg); };

  // Node-level integrity: §10.4 conditions 1, 2, 3 (6 is checked during traversal).
  function checkNode(id, node) {
    if (nodeOk.has(id)) return nodeOk.get(id);
    let ok = true;
    const s = structuralError(node);
    if (s) { sets.invalid.add(id); note(id, `structural: ${s}`); ok = false; }
    let recomputed;
    if (ok) {
      try { recomputed = computeNodeId(node); } catch (e) { sets.invalid.add(id); note(id, `canonicalization failed: ${e.message}`); ok = false; }
    }
    if (ok && recomputed !== id) { sets.invalid.add(id); note(id, `nodeId mismatch: recomputed ${recomputed}`); ok = false; }
    if (ok && node.nodeId !== undefined && node.nodeId !== id) { sets.invalid.add(id); note(id, 'embedded nodeId differs from reference'); ok = false; }
    if (ok && node.profile !== undefined && !knownProfiles.has(node.profile)) {
      sets.profileUnresolved.add(id);
      note(id, `profile not recognized: ${node.profile}`);
      if (strict) { sets.invalid.add(id); ok = false; }
    }
    if (ok) {
      const key = opts.resolveKey(node.issuer.issuerId, node.issuer.keyId);
      if (!key) { sets.keyUnresolved.add(id); note(id, `no public key for (${node.issuer.issuerId}, ${node.issuer.keyId})`); ok = false; }
      else if (!verifyNodeId(id, node.signature, key)) { sets.invalid.add(id); note(id, 'signature verification failed'); ok = false; }
    }
    nodeOk.set(id, ok);
    return ok;
  }

  function withinHorizon(node, depth) {
    if (mode !== 'bounded') return true;
    const b = opts.boundary;
    if (b.depth !== undefined && depth > b.depth) return false;
    if (b.sinceTimestamp !== undefined && Date.parse(node.timestamp) < Date.parse(b.sinceTimestamp)) return false;
    return true;
  }

  // Returns true if the node's lineage (within the applicable boundary) is valid.
  function visit(id, depth, stack) {
    if (stack.has(id)) { sets.invalid.add(id); note(id, 'cycle detected'); return false; }
    if (lineageOk.has(id)) return lineageOk.get(id);
    if (withheld.has(id) && (mode === 'redacted' || !store.has(id))) {
      // INTERPRETATION: in redacted mode a declared-withheld node is an
      // acknowledged, reported gap and does not by itself break the
      // mode-specific result; in other modes it does.
      sets.withheld.add(id);
      const acceptable = mode === 'redacted';
      lineageOk.set(id, acceptable);
      return acceptable;
    }
    const node = store.get(id);
    if (!node) { sets.unresolved.add(id); lineageOk.set(id, false); return false; }
    if (!withinHorizon(node, depth)) { sets.outOfHorizon.add(id); return true; } // not re-verified, not a failure
    const ok = checkNode(id, node);
    if (mode === 'tip') { lineageOk.set(id, ok); return ok; }

    stack.add(id);
    let parentsOk = true;
    for (const p of Array.isArray(node.parents) ? node.parents : []) {
      if (!visit(p, depth + 1, stack)) parentsOk = false;
    }
    stack.delete(id);

    if (ok && node.action?.type === 'atp:relay') relayFidelity[id] = relayState(node);
    const lineage = ok && parentsOk;
    lineageOk.set(id, lineage);
    if (ok && !parentsOk) note(id, 'node-level integrity OK but lineage not established (see parents)');
    return lineage;
  }

  // §14.2 relay fidelity: compare against a resolved, verified originating parent.
  function relayState(node) {
    const claimsFidelity = node.action.inputHash !== undefined && node.action.inputHash === node.action.outputHash;
    for (const p of node.parents) {
      const parent = store.get(p);
      if (parent && nodeOk.get(p) && parent.action?.outputHash !== undefined) {
        return claimsFidelity && parent.action.outputHash === node.action.inputHash ? 'Verified' : 'Contradicted';
      }
    }
    return 'Asserted';
  }

  for (const t of opts.targets) visit(t, 0, new Set());

  // verified: nodes that passed the checks applicable to the mode.
  // INTERPRETATION (spec gap, see review): in full/bounded/redacted modes a
  // node with node-level integrity but a broken lineage is NOT placed in
  // "verified"; it is listed in the extension key "lineageIncomplete".
  const lineageIncomplete = new Set();
  for (const [id, ok] of nodeOk) {
    if (!ok) continue;
    if (mode === 'tip' || lineageOk.get(id)) sets.verified.add(id);
    else lineageIncomplete.add(id);
  }

  const result = { mode };
  if (mode === 'bounded') result.boundary = { ...opts.boundary };
  for (const k of ['verified', 'invalid', 'unresolved', 'withheld', 'outOfHorizon', 'keyUnresolved', 'profileUnresolved']) {
    result[k] = [...sets[k]].sort();
  }
  if (Object.keys(relayFidelity).length) result.relayFidelity = relayFidelity;
  result.lineageIncomplete = [...lineageIncomplete].sort(); // implementation extension
  result.details = details;                                   // implementation extension
  return result;
}

/** Convenience: build a key resolver from { "issuerId|keyId": base64RawPublicKey }. */
export function keyResolver(map) {
  const cache = new Map();
  return (issuerId, keyId) => {
    const k = `${issuerId}|${keyId}`;
    if (!(k in map)) return null;
    if (!cache.has(k)) cache.set(k, publicKeyFromRaw(map[k]));
    return cache.get(k);
  };
}

/** Index a bundle (§16.12 Pattern X) by nodeId. */
export function indexNodes(nodes) {
  const m = new Map();
  for (const n of nodes) m.set(n.nodeId ?? computeNodeId(n), n);
  return m;
}

/** Nodes that no other node in the set references (the DAG tips). */
export function tips(nodes) {
  const referenced = new Set(nodes.flatMap((n) => n.parents ?? []));
  return nodes.map((n) => n.nodeId ?? computeNodeId(n)).filter((id) => !referenced.has(id));
}
