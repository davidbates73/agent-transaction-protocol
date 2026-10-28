#!/usr/bin/env node
// Regenerates vectors/atp-core-test-vectors.json.
// Published -00 values are hard-coded from draft-bates-atp-test-vectors-00
// and asserted, so this script fails loudly if the implementation drifts.
// "proposed-01" entries are computed here and independently re-checked by
// the Python implementation (implementations/python).

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  canonicalize, computeNodeId, keyPairFromSeed, signNodeId, nodeContent,
} from '../src/atp.js';
import crypto from 'node:crypto';

const here = path.dirname(fileURLToPath(import.meta.url));
const out = path.resolve(here, '../../../vectors/atp-core-test-vectors.json');

const assertEq = (a, b, what) => { if (a !== b) throw new Error(`${what}: expected ${b}, got ${a}`); };

// ---- keys -----------------------------------------------------------------
const SEED1 = 'aa'.repeat(32); // published in -00 §5.1
const SEED2 = 'bb'.repeat(32); // proposed for -01 (V4 is signed by test-issuer-2/test-key-2)
const k1 = keyPairFromSeed(SEED1);
const k2 = keyPairFromSeed(SEED2);
assertEq(k1.publicKeyRaw.toString('hex'), 'e734ea6c2b6257de72355e472aa05a4c487e6b463c029ed306df2f01b5636b58', 'K1 public key');

const keys = {
  'test-key-1': { status: 'published-00', issuerId: 'test-issuer', seedHex: SEED1, publicKeyHex: k1.publicKeyRaw.toString('hex'), publicKeyBase64: k1.publicKeyRaw.toString('base64') },
  'test-key-2': { status: 'proposed-01', issuerId: 'test-issuer-2', seedHex: SEED2, publicKeyHex: k2.publicKeyRaw.toString('hex'), publicKeyBase64: k2.publicKeyRaw.toString('base64') },
};

// ---- canonicalization -----------------------------------------------------
const C = [
  ['C1', 'published-00', 'empty object', '{}', '{}'],
  ['C2', 'published-00', 'object key reordering', '{"b": 1, "a": 2}', '{"a":2,"b":1}'],
  ['C3', 'published-00', 'null-valued field omission per ATP §9', '{"a": 1, "b": null}', '{"a":1}'],
  ['C4', 'published-00', 'array order preservation', '{"items": [3, 1, 2]}', '{"items":[3,1,2]}'],
  ['C5', 'published-00', 'nested object key sorting', '{"outer": {"z": 1, "a": 2}, "alpha": 3}', '{"alpha":3,"outer":{"a":2,"z":1}}'],
  ['C6', 'proposed-01', 'nested null omission (recursive §9 interpretation)', '{"a": {"b": null, "c": 1}}', null],
  ['C7', 'proposed-01', 'string escaping: non-ASCII raw, control chars \\u00XX, DEL raw', '{"s": "\\u00e9\\n\\"\\\\\\u0001\\u007f"}', null],
  ['C8', 'proposed-01', 'key sort by UTF-16 code units (differs from code-point order)', '{"\\uff61": 1, "\\ud83d\\ude00": 2, "a": 3}', null],
  ['C9', 'proposed-01', 'ECMAScript number serialization', '{"n": [1e21, 1e-7, 0.1, -0, 100, 1.5e300, 123456789012345680000, 4.50]}', null],
  ['C10', 'proposed-01', 'empty string, empty array, empty object values', '{"c": {}, "b": [], "a": ""}', null],
];
const canonicalization = C.map(([id, status, description, inputJson, expected]) => {
  const canonical = canonicalize(JSON.parse(inputJson));
  if (expected !== null) assertEq(canonical, expected, id);
  return { id, status, description, inputJson, canonical, canonicalHex: Buffer.from(canonical, 'utf8').toString('hex'), length: Buffer.byteLength(canonical, 'utf8') };
});

