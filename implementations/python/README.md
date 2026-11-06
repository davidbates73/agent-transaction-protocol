# ATP — Python reference implementation

Standard library only. Requires Python 3.9 or later. It includes its own RFC 8785 canonicalizer and a pure-Python RFC 8032 Ed25519, so it shares nothing with the Node.js implementation.

```sh
python3 run_vectors.py        # run all vectors in ../../vectors
python3 atp_cli.py interop    # rebuild Appendix A; must be byte-identical to the Node.js bundle
python3 atp_cli.py verify <bundle.json> --keys <keys.json> [--mode full|tip|bounded|redacted]
```

The Ed25519 here is not constant-time. Use it for tests and verification, not for signing production nodes.

See [`../README.md`](../README.md) for the full guide.
