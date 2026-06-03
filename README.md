# Agent Transaction Protocol (ATP)

ATP is an individual Internet-Draft effort defining a cryptographically verifiable DAG model for agent transactions.

## Current Internet-Drafts

- ATP: https://datatracker.ietf.org/doc/draft-bates-atp/
- ATP Core Test Vectors: https://datatracker.ietf.org/doc/draft-bates-atp-test-vectors/

## What ATP is

ATP defines signed, causally linked records of actions with explicit separation of:

- issuer
- agent
- actor

Its purpose is narrow and deliberate: make agent and system actions independently verifiable without requiring the verifier to trust the platform that produced the record.

## What ATP is not

ATP does not define:

- authorization policy
- trust registries
- key discovery
- transport protocols
- storage topology
- profile-specific governance rules

Those concerns may be layered alongside ATP, but they are not ATP Core.

## Repository purpose

This repository is the public working space for the ATP specification family, including:

- Internet-Draft source text
- companion test vectors
- examples and diagrams
- issue tracking and technical review
- future profile discussion

## Repository layout

- `submitted/` — submitted Internet-Draft text artifacts as filed
- `friendly/` — cleaned Markdown mirrors for easier review on GitHub
- `vectors/` — test vectors and expected outputs
- `examples/` — worked examples and diagrams
- `profiles/` — future profile notes and related work
- `docs/` — contributor and review guidance

## Submitted vs. friendly versions

This repository contains two forms of the drafts for some revisions:

### `submitted/`
These files are the submitted Internet-Draft text artifacts for a given revision.  
They are the authoritative archival record of what was submitted for that revision.

Examples:
- `submitted/draft-bates-atp-00.txt`
- `submitted/draft-bates-atp-test-vectors-00.txt`

### `friendly/`
These files are cleaned, GitHub-friendly Markdown mirrors intended to make review easier.  
They may include readability improvements such as formatting cleanup, Markdown normalization, or diagram-friendly rendering that were not part of the exact submitted artifact.

Examples:
- `friendly/draft-bates-atp-00.md`
- `friendly/draft-bates-atp-test-vectors-00.md`

Where there is any difference between a friendly Markdown mirror and the corresponding submitted artifact, the file in `submitted/` is the authoritative record for that draft revision.

## Working drafts

Later in the development process, this repository may also contain newer working drafts that are ahead of the currently submitted Datatracker revision. Those working drafts are intended for review and iteration and should not be confused with the currently posted Internet-Draft version.

## Feedback

Technical review is welcome.

Please open an issue for:

- specification ambiguity
- interoperability concern
- security or privacy concern
- editorial correction
- profile proposal
- test-vector discrepancy

Concrete replacement text is especially appreciated.

## Status

Work in progress. Internet-Drafts are not final standards documents.

## Contribution note

ATP is currently being developed as an individual Internet-Draft effort. Contributions, review comments, and implementation feedback are welcome through issues and pull requests.