// ---- nodeId ---------------------------------------------------------------
const Z = (d) => 'sha256:' + String(d).repeat(64);
const iss1 = { issuerId: 'test-issuer', keyId: 'test-key-1' };
const ag1 = { agentId: 'test-agent', version: '1.0.0' };

const V1 = { timestamp: '2026-01-01T00:00:00Z', scope: 'test-scope-001', issuer: iss1, agent: ag1, action: { type: 'atp:request', inputHash: Z(0) }, parents: [] };
const id1 = '77d803c2d67e6cbe893172e5676e52b8f1bb80910bcbe1ca4c9aa5273f46ce70';
const V2 = { timestamp: '2026-01-01T00:00:01Z', scope: 'test-scope-001', issuer: iss1, agent: ag1, actor: { actorId: 'psn:0000-test-actor', authContext: 'test:none' }, action: { type: 'atp:completion', inputHash: Z(0), outputHash: Z(1) }, parents: [id1] };
const id2 = '881b552dd7d4a8598abe44ceab49257bb63b5e6420eeaf949ac2657b5495ae5e';
const V3json = '{"scope": "test-scope-001", "agent": {"version": "1.0.0", "agentId": "test-agent"}, "action": {"inputHash": "sha256:0000000000000000000000000000000000000000000000000000000000000000", "type": "atp:request"}, "issuer": {"keyId": "test-key-1", "issuerId": "test-issuer"}, "timestamp": "2026-01-01T00:00:00Z", "parents": []}';
const V4 = { timestamp: '2026-01-01T00:00:02Z', scope: 'test-scope-001', issuer: { issuerId: 'test-issuer-2', keyId: 'test-key-2' }, agent: { agentId: 'synth-agent', version: '1.0.0' }, action: { type: 'atp:decision', inputHash: Z(2), outputHash: Z(3) }, parents: [id1, id2] };
const id4 = '25abc84ddbd4ca932502e83e92050f00b1ecb70b4e3cf071d5823b3d3d23de4c';
const V5 = { timestamp: '2026-01-01T00:00:03Z', scope: 'test-scope-001', issuer: iss1, agent: ag1, action: { type: 'atp:request', inputHash: Z(4) }, parents: [], profile: 'urn:ietf:params:atp:profile:test:1.0' };
const id5 = '2356e89a5e787e9312287dfa4b3440d823b7fac59e401f060d42757e8f452803';

const sigV1 = signNodeId(id1, k1.privateKey);
const V6 = { nodeId: id1, ...V1, signature: sigV1 };
const V7 = { ...V4, parents: [id2, id1] };
const V8 = { ...V1, timestamp: '2026-01-01T00:00:00+00:00' };

const N = [
  ['V1', 'published-00', 'minimum-viable request node', JSON.stringify(V1), id1],
  ['V2', 'published-00', 'completion node with actor and parent reference', JSON.stringify(V2), id2],
  ['V3', 'published-00', 'same content as V1 with mis-ordered keys', V3json, id1],
  ['V4', 'published-00', 'fan-in decision with two parents', JSON.stringify(V4), id4],
  ['V5', 'published-00', 'node with profile field', JSON.stringify(V5), id5],
  ['V6', 'proposed-01', 'V1 with nodeId and signature present: both MUST be stripped (§10.1)', JSON.stringify(V6), id1],
  ['V7', 'proposed-01', 'V4 with parent order reversed: different nodeId (§8)', JSON.stringify(V7), null],
  ['V8', 'proposed-01', 'V1 with timestamp "+00:00" instead of "Z": same instant, different nodeId', JSON.stringify(V8), null],
];
const nodeId = N.map(([id, status, description, inputJson, expected]) => {
  const input = JSON.parse(inputJson);
  const canonical = canonicalize(nodeContent(input));
  const got = computeNodeId(input);
  if (expected !== null) assertEq(got, expected, id);
  return { id, status, description, inputJson, canonical, nodeId: got };
});
if (nodeId[6].nodeId === id4) throw new Error('V7 should differ from V4');
if (nodeId[7].nodeId === id1) throw new Error('V8 should differ from V1');

