## MODIFIED Requirements

### Requirement: Actionable dispositions use exact public finding IDs

Gate MUST classify CONFIRMED and UNCERTAIN groups as actionable, MUST require exactly one strict disposition record for every actionable public finding ID, and MUST reject duplicate, omitted, unknown, or REJECTED-group IDs. Each disposition MUST contain one of `accepted` or `must_fix` and a nonblank human-authored reason. A disposition record MAY carry an `inherited_from` run identifier, and records without it MUST remain valid. When `inherited_from` is present, gate MUST reject the document with machine-readable reason `inherited_from_unbound` unless the named run is the selected `--inherit` source and a matcher recomputed from that source carried or prefilled the finding.

#### Scenario: Duplicate record masks an omission

- **WHEN** a disposition file repeats one finding ID and omits another actionable finding ID
- **THEN** gate rejects the file rather than accepting equal aggregate counts

#### Scenario: No disposition file is available

- **WHEN** a completed review has actionable findings, no disposition file is supplied, and the findings are not fully covered by inherited acceptances
- **THEN** gate writes a keyed disposition template for that run and exits nonzero without rerunning review

#### Scenario: Hand-authored provenance is not bound to a source

- **WHEN** a disposition record names an inherited run but the invocation has no matching `--inherit` source or the recomputed matcher left that finding unmatched
- **THEN** gate rejects the document with machine-readable reason `inherited_from_unbound`

## ADDED Requirements

### Requirement: Inheritance loads only a validated same-PR verdict

The `rvw gate` command MUST accept an `--inherit <run-id>` option in target and resume modes, MUST load the inherited run's persisted gate verdict artifact as the sole carry source, and MUST fail closed with a usage error before writing any template when that run is missing, lacks a verdict artifact, or is anchored to a different repository or pull-request number. Run lookup MUST reject path separators, dot components, symlinked run entries, and any resolved path outside the configured output root before loading artifacts. A BLOCK verdict MUST be accepted as a source, and all of its findings MUST remain available to ambiguity counting while only its `accepted` records are eligible to carry or prefill.

#### Scenario: Inherited run belongs to another pull request

- **WHEN** `--inherit` names a run whose persisted target is a different repository or PR number
- **THEN** gate exits with a usage error and writes no template

#### Scenario: Inherited run never reached a verdict

- **WHEN** `--inherit` names a run directory that has stage artifacts but no gate verdict artifact
- **THEN** gate exits with a usage error identifying the missing artifact

#### Scenario: Inherited run ID attempts to escape the output root

- **WHEN** `--inherit` contains a path separator or dot component or resolves through a symlinked run entry
- **THEN** gate exits with machine-readable reason `inherit_run_invalid` before loading any artifact outside the output root

### Requirement: Accepted dispositions carry by tiered identity matching

For each actionable finding of the current run, gate MUST persist an optional `hunk_sha256` computed from that run's own unified-diff hunk text. Gate MUST auto-carry the accepted decision and reason only when the public finding ID exactly matches an `accepted` inherited finding and both findings have equal known `hunk_sha256` values. An exact-ID digest mismatch or unknown digest MUST be demoted to tier-two handling and MUST NOT auto-carry. Gate MUST prefill only the reason while keeping the decision `must_fix` when an accepted `(file, rule_id)` candidate is unique among all inherited findings and all current actionable findings. Ambiguity counts MUST include inherited `accepted` and `must_fix` findings. Gate MUST NOT carry `must_fix` dispositions in any tier and MUST stamp every carried or prefilled record with the inherited run's identifier.

Every non-carried match result MUST expose a machine-readable `blank_reason` that distinguishes a changed finding ID, unmatched findings, prior `must_fix` findings, source-side pair ambiguity, current-side pair ambiguity, changed content, and unknown content digests. Generated disposition templates MUST render the applicable reason as a YAML comment beside each affected entry.

#### Scenario: Exact finding recurs after an unrelated push

- **WHEN** a new run re-detects a finding whose public finding ID and known hunk digest equal one accepted in the inherited verdict
- **THEN** the generated record contains the accepted decision, the prior reason, and the inherited run ID

#### Scenario: Exact finding ID has changed hunk content

- **WHEN** a public finding ID equals an accepted inherited finding but the known hunk digests differ
- **THEN** gate keeps `must_fix`, prefills under tier-two uniqueness rules, records `blank_reason: content_changed`, and does not auto-proceed

#### Scenario: Prior verdict has no hunk digest

- **WHEN** an exact-ID inherited finding has no `hunk_sha256`
- **THEN** gate treats its content as unknown and applies tier-two rules rather than auto-carrying

#### Scenario: Prior must-fix finding recurs

- **WHEN** the inherited verdict marked a finding `must_fix` and the new run re-detects the same finding ID
- **THEN** the generated record is a blank `must_fix` template entry without a carried reason

#### Scenario: Same rule moved to a different hunk

- **WHEN** an accepted finding's `(file, rule_id)` pair matches exactly one finding in each run but the finding IDs differ
- **THEN** the generated record keeps decision `must_fix`, prefills the prior reason, and stamps the inherited run ID

#### Scenario: Rule fires twice in one file

- **WHEN** either run contains two findings with the same `(file, rule_id)` pair, including a mixed accepted and must-fix pair in the inherited verdict
- **THEN** no disposition carries for that pair and the entries remain blank

### Requirement: Fully inherited runs proceed without pausing

When every actionable finding of the current run is covered by a digest-verified exact-match carried acceptance, gate MUST persist the generated disposition document under the run directory and MUST continue into disposition validation and verdict construction in the same invocation instead of exiting for a resume round. A partial-inheritance pause MUST report and persist the source run ID plus carried, prefilled, and blank counts grouped by machine-readable reason. Owner authorization for accepted blockers MUST be re-verified in the inheriting run, and a failed re-verification MUST persist the finding ID, verified actor, and returned permission. The verdict artifact MUST render each carried record's inherited run identifier.

#### Scenario: Every actionable finding was previously accepted

- **WHEN** all actionable findings receive tier-one carried acceptances from the inherited verdict
- **THEN** gate validates the persisted generated document and reports a verdict in the same invocation

#### Scenario: One finding is new

- **WHEN** one actionable finding has no match in the inherited verdict
- **THEN** gate writes the partially prefilled template and exits nonzero for human completion

#### Scenario: Carried blocker acceptance without admin actor

- **WHEN** every actionable finding carries but one accepted blocker's re-verified actor lacks repository admin permission
- **THEN** gate fails closed and does not publish
