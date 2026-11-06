#!/usr/bin/env node
// ATP command-line tool.
//
//   atp verify <bundle.json> --keys <keys.json> [--mode full|tip|bounded|redacted]
//              [--depth N] [--since <RFC3339>] [--target <nodeId>] [--strict-profiles]
//   atp nodeid <node.json>          print canonical form and nodeId
//   atp keygen                      print a fresh Ed25519 seed + public key
//
// keys.json maps "issuerId|keyId" -> base64 raw 32-byte Ed25519 public key.
// Bundles follow draft-bates-atp §16.12 Pattern X.

import fs from 'node:fs';
import crypto from 'node:crypto';
import { validate, keyResolver, indexNodes, tips, canonicalize, nodeContent, computeNodeId, keyPairFromSeed } from '../src/atp.js';

const [cmd, file, ...rest] = process.argv.slice(2);
const flag = (name) => { const i = rest.indexOf(`--${name}`); return i >= 0 ? (rest[i + 1] ?? true) : undefined; };
const readJson = (p) => JSON.parse(fs.readFileSync(p, 'utf8'));

function usage(code = 1) {
  console.error('usage:\n  atp verify <bundle.json> --keys <keys.json> [--mode full|tip|bounded|redacted] [--depth N] [--since T] [--target ID] [--strict-profiles]\n  atp nodeid <node.json>\n  atp keygen');
  process.exit(code);
}

if (cmd === 'verify') {
  if (!file || !flag('keys')) usage();
  const bundle = readJson(file);
  const nodes = Array.isArray(bundle) ? bundle : bundle.nodes;
  const mode = flag('mode') ?? 'full';
  const boundary = mode === 'bounded'
    ? { ...(flag('depth') !== undefined && { depth: Number(flag('depth')) }), ...(flag('since') && { sinceTimestamp: flag('since') }) }
    : undefined;
  const result = validate({
    mode, boundary,
    targets: flag('target') ? [flag('target')] : tips(nodes),
    nodes: indexNodes(nodes),
    withheld: bundle.withheldNodeIds ?? [],
    resolveKey: keyResolver(readJson(flag('keys'))),
    knownProfiles: (flag('profiles') ?? '').split(',').filter(Boolean),
    strictProfiles: rest.includes('--strict-profiles'),
  });
  console.log(JSON.stringify(result, null, 2));
  process.exit(result.invalid.length ? 2 : 0);
} else if (cmd === 'nodeid') {
  if (!file) usage();
  const node = readJson(file);
  console.log(canonicalize(nodeContent(node)));
  console.log(computeNodeId(node));
} else if (cmd === 'keygen') {
  const seed = crypto.randomBytes(32).toString('hex');
  const kp = keyPairFromSeed(seed);
  console.log(JSON.stringify({ seedHex: seed, publicKeyBase64: kp.publicKeyRaw.toString('base64') }, null, 2));
} else {
  usage(cmd ? 1 : 0);
}
