# Review of draft-bates-atp-00 and draft-bates-atp-test-vectors-00

Reviewed 2026-10-28 against the submitted `.txt` artifacts in `submitted/`. Both drafts **expire 11 November 2026**.

## Summary

- **All published test vectors are correct.** C1–C5, V1–V5, the §5.1 public key, and S1 were reproduced byte-for-byte by two independent implementations (Node.js using OpenSSL, and Python using a pure RFC 8032 Ed25519). There are no errata in the numbers.
- **The main defect is mechanical.** 31 cross-references in the Core draft point to the wrong section (§20.x where §19.x is meant). The cause is hand-typed section numbers that broke when xml2rfc renumbered the document.
- **There are seven normative gaps that affect interop.** Building the implementations exposed them. The most important is that Core never says the signature covers the 32 raw nodeId bytes. Only the test-vectors draft says so.
- **There is one process problem to settle before -01.** An Independent Submission cannot register a `urn:ietf:params` sub-namespace (§17.3), because RFC 3553 gates that on IETF Consensus.

Severity key: **N** = normative / interop, **P** = process, **E** = editorial / formatting.

---

## Core draft (draft-bates-atp-00)

### N1. 31 cross-references point at the wrong section
In the submitted text, the Profile Framework is **§19**, and §20 is Normative References. Every `§20.x` reference is therefore wrong by one:

| Line(s) in submitted .txt | Says | Should be |
|---|---|---|
| 212, 291, 378, 587 | §20 | §19 |
| 296, 3432 | §20.7 | §19.7 |
| 643, 696, 2441, 2485, 2653 | §20.1 | §19.1 |
| 685, 702, 1370, 1812, 2192, 3047, 3333, 3378, 3440 | §20.3 | §19.3 |
| 696, 702, 771, 965, 1253, 1275, 1586, 2529 | §20.4 | §19.4 |
| 2445, 2594 | §20.2 | §19.2 |
| 3428 | §20.5 | §19.5 |

The test-vectors draft §4.5 also cites "[ATP-CORE] §20.4", which should be §19.4.

**Root cause:** the Markdown source numbers the sections differently (References 18, Implementation Status 19, Profile Framework 20). **Fix:** use `<xref target="..."/>` in the XML so xml2rfc computes the numbers. The `friendly/` mirror uses the old numbering, so it does not match the submitted text either.

### N2. The signature input is not specified in Core (§10.3)
`signature = Sign(privateKey, nodeId)` doesn't say whether the message is the 32 raw digest bytes or the 64-character hex string. Only test-vectors §5.2 says "bytes.fromhex(nodeId)". A signer that uses the hex string produces signatures that look valid but don't interoperate (see the new vector N1). **Fix:** add to §10.3: "The Sign input is the 32-octet SHA-256 digest, not its hexadecimal encoding."

### N3. Encodings are underspecified (§8, §10.1, §10.3)
- nodeId is shown as `hex(SHA256(...))`, but the case isn't specified. The vectors assume lowercase.
- signature is `"base64"`, but the draft doesn't say whether that is the standard or URL alphabet, or whether it is padded. The vectors assume standard alphabet with padding.

**Fix:** state "lowercase hexadecimal" and "base64 per RFC 4648 §4 with padding". Add RFC 4648 as a normative reference.

### N4. The payload hash format contradicts itself
The §8 schema says `"inputHash": "hex"`. But every test vector and the whole of Appendix A use `"sha256:<hex>"`. The Appendix A placeholders `gh78…`, `ij90…`, `kl12…`, `mn34…` aren't even hex. **Fix:** define the format normatively, for example: `"sha256:" 64LOWERHEX`. Say what is hashed: payload bytes are deployment-defined, and JCS bytes are recommended for JSON payloads. Replace the placeholders with real digests, which `examples/appendix-a/` now provides.

### N5. §10.4 condition 2 contradicts §10.1
Condition 2 says the nodeId is recomputed "excluding signature". §10.1 requires excluding **both** nodeId and signature. **Fix:** change the text to "excluding nodeId and signature".

### N6. The validation result has no category for "node OK, lineage broken" (§10.4, §13.6)
Take a full-mode run where a node's own checks pass but a parent is unresolved, invalid, or keyUnresolved:
- §10.4 forbids reporting it as fully lineage-valid, so it can't go in `verified`.
- It isn't invalid, unresolved, or keyUnresolved itself.

