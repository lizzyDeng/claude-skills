#!/usr/bin/env python3
"""Worktree-aware compaction-log resolution in orchestrator (A1 twin)."""
import os
import subprocess
import importlib
from datetime import datetime
import pytest
import orchestrator  # sys.path injected by tests/fastship/conftest.py


@pytest.fixture(autouse=True)
def reload_module():
    importlib.reload(orchestrator)
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


def test_orchestrator_compaction_reads_main_worktree_log_from_linked(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(wt), "-b", "feat")
    _write_log(repo, "2026-06-03T12:00:00Z")  # only in MAIN worktree
    monkeypatch.setenv("FASTSHIP_REPO_ROOT", str(wt))
    assert orchestrator._last_compaction_epoch() == _epoch("2026-06-03T12:00:00Z")


def test_orchestrator_compaction_is_max_of_shared_and_local(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    wt = tmp_path / "wt"
    _git(repo, "worktree", "add", "-q", str(wt), "-b", "feat")
    _write_log(repo, "2026-06-03T18:00:00Z")  # main = newer
    _write_log(wt, "2026-06-03T10:00:00Z")    # local = older
    monkeypatch.setenv("FASTSHIP_REPO_ROOT", str(wt))
    assert orchestrator._last_compaction_epoch() == _epoch("2026-06-03T18:00:00Z")


def test_orchestrator_compaction_main_repo_unchanged(tmp_path, monkeypatch):
    repo = _make_repo(tmp_path)
    _write_log(repo, "2026-06-03T09:30:00Z")
    monkeypatch.setenv("FASTSHIP_REPO_ROOT", str(repo))
    assert orchestrator._last_compaction_epoch() == _epoch("2026-06-03T09:30:00Z")
