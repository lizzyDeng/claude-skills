#!/usr/bin/env python3
"""Tests for forge_gate.py"""

import json
import os
import subprocess
import sys
import importlib
import hashlib
import pytest
from unittest.mock import patch
from datetime import datetime

# Add the hooks directory to path and import module once
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'skills', 'forge', 'hooks'))
import forge_gate


@pytest.fixture(autouse=True)
def reload_module():
    """Reload forge_gate before each test to reset module state."""
    importlib.reload(forge_gate)
    yield

# IMPORTANT: All tests MUST use `forge_gate.function_name()` style (module-level
# reference), NOT `from forge_gate import X`. The top-level import + autouse reload
# fixture ensures consistent behavior. When implementing, replace any remaining
# `from forge_gate import X` with `forge_gate.X` throughout ALL test classes.


def sha256_file(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def trusted_artifact(step_id, path):
    data = path.read_bytes()
    return {
        "step_id": step_id,
        "path": str(path),
        "sha256": hashlib.sha256(data).hexdigest(),
        "size": len(data),
        "source": "test",
    }


def codex_review_content(plan_sha):
    gate = {
        "gate": "PASS",
        "reviewed_plan_sha256": plan_sha,
        "p0_contract_reviewed": True,
        "ac_e2e_coverage_reviewed": True,
        "weak_case_reviewed": True,
        "evidence_plan_reviewed": True,
        "p0_requirements_missing": [],
        "uncovered_ac": [],
        "unmapped_e2e_scenarios": [],
        "weak_scenarios": [],
        "non_business_assertions": [],
        "missing_evidence": [],
    }
    return (
        "## Codex Plan Review\n"
        "### Contract Gate\n"
        "```json\n"
        f"{json.dumps(gate, indent=2)}\n"
        "```\n"
        "### GATE: PASS\n"
    )


def make_fastship_phase1_state(tmp_path, slug="f1"):
    plan = tmp_path / "docs" / "superpowers" / "plans" / "2026-05-28-f1.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text("# Plan\n> **For agentic workers:** REQUIRED\n**Goal:** f1\n- [ ] **Step 1:** x\n")
    grill = tmp_path / ".claude" / ".fastship-grill-result.md"
    grill.parent.mkdir(parents=True, exist_ok=True)
    grill.write_text("## 拷问记录\nQ/A\n## 修订记录\n- none\n## 结论\n- pass\n" + "x " * 150)
    plan_rec = trusted_artifact("1.4", plan)
    codex = tmp_path / ".claude" / ".fastship-codex-review.md"
    codex.write_text(codex_review_content(plan_rec["sha256"]))
    orch = {
        "current_step": "2.0",
        "completed_steps": ["1.0", "1.1", "1.2", "1.3", "1.4", "1.5", "1.5c", "1.6"],
        "plan_path": str(plan),
        "artifacts": {
            "user_confirmed": True,
            "grill_result_path": str(grill),
            "codex_review_path": str(codex),
            "trusted_artifacts": {
                "1.4": plan_rec,
                "1.5": trusted_artifact("1.5", grill),
                "1.5c": trusted_artifact("1.5c", codex),
            },
        },
    }
    gate = {"forge_feature": slug, "plan_ready": True}
    return gate, orch


def make_fastship_done_state(tmp_path, slug="f1"):
    gate, orch = make_fastship_phase1_state(tmp_path, slug)
    result_hash = "a" * 64
    report = tmp_path / "report.md"
    report.write_text(f"## E2E Report\n\ne2e_result_hash: {result_hash}\n" + "x " * 150)
    knowledge = tmp_path / "KNOWLEDGE.md"
    knowledge.write_text("## lesson\nvalidated")
    orch["current_step"] = "done"
    orch["completed_steps"].extend(["2.0", "3.0", "3.1", "3.2", "3.3", "3.4", "3.5", "3.6"])
    orch["report_path"] = str(report)
    orch["artifacts"]["knowledge_path"] = str(knowledge)
    orch["artifacts"]["trusted_artifacts"]["3.3"] = trusted_artifact("3.3", report)
    orch["artifacts"]["trusted_artifacts"]["3.6"] = trusted_artifact("3.6", knowledge)
    gate.update({
        "test_passed": True,
        "e2e_executed": True,
        "e2e_gate_passed": True,
        "knowledge_acknowledged": True,
        "e2e_result_hash": result_hash,
        "last_loop_outcome": "pass",
        "loop_count": 1,
        "e2e_runs_since_last_record": 0,
    })
    return gate, orch


def make_harvest(tmp_path, slug="f1", **overrides):
    feature_dir = tmp_path / "project-roadmap" / "features" / slug
    feature_dir.mkdir(parents=True, exist_ok=True)
    evidence = feature_dir / "evidence.json"
    evidence.write_text(json.dumps({"actual": 0.41, "source": "warehouse query"}))
    harvest = {
        "harvested_at": "2026-05-13",
        "actual": 0.41,
        "baseline": 0.32,
        "target": 0.45,
        "verdict": "partial",
        "notes": "Some notes",
        "next_action": "iterate",
        "evidence": {
            "source": "warehouse query",
            "collected_at": "2026-05-13T00:00:00",
            "raw_path": "evidence.json",
            "raw_sha256": sha256_file(evidence),
        },
    }
    harvest.update(overrides)
    return harvest


class TestRoadmapIO:
    """Test roadmap.json read/write operations."""

    def test_load_roadmap_returns_none_if_missing(self, tmp_path):
        with patch.object(forge_gate, 'get_repo_root', return_value=str(tmp_path)):
            assert forge_gate.load_roadmap() is None

    def test_load_roadmap_returns_parsed_json(self, tmp_path):
        roadmap_dir = tmp_path / "project-roadmap"
        roadmap_dir.mkdir()
        roadmap_file = roadmap_dir / "roadmap.json"
        data = {"project": {"name": "test"}, "objectives": [], "features": []}
        roadmap_file.write_text(json.dumps(data))

        with patch.object(forge_gate, 'get_repo_root', return_value=str(tmp_path)):
            result = forge_gate.load_roadmap()
            assert result["project"]["name"] == "test"

    def test_save_roadmap_writes_json(self, tmp_path):
        roadmap_dir = tmp_path / "project-roadmap"
        roadmap_dir.mkdir()

        data = {"project": {"name": "test"}, "objectives": [], "features": []}
        with patch.object(forge_gate, 'get_repo_root', return_value=str(tmp_path)):
            forge_gate.save_roadmap(data)
            result = json.loads((roadmap_dir / "roadmap.json").read_text())
            assert result["project"]["name"] == "test"


class TestFastshipStateIO:
    """Test fastship state discovery used by Forge gates."""

    def test_loads_feature_scoped_fastship_gate_state(self, tmp_path):
        subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
        state_dir = tmp_path / ".git" / "fastship" / "sessions" / "f1"
        state_dir.mkdir(parents=True)
        state = {"plan_ready": True, "test_passed": True}
        (state_dir / "gate.json").write_text(json.dumps(state))

        with patch.object(forge_gate, "get_repo_root", return_value=str(tmp_path)):
            assert forge_gate.fastship_state_path("f1") == str((state_dir / "gate.json").resolve())
            assert forge_gate.load_fastship_state("f1")["plan_ready"] is True

    def test_requires_feature_session_when_current_state_missing(self, tmp_path):
        subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
        legacy_dir = tmp_path / ".claude"
        legacy_dir.mkdir()
        legacy = {"plan_ready": True}
        legacy_path = legacy_dir / ".ship-verify-state.json"
        legacy_path.write_text(json.dumps(legacy))

        with patch.object(forge_gate, "get_repo_root", return_value=str(tmp_path)):
            assert forge_gate.fastship_state_path() is None
            assert forge_gate.load_fastship_state() == {}

    def test_loads_feature_scoped_fastship_orchestrator_state(self, tmp_path):
        subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
        state_dir = tmp_path / ".git" / "fastship" / "sessions" / "f1"
        state_dir.mkdir(parents=True)
        state = {"current_step": "done"}
        (state_dir / "orchestrator.json").write_text(json.dumps(state))

        with patch.object(forge_gate, "get_repo_root", return_value=str(tmp_path)):
            assert forge_gate.fastship_orchestrator_state_path("f1") == str((state_dir / "orchestrator.json").resolve())
            assert forge_gate.load_fastship_orchestrator_state("f1")["current_step"] == "done"

    def test_binding_feature_does_not_reset_other_fastship_session(self, tmp_path):
        subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
        f1_dir = tmp_path / ".git" / "fastship" / "sessions" / "f1"
        f1_dir.mkdir(parents=True)
        (f1_dir / "gate.json").write_text(json.dumps({"forge_feature": "f1", "plan_ready": True}))

        with patch.object(forge_gate, "get_repo_root", return_value=str(tmp_path)):
            forge_gate.bind_fastship_state_for_feature("f2")

        assert json.loads((f1_dir / "gate.json").read_text())["plan_ready"] is True
        f2_gate = tmp_path / ".git" / "fastship" / "sessions" / "f2" / "gate.json"
        assert json.loads(f2_gate.read_text())["forge_feature"] == "f2"
        registry = json.loads((tmp_path / ".git" / "fastship" / "registry.json").read_text())
        assert registry["current_session"] == "f2"


class TestFeatureLookup:
    """Test finding features in roadmap."""

    def test_find_feature_by_slug(self):
        roadmap = {
            "features": [
                {"slug": "feat-a", "status": "draft"},
                {"slug": "feat-b", "status": "planned"},
            ]
        }
        assert forge_gate.find_feature(roadmap, "feat-b")["status"] == "planned"

    def test_find_feature_returns_none_if_missing(self):
        roadmap = {"features": [{"slug": "feat-a"}]}
        assert forge_gate.find_feature(roadmap, "nonexistent") is None


class TestValidateMetric:
    """Test metric.json validation (Gate 1)."""

    def test_valid_metric_passes(self):
        metric = {
            "metric_name": "Conversion rate",
            "event_name": "conversion",
            "baseline": 0.32,
            "target": 0.45,
            "harvest_days": 7,
            "data_source": "manual",
        }
        ok, errors = forge_gate.validate_metric(metric)
        assert ok is True
        assert errors == []

    def test_missing_required_fields_fails(self):
        metric = {"metric_name": "X"}
        ok, errors = forge_gate.validate_metric(metric)
        assert ok is False
        assert len(errors) > 0

    def test_baseline_must_be_numeric(self):
        metric = {
            "metric_name": "X",
            "event_name": "e",
            "baseline": "not a number",
            "target": 0.5,
            "harvest_days": 7,
            "data_source": "manual",
        }
        ok, errors = forge_gate.validate_metric(metric)
        assert ok is False
        assert any("baseline" in e for e in errors)


class TestValidateHarvest:
    """Test harvest.json validation (Gate 6)."""

    def test_valid_harvest_passes(self):
        harvest = {
            "harvested_at": "2026-05-13",
            "actual": 0.41,
            "baseline": 0.32,
            "target": 0.45,
            "verdict": "partial",
            "notes": "Some notes",
            "next_action": "iterate",
            "evidence": {
                "source": "warehouse query",
                "collected_at": "2026-05-13T00:00:00",
                "raw_path": "evidence.json",
                "raw_sha256": "a" * 64,
            },
        }
        ok, errors = forge_gate.validate_harvest(harvest)
        assert ok is True

    def test_invalid_verdict_fails(self):
        harvest = {
            "harvested_at": "2026-05-13",
            "actual": 0.41,
            "baseline": 0.32,
            "target": 0.45,
            "verdict": "invalid_value",
            "notes": "Notes",
            "next_action": "done",
            "evidence": {
                "source": "warehouse query",
                "collected_at": "2026-05-13T00:00:00",
                "raw_path": "evidence.json",
                "raw_sha256": "a" * 64,
            },
        }
        ok, errors = forge_gate.validate_harvest(harvest)
        assert ok is False

    def test_missing_evidence_fails(self):
        harvest = {
            "harvested_at": "2026-05-13",
            "actual": 0.41,
            "baseline": 0.32,
            "target": 0.45,
            "verdict": "partial",
            "notes": "Notes",
            "next_action": "done",
        }
        ok, errors = forge_gate.validate_harvest(harvest)
        assert ok is False
        assert any("evidence" in e for e in errors)


class TestStateTransition:
    """Test state machine transitions with gate checks."""

    def test_valid_transition_draft_to_planned(self, tmp_path):
        fastship_state, orch_state = make_fastship_phase1_state(tmp_path, "f1")
        ok, reason = forge_gate.can_transition("f1", "draft", "planned", str(tmp_path), fastship_state, orch_state)
        assert ok is True

    def test_draft_to_planned_rejects_plan_ready_without_artifacts(self, tmp_path):
        fastship_state = {"forge_feature": "f1", "plan_ready": True}
        ok, reason = forge_gate.can_transition("f1", "draft", "planned", str(tmp_path), fastship_state, {})
        assert ok is False
        assert "orchestrator" in reason or "trusted" in reason

    def test_invalid_transition_skipping_states(self):
        ok, reason = forge_gate.can_transition("f1", "draft", "shipped", None, {})
        assert ok is False
        assert "invalid transition" in reason.lower() or "not allowed" in reason.lower()


class TestGate1Metric:
    """Test Gate 1: metric.json must exist and be valid."""

    def test_g1_passes_with_valid_metric(self, tmp_path):
        features_dir = tmp_path / "project-roadmap" / "features" / "f1"
        features_dir.mkdir(parents=True)
        metric = {"metric_name": "X", "event_name": "e", "baseline": 0.3,
                  "target": 0.5, "harvest_days": 7, "data_source": "manual"}
        (features_dir / "metric.json").write_text(json.dumps(metric))
        ok, reason = forge_gate.check_g1_metric("f1", str(tmp_path))
        assert ok is True

    def test_g1_fails_without_metric_file(self, tmp_path):
        ok, reason = forge_gate.check_g1_metric("f1", str(tmp_path))
        assert ok is False
        assert "not found" in reason

    def test_g1_fails_with_invalid_metric(self, tmp_path):
        features_dir = tmp_path / "project-roadmap" / "features" / "f1"
        features_dir.mkdir(parents=True)
        (features_dir / "metric.json").write_text(json.dumps({"metric_name": "X"}))
        ok, reason = forge_gate.check_g1_metric("f1", str(tmp_path))
        assert ok is False
        assert "invalid" in reason.lower()


class TestGate6Harvest:
    """Test Gate 6: harvest.json must exist and be valid."""

    def test_g6_passes_with_valid_harvest(self, tmp_path):
        features_dir = tmp_path / "project-roadmap" / "features" / "f1"
        features_dir.mkdir(parents=True)
        harvest = make_harvest(tmp_path, "f1")
        (features_dir / "harvest.json").write_text(json.dumps(harvest))
        ok, reason = forge_gate.check_g6_harvest("f1", str(tmp_path))
        assert ok is True

    def test_g6_rejects_harvest_without_evidence_hash(self, tmp_path):
        features_dir = tmp_path / "project-roadmap" / "features" / "f1"
        features_dir.mkdir(parents=True)
        harvest = {"harvested_at": "2026-05-13", "actual": 0.41, "baseline": 0.32,
                   "target": 0.45, "verdict": "partial", "notes": "N", "next_action": "iterate"}
        (features_dir / "harvest.json").write_text(json.dumps(harvest))
        ok, reason = forge_gate.check_g6_harvest("f1", str(tmp_path))
        assert ok is False
        assert "evidence" in reason

    def test_g6_rejects_evidence_hash_mismatch(self, tmp_path):
        features_dir = tmp_path / "project-roadmap" / "features" / "f1"
        features_dir.mkdir(parents=True)
        harvest = make_harvest(tmp_path, "f1")
        harvest["evidence"]["raw_sha256"] = "b" * 64
        (features_dir / "harvest.json").write_text(json.dumps(harvest))
        ok, reason = forge_gate.check_g6_harvest("f1", str(tmp_path))
        assert ok is False
        assert "mismatch" in reason

    def test_g6_fails_without_harvest_file(self, tmp_path):
        ok, reason = forge_gate.check_g6_harvest("f1", str(tmp_path))
        assert ok is False
        assert "not found" in reason

    def test_g6_blocks_concluded_transition(self, tmp_path):
        """measuring -> concluded should be blocked without harvest.json."""
        ok, reason = forge_gate.can_transition("f1", "measuring", "concluded", str(tmp_path), {})
        assert ok is False
        assert "Gate 6" in reason


class TestOverdueHarvest:
    """Test overdue harvest detection."""

    def test_detects_overdue_feature(self):
        features = [
            {
                "slug": "f1",
                "name": "Feature 1",
                "status": "measuring",
                "shipped_at": "2026-04-01",
                "harvest_due": "2026-04-08",
            }
        ]
        # Current date is 2026-05-06 (well past due)
        overdue = forge_gate.get_overdue_harvests(features, "2026-05-06")
        assert len(overdue) == 1
        assert overdue[0]["slug"] == "f1"

    def test_not_overdue_yet(self):
        features = [
            {
                "slug": "f1",
                "name": "Feature 1",
                "status": "measuring",
                "shipped_at": "2026-05-01",
                "harvest_due": "2026-05-30",
            }
        ]
        overdue = forge_gate.get_overdue_harvests(features, "2026-05-06")
        assert len(overdue) == 0


class TestFullLifecycle:
    """Integration test: simulate full feature lifecycle."""

    def test_full_lifecycle(self, tmp_path):
        """draft → planned → in_progress → shipped(→measuring) → concluded"""

        # Setup: create project-roadmap structure
        roadmap_dir = tmp_path / "project-roadmap"
        roadmap_dir.mkdir()
        features_dir = roadmap_dir / "features" / "test-feature"
        features_dir.mkdir(parents=True)

        with patch.object(forge_gate, 'get_repo_root', return_value=str(tmp_path)):
            # 1. Create roadmap with a draft feature (G1 passed: metric.json exists)
            metric = {
                "metric_name": "Conversion",
                "event_name": "convert",
                "baseline": 0.30,
                "target": 0.50,
                "harvest_days": 7,
                "data_source": "manual",
            }
            (features_dir / "metric.json").write_text(json.dumps(metric))
            ok, _ = forge_gate.validate_metric(metric)
            assert ok

            roadmap = {
                "project": {"name": "test", "north_star": "test goal", "created_at": "2026-05-06"},
                "objectives": [{"id": "obj-1", "name": "Test Obj", "target_metric": "conv >= 0.5", "features": ["test-feature"]}],
                "features": [{
                    "slug": "test-feature",
                    "name": "Test Feature",
                    "objective_id": "obj-1",
                    "status": "draft",
                    "created_at": "2026-05-06",
                    "shipped_at": None,
                    "harvest_due": None,
                    "concluded_at": None,
                    "previous_feature": None,
                    "next_feature": None,
                }]
            }
            forge_gate.save_roadmap(roadmap)

            # 2. draft → planned (G2: trusted fastship Phase 1)
            fs_state, orch_state = make_fastship_phase1_state(tmp_path, "test-feature")
            ok, reason = forge_gate.can_transition(
                "test-feature", "draft", "planned", str(tmp_path), fs_state, orch_state
            )
            assert ok, reason

            # 3. planned → in_progress (G3: automatic)
            ok, reason = forge_gate.can_transition("test-feature", "planned", "in_progress", str(tmp_path), {})
            assert ok, reason

            # 4. in_progress → shipped (G4: trusted fastship Phase 3)
            fs_state_complete, orch_done = make_fastship_done_state(tmp_path, "test-feature")
            ok, reason = forge_gate.can_transition(
                "test-feature", "in_progress", "shipped", str(tmp_path), fs_state_complete, orch_done
            )
            assert ok, reason

            # 5. shipped → measuring (G5: automatic, done within cmd_transition)
            ok, reason = forge_gate.can_transition("test-feature", "shipped", "measuring", str(tmp_path), {})
            assert ok, reason

            # 6. measuring → concluded (G6: harvest.json must exist on disk)
            harvest = make_harvest(tmp_path, "test-feature", actual=0.52, verdict="achieved", next_action="done")
            (features_dir / "harvest.json").write_text(json.dumps(harvest))
            ok_h, _ = forge_gate.validate_harvest(harvest)
            assert ok_h
            ok, reason = forge_gate.can_transition("test-feature", "measuring", "concluded", str(tmp_path), {})
            assert ok, reason

    def test_gate4_blocks_without_fastship_complete(self, tmp_path):
        """G4 should block if fastship hasn't completed."""
        with patch.object(forge_gate, 'get_repo_root', return_value=str(tmp_path)):
            fs_state, orch_state = make_fastship_done_state(tmp_path, "f1")
            fs_state["e2e_executed"] = False
            ok, reason = forge_gate.can_transition("f1", "in_progress", "shipped", str(tmp_path), fs_state, orch_state)
            assert not ok
            assert "e2e_executed" in reason

    def test_gate4_blocks_without_e2e_gate_passed(self, tmp_path):
        with patch.object(forge_gate, 'get_repo_root', return_value=str(tmp_path)):
            fs_state, orch_state = make_fastship_done_state(tmp_path, "f1")
            fs_state["e2e_gate_passed"] = False
            ok, reason = forge_gate.can_transition("f1", "in_progress", "shipped", str(tmp_path), fs_state, orch_state)
            assert not ok
            assert "e2e_gate_passed" in reason

    def test_gate4_blocks_without_trusted_report(self, tmp_path):
        with patch.object(forge_gate, 'get_repo_root', return_value=str(tmp_path)):
            fs_state, orch_state = make_fastship_done_state(tmp_path, "f1")
            orch_state["artifacts"]["trusted_artifacts"].pop("3.3")
            ok, reason = forge_gate.can_transition("f1", "in_progress", "shipped", str(tmp_path), fs_state, orch_state)
            assert not ok
            assert "3.3" in reason


class TestRoadmapMdGeneration:
    """Test roadmap.md auto-generation."""

    def test_generates_valid_markdown(self):
        roadmap = {
            "project": {"name": "MyApp", "north_star": "Best app ever"},
            "objectives": [
                {"id": "obj-1", "name": "User Growth", "target_metric": "users >= 1000", "features": ["feat-a", "feat-b"]}
            ],
            "features": [
                {"slug": "feat-a", "name": "Onboarding", "objective_id": "obj-1", "status": "measuring", "shipped_at": "2026-05-10", "harvest_due": "2026-05-17"},
                {"slug": "feat-b", "name": "Referrals", "objective_id": "obj-1", "status": "draft", "shipped_at": None, "harvest_due": None},
            ]
        }
        md = forge_gate.generate_roadmap_md(roadmap)
        assert "# MyApp Roadmap" in md
        assert "Best app ever" in md
        assert "User Growth" in md
        assert "Onboarding" in md
        assert "Referrals" in md
        assert "measuring" in md
        assert "draft" in md

    def test_empty_roadmap(self):
        roadmap = {"project": {"name": "Empty"}, "objectives": [], "features": []}
        md = forge_gate.generate_roadmap_md(roadmap)
        assert "# Empty Roadmap" in md
        assert "No features yet" in md


# ── cmd_transition delivery integration: worktree cleanup (AC7) ───────────────

def _init_repo_with_origin_main(tmp_path):
    def g(*a):
        subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True, text=True)
    g("init", "-q", "-b", "main"); g("config", "user.email", "t@t.io"); g("config", "user.name", "t")
    (tmp_path / "README.md").write_text("base\n"); g("add", "-A"); g("commit", "-q", "-m", "base")
    g("update-ref", "refs/remotes/origin/main", "HEAD")


