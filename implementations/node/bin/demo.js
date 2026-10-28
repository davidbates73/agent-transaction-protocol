#!/usr/bin/env node
// End-to-end demo: builds the draft-bates-atp Appendix A chain
// (MCP tool discovery + invocation, 3 issuers, 7 nodes, fan-in, relay),
// signs it, exports a §16.12 bundle, and then shows what each
// validation mode reports when the chain is tampered with, redacted,
// or missing a key.
//
//   node bin/demo.js            # run the demo
//   node bin/demo.js --write    # also (re)write examples/appendix-a/*.json

import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import {
  createNode, payloadHash, keyPairFromSeed, sha256Hex, validate, keyResolver,
  indexNodes, computeNodeId,
} from '../src/atp.js';

const here = path.dirname(fileURLToPath(import.meta.url));
const outDir = path.resolve(here, '../../../examples/appendix-a');
const tty = process.stdout.isTTY;
const B = (s) => (tty ? `\x1b[1m${s}\x1b[0m` : s);
const short = (id) => id.slice(0, 12) + '…';

// --- 1. Issuers. Seeds are derived deterministically so output is reproducible.
//        TEST KEYS ONLY — never use derived/public seeds in production.
const issuers = {};
for (const [issuerId, keyId] of [['platform.example', 'platform-2026-04'], ['mcp-broker.example', 'broker-2026-04'], ['tool-crm.example', 'crm-2026-04']]) {
  const kp = keyPairFromSeed(sha256Hex(`atp-demo-seed:${issuerId}:${keyId}`));
  issuers[issuerId] = { issuerId, keyId, ...kp };
}
const keysFile = Object.fromEntries(Object.values(issuers).map((i) => [`${i.issuerId}|${i.keyId}`, i.publicKeyRaw.toString('base64')]));

// --- 2. Payloads (stored OUTSIDE the chain; only their hashes go in nodes).
const P = {
  catalogQuery: { method: 'tools/list', endpoint: 'mcp://broker.example' },
  catalog: { tools: [{ name: 'crm.lookup_account' }, { name: 'crm.update_account' }] },
  selection: { chosen: 'crm.lookup_account', reason: 'read-only tool satisfies the task' },
  invocation: { method: 'tools/call', name: 'crm.lookup_account', arguments: { accountId: 'A-1001' } },
  result: { accountId: 'A-1001', tier: 'gold', openTickets: 2 },
  final: { answer: 'Account A-1001 is gold tier with 2 open tickets.' },
};
const H = Object.fromEntries(Object.entries(P).map(([k, v]) => [k, payloadHash(v)]));
H.synthesisInput = payloadHash({ result: H.result, selection: H.selection });

const scope = 'wf-8f3a1b';
const bob = { actorId: 'psn:9c3a7e4f-bob', authContext: 'saml:corp-idp' };
const orch = { agentId: 'orchestrator-agent', version: '1.3.0' };
const iss = (id) => ({ issuerId: issuers[id].issuerId, keyId: issuers[id].keyId });
const sign = (issuerId, fields) => createNode({ scope, issuer: iss(issuerId), ...fields }, issuers[issuerId].privateKey);

// --- 3. Build the seven nodes (Appendix A.2).
const n1 = sign('platform.example', { timestamp: '2026-04-23T12:58:00Z', agent: orch, actor: bob, action: { type: 'atp:request', subtype: 'tool_catalog_query', inputHash: H.catalogQuery }, parents: [] });
const n2 = sign('mcp-broker.example', { timestamp: '2026-04-23T12:58:00.110Z', agent: { agentId: 'mcp-catalog-service', version: '2.1.0' }, action: { type: 'atp:completion', subtype: 'tool_catalog_response', inputHash: H.catalogQuery, outputHash: H.catalog }, parents: [n1.nodeId] });
const n3 = sign('platform.example', { timestamp: '2026-04-23T12:58:00.240Z', agent: orch, actor: bob, action: { type: 'atp:decision', subtype: 'tool_selection_decision', inputHash: H.catalog, outputHash: H.selection }, parents: [n2.nodeId] });
const n4 = sign('platform.example', { timestamp: '2026-04-23T12:58:00.380Z', agent: orch, actor: bob, action: { type: 'atp:request', subtype: 'tool_invocation_request', inputHash: H.invocation }, parents: [n3.nodeId] });
const n5 = sign('tool-crm.example', { timestamp: '2026-04-23T12:58:00.610Z', agent: { agentId: 'crm-lookup-service', version: '5.0.2' }, action: { type: 'atp:completion', subtype: 'tool_execution', inputHash: H.invocation, outputHash: H.result }, parents: [n4.nodeId] });
const n6 = sign('mcp-broker.example', { timestamp: '2026-04-23T12:58:00.630Z', agent: { agentId: 'mcp-relay-service', version: '2.1.0' }, action: { type: 'atp:relay', subtype: 'tool_execution_response', inputHash: H.result, outputHash: H.result }, parents: [n5.nodeId] });
const n7 = sign('platform.example', { timestamp: '2026-04-23T12:58:00.820Z', agent: orch, actor: bob, action: { type: 'atp:decision', subtype: 'decision_synthesis', inputHash: H.synthesisInput, outputHash: H.final }, parents: [n6.nodeId, n3.nodeId] });
const chain = [n1, n2, n3, n4, n5, n6, n7];
const label = new Map(chain.map((n, i) => [n.nodeId, `N${i + 1}`]));
const L = (ids) => (ids.length ? ids.map((id) => label.get(id) ?? short(id)).sort().join(', ') : '—');

