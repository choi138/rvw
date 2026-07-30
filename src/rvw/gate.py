"""Fail-closed PR gate models, validation, checkout, and verdict rendering."""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from rvw.adjudicate import AdjudicationOutcome
from rvw.discover import LaneCoverage
from rvw.merge import CollapseGroup, MergeResult
from rvw.schema import Severity, Verdict
from rvw.target import ResolvedTarget


class GateInvariantError(ValueError):
    """Persisted review data does not satisfy the fail-closed gate contract."""


class GateAnchor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_sha: str = Field(min_length=40, max_length=40, pattern=r"^[0-9a-f]{40}$")
    head_sha: str = Field(min_length=40, max_length=40, pattern=r"^[0-9a-f]{40}$")


class PullRequestState(GateAnchor):
    state: str
    merged: bool


class GatePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    lane_ids: list[str] = Field(min_length=1)
    replicas: int = Field(ge=1)
    chunk_count: int = Field(ge=1)

    @field_validator("lane_ids")
    @classmethod
    def _lanes_must_be_unique(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("gate plan lane IDs must be unique")
        return value


class DispositionDecision(StrEnum):
    ACCEPTED = "accepted"
    MUST_FIX = "must_fix"


class DispositionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding_id: str
    decision: DispositionDecision
    reason: str
    inherited_from: str | None = None

    @field_validator("reason")
    @classmethod
    def _reason_must_be_nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("disposition reason must be nonblank")
        return value.strip()


class DispositionDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    dispositions: list[DispositionRecord]


class GateFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    finding_id: str
    rule_id: str
    file: str
    line: int | None
    severity: Severity
    verdict: Verdict
    disposition: DispositionDecision
    reason: str
    inherited_from: str | None = None


class InheritanceTier(StrEnum):
    EXACT_ID = "exact_id"
    UNIQUE_PAIR = "unique_pair"


class DispositionInheritance(BaseModel):
    """Generated inheritance state for one current actionable finding."""

    model_config = ConfigDict(extra="forbid")

    finding_id: str
    decision: DispositionDecision = DispositionDecision.MUST_FIX
    reason: str = ""
    inherited_from: str | None = None
    tier: InheritanceTier | None = None


class GateVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    run_id: str
    repo: str
    pr_number: int
    anchor: GateAnchor
    counts: dict[str, int]
    coverage: list[LaneCoverage]
    findings: list[GateFinding]
    actor: str | None = None
    verdict: Literal["PASS", "BLOCK"]
    failures: list[str] = Field(default_factory=list)


def load_dispositions(path: Path) -> DispositionDocument:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"could not load dispositions from {path}: {exc}") from exc
    return DispositionDocument.model_validate(raw)


def save_gate_plan(run_dir: Path, plan: GatePlan) -> Path:
    path = run_dir / "gate-plan.json"
    path.write_text(
        f"{json.dumps(plan.model_dump(mode='json'), indent=2, sort_keys=True)}\n",
        encoding="utf-8",
    )
    return path


def load_gate_plan(run_dir: Path) -> GatePlan:
    path = run_dir / "gate-plan.json"
    return GatePlan.model_validate_json(path.read_text(encoding="utf-8"))


