#!/usr/bin/env python3
"""Tests for /forge activate compaction-policy config knob (A4 seam)."""
import os
import json
import sys
import importlib
from datetime import datetime, timezone
import pytest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'skills', 'forge', 'hooks'))
import forge_gate


@pytest.fixture(autouse=True)
def reload_module():
    importlib.reload(forge_gate)
    yield


def _setup(tmp_path, slug="f1"):
    root = str(tmp_path)
    fdir = os.path.join(root, "project-roadmap", "features", slug)
    os.makedirs(fdir)
    json.dump({"metric_name": "m", "event_name": "e", "baseline": 0, "target": 1,
               "harvest_days": 7, "data_source": "db"},
              open(os.path.join(fdir, "metric.json"), "w"))
    json.dump({"project": {"name": "t"}, "objectives": [],
               "features": [{"slug": slug, "name": slug, "status": "planned"}]},
              open(os.path.join(root, "project-roadmap", "roadmap.json"), "w"))
    return root


def _write_recent_log(root):
    d = os.path.join(root, ".claude", "checkpoints")
    os.makedirs(d, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    open(os.path.join(d, "compaction.log"), "w").write(ts + " context compacted\n")


def test_activate_default_advisory_not_blocked(tmp_path, capsys):
    root = _setup(tmp_path)  # no compaction.log → not recent
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop("FORGE_ACTIVATE_REQUIRES_COMPACT", None)
        with patch.object(forge_gate, "get_repo_root", return_value=root):
            forge_gate.cmd_activate("f1")  # must NOT raise SystemExit
    out = capsys.readouterr().out
    assert "SUGGESTION" in out and "Active feature set to: f1" in out


def test_activate_env_strict_blocks(tmp_path, capsys):
    root = _setup(tmp_path)
    with patch.dict(os.environ, {"FORGE_ACTIVATE_REQUIRES_COMPACT": "true"}):
        with patch.object(forge_gate, "get_repo_root", return_value=root):
            with pytest.raises(SystemExit) as e:
                forge_gate.cmd_activate("f1")
    assert e.value.code == 1
    assert "BLOCKED" in capsys.readouterr().out


def test_activate_strict_but_recent_not_blocked(tmp_path, capsys):
    root = _setup(tmp_path)
    _write_recent_log(root)  # recent compact → knob does not block
    with patch.dict(os.environ, {"FORGE_ACTIVATE_REQUIRES_COMPACT": "true"}):
        with patch.object(forge_gate, "get_repo_root", return_value=root):
            forge_gate.cmd_activate("f1")  # must NOT raise
    assert "Active feature set to: f1" in capsys.readouterr().out


@pytest.mark.parametrize("val,blocks", [
    ("1", True), ("true", True), ("yes", True), ("ON", True),
    ("0", False), ("false", False), ("no", False), ("", False),
])
def test_activate_truthy_parsing(tmp_path, val, blocks):
    root = _setup(tmp_path)  # not recent
    with patch.dict(os.environ, {"FORGE_ACTIVATE_REQUIRES_COMPACT": val}):
        with patch.object(forge_gate, "get_repo_root", return_value=root):
            if blocks:
                with pytest.raises(SystemExit) as e:
                    forge_gate.cmd_activate("f1")
                assert e.value.code == 1
            else:
                forge_gate.cmd_activate("f1")  # not blocked
