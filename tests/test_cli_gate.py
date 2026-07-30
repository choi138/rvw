from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path
from typing import Literal

import pytest
from typer.testing import CliRunner

import rvw.cli as cli_module
from rvw.adjudicate import AdjudicationOutcome
from rvw.discover import DiscoverResult, EnrichedFinding, LaneCoverage, RunCoverage
from rvw.gate import (
    DispositionDecision,
    GateAnchor,
    GateFinding,
    GatePlan,
    GateVerdict,
    PullRequestState,
    save_gate_plan,
    save_gate_verdict,
)
from rvw.merge import merge
from rvw.schema import Severity, Tier, Verdict
from rvw.store import RunHandle, RunStore
from rvw.target import ResolvedTarget

runner = CliRunner()

HUNK_TEXT = "@@ -1 +1 @@\n-old\n+new\n"
HUNK_DIFF = f"diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n{HUNK_TEXT}"
HUNK_SHA256 = hashlib.sha256(HUNK_TEXT.encode()).hexdigest()
BODY_SHA256 = hashlib.sha256(b"actionable").hexdigest()


def target() -> ResolvedTarget:
    return ResolvedTarget(
        kind="pr",
        repo="owner/repo",
        base_sha="a" * 40,
        head_sha="b" * 40,
        changed_paths=["src/a.py"],
        diff=HUNK_DIFF,
        pr_number=42,
    )


def current_state(*, head_sha: str = "b" * 40) -> PullRequestState:
    return PullRequestState(
        base_sha="a" * 40,
        head_sha=head_sha,
        state="open",
        merged=False,
    )


def prepared_artifacts(
    out_root: Path,
    *,
    actionable: bool = False,
    valid: int = 3,
    blocker: bool = False,
) -> cli_module._PipelineArtifacts:
    run = RunStore(out_root).create(target())
    run.save_target(target())
    findings: list[EnrichedFinding] = []
    if actionable:
        findings.append(
            EnrichedFinding(
                rule_id="rule/actionable",
                file="src/a.py",
                hunk_id="src/a.py@@-1,1+1,1@@",
                line=1,
                severity=Severity.BLOCKER if blocker else Severity.WARNING,
                body="actionable",
                anchorable=True,
                lane_id="lane-a",
                replica=1,
            )
        )
    discovered = DiscoverResult(
        lane_results={},
        findings=findings,
        coverage=[
            LaneCoverage(
                lane_id="lane-a",
                dispatched=3,
                valid=valid,
                findings=len(findings),
                runs=[
                    RunCoverage(
                        replica=replica,
                        chunk=1,
                        valid=replica <= valid,
                        findings=len(findings) if replica == 1 else 0,
                        invalid_reason=None if replica <= valid else "scripted_invalid",
                    )
                    for replica in range(1, 4)
                ],
            )
        ],
        budget=None,
    )
    run.save_discover(discovered)
    merged = merge(findings, lane_tiers={"lane-a": Tier.BASE})
    run.save_merge(merged)
    outcome = AdjudicationOutcome(
        verdicts={group.key: Verdict.CONFIRMED for group in merged.groups},
        reasons={group.key: "confirmed" for group in merged.groups},
        evidence={group.key: "evidence" for group in merged.groups},
        replica_votes={group.key: [Verdict.CONFIRMED] * 3 for group in merged.groups},
        unresolved=[],
        coerced_rejections=0,
    )
    run.save_outcome(outcome)
    report = "# ordinary report\n"
    run.save_report(report)
    return cli_module._PipelineArtifacts(
        run=run,
        target=target(),
        discovered=discovered,
        merged=merged,
        outcome=outcome,
        report_md=report,
        report_path=run.dir / "report.md",
    )


