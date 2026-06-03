# Contributing to ATP

Thanks for reviewing or contributing to the Agent Transaction Protocol (ATP).

## Project status

ATP is currently being developed as an individual Internet-Draft effort.  
This repository contains both submitted draft artifacts and working materials used for review and iteration.

## Before you contribute

Please first determine which kind of material you are commenting on:

- `submitted/` — exact submitted draft artifacts for a given revision
- `friendly/` — cleaned Markdown mirrors of submitted drafts
- `working/` — newer working drafts that may be ahead of the currently posted Datatracker revision

If your comment is about wording, conformance, or a standards claim, please identify the exact file and section number.

## Best ways to contribute

Please open an issue for:

- specification ambiguity
- interoperability concern
- security concern
- privacy concern
- editorial correction
- test-vector discrepancy
- profile proposal

Concrete replacement text is especially helpful.

## When filing an issue

Please include:

- the file name
- the section number(s)
- the exact problematic text, if practical
- why it is a problem
- your suggested replacement text, if you have one

A very useful pattern is:

1. What the text says
2. Why that is ambiguous, incomplete, or risky
3. What wording would improve it

## Pull requests

Small editorial pull requests are welcome.

For larger normative changes, please open an issue first so the change can be discussed before draft text is updated.

Examples of good pull requests:

- typo fixes
- formatting cleanup
- reference cleanup
- broken internal cross-reference fixes
- non-normative readability improvements

Examples that should usually begin as issues:

- new normative requirements
- conformance changes
- validation model changes
- new action types
- new profile rules
- cryptographic or canonicalization changes

## Labels

Suggested issue labels:

- core
- profiles
- test-vectors
- interoperability
- security
- privacy
- editorial
- iana
- future-work

## Submitted vs. friendly files

If you notice a difference between:

- a file in `submitted/`
- and the corresponding file in `friendly/`

please assume the file in `submitted/` is the authoritative record for that draft revision unless the repository clearly states otherwise.

The `friendly/` copies exist to make review easier on GitHub and may include formatting cleanup.

## Working drafts

Files in `working/` may be newer than the latest Datatracker-posted draft.  
Please avoid citing a `working/` file as though it were an officially submitted Internet-Draft revision.

## Test vectors and implementation feedback

Implementation feedback is strongly encouraged.

Especially useful feedback includes:

- canonicalization mismatches
- nodeId mismatches
- Ed25519 signature reproduction issues
- validation-result format edge cases
- profile-field canonicalization behavior
- parent resolution and cross-scope verification behavior

If reporting an implementation mismatch, please include:

- the vector name
- the expected value
- the value your implementation produced
- the runtime/language/library you used

## Standards posture

ATP is intentionally narrow.  
Please keep comments scoped clearly to one of these buckets:

- ATP Core
- companion test vectors
- a future profile
- deployment guidance
- future work

Not every good idea belongs in ATP Core.

## Conduct

Be direct, technical, and specific.  
Sharp critique is welcome; vague critique is less useful.

Thanks for helping improve ATP.