def validate_coverage(
    planned_lane_ids: Sequence[str],
    coverage: Sequence[LaneCoverage],
    *,
    replicas: int,
    chunk_count: int,
) -> list[LaneCoverage]:
    if replicas < 1:
        raise GateInvariantError("expected replicas must be positive")
    if chunk_count < 1:
        raise GateInvariantError("expected chunk_count must be positive")
    if not coverage:
        raise GateInvariantError("coverage must be nonempty")

    coverage_ids = [item.lane_id for item in coverage]
    duplicates = sorted(lane_id for lane_id, count in Counter(coverage_ids).items() if count > 1)
    if duplicates:
        raise GateInvariantError(f"duplicate coverage lanes: {', '.join(duplicates)}")

    planned = set(planned_lane_ids)
    actual = set(coverage_ids)
    missing = sorted(planned - actual)
    unexpected = sorted(actual - planned)
    if not planned:
        detail = f"; unexpected coverage lanes: {', '.join(unexpected)}" if unexpected else ""
        raise GateInvariantError(f"planned lane set must be nonempty{detail}")
    if missing:
        raise GateInvariantError(f"missing planned coverage lanes: {', '.join(missing)}")
    if unexpected:
        raise GateInvariantError(f"unexpected coverage lanes: {', '.join(unexpected)}")

    by_lane = {item.lane_id: item for item in coverage}
    ordered: list[LaneCoverage] = []
    expected_runs = {
        (replica, chunk)
        for chunk in range(1, chunk_count + 1)
        for replica in range(1, replicas + 1)
    }
    for lane_id in planned_lane_ids:
        item = by_lane[lane_id]
        if item.dispatched <= 0:
            raise GateInvariantError(
                f"lane {lane_id} dispatched {item.dispatched}; expected positive runs"
            )
        actual_runs = {(run.replica, run.chunk) for run in item.runs}
        missing_runs = sorted(expected_runs - actual_runs, key=lambda value: (value[1], value[0]))
        unexpected_runs = sorted(
            actual_runs - expected_runs, key=lambda value: (value[1], value[0])
        )
        if missing_runs:
            detail = ", ".join(
                f"replica {replica} chunk {chunk}" for replica, chunk in missing_runs
            )
            raise GateInvariantError(f"lane {lane_id} missing planned coverage runs: {detail}")
        if unexpected_runs:
            detail = ", ".join(
                f"replica {replica} chunk {chunk}" for replica, chunk in unexpected_runs
            )
            raise GateInvariantError(f"lane {lane_id} has unexpected coverage runs: {detail}")
        expected_count = replicas * chunk_count
        if item.dispatched != expected_count:
            raise GateInvariantError(
                f"lane {lane_id} dispatched {item.dispatched}; expected {expected_count} runs"
            )
        invalid_runs = [run for run in item.runs if not run.valid]
        if invalid_runs:
            run = invalid_runs[0]
            raise GateInvariantError(
                f"lane {lane_id} replica {run.replica} chunk {run.chunk} invalid: "
                f"{run.invalid_reason}"
            )
        ordered.append(item)
    return ordered


def _actionable(
    merged: MergeResult, outcome: AdjudicationOutcome
) -> list[tuple[CollapseGroup, Verdict]]:
    actionable: list[tuple[CollapseGroup, Verdict]] = []
    for group in merged.groups:
        verdict = outcome.verdicts.get(group.key)
        if verdict is None:
            raise GateInvariantError(f"missing adjudication verdict for finding {group.key}")
        if verdict in {Verdict.CONFIRMED, Verdict.UNCERTAIN}:
            actionable.append((group, verdict))
    return actionable


def _dispositions_by_id(
    document: DispositionDocument, expected_ids: set[str]
) -> dict[str, DispositionRecord]:
    ids = [record.finding_id for record in document.dispositions]
    duplicates = sorted(finding_id for finding_id, count in Counter(ids).items() if count > 1)
    if duplicates:
        raise GateInvariantError(f"duplicate disposition finding IDs: {', '.join(duplicates)}")
    actual_ids = set(ids)
    unknown = sorted(actual_ids - expected_ids)
    if unknown:
        raise GateInvariantError(f"unknown disposition finding IDs: {', '.join(unknown)}")
    missing = sorted(expected_ids - actual_ids)
    if missing:
        raise GateInvariantError(f"missing disposition finding IDs: {', '.join(missing)}")
    return {record.finding_id: record for record in document.dispositions}


def match_inherited_dispositions(
    inherited_findings: Sequence[GateFinding],
    merged: MergeResult,
    outcome: AdjudicationOutcome,
    *,
    inherited_run_id: str,
) -> dict[str, DispositionInheritance]:
    """Match validated prior findings to the current actionable finding set."""

    actionable = [group for group, _ in _actionable(merged, outcome)]
    results = {group.key: DispositionInheritance(finding_id=group.key) for group in actionable}
    accepted_by_id = {
        finding.finding_id: finding
        for finding in inherited_findings
        if finding.disposition is DispositionDecision.ACCEPTED
    }
    inherited_pair_counts = Counter(
        (finding.file, finding.rule_id) for finding in inherited_findings
    )
    current_pair_counts = Counter((group.file, group.rule_id) for group in actionable)
    inherited_by_pair = {
        (finding.file, finding.rule_id): finding
        for finding in inherited_findings
        if finding.disposition is DispositionDecision.ACCEPTED
    }

    for group in actionable:
        if inherited := accepted_by_id.get(group.key):
            results[group.key] = DispositionInheritance(
                finding_id=group.key,
                decision=DispositionDecision.ACCEPTED,
                reason=inherited.reason,
                inherited_from=inherited_run_id,
                tier=InheritanceTier.EXACT_ID,
            )
            continue

        pair = (group.file, group.rule_id)
        if inherited_pair_counts[pair] != 1 or current_pair_counts[pair] != 1:
            continue
        inherited = inherited_by_pair.get(pair)
        if inherited is None:
            continue
        results[group.key] = DispositionInheritance(
            finding_id=group.key,
            decision=DispositionDecision.MUST_FIX,
            reason=inherited.reason,
            inherited_from=inherited_run_id,
            tier=InheritanceTier.UNIQUE_PAIR,
        )
    return results