def patch_target_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    artifacts: cli_module._PipelineArtifacts,
) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []
    monkeypatch.setattr(cli_module, "_resolve_cli_target", lambda spec: target())
    monkeypatch.setattr(
        cli_module,
        "_gate_plan",
        lambda registry_root, resolved, replicas: GatePlan(
            schema_version=1, lane_ids=["lane-a"], replicas=replicas, chunk_count=1
        ),
    )

    def fake_checkout(*, repo: str, pr_number: int, head_sha: str, destination: Path) -> Path:
        del repo, pr_number, head_sha
        destination.mkdir()
        return destination

    async def fake_execute(**kwargs: object) -> cli_module._PipelineArtifacts:
        calls.append(kwargs)
        return artifacts

    monkeypatch.setattr(cli_module, "provision_checkout", fake_checkout)
    monkeypatch.setattr(cli_module, "_execute_pipeline", fake_execute)
    monkeypatch.setattr(cli_module, "query_pull_request", lambda repo, number: current_state())
    return calls


def inherited_source(
    out_root: Path,
    current: cli_module._PipelineArtifacts,
    *,
    repo: str = "owner/repo",
    pr_number: int = 42,
    write_verdict: bool = True,
    verdict: Literal["PASS", "BLOCK"] = "PASS",
) -> RunHandle:
    run = RunHandle(run_id="source-run", dir=out_root / "source-run")
    run.dir.mkdir(parents=True)
    source_target = target().model_copy(update={"repo": repo, "pr_number": pr_number})
    run.save_target(source_target)
    if write_verdict:
        group = current.merged.groups[0]
        save_gate_verdict(
            run.dir,
            GateVerdict(
                run_id=run.run_id,
                repo=repo,
                pr_number=pr_number,
                anchor=GateAnchor(base_sha="c" * 40, head_sha="d" * 40),
                counts={"CONFIRMED": 1, "REJECTED": 0, "UNCERTAIN": 0},
                coverage=[],
                findings=[
                    GateFinding(
                        finding_id=group.key,
                        rule_id=group.rule_id,
                        file=group.file,
                        line=group.line,
                        severity=group.severity,
                        verdict=Verdict.CONFIRMED,
                        disposition=DispositionDecision.ACCEPTED,
                        reason="accepted in prior run",
                        hunk_sha256=HUNK_SHA256,
                        body_sha256=BODY_SHA256,
                    )
                ],
                verdict=verdict,
            ),
        )
    return run


def update_source_finding(source: RunHandle, **updates: object) -> None:
    verdict = GateVerdict.model_validate_json(
        (source.dir / "gate-verdict.json").read_text(encoding="utf-8")
    )
    verdict.findings[0] = verdict.findings[0].model_copy(update=updates)
    save_gate_verdict(source.dir, verdict)


def test_gate_target_executes_review_once_and_writes_dry_run_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_root = tmp_path / "runs"
    artifacts = prepared_artifacts(out_root)
    calls = patch_target_dependencies(monkeypatch, artifacts)

    result = runner.invoke(
        cli_module.app,
        ["gate", "--target", "42", "--out", str(out_root), "--json"],
    )

    assert result.exit_code == 0, result.stdout
    assert len(calls) == 1
    assert calls[0]["resolved_target"] == target()
    repo_dir = calls[0]["repo_dir"]
    assert isinstance(repo_dir, Path)
    assert repo_dir.name == "checkout"
    payload = json.loads(result.stdout)
    assert payload["verdict"] == "PASS"
    assert (artifacts.run.dir / "gate-plan.json").is_file()
    assert (artifacts.run.dir / "gate-verdict.json").is_file()
    publish_payload = json.loads(
        (artifacts.run.dir / "publish-payload.json").read_text(encoding="utf-8")
    )
    assert publish_payload["event"] == "COMMENT"
    assert "rvw gate — PASS" in publish_payload["body"]


@pytest.mark.parametrize(
    "args",
    [
        ["gate"],
        ["gate", "--target", "42", "--run", "run-1"],
    ],
)
def test_gate_requires_exactly_one_target_or_run(args: list[str]) -> None:
    result = runner.invoke(cli_module.app, args)

    assert result.exit_code == 2
    assert "exactly one" in result.stderr


