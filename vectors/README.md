# ATP Test Vectors

This directory contains implementation-facing assets related to ATP Core test vectors.

## Purpose

The goal of this directory is to support reproducible interoperability testing for:

- canonicalization
- `nodeId` computation
- Ed25519 signing and verification
- validation-result behavior
- future profile-field canonicalization cases

## Relationship to the Internet-Draft

The authoritative published test-vector specification for a submitted revision is the corresponding Internet-Draft artifact in:

- `submitted/`
- and, where present, the cleaned mirror in `friendly/`

This directory exists to hold machine-friendly or implementation-friendly assets that may accompany those drafts.

## Machine-readable vectors

[`atp-core-test-vectors.json`](atp-core-test-vectors.json) holds every published -00 vector, exactly as printed in draft-bates-atp-test-vectors-00, plus proposed -01 additions. Each entry is tagged `published-00` or `proposed-01`. Runners:

- Node.js: `node implementations/node/bin/run-vectors.js`
- Python: `python3 implementations/python/run_vectors.py`

Regenerate the file with `node implementations/node/bin/generate-vectors.js`. CI fails if the committed file is stale.

## Suggested subdirectories

- `canonicalization/` — canonical input/output fixtures
- `nodeid/` — node identity fixtures and expected hashes
- `signatures/` — signing and verification fixtures
- `validation/` — validation-result examples and future edge cases

## Notes

- Files here may evolve more quickly than the currently submitted Internet-Draft revision.
- If there is ever a discrepancy between a submitted draft artifact and a working vector file in this directory, the submitted draft is the authoritative record for that submitted revision.
- Future revisions may add machine-readable fixtures for bounded validation, redacted-lineage validation, profile-defined fields, and adversarial edge cases.