So it appears in **no** category. That contradicts §13.5 ("no validation mode may silently suppress … lineage gaps"). The reference implementations add a `lineageIncomplete` array as an extension. **Fix:** either add `lineageIncomplete` to §13.6, or define `verified` as node-level only in every mode and add a separate `lineageValid` array.

### N7. Withheld semantics in redacted mode are unspecified (§13.4)
The draft doesn't say whether visible descendants of a withheld node are reported as `verified` in redacted mode. The implementations treat a declared-withheld parent as an accepted, reported gap in redacted mode, and as a lineage failure in every other mode. Vectors R4 and R10 pin this down. **Fix:** state it in §13.4.

### N8. Null-omission scope is ambiguous (§9)
"Null-valued fields MUST be omitted" leaves three things open:
- whether it applies to nested objects;
- whether it applies to array elements;
- how it squares with "implementations that do not use RFC 8785 JCS are not conformant", given that pure JCS keeps nulls.

**Fix:**
- Define ATP canonicalization as a two-step operation: recursive removal of null object members, then RFC 8785. Array elements are preserved.
- Proposed vector C6 covers the nested case.

### N9. Timestamp lexical form is identity-bearing, but unconstrained (§8, §14.3)
`2026-01-01T00:00:00Z` and `2026-01-01T00:00:00+00:00` are the same instant but give different nodeIds (proposed vector V8). RFC 3339 is cited but missing from the references. **Fix:** add RFC 3339 as normative. Either require UTC with `Z`, or state explicitly that the lexical form, not the instant, is what gets hashed.

### N10. Profile identifier examples break the profile versioning rule (§19.1, §19.2)
§19.2 says profiles **MUST** use MAJOR.MINOR.PATCH. But the §19.1 examples (`…:mcp:1.0`, `…internal-audit:1.0`) and test vector V5 (`urn:ietf:params:atp:profile:test:1.0`) use two components. V5 also uses an unregistered `urn:ietf:params` identifier, which §19.1 says validators SHOULD reject. **Fix:**
- Change the examples to `1.0.0`.
- Keep V5's published value, because it is a canonicalization vector.
- Add a note, plus a tag-URI variant.

### N11. Smaller normative points
- **§11.1:** suggests embedding a sequence number in `agent.version`. That contradicts §16.5, where version changes only on configuration changes. Delete that bullet.
- **§13.3:** "structural presence" is undefined. Define it: `parents` is an array, each entry is 64 lowercase hex characters, and there are no duplicates.
- **§8:** state explicitly that `parents` is REQUIRED even when empty (`[]`). Omitting it changes the nodeId, and V1 depends on it.
- **§10.4 condition 6:** acyclicity can't be evaluated in tip mode. Also, content addressing makes cycles infeasible without a SHA-256 preimage. One sentence saying both would pre-empt a reviewer question.

### P1. `urn:ietf:params` registration isn't available to the Independent stream (§17.3)
RFC 3553 §3 gates `urn:ietf:params` sub-namespaces on "IETF Consensus". An Independent Submission can't meet that. "IETF Consensus" is also the RFC 2434 term; RFC 8126 calls it "IETF Review". There are two options:
- **(a)** Keep the Independent stream. Drop §17.3 and choose a non-IETF namespace for registered profiles, such as a `tag:` authority or an http(s) URI.
- **(b)** Plan for the IETF stream (AD sponsorship or a WG) and keep §17.3.

Separately, ask the ISE early whether it will request the two new IANA registries in §17.1–17.2. I'm not certain of current ISE practice there.

### P2. Internal revision numbers leaked into the text
- §16.12: the bundle example has `"atpVersion": "11"`.
- §19.1: "…before claiming **-10** conformance", and a migration paragraph for "earlier ATP drafts" using `private:` identifiers. No such public drafts exist.
- `friendly/draft-bates-atp-test-vectors-current.md` cites `draft-bates-atp-10`.

**Fix:** define `atpVersion` (for example `"1"`) or remove it, and delete the legacy `private:` paragraph.

### E1. Mermaid source is embedded in the plain text
§7 and both A.3 diagrams appear as raw `graph TD` / `sequenceDiagram` code, with `<br/>` and colour styles. In the .txt they read as noise. **Fix:** use ASCII-art `<artwork>`, optionally with an SVG alternative in v3 XML.