def test_gate_invalid_target_is_user_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def invalid_target(spec: str) -> ResolvedTarget:
        raise ValueError(f"unsupported target: {spec}")

    monkeypatch.setattr(cli_module, "_resolve_cli_target", invalid_target)
    result = runner.invoke(cli_module.app, ["gate", "--target", "invalid"])

    assert result.exit_code == 2
    assert "unsupported target" in result.stderr


def test_gate_target_invalid_inherit_stops_before_provision_or_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_root = tmp_path / "runs"
    current = prepared_artifacts(out_root)
    provision_calls: list[dict[str, object]] = []
    pipeline_calls: list[dict[str, object]] = []
    monkeypatch.setattr(cli_module, "_resolve_cli_target", lambda spec: target())
    monkeypatch.setattr(
        cli_module,
        "_gate_plan",
        lambda registry_root, resolved, replicas: GatePlan(
            schema_version=1, lane_ids=["lane-a"], replicas=replicas, chunk_count=1
        ),
    )

    def fake_checkout(**kwargs: object) -> Path:
        provision_calls.append(kwargs)
        destination = kwargs["destination"]
        assert isinstance(destination, Path)
        destination.mkdir()
        return destination

    async def fake_execute(**kwargs: object) -> cli_module._PipelineArtifacts:
        pipeline_calls.append(kwargs)
        return current

    monkeypatch.setattr(cli_module, "provision_checkout", fake_checkout)
    monkeypatch.setattr(cli_module, "_execute_pipeline", fake_execute)

    result = runner.invoke(
        cli_module.app,
        [
            "gate",
            "--target",
            "42",
            "--inherit",
            "bad-run-id",
            "--out",
            str(out_root),
        ],
    )

    assert result.exit_code == 2
    assert "inherit_run_missing" in result.stderr
    assert provision_calls == []
    assert pipeline_calls == []


def test_gate_target_rejects_traversal_inherit_as_invalid_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_module, "_resolve_cli_target", lambda spec: target())

    result = runner.invoke(
        cli_module.app,
        ["gate", "--target", "42", "--inherit", "..", "--out", str(tmp_path / "runs")],
    )

    assert result.exit_code == 2
    assert "inherit_run_invalid" in result.stderr


def test_gate_resume_rejects_invalid_run_id_as_user_error(tmp_path: Path) -> None:
    result = runner.invoke(
        cli_module.app,
        ["gate", "--run", "nested/run", "--out", str(tmp_path / "runs")],
    )

    assert result.exit_code == 2
    assert "invalid run ID" in result.stderr
    assert "Traceback" not in result.stderr


def test_gate_rejects_self_inheritance_before_loading_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_load(run_id: str, out_root: Path) -> cli_module._PipelineArtifacts:
        del run_id, out_root
        raise AssertionError("self-inheritance reached run loading")

    monkeypatch.setattr(cli_module, "_load_gate_artifacts", forbidden_load)

    result = runner.invoke(
        cli_module.app,
        [
            "gate",
            "--run",
            "same-run",
            "--inherit",
            "same-run",
            "--out",
            str(tmp_path / "runs"),
        ],
    )

    assert result.exit_code == 2
    assert "inherit_self_reference" in result.stderr


