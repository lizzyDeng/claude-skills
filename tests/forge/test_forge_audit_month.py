#!/usr/bin/env python3
"""Tests for /forge audit-month (A3)."""
import os
import json
import sys
import importlib
import pytest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'skills', 'forge', 'hooks'))
import forge_gate


@pytest.fixture(autouse=True)
def reload_module():
    importlib.reload(forge_gate)
    yield


METRIC = {"metric_name": "m", "event_name": "e", "baseline": 0,
          "target": 1, "harvest_days": 7, "data_source": "db"}


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)


def _setup(tmp_path, roadmap_slugs=(), metric_slugs=(), plan_slugs=(), month="2026-05"):
    root = str(tmp_path)
    _write_json(os.path.join(root, "project-roadmap", "roadmap.json"),
                {"project": {"name": "t"}, "objectives": [],
                 "features": [{"slug": s, "name": s, "status": "planned"} for s in roadmap_slugs]})
    for s in metric_slugs:
        _write_json(os.path.join(root, "project-roadmap", "features", s, "metric.json"), METRIC)
    plans_dir = os.path.join(root, "docs", "superpowers", "plans")
    os.makedirs(plans_dir, exist_ok=True)
    for s in plan_slugs:
        with open(os.path.join(plans_dir, f"{month}-10-{s}.md"), "w") as f:
            f.write("# plan\n")
    return root


def test_audit_clean_passes_with_counts(tmp_path, capsys):
    root = _setup(tmp_path, roadmap_slugs=["f1"], metric_slugs=["f1"], plan_slugs=["f1"])
    with patch.object(forge_gate, "get_repo_root", return_value=root):
        forge_gate.cmd_audit_month("2026-05")  # no SystemExit
    out = capsys.readouterr().out
    assert "✅ Forge audit completed" in out
    assert "plans:   1" in out and "metrics: 1" in out and "roadmap: 1" in out


def test_audit_roadmap_without_metric_exits_1(tmp_path, capsys):
    root = _setup(tmp_path, roadmap_slugs=["f1", "f2"], metric_slugs=["f1"], plan_slugs=["f1"])
    with patch.object(forge_gate, "get_repo_root", return_value=root):
        with pytest.raises(SystemExit) as e:
            forge_gate.cmd_audit_month("2026-05")
    assert e.value.code == 1
    out = capsys.readouterr().out
    assert "roadmap feature missing metric.json" in out and "f2" in out


def test_audit_strict_flips_exit_on_plan_without_metric(tmp_path):
    # plan f1 exists, no metric, f1 NOT in roadmap → only missing_metric_for_plan fires
    root = _setup(tmp_path, roadmap_slugs=[], metric_slugs=[], plan_slugs=["f1"])
    with patch.object(forge_gate, "get_repo_root", return_value=root):
        # non-strict: warns but exits 0 (no SystemExit)
        forge_gate.cmd_audit_month("2026-05", strict=False)
    importlib.reload(forge_gate)
    with patch.object(forge_gate, "get_repo_root", return_value=root):
        with pytest.raises(SystemExit) as e:
            forge_gate.cmd_audit_month("2026-05", strict=True)
    assert e.value.code == 1
