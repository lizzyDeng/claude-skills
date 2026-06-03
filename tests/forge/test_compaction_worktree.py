#!/usr/bin/env python3
"""Worktree-aware compaction-log resolution in forge_gate (A1)."""
import os
import subprocess
import sys
import importlib
from datetime import datetime
import pytest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'skills', 'forge', 'hooks'))
import forge_gate


@pytest.fixture(autouse=True)
def reload_module():
    importlib.reload(forge_gate)
    yield


def _git(cwd, *a):
    subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True, text=True)


def _make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.io")
    _git(repo, "config", "user.name", "t")
    (repo / "f").write_text("x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def _write_log(root, ts):
    d = os.path.join(str(root), ".claude", "checkpoints")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "compaction.log"), "w") as f:
        f.write(f"{ts} context compacted\n")


def _epoch(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def test_compaction_epoch_reads_main_worktree_log_from_linked(tmp_path):
    """Inside a linked worktree, /compact wrote to the MAIN worktree's log."""
    repo = _make_repo(tmp_path)
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(wt), "-b", "feat")
    _write_log(repo, "2026-06-03T12:00:00Z")  # only in MAIN worktree
    with patch.object(forge_gate, "get_repo_root", return_value=str(wt)):
        epoch = forge_gate._last_compaction_epoch()
    assert epoch == _epoch("2026-06-03T12:00:00Z")


def test_compaction_epoch_is_max_of_shared_and_local(tmp_path):
    repo = _make_repo(tmp_path)
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(wt), "-b", "feat")
    # main NEWER, local OLDER → single-path (local-only) bug would miss the newer
    # main log; correct max(shared, local) must pick the main timestamp.
    _write_log(repo, "2026-06-03T18:00:00Z")  # main = newer
    _write_log(wt, "2026-06-03T10:00:00Z")    # local = older
    with patch.object(forge_gate, "get_repo_root", return_value=str(wt)):
        epoch = forge_gate._last_compaction_epoch()
    assert epoch == _epoch("2026-06-03T18:00:00Z")


def test_compaction_main_repo_behavior_unchanged(tmp_path):
    """In the main worktree shared path == local path; behaviour identical."""
    repo = _make_repo(tmp_path)
    _write_log(repo, "2026-06-03T09:30:00Z")
    with patch.object(forge_gate, "get_repo_root", return_value=str(repo)):
        epoch = forge_gate._last_compaction_epoch()
    assert epoch == _epoch("2026-06-03T09:30:00Z")