@pytest.mark.parametrize(
    ("source_setup", "reason"),
    [
        ("missing_run", "inherit_run_missing"),
        ("missing_verdict", "inherit_verdict_missing"),
        ("target_mismatch", "inherit_target_mismatch"),
    ],
)
def test_gate_inherit_source_errors_before_template_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_setup: str,
    reason: str,
) -> None:
    out_root = tmp_path / "runs"
    current = prepared_artifacts(out_root, actionable=True)
    save_gate_plan(
        current.run.dir,
        GatePlan(schema_version=1, lane_ids=["lane-a"], replicas=3, chunk_count=1),
    )
    if source_setup == "missing_verdict":
        inherited_source(out_root, current, write_verdict=False)
    elif source_setup == "target_mismatch":
        inherited_source(out_root, current, pr_number=99)
    monkeypatch.setattr(cli_module, "query_pull_request", lambda repo, number: current_state())

    result = runner.invoke(
        cli_module.app,
        [
            "gate",
            "--run",
            current.run.run_id,
            "--inherit",
            "source-run",
            "--out",
            str(out_root),
        ],
    )

    assert result.exit_code == 2
    assert reason in result.stderr
    assert not (current.run.dir / "gate-dispositions.yaml").exists()


def test_gate_inherit_rejects_symlinked_verdict_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_root = tmp_path / "runs"
    current = prepared_artifacts(out_root, actionable=True)
    save_gate_plan(
        current.run.dir,
        GatePlan(schema_version=1, lane_ids=["lane-a"], replicas=3, chunk_count=1),
    )
    source = inherited_source(out_root, current)
    foreign = tmp_path / "foreign-verdict.json"
    foreign.write_text(
        (source.dir / "gate-verdict.json").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (source.dir / "gate-verdict.json").unlink()
    (source.dir / "gate-verdict.json").symlink_to(foreign)
    monkeypatch.setattr(cli_module, "query_pull_request", lambda repo, number: current_state())

    result = runner.invoke(
        cli_module.app,
        [
            "gate",
            "--run",
            current.run.run_id,
            "--inherit",
            source.run_id,
            "--out",
            str(out_root),
        ],
    )

    assert result.exit_code == 2
    assert "inherit_verdict_invalid" in result.stderr
    assert not (current.run.dir / "gate-dispositions.yaml").exists()


@pytest.mark.parametrize(
    ("source_kind", "expected_exit", "expected_text"),
    [
        ("stub", 2, "inherit_source_incomplete"),
        ("clean", 1, "actionable findings require dispositions"),
    ],
)
def test_gate_inheritance_source_requires_completed_actionable_dispositions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_kind: str,
    expected_exit: int,
    expected_text: str,
) -> None:
    out_root = tmp_path / "runs"
    current = prepared_artifacts(out_root, actionable=True)
    save_gate_plan(
        current.run.dir,
        GatePlan(schema_version=1, lane_ids=["lane-a"], replicas=3, chunk_count=1),
    )
    source = inherited_source(out_root, current)
    source_verdict = source.load_gate_verdict()
    source_verdict.findings = []
    if source_kind == "stub":
        source_verdict.failures = ["actionable findings require explicit dispositions"]
    else:
        source_verdict.counts = {"CONFIRMED": 0, "REJECTED": 1, "UNCERTAIN": 0}
        source_verdict.failures = []
    save_gate_verdict(source.dir, source_verdict)
    monkeypatch.setattr(cli_module, "query_pull_request", lambda repo, number: current_state())

    result = runner.invoke(
        cli_module.app,
        [
            "gate",
            "--run",
            current.run.run_id,
            "--inherit",
            source.run_id,
            "--out",
            str(out_root),
        ],
    )

    assert result.exit_code == expected_exit
    assert expected_text in (result.stderr + result.stdout)


