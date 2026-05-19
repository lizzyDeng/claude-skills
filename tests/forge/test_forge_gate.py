#!/usr/bin/env python3
"""Tests for forge_gate.py"""

import json
import os
import subprocess
import sys
import importlib
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

    def test_loads_current_fastship_gate_state_from_git_common_dir(self, tmp_path):
        subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
        state_dir = tmp_path / ".git" / "fastship"
        state_dir.mkdir()
        state = {"plan_ready": True, "test_passed": True}
        (state_dir / "gate.json").write_text(json.dumps(state))

        with patch.object(forge_gate, "get_repo_root", return_value=str(tmp_path)):
            assert forge_gate.fastship_state_path() == str((state_dir / "gate.json").resolve())
            assert forge_gate.load_fastship_state()["plan_ready"] is True

    def test_loads_legacy_fastship_state_when_current_state_missing(self, tmp_path):
        subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
        legacy_dir = tmp_path / ".claude"
        legacy_dir.mkdir()
        legacy = {"plan_ready": True}
        legacy_path = legacy_dir / ".ship-verify-state.json"
        legacy_path.write_text(json.dumps(legacy))

        with patch.object(forge_gate, "get_repo_root", return_value=str(tmp_path)):
            assert forge_gate.fastship_state_path() == str(legacy_path)
            assert forge_gate.load_fastship_state()["plan_ready"] is True


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
        }
        ok, errors = forge_gate.validate_harvest(harvest)
        assert ok is False


class TestStateTransition:
    """Test state machine transitions with gate checks."""

    def test_valid_transition_draft_to_planned(self, tmp_path):
        # G2 requires plan_ready from fastship state
        fastship_state = {"plan_ready": True}
        ok, reason = forge_gate.can_transition("f1", "draft", "planned", str(tmp_path), fastship_state)
        assert ok is True

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
        harvest = {"harvested_at": "2026-05-13", "actual": 0.41, "baseline": 0.32,
                   "target": 0.45, "verdict": "partial", "notes": "N", "next_action": "iterate"}
        (features_dir / "harvest.json").write_text(json.dumps(harvest))
        ok, reason = forge_gate.check_g6_harvest("f1", str(tmp_path))
        assert ok is True

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

            # 2. draft → planned (G2: fastship plan_ready=true)
            fs_state = {"plan_ready": True}
            ok, reason = forge_gate.can_transition("test-feature", "draft", "planned", str(tmp_path), fs_state)
            assert ok, reason

            # 3. planned → in_progress (G3: automatic)
            ok, reason = forge_gate.can_transition("test-feature", "planned", "in_progress", str(tmp_path), {})
            assert ok, reason

            # 4. in_progress → shipped (G4: fastship complete)
            fs_state_complete = {"test_passed": True, "e2e_executed": True, "knowledge_acknowledged": True}
            ok, reason = forge_gate.can_transition("test-feature", "in_progress", "shipped", str(tmp_path), fs_state_complete)
            assert ok, reason

            # 5. shipped → measuring (G5: automatic, done within cmd_transition)
            ok, reason = forge_gate.can_transition("test-feature", "shipped", "measuring", str(tmp_path), {})
            assert ok, reason

            # 6. measuring → concluded (G6: harvest.json must exist on disk)
            harvest = {
                "harvested_at": "2026-05-13",
                "actual": 0.52,
                "baseline": 0.30,
                "target": 0.50,
                "verdict": "achieved",
                "notes": "Exceeded target",
                "next_action": "done",
            }
            (features_dir / "harvest.json").write_text(json.dumps(harvest))
            ok_h, _ = forge_gate.validate_harvest(harvest)
            assert ok_h
            ok, reason = forge_gate.can_transition("test-feature", "measuring", "concluded", str(tmp_path), {})
            assert ok, reason

    def test_gate4_blocks_without_fastship_complete(self, tmp_path):
        """G4 should block if fastship hasn't completed."""
        with patch.object(forge_gate, 'get_repo_root', return_value=str(tmp_path)):
            fs_state = {"test_passed": True, "e2e_executed": False, "knowledge_acknowledged": True}
            ok, reason = forge_gate.can_transition("f1", "in_progress", "shipped", str(tmp_path), fs_state)
            assert not ok
            assert "e2e_executed" in reason


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