// ---- signatures -----------------------------------------------------------
const S1_HEX = '3f4d9fb756aba9bca11cfac15d65d82441dbf6f69adc9ba527b506c3379855500a2ef1a4e471323f2e8c8d190868e4f5ef303bef1e3e57e1988b1b46d83d5509';
const mkSig = (id, status, description, keyName, msg, kp) => {
  const b64 = signNodeId(msg, kp.privateKey);
  return { id, status, description, key: keyName, messageNodeIdHex: msg, signatureHex: Buffer.from(b64, 'base64').toString('hex'), signatureBase64: b64 };
};
const signatures = [
  mkSig('S1', 'published-00', "sign V1's nodeId (32 raw bytes)", 'test-key-1', id1, k1),
  mkSig('S2', 'proposed-01', "sign V2's nodeId", 'test-key-1', id2, k1),
  mkSig('S3', 'proposed-01', "sign V4's nodeId with test-key-2", 'test-key-2', id4, k2),
  mkSig('S4', 'proposed-01', "sign V5's nodeId", 'test-key-1', id5, k1),
];
assertEq(signatures[0].signatureHex, S1_HEX, 'S1');

// ---- negative vectors -----------------------------------------------------
const wrongEncodingSig = crypto.sign(null, Buffer.from(id1, 'utf8'), k1.privateKey);
const flipped = Buffer.from(S1_HEX, 'hex'); flipped[0] ^= 0x01;
const negative = [
  { id: 'N1', status: 'proposed-01', description: 'signature computed over the 64-char hex STRING of V1 nodeId instead of its 32 raw bytes; MUST fail verification', key: 'test-key-1', messageNodeIdHex: id1, signatureBase64: wrongEncodingSig.toString('base64'), expectVerify: false },
  { id: 'N2', status: 'proposed-01', description: 'S1 with first signature byte flipped; MUST fail verification', key: 'test-key-1', messageNodeIdHex: id1, signatureBase64: flipped.toString('base64'), expectVerify: false },
  { id: 'N3', status: 'proposed-01', description: 'S3 (signed with test-key-2) verified against test-key-1; MUST fail', key: 'test-key-1', messageNodeIdHex: id4, signatureBase64: signatures[2].signatureBase64, expectVerify: false },
  { id: 'N4', status: 'proposed-01', description: 'valid S1 signature, message nodeId with last nibble altered; MUST fail', key: 'test-key-1', messageNodeIdHex: id1.slice(0, -1) + '1', signatureBase64: signatures[0].signatureBase64, expectVerify: false },
];