### E2. idnits will flag line lengths
60 lines in Core and 66 in the test vectors exceed 72 columns. The worst offenders are the 140-column tables in test-vectors §5 and the column-0 canonical-byte lines. **Fix:** use `<sourcecode>` with RFC 8792 line folding, or split hex across rows.

### E3. Appendices aren't labelled
The ToC and headings show "Worked Example…", "Future Work…" and "Related Work" with no "Appendix A/B/C". The text still cites "Appendix A" and "C.1", and Future Work is silently B. **Fix:** move them into `<back>` as proper appendix sections.

### E4. Other editorial items
- **§16.10:** the closing paragraph ("Implementations that support full recursive validation…") is a verbatim duplicate of the end of §16.7. Delete one.
- **A.2 Node 3:** "decisional node per §17.1" should say the definition is in §8.
- **A.2 Nodes 2 and 5:** the examples omit `actor` because the action is "not on behalf of a human". But §8 and §16.4 say governed deployments SHOULD name the accountable service principal. Either add a service actor, or say why this example deliberately models omission.
- **§18 Implementation Status:** RFC 7942 asks for the implementation's name, location (URL), maturity, coverage, licence and contact. The text says "accompanying this draft" but gives no location. Point it to this repository's `implementations/` folder.
- **Missing normative references:** RFC 3339 and RFC 4648, plus RFC 6234 or FIPS 180-4 for SHA-256. RFC 8259 for JSON should be added as normative or informative.
- **Stale references:**
  - [SCITT] is now **RFC 9943**, a Proposed Standard published June 2026.
  - Check the current revisions of [KASSELMAN] (draft-klrc-aiagent-auth), [ROSENBERG], [NI] and [SMITH] at submission time.
- **Author block:** confirm the affiliation and email are still current.

---

## Test-vectors draft (draft-bates-atp-test-vectors-00)

| # | Section | Issue | Fix |
|---|---|---|---|
| T1 | §4.5 | cites [ATP-CORE] §20.4 | §19.4 |
| T2 | §5 tables, §4 canonical bytes | lines up to ~140 columns, starting at column 0 | RFC 8792 folding |
| T3 | §6.1 | lists "empty arrays" as not covered, but V1, V3 and V5 contain `"parents":[]` | remove, or say "empty arrays other than `parents`" |
| T4 | §6.1 | "control characters not in the basic ASCII range" — U+0000–U+001F *are* ASCII | "control characters U+0000–U+001F" |
| T5 | §6.3 | says the generating Python script "is available alongside the published draft"; it wasn't published | point to `vectors/` and `implementations/` in this repo |
| T6 | §3.4, §3.5 | Length lines missing | add "Length: 17 bytes" and "Length: 33 bytes" |
| T7 | §1 | "the §13.6 validation result format" is ambiguous | "[ATP-CORE] §13.6" |
| T8 | §5 | only one signature vector; V4's key (test-key-2) has no published key material | publish test-key-2 (seed `bb…bb`) and S2–S4 |
| T9 | §2 | BCP 14 keywords not quoted, unlike the current boilerplate | use standard boilerplate |
| T10 | — | no negative vectors | add N1–N4 and R1–R10 (see below) |

### Proposed additions for -01 (all generated and cross-checked)
`vectors/atp-core-test-vectors.json` tags every entry `published-00` or `proposed-01`.

- **C6–C10:**
  - C6: recursive null omission
  - C7: string escaping
  - C8: UTF-16 key ordering, a case where it differs from code-point order
  - C9: ECMAScript number serialization
  - C10: empty values
- **V6–V8:**
  - V6: nodeId and signature present in the input, both stripped
  - V7: reversed parent order changes the nodeId
  - V8: timestamp lexical form changes the nodeId
- **K2, S2–S4:** second test key, and signatures over V2, V4 and V5.
- **N1–N4:**
  - N1: hex-string signing (rejected)
  - N2: flipped signature byte
  - N3: wrong key
  - N4: altered message
- **R1–R10:** all four validation modes plus permissive and strict profile handling, with the expected §13.6 results.

---

## Decisions needed from the author
1. **Stream** (P1): stay Independent and drop `urn:ietf:params`, or plan for the IETF stream?
2. **N6:** add `lineageIncomplete` to §13.6, or redefine `verified`?
3. **N8 and N9:** confirm recursive null omission and UTC-`Z` timestamps.
4. **N4:** confirm `sha256:<lowercase-hex>` as the normative payload hash format.
