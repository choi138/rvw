## Why

The six-round tabelog PR #27 gate loop demonstrated a structural non-convergence: every push produces a new run whose actionable findings must all be re-dispositioned from scratch, even though most are re-detections of findings the owner already accepted with reasons in the previous round (45 of 55 round-5 findings were prior-accepted re-detections or re-critiques of just-made fixes). Re-authoring the same acceptance reasons each round is the mechanism that keeps the loop open. Separately, an unchanged head was re-reviewed in full (runs 105629 and 111704 both reviewed f9936ad) although artifact resume already exists; the reuse path needs to be the documented, spec-backed answer for the same-head case so a full re-review is never the default.

## What Changes

- Add an `--inherit <run-id>` option to `rvw gate` (valid in both target and resume modes) that loads a prior validated gate verdict for the same repository and pull request and carries its `accepted` dispositions forward.
- Carry rules are fail-closed and tiered: an exact public finding-ID match auto-carries the accepted decision and reason with provenance; a unique `(file, rule_id)` match with a changed hunk prefills the prior reason into the template but keeps the decision `must_fix` so a human must consciously re-accept; ambiguous matches and everything else fall back to today's blank template entries.
- `must_fix` dispositions never carry in any tier; REJECTED groups remain non-actionable as today.
- When every actionable finding of the new run is covered by exact-match carried acceptances, gate persists the generated disposition document and proceeds directly to validation in the same invocation instead of pausing for a resume round.
- Record inheritance provenance (`inherited_from` run ID) on carried disposition records and in the gate verdict artifact so verdicts remain reconstructable.
- Document in the pr-gate context that an unchanged head MUST be resumed (`rvw gate --run`), not re-targeted; target-mode re-review of an identical head is operator error, and `--inherit` composes with resume for the changed-head case.

## Capabilities

### Modified Capabilities

- `pr-gate`: disposition template generation gains inheritance tiers and an auto-proceed path for fully inherited runs; the disposition document and verdict artifact gain optional inheritance provenance; invocation validation gains the `--inherit` source checks.

## Impact

Affects gate CLI options and orchestration (`cli.py`), disposition models, template generation, verdict construction and rendering (`gate.py`), and their tests. Does not change discovery, merge, adjudication, publication event types, the external `~/.hermes/review/` registry, or add dependencies. The disposition document schema gains one optional field; existing documents remain valid.
