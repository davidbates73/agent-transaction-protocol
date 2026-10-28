# ATP — Node.js reference implementation

Zero dependencies. Requires Node.js 18 or later.

```sh
npm test              # run all vectors in ../../vectors
npm run demo          # Appendix A end-to-end walkthrough
node bin/atp.js verify <bundle.json> --keys <keys.json> [--mode full|tip|bounded|redacted] [--depth N]
node bin/atp.js nodeid <node.json>
```

| File | Purpose |
|---|---|
| `src/atp.js` | Library: `canonicalize`, `computeNodeId`, `createNode`, `signNodeId`, `verifyNodeId`, `validate` |
| `bin/run-vectors.js` | Golden-vector runner; exits non-zero on any failure |
| `bin/demo.js` | Builds, signs, attacks, and validates the Appendix A chain (`--write` refreshes `examples/appendix-a/`) |
| `bin/generate-vectors.js` | Regenerates the vector file; asserts every published -00 value |
| `bin/atp.js` | CLI |

See [`../README.md`](../README.md) for the full guide.
