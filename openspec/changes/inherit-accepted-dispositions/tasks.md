## 1. Disposition schema and matching domain

- [x] 1.1 Add failing tests for the optional `inherited_from` field: absent-field documents stay valid, present field round-trips through load/validate/render, `extra="forbid"` still rejects unknown keys.
- [x] 1.2 Add failing tests for the tiered matcher: exact-ID carry, unique `(file, rule_id)` prefill, ambiguity (duplicate pair on either side) yields blank, `must_fix` never carries, REJECTED groups stay non-actionable.
- [x] 1.3 Implement the `inherited_from` field and a pure matching function from (inherited verdict findings, current actionable findings) to per-finding carry outcomes.

## 2. Inheritance source loading

- [x] 2.1 Add failing tests for `--inherit` source validation: missing run, missing gate-verdict artifact, repo/PR mismatch each exit with a usage error before template writing; BLOCK-verdict source contributes its accepted records.
- [x] 2.2 Implement inherited-verdict loading (run lookup, target match check, accepted-record extraction) with machine-readable failure reasons.

## 3. Template generation and auto-proceed orchestration

- [x] 3.1 Add failing tests for template generation with inheritance: carried records contain decision/reason/provenance, prefilled records keep `must_fix`, unmatched entries stay blank.
- [x] 3.2 Add failing CLI tests: full tier-1 coverage validates and reports a verdict in one invocation with the generated document persisted; partial coverage writes the prefilled template and exits nonzero; `--inherit` composes with both `--target` and `--run`; owner re-verification failure blocks a fully carried run.
- [x] 3.3 Implement gate orchestration for inheritance: generate-with-carries, auto-proceed on full tier-1 coverage, provenance rendering in the verdict artifact.

## 4. Specs and context

- [x] 4.1 Synchronize the pr-gate main spec with the implemented contract and record the six-round non-convergence evidence in `context.md`.
- [x] 4.2 Document the unchanged-head rule (resume, never re-target) and the changed-head `--inherit` workflow in the pr-gate context.
- [x] 4.3 Run all bare gates and `openspec validate --specs`.
