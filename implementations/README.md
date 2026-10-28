# ATP reference implementations

Two independent implementations of ATP Core ([draft-bates-atp](https://datatracker.ietf.org/doc/draft-bates-atp/)), both checked against the golden vectors in [`../vectors/atp-core-test-vectors.json`](../vectors/atp-core-test-vectors.json).

| | Node.js | Python |
|---|---|---|
| Location | [`node/`](node/) | [`python/`](python/) |
| Requires | Node.js 18+ | Python 3.9+ |
| Dependencies | none (`node:crypto`) | none (standard library) |
| Ed25519 | OpenSSL, via Node | pure Python, RFC 8032 §6 |
| JCS | own implementation | own implementation |

The two implementations share no code or crypto library. When they agree byte-for-byte, that is real interoperability evidence.

## Quick start (about 60 seconds)

```sh
git clone https://github.com/davidbates73/agent-transaction-protocol.git
cd agent-transaction-protocol
./implementations/run-all.sh
```

That single command does three things:

1. Runs every vector against Node.js and Python: canonicalization, nodeId, Ed25519, negative cases, and validation modes.
2. Runs the end-to-end demo. It builds the draft's Appendix A chain (MCP tool discovery and invocation: 3 issuers, 7 nodes, a fan-in and a relay), then tampers with it, redacts it, drops a key, and shows what each validation mode reports.
3. Rebuilds that chain in Python and confirms that every nodeId and signature is byte-identical to the Node.js output.

Expected ending: `ALL CHECKS PASSED`.

## Run pieces individually

```sh
# Node.js
cd implementations/node
npm test                     # all vectors (add -- --published for the -00 conformance floor only)
npm run demo                 # Appendix A walkthrough
node bin/atp.js verify ../../examples/appendix-a/bundle.json --keys ../../examples/appendix-a/keys.json

# Python
cd implementations/python
python3 run_vectors.py
python3 atp_cli.py interop   # cross-check against the Node.js bundle
python3 atp_cli.py verify ../../examples/appendix-a/bundle.json --keys ../../examples/appendix-a/keys.json --mode tip
```

## Validating your own chain

1. Put the signed nodes in a bundle (§16.12, Pattern X):

   ```json
   { "atpVersion": "1", "nodes": [ ... ], "withheldNodeIds": [] }
   ```

2. Put the public keys in a keys file:

   ```json
   { "issuerId|keyId": "<base64 raw 32-byte Ed25519 public key>" }
   ```

3. Run `atp verify` with `--mode full|tip|bounded|redacted`. The output is the §13.6 result object.

## Interpretations of -00

A few points are ambiguous in -00, so the implementations had to choose. Each choice is marked `INTERPRETATION` in the source and listed in [`../docs/review-draft-00.md`](../docs/review-draft-00.md):

- The signature covers the 32 raw nodeId bytes.
- Signatures are standard base64 with padding.
- Null omission is recursive into nested objects; nulls inside arrays are kept.
- In redacted mode, a declared-withheld parent is a reported gap, not a failure.
- A node whose own checks pass but whose lineage fails goes in a `lineageIncomplete` extension array.

## Caveats

- The test keys are public. Never use them, or the demo's derived seeds, in production.
- The pure-Python Ed25519 is not constant-time. Use it for verification and tests only.
