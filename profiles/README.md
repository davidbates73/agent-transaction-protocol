# ATP Profiles

This directory is reserved for future ATP profile work.

## What is a profile?

In ATP, a **profile** is a normative specification that extends or constrains ATP Core for a specific deployment context, domain, or interoperability scope.

Examples of what a profile may define include:

- domain-specific action vocabularies
- additional schema fields
- node-worthiness rules
- completion-pairing semantics
- streaming semantics
- profile-specific conformance levels
- scope-assessment or sealing rules
- relay-fidelity rules for profile-defined relay types

ATP Core defines the **Profile Framework**, but ATP Core does not define specific profiles.

## Purpose of this directory

This directory exists to hold:

- early profile ideas
- design notes
- example vocabularies
- future profile drafts
- supporting material for profile-specific conformance discussions

## Important boundary

Material in this directory does **not** change ATP Core by itself.

A profile is authoritative only when it is defined in its own specification.  
Ideas, notes, and drafts placed here are working materials unless and until they are published as a formal profile specification.

## Likely future profile areas

Examples of possible future profiles include:

- ATP-MCP
- ATP-Gateway
- ATP-Healthcare
- ATP-Finance
- ATP-Audit

These are examples only. Inclusion in this directory does not imply endorsement, registration, or standard status.

## Suggested structure

You may use this directory for subfolders such as:

- `ideas/` — rough concepts and early notes
- `drafts/` — working profile texts
- `examples/` — profile-specific worked examples
- `vectors/` — profile-specific test materials

## Relationship to the IANA ATP Profiles Registry

Registered ATP profiles are identified under the ATP Profiles registry described by ATP Core.

This directory may contain:
- private working ideas
- draft profile material
- pre-registration profile text

but registry status is determined by the relevant published specification, not by repository placement alone.