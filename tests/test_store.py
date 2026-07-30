from __future__ import annotations

import json
from pathlib import Path

import pytest

from rvw.adjudicate import AdjudicationOutcome
from rvw.diffbudget import DiffBudgetReport, DiffChunkPlacement
from rvw.discover import DiscoverResult, EnrichedFinding, LaneCoverage, RunCoverage
from rvw.merge import merge
from rvw.schema import Tier, Verdict
from rvw.store import InvalidRunId, RunHandle, RunNotFound, RunStore, StageMissing
from rvw.target import ResolvedTarget

FIXTURES = Path(__file__).parent / "fixtures"


def target_fixture() -> ResolvedTarget:
    return ResolvedTarget(
        kind="pr",
        repo="APIFuseHQ/apifuse",
        base_sha="a" * 40,
        head_sha="b5b9a7c8a" + "0" * 31,
        changed_paths=["providers/naver-map/index.ts"],
        diff="diff --git a/a b/a\n",
        pr_number=1119,
    )


def findings_fixture() -> list[EnrichedFinding]:
    raw = json.loads((FIXTURES / "smoke_1119_findings.json").read_text(encoding="utf-8"))
    return [EnrichedFinding.model_validate(item) for item in raw]


def outcome_fixture() -> AdjudicationOutcome:
    raw = json.loads((FIXTURES / "smoke_1119_outcome.json").read_text(encoding="utf-8"))
    return AdjudicationOutcome(
        verdicts={key: Verdict(value) for key, value in raw["verdicts"].items()},
        reasons=raw["reasons"],
        evidence=raw["evidence"],
        replica_votes={
            key: [Verdict(value) for value in values]
            for key, values in raw["replica_votes"].items()
        },
        unresolved=raw["unresolved"],
        coerced_rejections=raw["coerced_rejections"],
    )


def test_round_trips_every_stage(tmp_path: Path) -> None:
    store = RunStore(tmp_path)
    target = target_fixture()
    run = store.create(target)
    findings = findings_fixture()
    coverage = [
        LaneCoverage(
            lane_id="slop-hygiene",
            dispatched=3,
            valid=2,
            findings=4,
            runs=[
                RunCoverage(
                    replica=replica,
                    chunk=1,
                    valid=replica < 3,
                    findings=4 if replica == 1 else 0,
                    invalid_reason=None if replica < 3 else "scripted_invalid",
                )
                for replica in range(1, 4)
            ],
        )
    ]
    budget = DiffBudgetReport(
        kept_files=["a.ts"],
        excluded_files=["generated.ts"],
        excluded_reason={"generated.ts": "generated-path"},
        kept_chars=1234,
        excluded_chars=567,
        chunk_count=1,
        chunks=[DiffChunkPlacement(index=1, files=["a.ts"], chars=1234)],
    )
    discovered = DiscoverResult(
        lane_results={}, findings=findings, coverage=coverage, budget=budget
    )
    tiers = {
        finding.lane_id: (Tier.DYNAMIC if finding.lane_id.startswith("dynamic/") else Tier.BASE)
        for finding in findings
    }
    merged = merge(findings, lane_tiers=tiers)
    outcome = outcome_fixture()
    report = "# 보고서\n\n본문\n"

    run.save_target(target)
    run.save_discover(discovered)
    run.save_merge(merged)
    run.save_outcome(outcome)
    run.save_report(report)

    reopened = store.open(run.run_id)
    assert reopened.load_target() == target
    assert reopened.load_discover() == discovered
    assert reopened.load_merge() == merged
    assert reopened.load_outcome() == outcome
    assert reopened.load_report() == report


def test_create_uses_target_specific_run_id(tmp_path: Path) -> None:
    run = RunStore(tmp_path).create(target_fixture())

    assert run.run_id.startswith("rvw-")
    assert run.run_id.endswith("-pr-1119")
    assert run.dir.is_dir()


def test_open_unknown_run_raises(tmp_path: Path) -> None:
    with pytest.raises(RunNotFound, match="missing-run"):
        RunStore(tmp_path).open("missing-run")


@pytest.mark.parametrize("run_id", [".", "..", "nested/run"])
def test_open_rejects_non_child_run_ids_before_lookup(tmp_path: Path, run_id: str) -> None:
    with pytest.raises(ValueError, match="invalid run ID"):
        RunStore(tmp_path).open(run_id)


@pytest.mark.parametrize("run_id", ["bad\nrun", "bad`run", "bad\u202erun"])
def test_open_rejects_unsafe_run_id_characters_before_filesystem_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    run_id: str,
) -> None:
    def forbidden_resolve(path: Path, *args: object, **kwargs: object) -> Path:
        del path, args, kwargs
        raise AssertionError("unsafe run ID reached filesystem resolution")

    monkeypatch.setattr(Path, "resolve", forbidden_resolve)

    with pytest.raises(InvalidRunId, match="invalid run ID"):
        RunStore(tmp_path).open(run_id)


def test_open_rejects_symlinked_run_directory(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "linked-run").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="invalid run ID"):
        RunStore(root).open("linked-run")


@pytest.mark.parametrize(
    ("artifact_name", "loader_name"),
    [("target.json", "load_target"), ("gate-verdict.json", "load_gate_verdict")],
)
def test_inheritance_artifact_loaders_reject_symlinked_files(
    tmp_path: Path,
    artifact_name: str,
    loader_name: str,
) -> None:
    root = tmp_path / "runs"
    run_dir = root / "safe-run"
    run_dir.mkdir(parents=True)
    foreign = tmp_path / "foreign.json"
    foreign.write_text("{}\n", encoding="utf-8")
    (run_dir / artifact_name).symlink_to(foreign)
    run = RunHandle(run_id="safe-run", dir=run_dir)

    with pytest.raises(InvalidRunId, match="invalid run ID"):
        getattr(run, loader_name)()


def test_missing_stage_names_stage_and_directory(tmp_path: Path) -> None:
    run = RunStore(tmp_path).create(target_fixture())

    with pytest.raises(StageMissing) as caught:
        run.load_merge()

    assert "MERGE" in str(caught.value)
    assert str(run.dir) in str(caught.value)
