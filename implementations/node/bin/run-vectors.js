#!/usr/bin/env node
// Runs every vector in vectors/atp-core-test-vectors.json.
// Exit code 0 = all pass. Use --published to run only the -00 vectors
// (the ATP Core §13.7 conformance floor).

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  canonicalize, computeNodeId, nodeContent, keyPairFromSeed, signNodeId,
  verifyNodeId, publicKeyFromRaw, validate, keyResolver, indexNodes,
} from '../src/atp.js';

const here = path.dirname(fileURLToPath(import.meta.url));
const file = process.argv.find((a) => a.endsWith('.json')) ?? path.resolve(here, '../../../vectors/atp-core-test-vectors.json');
const onlyPublished = process.argv.includes('--published');
const V = JSON.parse(fs.readFileSync(file, 'utf8'));

const color = process.stdout.isTTY ? { g: '\x1b[32m', r: '\x1b[31m', d: '\x1b[2m', x: '\x1b[0m' } : { g: '', r: '', d: '', x: '' };
let pass = 0; let fail = 0;
function check(id, status, label, ok, info = '') {
  if (onlyPublished && status !== 'published-00') return;
  if (ok) pass++; else fail++;
  const tag = ok ? `${color.g}PASS${color.x}` : `${color.r}FAIL${color.x}`;
  console.log(`  ${tag}  ${id.padEnd(4)} ${color.d}[${status}]${color.x} ${label}${info ? `  -> ${info}` : ''}`);
}
const sameSet = (a = [], b = []) => JSON.stringify([...a].sort()) === JSON.stringify([...b].sort());

console.log(`ATP Core vectors (Node.js ${process.version})\n  file: ${path.relative(process.cwd(), file)}\n`);

console.log('Canonicalization (§9 + RFC 8785)');
for (const c of V.canonicalization) {
  const got = canonicalize(JSON.parse(c.inputJson));
  check(c.id, c.status, c.description, got === c.canonical && Buffer.byteLength(got) === c.length, got === c.canonical ? '' : got);
}

console.log('\nnodeId computation (§10.1)');
for (const n of V.nodeId) {
  const input = JSON.parse(n.inputJson);
  const canon = canonicalize(nodeContent(input));
  const id = computeNodeId(input);
  check(n.id, n.status, n.description, canon === n.canonical && id === n.nodeId, id === n.nodeId ? '' : id);
}

console.log('\nEd25519 (§10.2–10.3)');
const pub = {};
for (const [name, k] of Object.entries(V.keys)) {
  const kp = keyPairFromSeed(k.seedHex);
  pub[name] = kp;
  check(name.replace('test-key-', 'K'), k.status, `public key from seed (${name})`, kp.publicKeyRaw.toString('hex') === k.publicKeyHex && kp.publicKeyRaw.toString('base64') === k.publicKeyBase64);
}
for (const s of V.signatures) {
  const kp = pub[s.key];
  const sig = signNodeId(s.messageNodeIdHex, kp.privateKey);
  const ok = Buffer.from(sig, 'base64').toString('hex') === s.signatureHex && sig === s.signatureBase64
    && verifyNodeId(s.messageNodeIdHex, s.signatureBase64, publicKeyFromRaw(kp.publicKeyRaw));
  check(s.id, s.status, s.description, ok);
}

console.log('\nNegative cases (MUST reject)');
for (const n of V.negative) {
  const got = verifyNodeId(n.messageNodeIdHex, n.signatureBase64, publicKeyFromRaw(pub[n.key].publicKeyRaw));
  check(n.id, n.status, n.description, got === n.expectVerify);
}

console.log('\nValidation modes and §13.6 result format');
const allKeys = Object.fromEntries(Object.entries(V.keys).map(([name, k]) => [`${k.issuerId}|${name}`, k.publicKeyBase64]));
for (const r of V.validation) {
  const keys = Object.fromEntries(Object.entries(allKeys).filter(([k]) => r.keys.includes(k.split('|')[1])));
  const nodes = indexNodes(r.nodes.map((ref) => V.signedNodes[ref]));
  const got = validate({
    mode: r.mode, boundary: r.boundary, targets: r.targets, nodes, withheld: r.withheld,
    resolveKey: keyResolver(keys), knownProfiles: [], strictProfiles: r.strictProfiles,
  });
  const cats = ['verified', 'invalid', 'unresolved', 'withheld', 'outOfHorizon', 'keyUnresolved', 'profileUnresolved'];
  let ok = got.mode === r.mode && cats.every((c) => Array.isArray(got[c]) && sameSet(got[c], r.expected[c]));
  if (r.boundary) ok &&= JSON.stringify(got.boundary) === JSON.stringify(r.expected.boundary);
  if (r.expectedExtension) ok &&= sameSet(got.lineageIncomplete, r.expectedExtension.lineageIncomplete);
  const mismatch = ok ? '' : cats.filter((c) => !sameSet(got[c], r.expected[c])).map((c) => `${c}=${JSON.stringify(got[c].map((x) => x.slice(0, 8)))}`).join(' ');
  check(r.id, r.status, r.description, ok, mismatch);
}

console.log(`\n${fail === 0 ? color.g : color.r}${pass} passed, ${fail} failed${color.x}${onlyPublished ? ' (published -00 vectors only)' : ''}`);
process.exit(fail === 0 ? 0 : 1);
