# ATP Submission Notes

This document tracks practical notes about ATP Internet-Draft submission and repository organization.

## Current posture

ATP is being developed as an individual Internet-Draft effort.

This repository may contain three kinds of draft material:

- `submitted/` — exact submitted draft artifacts
- `friendly/` — cleaned Markdown mirrors for easier review
- `working/` — drafts in progress that may be ahead of the currently posted Datatracker revision

## Source of truth by purpose

Use the right file for the right purpose:

- **Citing a submitted revision:** use `submitted/`
- **Reviewing draft text on GitHub:** use `friendly/`
- **Continuing active editing:** use `working/`

Where there is any difference between a friendly mirror and a submitted artifact for the same revision, the file in `submitted/` is the authoritative record of what was submitted.

## Submission hygiene reminders

Before submitting a new revision, review at least the following:

- draft name and revision number match the intended submission
- title page metadata is correct
- references are current
- internal section references are correct
- IANA considerations are aligned with the current draft text
- Security and Privacy sections reflect the latest protocol semantics
- companion documents reference the correct ATP revision where needed
- implementation-status text is appropriate for an Internet-Draft and removable later

## Repository hygiene reminders

Before publishing repository changes, check:

- submitted artifacts are in `submitted/`
- friendly Markdown mirrors are in `friendly/`
- current draft work is in `working/`
- issue templates and contribution guidance are present
- placeholder files are either populated or removed
- file names match the draft version inside the document

## Review workflow suggestion

A practical cycle for ATP revisions:

1. Edit in `working/`
2. Request targeted review
3. Resolve comments
4. Freeze the revision text
5. Generate the submitted artifact
6. Place the submitted artifact in `submitted/`
7. Add or refresh the friendly mirror in `friendly/`
8. Update changelog or notes as needed

## Companion draft note

ATP Core and ATP Core Test Vectors should remain version-aware.

When updating ATP Core:
- check whether the test-vectors draft references the correct ATP revision

When updating test vectors:
- check whether ATP Core still points to the intended test-vectors draft revision

## Public communication note

When discussing ATP publicly:

- Datatracker links should point to the public submitted drafts
- GitHub links should point to the repository for ongoing review and implementation-facing materials
- avoid implying that a working draft is the same thing as a finalized standard

## Future cleanup

As the work matures, this file may also track:

- submission dates
- revision deltas
- public review notes
- implementation references
- interoperability events or results