// ---- signed nodes + validation scenarios ----------------------------------
const signed = {
  V1: { nodeId: id1, ...V1, signature: signatures[0].signatureBase64 },
  V2: { nodeId: id2, ...V2, signature: signatures[1].signatureBase64 },
  V4: { nodeId: id4, ...V4, signature: signatures[2].signatureBase64 },
  V5: { nodeId: id5, ...V5, signature: signatures[3].signatureBase64 },
};
const V2tampered = { ...signed.V2, action: { ...signed.V2.action, outputHash: Z(9) } };
const empty = { verified: [], invalid: [], unresolved: [], withheld: [], outOfHorizon: [], keyUnresolved: [], profileUnresolved: [] };
const R = (o) => ({ status: 'proposed-01', ...o, expected: { ...empty, ...o.expected } });
const validation = [
  R({ id: 'R1', description: 'full recursive validation of V4 with all heritage and keys', mode: 'full', targets: [id4], nodes: ['V1', 'V2', 'V4'], keys: ['test-key-1', 'test-key-2'], expected: { verified: [id1, id2, id4].sort() } }),
  R({ id: 'R2', description: 'tip validation of V4 alone: parents are not resolved', mode: 'tip', targets: [id4], nodes: ['V4'], keys: ['test-key-2'], expected: { verified: [id4] } }),
  R({ id: 'R3', description: 'full validation with V1 absent: V1 unresolved, V2 and V4 lineage not established', mode: 'full', targets: [id4], nodes: ['V2', 'V4'], keys: ['test-key-1', 'test-key-2'], expected: { unresolved: [id1] }, expectedExtension: { lineageIncomplete: [id2, id4].sort() } }),
  R({ id: 'R4', description: 'redacted-lineage validation with V1 declared withheld: gap reported, visible nodes verified', mode: 'redacted', targets: [id4], nodes: ['V2', 'V4'], withheld: [id1], keys: ['test-key-1', 'test-key-2'], expected: { verified: [id2, id4].sort(), withheld: [id1] }, expectedExtension: { lineageIncomplete: [] } }),
  R({ id: 'R10', description: 'full validation with V1 declared withheld and absent: withheld, lineage not established', mode: 'full', targets: [id4], nodes: ['V2', 'V4'], withheld: [id1], keys: ['test-key-1', 'test-key-2'], expected: { withheld: [id1] }, expectedExtension: { lineageIncomplete: [id2, id4].sort() } }),
  R({ id: 'R5', description: 'full validation without test-key-2: V4 keyUnresolved (not invalid)', mode: 'full', targets: [id4], nodes: ['V1', 'V2', 'V4'], keys: ['test-key-1'], expected: { verified: [id1, id2].sort(), keyUnresolved: [id4] } }),
  R({ id: 'R6', description: 'bounded validation, depth 0: parents reported outOfHorizon', mode: 'bounded', boundary: { depth: 0 }, targets: [id4], nodes: ['V1', 'V2', 'V4'], keys: ['test-key-1', 'test-key-2'], expected: { verified: [id4], outOfHorizon: [id1, id2].sort() } }),
  R({ id: 'R7', description: 'full validation with V2 tampered after signing (outputHash changed)', mode: 'full', targets: [id4], nodes: ['V1', 'V2-tampered', 'V4'], keys: ['test-key-1', 'test-key-2'], expected: { verified: [id1], invalid: [id2] }, expectedExtension: { lineageIncomplete: [id4] } }),
  R({ id: 'R8', description: 'tip validation of V5, unknown profile, permissive', mode: 'tip', targets: [id5], nodes: ['V5'], keys: ['test-key-1'], strictProfiles: false, expected: { verified: [id5], profileUnresolved: [id5] } }),
  R({ id: 'R9', description: 'tip validation of V5, unknown profile, strict', mode: 'tip', targets: [id5], nodes: ['V5'], keys: ['test-key-1'], strictProfiles: true, expected: { invalid: [id5], profileUnresolved: [id5] } }),
];
for (const r of validation) if (r.boundary) r.expected.boundary = r.boundary;

const doc = {
  title: 'ATP Core Test Vectors (machine-readable)',
  source: 'draft-bates-atp-test-vectors-00; entries marked proposed-01 are candidates for draft-bates-atp-test-vectors-01',
  conventions: {
    canonicalization: 'ATP §9 null-member omission (recursive) followed by RFC 8785 JCS',
    nodeId: 'lowercase hex SHA-256 of canonical UTF-8 bytes, nodeId and signature removed first',
    signatureMessage: '32 raw bytes of nodeId (bytes.fromhex(nodeId)), not the hex string',
    signatureEncoding: 'base64 (RFC 4648 §4, standard alphabet, padded)',
    payloadHashFormat: '"sha256:" + 64 lowercase hex chars (as used by -00 vectors)',
    validationResult: '§13.6 keys; "lineageIncomplete" is an implementation extension proposed for -01',
  },
  keys,
  canonicalization,
  nodeId,
  signatures,
  negative,
  signedNodes: { ...signed, 'V2-tampered': V2tampered },
  validation,
};
fs.writeFileSync(out, JSON.stringify(doc, null, 2) + '\n');
console.log(`wrote ${path.relative(process.cwd(), out)}`);