def _roadmap_in_progress(base, slug="test-feature"):
    fdir = base / "project-roadmap" / "features" / slug
    fdir.mkdir(parents=True)
    (fdir / "metric.json").write_text(json.dumps(
        {"metric_name": "C", "event_name": "c", "baseline": 0.3, "target": 0.5,
         "harvest_days": 7, "data_source": "manual"}))
    return {"project": {"name": "t", "north_star": "g", "created_at": "2026-05-06"},
            "objectives": [{"id": "obj-1", "name": "O", "target_metric": "c>=0.5", "features": [slug]}],
            "features": [{"slug": slug, "name": "F", "objective_id": "obj-1", "status": "in_progress",
                          "created_at": "2026-05-06", "shipped_at": None, "harvest_due": None,
                          "concluded_at": None, "previous_feature": None, "next_feature": None}]}


def _write_g4_state(base, slug="test-feature"):
    gate, orch = make_fastship_done_state(base, slug)
    sdir = forge_gate.fastship_session_dir(slug)
    os.makedirs(sdir, exist_ok=True)
    with open(forge_gate.fastship_state_path(slug), "w") as f:
        json.dump(gate, f)
    with open(forge_gate.fastship_orchestrator_state_path(slug), "w") as f:
        json.dump(orch, f)


def test_cmd_transition_triggers_sweep_and_reaps_orphan(tmp_path):
    """AC7: a delivery transition runs the sweep and removes a clean+merged orphan,
    exercised through the real cmd_transition path (not the helper directly)."""
    _init_repo_with_origin_main(tmp_path)
    wt = tmp_path / ".claude" / "worktrees" / "old-feat"
    wt.parent.mkdir(parents=True)
    subprocess.run(["git", "-C", str(tmp_path), "worktree", "add", "-q", "-b", "feat/old", str(wt), "main"],
                   check=True, capture_output=True)
    with patch.object(forge_gate, "get_repo_root", return_value=str(tmp_path)):
        forge_gate.save_roadmap(_roadmap_in_progress(tmp_path))
        _write_g4_state(tmp_path)
        forge_gate.cmd_transition("test-feature", "shipped")
        rm = forge_gate.load_roadmap()
        assert forge_gate.find_feature(rm, "test-feature")["status"] == "measuring"  # transition applied
    assert not wt.exists()  # delivery swept the clean+merged orphan


