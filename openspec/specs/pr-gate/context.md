# PR gate context

## Purpose and scope

This capability replaces a prose-and-copy-paste PR gate sequence with one artifact-backed command. It owns anchor capture and revalidation, isolated checkout, exact review invocation count, coverage and disposition validation, verdict rendering, and COMMENT-only publication. Normative behavior is in [spec.md](spec.md).

## Key decisions and measured basis

- Six apifuse dogfood rounds found unbound shell variables, captured-but-unchecked SHAs, count-only disposition checks, missing coverage checks, double review ambiguity, and manual 40-character SHA relay.
- Target mode performs one review and writes `gate-plan.json`, including the planner-derived chunk count. Coverage validation compares the exact lane x replica x chunk Cartesian product rather than relying on aggregate counts. When findings need a human decision, `gate-dispositions.yaml` contains the deterministic group keys and resume mode consumes the same run without invoking models again.
- The repository-admin permission returned by GitHub is the verifiable owner authority for blocker acceptance. rvw records that actor and the human reason but does not decide or publish an approval.
- `accepted` and `must_fix` are deliberately small disposition states. The latter keeps the gate blocked; the former records explicit risk acceptance subject to blocker authority.
- Gate publication reuses the ordinary publish implementation, so COMMENT hardcoding, dry-run default, inline selection, and the bounded bulk 422 fallback have one code path.
- Six tabelog PR #27 rounds (runs 051207 through 111704 on 2026-07-29) did not converge because every changed-head review forgot prior owner dispositions. Round 5 contained 55 actionable findings, about 45 of which were accepted re-detections or re-critiques of the immediately preceding fixes. Carrying validated acceptances is therefore a convergence mechanism, not merely a template convenience.
- Runs 105629 and 111704 both reviewed head `f9936ad`. An unchanged head uses the existing artifact-backed resume path and MUST NOT be re-targeted for a fresh review; re-targeting an identical head is operator error. A changed head starts a new target run and may inherit from the prior run's validated verdict.
- The persisted gate verdict is the inheritance source because its dispositions have already passed exact-ID, completeness, duplicate, unknown-ID, and owner checks. BLOCK verdicts remain useful sources: their `must_fix` records never carry, but remain in the source set so mixed-disposition duplicate pairs stay ambiguous.
- In target mode, `--inherit` source validation follows PR target resolution and precedes checkout provisioning and review execution.
- Exact finding IDs encode diff coordinates rather than content. Tier-one carry therefore additionally compares SHA-256 digests of the persisted hunk text from each run. A changed or unavailable digest demotes to tier two; a unique `(file, rule_id)` match only prefills the reason for conscious re-acceptance. Duplicate pairs are counted across all inherited findings and all current actionable findings and deliberately under-carry.
- Optional provenance remains backward compatible, but it is authoritative only after validation binds it to the selected source and a recomputed carried or prefilled match. Hand-authored provenance without that proof is rejected.
- Partial-inheritance artifacts retain source-correlated carried, prefilled, and reasoned blank counts. Template comments distinguish changed-ID prefills, unmatched findings, prior-must-fix findings, ambiguity, and content-digest outcomes without weakening the strict YAML schema.

## Constraints

- Gate targets GitHub pull requests and requires working `gh` and `git` commands plus authenticated repository access.
- The checkout clone fetches GitHub's `refs/pull/<number>/head` before detaching at the captured SHA so fork pull requests do not depend on the base branch advertising the commit.
- Public finding IDs include hunk coordinates but not content; automatic inheritance additionally requires equal known hunk digests.
- Resume requires the ordinary run stages and `gate-plan.json` written by target mode.
- A coverage failure identifies the missing, unexpected, or invalid replica-chunk identity from persisted artifacts.
- Inheritance is limited to persisted verdicts for the same repository and pull-request number. Base and head anchors may differ between the source and inheriting runs.
- Run IDs are direct child names beneath the configured artifact root. Dot components, separators, symlinked entries, and resolved escapes are invalid lookup inputs.

## Failure modes

- A large repository makes disposable cloning more expensive than reusing a checkout; isolation is favored over speed for the gate path.
- GitHub installations without the pull-request ref namespace cannot use the current checkout provisioner.
- Repository-admin authority may be narrower than an organization's informal owner group; configurable authority sources are outside this version.
- `accepted` records a human judgment and cannot prove the judgment was substantively correct.
- Legacy verdicts have no hunk digest. They remain readable, but exact-ID findings from them are conservatively handled as tier two until a digest-bearing verdict exists.

## Concrete example

```bash
rvw gate --target 1134
# edit /tmp/rvw/<run-id>/gate-dispositions.yaml
rvw gate --run <run-id> \
  --dispositions /tmp/rvw/<run-id>/gate-dispositions.yaml
```

The first command reviews once and exits 1 when actionable findings need dispositions. The second command revalidates the PR anchors and saved coverage, produces `gate-verdict.json` and `gate-verdict.md`, and writes a dry-run COMMENT payload without repeating review.

For a changed head, inherit the previous run's accepted dispositions while creating the new run:

```bash
rvw gate --target 1134 --inherit <prior-run-id>
# if the generated template contains prefilled or blank records, edit it and resume
rvw gate --run <new-run-id> \
  --dispositions /tmp/rvw/<new-run-id>/gate-dispositions.yaml \
  --inherit <prior-run-id>
```

If the pull request head still equals an existing run's captured head, resume that run instead: `rvw gate --run <existing-run-id>`. Never use `--target` to repeat review at an unchanged head.

## Historical deltas

Before this capability, checkout ownership and anchor freshness were external concerns, and ordinary publish had no pre-publication stale-target guard. Those limitations remain for standalone `review` and `publish`; `gate` adds the stronger composed contract without changing their behavior.
