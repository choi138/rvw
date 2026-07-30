## Context

Six gate rounds on tabelog PR #27 (runs 051207 → 111704, 2026-07-29) surfaced two loop-breaking costs:

1. **Disposition amnesia.** Each round's template starts blank. Round 5 had 55 actionable findings; ~45 were re-detections of items already accepted with owner reasons in earlier rounds (slotId design, area-discovery scope-out, SDK status mapping, i18n copy) or re-critiques of the immediately preceding round's fixes. The stale-anchor policy forces "fix → new run → re-author everything", so acceptance work grows linearly with rounds.
2. **Same-head re-review.** Runs 105629 and 111704 both fully reviewed head `f9936ad`. `gate --run` resume plus the mandated pre-resume anchor requery already makes a second target-mode review of an identical head pure waste (~10 min wall, full token spend, and a fresh nondeterministic finding set to re-disposition).

## Goals / Non-Goals

**Goals**

- Carry owner-authored `accepted` dispositions across runs of the same PR with fail-closed identity matching and visible provenance.
- Make a fully inherited run converge in one invocation (no human pause) when nothing new was found.
- Establish resume as the only sanctioned path for an unchanged head.

**Non-goals**

- Incremental review (re-discovering only changed chunks and reusing adjudication verdicts for untouched code) — future work; requires content-addressed finding identity first.
- Carrying `must_fix` (a fix claim must be re-verified by a fresh review of the new head, never assumed).
- Cross-PR or cross-repository inheritance.
- Any relaxation of owner-only blocker acceptance.

## Decisions

### D1. Inheritance source = persisted gate verdict artifact, same repo + PR only

`--inherit <run-id>` loads the prior run's `gate-verdict.json` (not its raw `gate-dispositions.yaml`): the verdict is the only artifact whose dispositions passed exact-ID, duplicate, unknown, and owner-authorization validation. The source run's `target.json` must match the current repository and PR number; head/base anchors MAY differ (that is the point). Missing run, missing verdict artifact, a foreign PR, a symlinked or escaped artifact, or a pause-stub verdict whose counts still report actionable findings is a usage error (exit 2) before any template is written. Run identifiers use a conservative ASCII allowlist so they are safe direct-child names and safe to render. Resume rejects self-inheritance before loading because rewriting the source would create circular provenance and destroy the only completed evidence. A completed verdict with verdict=BLOCK is an acceptable source — its `accepted` records were validated; only its `must_fix` records are ignored. A genuinely clean verdict with no actionable counts remains a valid empty source.

### D2. Two matching tiers, biased toward under-carrying

The public finding ID is `sha1(file:hunk_id:rule_id)` and `hunk_id` embeds unified-diff line coordinates, so any push that shifts lines re-keys every downstream finding. Matching therefore runs in two tiers:

- **Tier 1 — exact ID plus code-and-diagnosis match**: after confirming the `(file, rule_id)` pair is unique on both sides, the public ID, the persisted SHA-256 digest of the run's canonical unified-diff hunk text, and the persisted SHA-256 digest of the representative finding body must all match. The accepted decision and reason then auto-carry, stamped `inherited_from: <run-id>`. A missing legacy digest or a digest mismatch demotes the finding to tier 2 because coordinate and code identity do not prove that a nondeterministic review made the same diagnosis.
- **Tier 2 — unique `(file, rule_id)` match**: the same rule fired in the same file but the hunk moved or tier-one content proof is unavailable. The prior reason is prefilled into the template (with `inherited_from`) but the decision stays `must_fix`, so a human must consciously flip it. Pair multiplicity is counted over all inherited verdict findings, including `must_fix`, and all current actionable findings. If either side has more than one finding for that pair, the pair is ambiguous and nothing carries.

Under-carrying is always safe (worst case: today's behavior). Over-carrying would silently launder a stale acceptance onto a semantically different finding, so tier 2 never auto-accepts.

### D3. Auto-proceed only on full tier-1 coverage

If and only if every actionable finding of the new run received a code-and-body-digest-verified tier-1 carried acceptance, gate writes the generated document to the run directory and continues into validation in the same invocation. Any tier-2 prefill, blank entry, or prior `must_fix` keeps today's pause-and-resume flow. Partial runs persist a structured outcome summary with closed reason keys and render machine-readable reasons as template comments. Final finding records also retain their matcher tier and blank or demotion reason so a manually re-accepted tier-2 prefill remains distinguishable from an automatic tier-1 carry.

### D4. Provenance is part of the strict schema

`DispositionRecord` gains an optional `inherited_from: str | None` (default `None`, `extra="forbid"` retained). It appears in generated templates for carried/prefilled records, is preserved through validation, and is rendered in the verdict artifact so a reviewer can distinguish fresh owner judgment from carried judgment. Validation recomputes matching from the selected source and rejects provenance that names another run, has no selected source, or is attached to an outcome the matcher did not carry or prefill. Existing disposition documents without the field remain valid.

### D5. Owner authorization is re-verified per run

Carried accepted blockers still pass through `requires_owner_authorization` → `github_actor_permission` on the new run. Inheritance moves reason text, never authorization: the actor executing the inheriting run is re-verified as admin at validation time. A failed check records the blocker finding ID and the observed actor and permission rather than collapsing to a generic invariant. If actor or permission lookup fails operationally, gate persists a BLOCK failure with the affected IDs, lookup step, resolved actor when available, and captured stderr before exiting 3.

### D6. Same-head reuse is resume, not a new flag

No `--reuse` option. The pr-gate spec already mandates resume-without-re-review and pre-resume anchor requery; the context doc now states the operational rule: if the PR head equals a completed run's head anchor, resume that run (`gate --run <id> [--dispositions …] [--inherit <older-id>]`); re-targeting an unchanged head is operator error. This keeps the CLI surface minimal and the invariant already spec-backed.

## Risks / Trade-offs

- **Line-shift fragility of tier 1**: pushes that only shift lines still demote carries to tier 2 (human re-confirm). Canonical hunk digests harden exact-coordinate reuse without changing public finding IDs, while body digests bind the carry to the prior diagnosis; content-addressed public identity remains future work.
- **Reason drift**: a carried reason may reference commit SHAs of the old round. Mitigated by provenance stamping — the verdict shows which run authored the reason.
- **Rule-ID coupling**: tier 2 depends on closed rule enums staying stable across registry edits mid-loop. Registry is versioned and gate loops are short-lived; acceptable.

## Migration

None. New digest and diagnostic fields are optional with defaults; legacy verdicts remain readable but cannot auto-carry without both known digests. Absent `--inherit` reproduces current behavior exactly.