def build_gate_verdict(
    *,
    run_id: str,
    target: ResolvedTarget,
    coverage: Sequence[LaneCoverage],
    merged: MergeResult,
    outcome: AdjudicationOutcome,
    dispositions: DispositionDocument,
    actor: str | None = None,
    actor_permission: str | None = None,
) -> GateVerdict:
    if target.kind != "pr" or target.pr_number is None or target.base_sha is None:
        raise GateInvariantError("gate verdict requires a PR target with base and head anchors")

    actionable = _actionable(merged, outcome)
    expected_ids = {group.key for group, _ in actionable}
    by_id = _dispositions_by_id(dispositions, expected_ids)
    findings: list[GateFinding] = []
    accepted_blocker = False
    blocked = False
    for group, verdict in actionable:
        record = by_id[group.key]
        if record.decision is DispositionDecision.MUST_FIX:
            blocked = True
        if group.severity is Severity.BLOCKER and record.decision is DispositionDecision.ACCEPTED:
            accepted_blocker = True
            if actor_permission != "admin" or not actor:
                raise GateInvariantError(
                    "accepted blocker requires an authenticated actor with repository admin permission"
                )
        findings.append(
            GateFinding(
                finding_id=group.key,
                rule_id=group.rule_id,
                file=group.file,
                line=group.line,
                severity=group.severity,
                verdict=verdict,
                disposition=record.decision,
                reason=record.reason,
                inherited_from=record.inherited_from,
            )
        )

    counts = Counter(outcome.verdicts.values())
    return GateVerdict(
        run_id=run_id,
        repo=target.repo,
        pr_number=target.pr_number,
        anchor=GateAnchor(base_sha=target.base_sha, head_sha=target.head_sha),
        counts={verdict.value: counts[verdict] for verdict in Verdict},
        coverage=list(coverage),
        findings=findings,
        actor=actor if accepted_blocker else None,
        verdict="BLOCK" if blocked else "PASS",
    )


def requires_owner_authorization(
    merged: MergeResult,
    outcome: AdjudicationOutcome,
    dispositions: DispositionDocument,
) -> bool:
    by_id = {record.finding_id: record for record in dispositions.dispositions}
    return any(
        group.severity is Severity.BLOCKER
        and outcome.verdicts.get(group.key) in {Verdict.CONFIRMED, Verdict.UNCERTAIN}
        and (record := by_id.get(group.key)) is not None
        and record.decision is DispositionDecision.ACCEPTED
        for group in merged.groups
    )