console.log(B('ATP demo — Appendix A: MCP tool discovery and invocation\n'));
for (const n of chain) {
  console.log(`  ${label.get(n.nodeId)}  ${n.nodeId}  ${n.action.type.padEnd(14)} ${n.issuer.issuerId.padEnd(19)} parents: ${L(n.parents)}`);
}

// --- 4. Export a §16.12 Pattern X bundle.
const bundle = { atpVersion: '1', nodes: chain, withheldNodeIds: [], scopes: [scope] };
if (process.argv.includes('--write')) {
  fs.mkdirSync(outDir, { recursive: true });
  fs.writeFileSync(path.join(outDir, 'bundle.json'), JSON.stringify(bundle, null, 2) + '\n');
  fs.writeFileSync(path.join(outDir, 'keys.json'), JSON.stringify(keysFile, null, 2) + '\n');
  fs.writeFileSync(path.join(outDir, 'payloads.json'), JSON.stringify(P, null, 2) + '\n');
  console.log(`\n  wrote ${path.relative(process.cwd(), outDir)}/{bundle,keys,payloads}.json`);
}

// --- 5. Scenarios.
const run = (title, opts) => {
  const r = validate({ targets: [n7.nodeId], resolveKey: keyResolver(keysFile), ...opts });
  console.log(`\n${B(title)}  (mode: ${r.mode})`);
  for (const k of ['verified', 'invalid', 'unresolved', 'withheld', 'outOfHorizon', 'keyUnresolved', 'lineageIncomplete']) {
    if (r[k].length || k === 'verified') console.log(`    ${k.padEnd(18)} ${L(r[k])}`);
  }
  if (r.relayFidelity) console.log(`    ${'relayFidelity'.padEnd(18)} ${Object.entries(r.relayFidelity).map(([id, s]) => `${label.get(id)}=${s}`).join(', ')}`);
  return r;
};

const all = indexNodes(chain);
const r1 = run('1. Full recursive validation of N7 — intact chain', { mode: 'full', nodes: all });

const tamperedN5 = { ...n5, action: { ...n5.action, outputHash: payloadHash({ accountId: 'A-1001', tier: 'platinum', openTickets: 0 }) } };
run('2. Tampering: N5 result hash altered after signing', { mode: 'full', nodes: indexNodes([n1, n2, n3, n4, n6, n7]).set(n5.nodeId, tamperedN5) });

const resigned = createNode({ ...tamperedN5 }, issuers['tool-crm.example'].privateKey);
console.log(`\n    (an attacker holding the tool key can re-sign N5, but the new nodeId ${short(resigned.nodeId)}`);
console.log(`     no longer matches what N6 references — downstream heritage still breaks)`);

run('3. Redaction: N1 withheld for privacy', { mode: 'redacted', nodes: indexNodes(chain.slice(1)), withheld: [n1.nodeId] });
run('4. Missing node: N1 never delivered (not declared withheld)', { mode: 'full', nodes: indexNodes(chain.slice(1)) });
run('5. Key unavailable: broker public key not resolvable', { mode: 'full', nodes: all, resolveKey: keyResolver(Object.fromEntries(Object.entries(keysFile).filter(([k]) => !k.startsWith('mcp-broker')))) });
run('6. Bounded validation: depth 2 from N7', { mode: 'bounded', boundary: { depth: 2 }, nodes: all });
run('7. Tip validation of N7 only (ingestion check)', { mode: 'tip', nodes: indexNodes([n7]) });

const liar = createNode({ ...n6, action: { ...n6.action, inputHash: payloadHash({ tier: 'bronze' }), outputHash: payloadHash({ tier: 'bronze' }) } }, issuers['mcp-broker.example'].privateKey);
const n7b = createNode({ ...n7, parents: [liar.nodeId, n3.nodeId] }, issuers['platform.example'].privateKey);
label.set(liar.nodeId, 'N6′'); label.set(n7b.nodeId, 'N7′');
const r8 = validate({ targets: [n7b.nodeId], nodes: indexNodes([n1, n2, n3, n4, n5, liar, n7b]), resolveKey: keyResolver(keysFile), mode: 'full' });
console.log(`\n${B('8. Dishonest relay: broker signs a relay whose hash ≠ the tool’s output')}  (mode: full)`);
console.log(`    verified           ${L(r8.verified)}`);
console.log(`    relayFidelity      ${Object.entries(r8.relayFidelity).map(([id, s]) => `${label.get(id)}=${s}`).join(', ')}`);
console.log('    (every signature is valid — ATP still exposes the contradiction, §14.1–14.2)');

// Sanity: the intact chain must fully verify.
const ok = r1.verified.length === 7 && r1.invalid.length === 0 && computeNodeId(n7) === n7.nodeId && r8.relayFidelity[liar.nodeId] === 'Contradicted';
console.log(`\n${ok ? 'Demo OK' : 'Demo FAILED'}`);
process.exit(ok ? 0 : 1);
