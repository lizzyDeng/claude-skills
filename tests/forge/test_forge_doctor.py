#!/usr/bin/env python3
"""Tests for /forge doctor (A2)."""
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


VALID_METRIC = {
    "metric_name": "signups", "event_name": "signup", "baseline": 0,
    "target": 100, "harvest_days": 7, "data_source": "db",
}


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)


def _setup(tmp_path, features, metrics):
    """features: list of {slug,status}; metrics: {slug: metric_dict}."""
    root = str(tmp_path)
    _write_json(os.path.join(root, "project-roadmap", "roadmap.json"),
                {"project": {"name": "t"}, "objectives": [],
                 "features": [{"name": f["slug"], **f} for f in features]})
    for slug, metric in metrics.items():
        _write_json(os.path.join(root, "project-roadmap", "features", slug, "metric.json"), metric)
    return root


def test_doctor_passes_valid_roadmap(tmp_path, capsys):
    root = _setup(tmp_path, [{"slug": "f1", "status": "planned"}], {"f1": VALID_METRIC})
    with patch.object(forge_gate, "get_repo_root", return_value=root):
        forge_gate.cmd_doctor()  # no SystemExit on success
    out = capsys.readouterr().out
    assert "✅ Forge doctor passed (1" in out


def test_doctor_fails_invalid_status(tmp_path, capsys):
    root = _setup(tmp_path, [{"slug": "f1", "status": "bogus"}], {"f1": VALID_METRIC})
    with patch.object(forge_gate, "get_repo_root", return_value=root):
        with pytest.raises(SystemExit) as e:
            forge_gate.cmd_doctor()
    assert e.value.code == 1
    assert "invalid status 'bogus'" in capsys.readouterr().out


def test_doctor_fails_bad_g1_metric(tmp_path, capsys):
    bad = {"metric_name": "m"}  # missing required fields
    root = _setup(tmp_path, [{"slug": "f1", "status": "planned"}], {"f1": bad})
    with patch.object(forge_gate, "get_repo_root", return_value=root):
        with pytest.raises(SystemExit) as e:
            forge_gate.cmd_doctor()
    assert e.value.code == 1
    assert "Gate 1" in capsys.readouterr().out


def test_doctor_warns_orphan_metric_but_passes(tmp_path, capsys):
    # f1 in roadmap (valid); orphan has metric.json but is NOT in roadmap
    root = _setup(tmp_path, [{"slug": "f1", "status": "planned"}],
                  {"f1": VALID_METRIC, "orphan": VALID_METRIC})
    with patch.object(forge_gate, "get_repo_root", return_value=root):
        forge_gate.cmd_doctor()  # warn only, no SystemExit
    out = capsys.readouterr().out
    assert "not listed in roadmap.json" in out
    assert "orphan" in out
