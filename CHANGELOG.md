# Changelog

## Unreleased

- Hermes recorder slice 2: subagent sessions are linked to their parent via the `subagent_start` hook (`hermes.parentSessionId` on the child's first record); `verify` lists parent→child `links` in both CLIs and warns on a missing parent session. `verify --json` now always includes an additive `links` key (an empty list for logs with no links); verdicts, errors and warnings are unchanged
- Added the Hermes Agent ATP recorder plugin, slice 1 (`implementations/hermes-plugin/`), tested with Hermes v0.23.1
- Added `verify <records.jsonl> --pub <key.pub> [--json]` to both CLIs, with identical verdicts for tampered, missing and out-of-order records, stored bodies that don't match their hash, and empty logs (a failure)
- Added record-log test vectors (`implementations/hermes-plugin/vectors/`)
- Added zero-dependency Node.js and Python reference implementations (`implementations/`)
- Added machine-readable vectors (`vectors/atp-core-test-vectors.json`): published -00 plus proposed -01 additions
- Added a signed Appendix A example (`examples/appendix-a/`)
- Added a full review of the -00 drafts (`docs/review-draft-00.md`)
- Added CI that re-runs all vectors on Node 18/20/22 and Python 3.9/3.13

- Repository initialized
- Added submitted draft artifacts
- Added friendly Markdown mirrors
- Added issue templates and contribution guidance