# ATP Review Guide

Thanks for reviewing the Agent Transaction Protocol (ATP).

This guide is meant to help reviewers give targeted, useful feedback.

## What ATP is trying to do

ATP defines a cryptographically verifiable DAG model for action records.

Its purpose is narrow:

- record what action occurred
- bind that action to an issuer
- link it causally to prior actions
- make the resulting lineage tamper-evident and independently verifiable

ATP is intentionally not trying to solve every adjacent problem.

## What ATP Core does not define

Please keep review comments scoped carefully.

ATP Core does not define:

- authorization policy
- trust registries
- key discovery
- transport binding
- storage topology
- deletion protocols
- federation protocols
- domain-specific action vocabularies
- profile-specific conformance levels

Those concerns may be layered alongside ATP, but they are not ATP Core unless the draft says otherwise.

## Best review categories

The most helpful comments usually fall into one of these buckets:

### 1. Normative clarity
Examples:
- Is a MUST/SHOULD/MAY statement ambiguous?
- Do two sections conflict?
- Does a result category overclaim what was actually verified?

### 2. Interoperability
Examples:
- Could two conforming implementations produce different results?
- Is canonicalization behavior clear enough?
- Are validation results structured tightly enough for exchange?

### 3. Security and trust semantics
Examples:
- Does the draft distinguish proof from assertion clearly enough?
- Are relay semantics correctly scoped?
- Are key-unresolved and profile-unresolved states handled correctly?

### 4. Privacy and retention
Examples:
- Does the draft give realistic privacy guidance?
- Does it avoid promising impossible deletion behavior?
- Does it separate protocol mechanics from deployment obligations?

### 5. Profile boundary discipline
Examples:
- Is something in Core that should belong in a profile?
- Is a profile rule under-specified?
- Does a profile mechanism weaken Core guarantees?

### 6. IANA and specification hygiene
Examples:
- Are registries structured clearly?
- Are references pointed at the right sections?
- Are identifiers and namespaces specified consistently?

## Helpful review style

Please try to include:

- the file name
- the section number
- the exact text at issue, if practical
- why it is a problem
- suggested replacement text, if you have one

A strong review comment usually looks like:

1. What the text says
2. Why that creates ambiguity, risk, or mismatch
3. What wording would improve it

## Questions reviewers may want to ask

- Does the node schema carry the necessary semantics without overreaching?
- Are node-level integrity and lineage validity clearly separated?
- Are validation modes precise and non-overlapping?
- Are unresolved, withheld, invalid, key-unresolved, and profile-unresolved states distinct enough?
- Is the Core/Profile boundary stable?
- Are the operational deferrals deliberate and clearly acknowledged?
- Does the draft remain useful without turning into a platform specification?

## Submitted vs. working materials

This repository may contain:

- `submitted/` — exact submitted Internet-Draft artifacts
- `friendly/` — easier-to-read Markdown mirrors
- `working/` — newer drafts that may be ahead of the Datatracker version

When reviewing a specific revision, please identify which file you are reviewing.

## Most useful kinds of feedback

Especially valuable feedback includes:

- contradictions between sections
- hidden interoperability risks
- overclaims in validation semantics
- weak or unclear trust assumptions
- registry or namespace issues
- profile-framework problems
- concrete implementation pain points

## Less useful kinds of feedback

These are usually less actionable unless tied to precise draft text:

- “this feels too complex”
- “you should solve X too” where X is already a declared non-goal
- broad architectural disagreement without identifying where the text breaks

## Final note

ATP is intended to be composable and narrowly scoped.

The best reviews help strengthen:
- precision
- interoperability
- trust clarity
- implementability

without expanding ATP Core into something much larger than it is meant to be.