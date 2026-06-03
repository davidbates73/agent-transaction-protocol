# ATP Interoperability Notes

This document captures practical interoperability considerations for ATP implementations and reviews.

## Goal

ATP interoperability means that two independent implementations can:

- canonicalize the same node content into the same byte sequence
- compute the same `nodeId`
- verify the same Ed25519 signatures
- interpret validation-result categories consistently
- agree on what ATP Core does and does not guarantee

## Core interoperability floor

An implementation should not claim ATP Core conformance unless it can do all of the following consistently:

- canonicalize nodes per ATP and RFC 8785 JCS
- omit null-valued fields when ATP Core requires omission
- include `profile` in canonicalization when present
- remove both `nodeId` and `signature` before recomputing `nodeId`
- sign and verify using Ed25519
- preserve parent-array order exactly
- distinguish:
  - invalid
  - unresolved
  - withheld
  - out-of-horizon
  - key-unresolved
  - profile-unresolved

## Most important interoperability risk areas

### 1. Canonicalization

This is the most fundamental interoperability surface.

Implementations must agree on:

- object key ordering
- UTF-8 output
- omission of insignificant whitespace
- preservation of array order
- omission of null-valued fields when ATP Core says to omit them

Typical failure modes:
- treating null omission as ordinary JCS behavior instead of ATP behavior
- including `nodeId` in canonicalization input
- including `signature` in canonicalization input
- dropping optional-but-present fields such as `profile`

### 2. `nodeId` computation

Two implementations must derive the same `nodeId` from the same canonical content.

Typical failure modes:
- hashing the wrong byte sequence
- hashing the hex string instead of raw bytes where the draft requires raw bytes
- excluding profile-defined fields when permissive validation should still include them
- reserializing in a non-canonical form before hashing

### 3. Signature handling

ATP Core requires Ed25519.

Typical failure modes:
- signing the wrong message
- signing the text form of `nodeId` when the implementation expects raw bytes
- inconsistent public-key encoding assumptions
- library-specific differences in how seed material is interpreted

### 4. Validation-result semantics

A lot of interoperability failures are semantic, not cryptographic.

Implementations must not blur the difference between:

- node-level integrity
- full lineage validity
- bounded validation
- tip validation
- redacted validation

Typical failure modes:
- treating `verified` as “fully lineage-valid” in every mode
- collapsing `withheld` into `unresolved`
- collapsing `keyUnresolved` into `invalid`
- silently ignoring `profileUnresolved`

### 5. Profile handling

Profiles are an interoperability amplifier when done well and an interoperability hazard when done poorly.

Typical failure modes:
- validator does not know whether to operate in strict or permissive profile mode
- profile-defined fields are not included in canonicalization
- profile identifiers are malformed or inconsistent
- private profile identifiers are treated as globally interoperable

## Validation-result category discipline

These distinctions matter a lot for interoperability:

- **invalid** means the implementation was able to evaluate the node and it failed ATP validation
- **unresolved** means the referenced node could not be obtained in the current verification context
- **withheld** means the node was intentionally omitted but declared as withheld
- **keyUnresolved** means the node was present but the issuer public key was not available
- **profileUnresolved** means the profile identifier was not recognized by the validator
- **out-of-horizon** means the node lies outside the declared bounded-validation boundary

Implementations should preserve these distinctions in logs, APIs, and exported validation results.

## Cross-scope verification

ATP permits cross-scope parent references.

Interoperability depends not on the protocol syntax but on deployment capability.

Typical failure modes:
- treating cross-scope parent references as invalid by default
- failing to distinguish “different scope” from “unreachable parent”
- assuming all validators have access to all scopes

A validator should treat a cross-scope parent as normal if it can be resolved and validated.

## Relay fidelity

Relay semantics are another likely source of divergence.

Implementations should align on the distinction between:

- a signed relay assertion
- independent proof of payload fidelity

Relay fidelity states should be reported consistently:

- `Verified`
- `Asserted`
- `Contradicted`

A relay node signature proves the relay issuer made the claim.  
It does not by itself prove the claim is true.

## Permissioned verification

Implementations may expose different verification views:

- full-chain
- scoped
- redacted
- hash-only

These can still be interoperable if they report limitations clearly.

Typical failure modes:
- treating a restricted verification context as though it were a full-chain result
- omitting withheld nodes from the result entirely
- returning partial structure without declaring the verification mode used

## Test vectors

The companion ATP test-vectors draft is the main interoperability anchor for:

- canonicalization
- `nodeId` computation
- signature generation and verification

As ATP evolves, implementations should also expect future vectors for:

- bounded validation
- validation-result objects
- profile-field canonicalization
- redacted-lineage behavior
- adversarial edge cases

## Practical recommendation for implementers

When building an ATP implementation, validate interoperability in this order:

1. canonicalization
2. `nodeId` computation
3. signature generation and verification
4. tip validation
5. full recursive validation
6. bounded validation
7. redacted-lineage validation
8. profile strict/permissive behavior

This sequence catches the highest-value mismatches earliest.

## Practical recommendation for reviewers

When reviewing ATP implementations or profiles, ask:

- Could two conforming implementations compute different bytes here?
- Could two validators classify this node differently?
- Does this behavior belong in ATP Core or in a profile?
- Is this a retrieval problem, a cryptographic problem, or a semantics problem?

## Final note

Most ATP interoperability problems will come from:

- incorrect canonicalization
- wrong `nodeId` input handling
- sloppy validation-result semantics
- profile ambiguity

They will usually not come from the DAG concept itself.

That is good news: interoperability is mostly a matter of precision and discipline.