def test_block_verdict_source_counts_mixed_dispositions_as_pair_ambiguity(
    tmp_path: Path,
) -> None:
    out_root = tmp_path / "runs"
    current = prepared_artifacts(out_root, actionable=True)
    source = inherited_source(out_root, current, verdict="BLOCK")
    source_verdict = GateVerdict.model_validate_json(
        (source.dir / "gate-verdict.json").read_text(encoding="utf-8")
    )
    source_verdict.findings[0] = source_verdict.findings[0].model_copy(
        update={"finding_id": "prior-accepted-id"}
    )
    source_verdict.findings.append(
        source_verdict.findings[0].model_copy(
            update={
                "finding_id": "must-fix-id",
                "disposition": DispositionDecision.MUST_FIX,
                "reason": "must still be fixed",
            }
        )
    )
    save_gate_verdict(source.dir, source_verdict)

    loaded = cli_module._load_inherited_dispositions(
        source.run_id,
        current_target=current.target,
        out_root=out_root,
    )

    assert loaded.run_id == source.run_id
    assert loaded.verdict == "BLOCK"
    assert [finding.finding_id for finding in loaded.findings] == [
        "prior-accepted-id",
        "must-fix-id",
    ]

    group = current.merged.groups[0]
    assert current.outcome is not None
    matched = cli_module.match_inherited_dispositions(
        loaded.findings,
        current.merged,
        current.outcome,
        inherited_run_id=source.run_id,
        current_hunk_sha256={group.key: HUNK_SHA256},
    )[group.key]

    assert matched.tier is None
    assert matched.reason == ""
    assert matched.blank_reason == "source_pair_ambiguous"


def test_gate_checkout_failure_is_operational_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli_module, "_resolve_cli_target", lambda spec: target())
    monkeypatch.setattr(
        cli_module,
        "_gate_plan",
        lambda registry_root, resolved, replicas: GatePlan(
            schema_version=1, lane_ids=["lane-a"], replicas=replicas, chunk_count=1
        ),
    )

    def failed_checkout(**kwargs: object) -> Path:
        raise OSError(f"clone failed: {kwargs['repo']}")

    monkeypatch.setattr(cli_module, "provision_checkout", failed_checkout)
    result = runner.invoke(
        cli_module.app,
        ["gate", "--target", "42", "--out", str(tmp_path / "runs")],
    )

    assert result.exit_code == 3
    assert "clone failed" in result.stderr


def test_gate_without_dispositions_writes_template_and_does_not_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_root = tmp_path / "runs"
    artifacts = prepared_artifacts(out_root, actionable=True)
    calls = patch_target_dependencies(monkeypatch, artifacts)

    result = runner.invoke(
        cli_module.app,
        ["gate", "--target", "42", "--out", str(out_root)],
    )

    assert result.exit_code == 1
    assert len(calls) == 1
    assert "--run" in result.stdout
    assert (artifacts.run.dir / "gate-dispositions.yaml").is_file()
    assert not (artifacts.run.dir / "publish-payload.json").exists()