def test_cmd_transition_cleanup_failure_does_not_block(tmp_path, monkeypatch):
    """AC7: a cleanup error must never block the transition."""
    _init_repo_with_origin_main(tmp_path)
    with patch.object(forge_gate, "get_repo_root", return_value=str(tmp_path)):
        forge_gate.save_roadmap(_roadmap_in_progress(tmp_path))
        _write_g4_state(tmp_path)
        def boom(*a, **k):
            raise RuntimeError("cleanup boom")
        monkeypatch.setattr(forge_gate, "sweep_worktrees", boom)
        forge_gate.cmd_transition("test-feature", "shipped")  # must NOT raise
        rm = forge_gate.load_roadmap()
        assert forge_gate.find_feature(rm, "test-feature")["status"] == "measuring"  # never blocked


def test_cmd_transition_from_linked_worktree_reaps_sibling_keeps_self(tmp_path):
    """AC7 / P1#1: shipping from INSIDE a linked feature worktree still reaps a
    clean+merged SIBLING orphan (managed scope anchored at the main worktree),
    while never removing the current worktree itself."""
    _init_repo_with_origin_main(tmp_path)
    wt_dir = tmp_path / ".claude" / "worktrees"
    wt_dir.mkdir(parents=True)
    cur = wt_dir / "current-feature"
    sib = wt_dir / "old-sibling"
    subprocess.run(["git", "-C", str(tmp_path), "worktree", "add", "-q", "-b", "feat/cur", str(cur), "main"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "worktree", "add", "-q", "-b", "feat/sib", str(sib), "main"],
                   check=True, capture_output=True)
    # forge runs from the CURRENT linked worktree; roadmap + G4 state live there.
    with patch.object(forge_gate, "get_repo_root", return_value=str(cur)):
        forge_gate.save_roadmap(_roadmap_in_progress(cur))
        _write_g4_state(cur)
        forge_gate.cmd_transition("test-feature", "shipped")
        rm = forge_gate.load_roadmap()
        assert forge_gate.find_feature(rm, "test-feature")["status"] == "measuring"
    assert not sib.exists()   # sibling orphan reaped despite running from a linked worktree
    assert cur.exists()       # current worktree never self-removed