def write_disposition_template(
    run_dir: Path,
    merged: MergeResult,
    outcome: AdjudicationOutcome,
    *,
    inheritance: Mapping[str, DispositionInheritance] | None = None,
) -> Path:
    records: list[dict[str, str]] = []
    for group, _ in _actionable(merged, outcome):
        matched = inheritance.get(group.key) if inheritance is not None else None
        record = {
            "finding_id": group.key,
            "decision": (
                matched.decision.value
                if matched is not None
                else DispositionDecision.MUST_FIX.value
            ),
            "reason": matched.reason if matched is not None else "",
        }
        if matched is not None and matched.inherited_from is not None:
            record["inherited_from"] = matched.inherited_from
        records.append(record)
    template = {
        "schema_version": 1,
        "dispositions": records,
    }
    path = run_dir / "gate-dispositions.yaml"
    path.write_text(
        yaml.safe_dump(template, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return path


def _cell(value: object) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def render_gate_verdict(verdict: GateVerdict) -> str:
    lines = [
        f"# rvw gate — {verdict.verdict}",
        "",
        f"Run ID: `{verdict.run_id}`",
        f"Target: `{verdict.repo}#{verdict.pr_number}`",
        f"Base SHA: `{verdict.anchor.base_sha}`",
        f"Head SHA: `{verdict.anchor.head_sha}`",
        "",
        "## Verdict counts",
        "",
        "| CONFIRMED | REJECTED | UNCERTAIN |",
        "| ---: | ---: | ---: |",
        (
            f"| {verdict.counts['CONFIRMED']} | {verdict.counts['REJECTED']} | "
            f"{verdict.counts['UNCERTAIN']} |"
        ),
        "",
        "## Lane validity",
        "",
        "| Lane | Dispatched | Valid | Findings |",
        "| --- | ---: | ---: | ---: |",
    ]
    lines.extend(
        f"| {_cell(item.lane_id)} | {item.dispatched} | {item.valid} | {item.findings} |"
        for item in verdict.coverage
    )
    lines.extend(
        [
            "",
            "## Gate findings",
            "",
            "| Finding ID | Severity | Verdict | Disposition | Inherited from | Reason |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    lines.extend(
        (
            f"| `{item.finding_id}` | {item.severity.value} | {item.verdict.value} | "
            f"{item.disposition.value} | "
            f"{f'`{_cell(item.inherited_from)}`' if item.inherited_from else '—'} | "
            f"{_cell(item.reason)} |"
        )
        for item in verdict.findings
    )
    if not verdict.findings:
        lines.append("| — | — | — | — | — | No actionable findings |")
    if verdict.actor is not None:
        lines.extend(["", f"Verified blocker-acceptance actor: `{verdict.actor}`"])
    if verdict.failures:
        lines.extend(["", "## Failures", "", *(f"- {_cell(item)}" for item in verdict.failures)])
    return "\n".join(lines) + "\n"


def save_gate_verdict(run_dir: Path, verdict: GateVerdict) -> tuple[Path, Path]:
    json_path = run_dir / "gate-verdict.json"
    markdown_path = run_dir / "gate-verdict.md"
    json_path.write_text(
        f"{json.dumps(verdict.model_dump(mode='json'), ensure_ascii=False, indent=2, sort_keys=True)}\n",
        encoding="utf-8",
    )
    markdown_path.write_text(render_gate_verdict(verdict), encoding="utf-8")
    return json_path, markdown_path


def _run(command: list[str]) -> str:
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout


def query_pull_request(
    repo: str,
    pr_number: int,
    *,
    run: Callable[[list[str]], str] = _run,
) -> PullRequestState:
    try:
        raw = json.loads(run(["gh", "api", f"repos/{repo}/pulls/{pr_number}"]))
        return PullRequestState(
            base_sha=raw["base"]["sha"],
            head_sha=raw["head"]["sha"],
            state=raw["state"],
            merged=raw["merged"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"invalid pull-request state returned for {repo}#{pr_number}") from exc


def verify_pull_request(anchor: GateAnchor, current: PullRequestState) -> None:
    if current.state != "open" or current.merged:
        raise GateInvariantError("pull request must remain open and unmerged")
    if current.base_sha != anchor.base_sha or current.head_sha != anchor.head_sha:
        raise GateInvariantError(
            "stale pull request anchor: "
            f"expected {anchor.base_sha}/{anchor.head_sha}, "
            f"found {current.base_sha}/{current.head_sha}"
        )


def github_actor_permission(
    repo: str,
    *,
    run: Callable[[list[str]], str] = _run,
) -> tuple[str, str]:
    actor = run(["gh", "api", "user", "--jq", ".login"]).strip()
    if not actor:
        raise ValueError("GitHub returned an empty authenticated actor")
    permission = run(
        [
            "gh",
            "api",
            f"repos/{repo}/collaborators/{actor}/permission",
            "--jq",
            ".permission",
        ]
    ).strip()
    return actor, permission


def provision_checkout(
    *,
    repo: str,
    pr_number: int,
    head_sha: str,
    destination: Path,
    run: Callable[[list[str]], str] = _run,
) -> Path:
    run(["gh", "repo", "clone", repo, str(destination), "--", "--no-checkout"])
    run(
        [
            "git",
            "-C",
            str(destination),
            "fetch",
            "--no-tags",
            "origin",
            f"refs/pull/{pr_number}/head",
        ]
    )
    run(["git", "-C", str(destination), "checkout", "--detach", head_sha])
    actual_head = run(["git", "-C", str(destination), "rev-parse", "HEAD"]).strip()
    if actual_head != head_sha:
        raise GateInvariantError(
            f"checkout HEAD {actual_head or '<empty>'} does not match captured head {head_sha}"
        )
    status = run(
        ["git", "-C", str(destination), "status", "--porcelain=v1", "--untracked-files=all"]
    ).strip()
    if status:
        raise GateInvariantError("disposable checkout must be clean")
    return destination


__all__ = [
    "DispositionDecision",
    "DispositionDocument",
    "DispositionInheritance",
    "DispositionRecord",
    "GateAnchor",
    "GateFinding",
    "GateInvariantError",
    "GatePlan",
    "GateVerdict",
    "InheritanceTier",
    "PullRequestState",
    "build_gate_verdict",
    "github_actor_permission",
    "load_dispositions",
    "load_gate_plan",
    "match_inherited_dispositions",
    "provision_checkout",
    "query_pull_request",
    "render_gate_verdict",
    "requires_owner_authorization",
    "save_gate_plan",
    "save_gate_verdict",
    "validate_coverage",
    "verify_pull_request",
    "write_disposition_template",
]