@pytest.mark.parametrize("mode", ["target", "run"])
def test_gate_full_tier_one_inheritance_persists_document_and_auto_proceeds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
) -> None:
    out_root = tmp_path / "runs"
    current = prepared_artifacts(out_root, actionable=True)
    source = inherited_source(out_root, current)
    if mode == "target":
        calls = patch_target_dependencies(monkeypatch, current)
        mode_args = ["--target", "42"]
    else:
        calls = []
        save_gate_plan(
            current.run.dir,
            GatePlan(schema_version=1, lane_ids=["lane-a"], replicas=3, chunk_count=1),
        )
        monkeypatch.setattr(cli_module, "query_pull_request", lambda repo, number: current_state())
        mode_args = ["--run", current.run.run_id]

    result = runner.invoke(
        cli_module.app,
        [
            "gate",
            *mode_args,
            "--inherit",
            source.run_id,
            "--out",
            str(out_root),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    assert len(calls) == (1 if mode == "target" else 0)
    document = (current.run.dir / "gate-dispositions.yaml").read_text(encoding="utf-8")
    assert "decision: accepted" in document
    assert "inherited_from: source-run" in document
    verdict = json.loads((current.run.dir / "gate-verdict.json").read_text(encoding="utf-8"))
    assert verdict["findings"][0]["inherited_from"] == source.run_id
    markdown = (current.run.dir / "gate-verdict.md").read_text(encoding="utf-8")
    assert "`source-run`" in markdown
    assert (current.run.dir / "publish-payload.json").is_file()


def test_gate_partial_inheritance_writes_prefilled_template_and_pauses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_root = tmp_path / "runs"
    current = prepared_artifacts(out_root, actionable=True)
    source = inherited_source(out_root, current)
    update_source_finding(source, finding_id="moved-finding-id")
    calls = patch_target_dependencies(monkeypatch, current)

    result = runner.invoke(
        cli_module.app,
        [
            "gate",
            "--target",
            "42",
            "--inherit",
            source.run_id,
            "--out",
            str(out_root),
        ],
    )

    assert result.exit_code == 1
    assert len(calls) == 1
    assert "--inherit source-run" in result.stdout
    template = (current.run.dir / "gate-dispositions.yaml").read_text(encoding="utf-8")
    assert "decision: must_fix" in template
    assert "reason: accepted in prior run" in template
    assert "inherited_from: source-run" in template
    assert "inheritance source=source-run carried=0 prefilled=1 blank=0" in result.stdout
    verdict = json.loads((current.run.dir / "gate-verdict.json").read_text(encoding="utf-8"))
    assert verdict["inheritance_summary"] == {
        "source_run_id": "source-run",
        "carried": 0,
        "prefilled": 1,
        "blank": 0,
        "reasons": {"finding_id_changed": 1},
    }
    assert not (current.run.dir / "publish-payload.json").exists()


def test_gate_exact_id_with_changed_hunk_digest_pauses_with_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_root = tmp_path / "runs"
    current = prepared_artifacts(out_root, actionable=True)
    source = inherited_source(out_root, current)
    update_source_finding(source, hunk_sha256="0" * 64)
    patch_target_dependencies(monkeypatch, current)

    result = runner.invoke(
        cli_module.app,
        [
            "gate",
            "--target",
            "42",
            "--inherit",
            source.run_id,
            "--out",
            str(out_root),
        ],
    )

    assert result.exit_code == 1
    template = (current.run.dir / "gate-dispositions.yaml").read_text(encoding="utf-8")
    assert "decision: must_fix" in template
    assert "reason: accepted in prior run" in template
    assert "# blank_reason: content_changed" in template
    verdict = json.loads((current.run.dir / "gate-verdict.json").read_text(encoding="utf-8"))
    assert verdict["inheritance_summary"]["reasons"] == {"content_changed": 1}
    assert not (current.run.dir / "publish-payload.json").exists()


def test_gate_carried_blocker_reverifies_owner_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_root = tmp_path / "runs"
    current = prepared_artifacts(out_root, actionable=True, blocker=True)
    source = inherited_source(out_root, current)
    patch_target_dependencies(monkeypatch, current)
    permission_calls: list[str] = []

    def contributor_permission(repo: str) -> tuple[str, str]:
        permission_calls.append(repo)
        return "contributor", "write"

    monkeypatch.setattr(cli_module, "github_actor_permission", contributor_permission)

    result = runner.invoke(
        cli_module.app,
        [
            "gate",
            "--target",
            "42",
            "--inherit",
            source.run_id,
            "--out",
            str(out_root),
        ],
    )

    assert result.exit_code == 1
    assert permission_calls == ["owner/repo"]
    verdict = json.loads((current.run.dir / "gate-verdict.json").read_text(encoding="utf-8"))
    assert verdict["verdict"] == "BLOCK"
    failure = verdict["failures"][0]
    assert current.merged.groups[0].key in failure
    assert "contributor" in failure
    assert "write" in failure
    assert not (current.run.dir / "publish-payload.json").exists()


def test_gate_persists_carried_blocker_authorization_operational_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_root = tmp_path / "runs"
    current = prepared_artifacts(out_root, actionable=True, blocker=True)
    source = inherited_source(out_root, current)
    patch_target_dependencies(monkeypatch, current)
    real_permission_lookup = cli_module.github_actor_permission

    def failed_permission_lookup(repo: str) -> tuple[str, str]:
        def fake_run(command: list[str]) -> str:
            if command[:3] == ["gh", "api", "user"]:
                return "repo-owner\n"
            raise subprocess.CalledProcessError(
                returncode=1,
                cmd=command,
                stderr="permission endpoint denied the request",
            )

        return real_permission_lookup(repo, run=fake_run)

    monkeypatch.setattr(cli_module, "github_actor_permission", failed_permission_lookup)

    result = runner.invoke(
        cli_module.app,
        [
            "gate",
            "--target",
            "42",
            "--inherit",
            source.run_id,
            "--out",
            str(out_root),
        ],
    )

    assert result.exit_code == 3
    persisted = current.run.load_gate_verdict()
    assert persisted.verdict == "BLOCK"
    assert persisted.actor == "repo-owner"
    failure = persisted.failures[0]
    assert "accepted_blocker_authorization_operational_failure" in failure
    assert current.merged.groups[0].key in failure
    assert "permission" in failure
    assert "permission endpoint denied the request" in failure
    assert "permission endpoint denied the request" in result.stderr
    assert not (current.run.dir / "publish-payload.json").exists()


@pytest.mark.parametrize("source_case", ["absent", "wrong_run", "unmatched"])
def test_gate_rejects_unbound_inherited_from_claims(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_case: str,
) -> None:
    out_root = tmp_path / "runs"
    current = prepared_artifacts(out_root, actionable=True)
    save_gate_plan(
        current.run.dir,
        GatePlan(schema_version=1, lane_ids=["lane-a"], replicas=3, chunk_count=1),
    )
    source = inherited_source(out_root, current) if source_case != "absent" else None
    if source_case == "unmatched":
        assert source is not None
        update_source_finding(source, finding_id="other-id", file="src/other.py")
    claim = "other-run" if source_case == "wrong_run" else "source-run"
    finding_id = current.merged.groups[0].key
    dispositions = tmp_path / f"{source_case}.yaml"
    dispositions.write_text(
        "schema_version: 1\ndispositions:\n"
        f"  - finding_id: {finding_id}\n"
        "    decision: accepted\n"
        "    reason: reviewed\n"
        f"    inherited_from: {claim}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cli_module, "query_pull_request", lambda repo, number: current_state())
    args = [
        "gate",
        "--run",
        current.run.run_id,
        "--dispositions",
        str(dispositions),
        "--out",
        str(out_root),
    ]
    if source is not None:
        args.extend(["--inherit", source.run_id])

    result = runner.invoke(cli_module.app, args)

    assert result.exit_code == 1
    assert "inherited_from_unbound" in result.stderr
    verdict = json.loads((current.run.dir / "gate-verdict.json").read_text(encoding="utf-8"))
    assert any("inherited_from_unbound" in failure for failure in verdict["failures"])


def test_gate_allows_fresh_disposition_when_inheritance_source_is_selected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    out_root = tmp_path / "runs"
    current = prepared_artifacts(out_root, actionable=True)
    save_gate_plan(
        current.run.dir,
        GatePlan(schema_version=1, lane_ids=["lane-a"], replicas=3, chunk_count=1),
    )
    source = inherited_source(out_root, current)
    update_source_finding(source, finding_id="other-id", file="src/other.py")
    finding_id = current.merged.groups[0].key
    dispositions = tmp_path / "fresh.yaml"
    dispositions.write_text(
        "schema_version: 1\ndispositions:\n"
        f"  - finding_id: {finding_id}\n"
        "    decision: accepted\n"
        "    reason: fresh review\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(cli_module, "query_pull_request", lambda repo, number: current_state())

    result = runner.invoke(
        cli_module.app,
        [
            "gate",
            "--run",
            current.run.run_id,
            "--dispositions",
            str(dispositions),
            "--inherit",
            source.run_id,
            "--out",
            str(out_root),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stderr
    verdict = json.loads((current.run.dir / "gate-verdict.json").read_text(encoding="utf-8"))
    assert verdict["findings"][0]["inherited_from"] is None


def test_gate_resume_uses_artifacts_without_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_root = tmp_path / "runs"
    artifacts = prepared_artifacts(out_root, actionable=True)
    save_gate_plan(
        artifacts.run.dir,
        GatePlan(schema_version=1, lane_ids=["lane-a"], replicas=3, chunk_count=1),
    )
    finding_id = artifacts.merged.groups[0].key
    disposition_path = tmp_path / "dispositions.yaml"
    disposition_path.write_text(
        "schema_version: 1\ndispositions:\n"
        f"  - finding_id: {finding_id}\n"
        "    decision: accepted\n"
        "    reason: reviewed by owner\n",
        encoding="utf-8",
    )

    async def forbidden_execute(**kwargs: object) -> None:
        raise AssertionError(f"resume executed review: {kwargs}")

    monkeypatch.setattr(cli_module, "_execute_pipeline", forbidden_execute)
    monkeypatch.setattr(cli_module, "query_pull_request", lambda repo, number: current_state())
    result = runner.invoke(
        cli_module.app,
        [
            "gate",
            "--run",
            artifacts.run.run_id,
            "--dispositions",
            str(disposition_path),
            "--out",
            str(out_root),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert json.loads(result.stdout)["run_id"] == artifacts.run.run_id
    assert (artifacts.run.dir / "publish-payload.json").is_file()


def test_gate_stale_resume_fails_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_root = tmp_path / "runs"
    artifacts = prepared_artifacts(out_root)
    save_gate_plan(
        artifacts.run.dir,
        GatePlan(schema_version=1, lane_ids=["lane-a"], replicas=3, chunk_count=1),
    )
    monkeypatch.setattr(
        cli_module,
        "query_pull_request",
        lambda repo, number: current_state(head_sha="c" * 40),
    )

    result = runner.invoke(
        cli_module.app,
        ["gate", "--run", artifacts.run.run_id, "--out", str(out_root)],
    )

    assert result.exit_code == 1
    assert "stale" in result.stderr
    verdict = json.loads((artifacts.run.dir / "gate-verdict.json").read_text(encoding="utf-8"))
    assert verdict["verdict"] == "BLOCK"
    assert any("stale" in failure for failure in verdict["failures"])
    assert not (artifacts.run.dir / "publish-payload.json").exists()


def test_gate_invalid_coverage_cannot_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_root = tmp_path / "runs"
    artifacts = prepared_artifacts(out_root, valid=2)
    patch_target_dependencies(monkeypatch, artifacts)

    result = runner.invoke(
        cli_module.app,
        ["gate", "--target", "42", "--out", str(out_root)],
    )

    assert result.exit_code == 1
    assert "valid" in result.stderr
    assert not (artifacts.run.dir / "publish-payload.json").exists()


def test_gate_accepted_blocker_verifies_admin_actor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    out_root = tmp_path / "runs"
    artifacts = prepared_artifacts(out_root, actionable=True, blocker=True)
    calls = patch_target_dependencies(monkeypatch, artifacts)
    finding_id = artifacts.merged.groups[0].key
    disposition_path = tmp_path / "dispositions.yaml"
    disposition_path.write_text(
        "schema_version: 1\ndispositions:\n"
        f"  - finding_id: {finding_id}\n"
        "    decision: accepted\n"
        "    reason: owner accepts release risk\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cli_module,
        "github_actor_permission",
        lambda repo: ("repo-owner", "admin"),
    )

    result = runner.invoke(
        cli_module.app,
        [
            "gate",
            "--target",
            "42",
            "--dispositions",
            str(disposition_path),
            "--out",
            str(out_root),
            "--json",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert len(calls) == 1
    verdict = json.loads((artifacts.run.dir / "gate-verdict.json").read_text(encoding="utf-8"))
    assert verdict["actor"] == "repo-owner"
    assert (
        json.loads((artifacts.run.dir / "publish-payload.json").read_text(encoding="utf-8"))[
            "event"
        ]
        == "COMMENT"
    )
