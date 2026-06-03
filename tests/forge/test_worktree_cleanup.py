"""Tests for forge worktree cleanup-on-delivery (reaper + sweep)."""
import json
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'skills', 'forge', 'hooks'))
import forge_gate  # noqa: E402


@pytest.fixture(autouse=True)
def _reload():
    import importlib
    importlib.reload(forge_gate)


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def make_main_repo(tmp_path):
    """A repo with one commit on `main` and an `origin/main` ref."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.io")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    # Fake an origin/main so detect_trunk finds a remote trunk.
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    return repo


def add_worktree(repo, name, branch):
    """Create .claude/worktrees/<name> on a new branch off main."""
    wt = repo / ".claude" / "worktrees" / name
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "-q", "-b", branch, str(wt), "main")
    return wt


# ── Task 1: helpers ──────────────────────────────────────────────────────────

def test_list_worktrees_parses_porcelain(tmp_path):
    repo = make_main_repo(tmp_path)
    add_worktree(repo, "feat-a", "feat/a")
    wts = forge_gate.list_worktrees(str(repo))
    paths = [os.path.realpath(w["path"]) for w in wts]
    assert os.path.realpath(str(repo)) in paths
    assert os.path.realpath(str(repo / ".claude/worktrees/feat-a")) in paths
    main_wt = next(w for w in wts if os.path.realpath(w["path"]) == os.path.realpath(str(repo)))
    assert main_wt["is_main"] is True
    feat = next(w for w in wts if w.get("branch") == "feat/a")
    assert feat["head"]


def test_detect_trunk_prefers_origin_main(tmp_path):
    repo = make_main_repo(tmp_path)
    assert forge_gate.detect_trunk(str(repo)) == "origin/main"


def test_worktree_is_clean(tmp_path):
    repo = make_main_repo(tmp_path)
    wt = add_worktree(repo, "feat-a", "feat/a")
    assert forge_gate.worktree_is_clean(str(wt)) is True
    (wt / "dirty.txt").write_text("x\n")
    assert forge_gate.worktree_is_clean(str(wt)) is False


def test_branch_merged_ancestor_only(tmp_path):
    repo = make_main_repo(tmp_path)
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "main"],
                          capture_output=True, text=True).stdout.strip()
    # main's own commit is an ancestor of origin/main → merged.
    assert forge_gate.branch_merged(str(repo), head, "origin/main") is True
    # A new unmerged commit is NOT an ancestor.
    wt = add_worktree(repo, "feat-a", "feat/a")
    (wt / "f.txt").write_text("x\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "wip")
    new_head = subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip()
    assert forge_gate.branch_merged(str(repo), new_head, "origin/main") is False


def test_is_managed_worktree(tmp_path):
    repo = make_main_repo(tmp_path)
    inside = repo / ".claude" / "worktrees" / "feat-a"
    outside = tmp_path / "sibling"
    assert forge_gate.is_managed_worktree(str(inside), str(repo)) is True
    assert forge_gate.is_managed_worktree(str(outside), str(repo)) is False
    assert forge_gate.is_managed_worktree(str(repo), str(repo)) is False


# ── Task 2: reaper core ──────────────────────────────────────────────────────

def _merge_branch_into_origin_main(repo, branch):
    """Fast-forward origin/main to include branch tip (simulate a real merge)."""
    tip = subprocess.run(["git", "-C", str(repo), "rev-parse", branch],
                         capture_output=True, text=True).stdout.strip()
    _git(repo, "update-ref", "refs/remotes/origin/main", tip)


def test_classify_removable_when_clean_and_merged(tmp_path):
    repo = make_main_repo(tmp_path)
    add_worktree(repo, "feat-a", "feat/a")  # no new commits → already ancestor
    wts = forge_gate.list_worktrees(str(repo))
    trunk = forge_gate.detect_trunk(str(repo))
    feat = next(w for w in wts if w.get("branch") == "feat/a")
    removable, status, _ = forge_gate.classify_worktree(feat, str(repo), trunk, str(repo))
    assert removable is True and status == "removable"


def test_classify_keeps_dirty(tmp_path):
    repo = make_main_repo(tmp_path)
    wt = add_worktree(repo, "feat-a", "feat/a")
    (wt / "dirty.txt").write_text("x\n")
    feat = next(w for w in forge_gate.list_worktrees(str(repo)) if w.get("branch") == "feat/a")
    removable, status, _ = forge_gate.classify_worktree(feat, str(repo), "origin/main", str(repo))
    assert removable is False and status == "kept-dirty"


def test_classify_keeps_unmerged(tmp_path):
    repo = make_main_repo(tmp_path)
    wt = add_worktree(repo, "feat-a", "feat/a")
    (wt / "f.txt").write_text("x\n")
    _git(wt, "add", "-A")
    _git(wt, "commit", "-q", "-m", "wip")
    feat = next(w for w in forge_gate.list_worktrees(str(repo)) if w.get("branch") == "feat/a")
    removable, status, _ = forge_gate.classify_worktree(feat, str(repo), "origin/main", str(repo))
    assert removable is False and status == "kept-unmerged"


def test_classify_keeps_current_and_unmanaged(tmp_path):
    repo = make_main_repo(tmp_path)
    wts = forge_gate.list_worktrees(str(repo))
    main_wt = wts[0]
    # main worktree == current → never removable
    removable, status, _ = forge_gate.classify_worktree(main_wt, str(repo), "origin/main", str(repo))
    assert removable is False and status in ("kept-current", "kept-main")


def test_sweep_removes_only_merged_clean(tmp_path):
    repo = make_main_repo(tmp_path)
    # merged + clean → removed
    add_worktree(repo, "done", "feat/done")
    # unmerged → kept
    wt_un = add_worktree(repo, "wip", "feat/wip")
    (wt_un / "f.txt").write_text("x\n"); _git(wt_un, "add", "-A"); _git(wt_un, "commit", "-q", "-m", "wip")
    # dirty → kept
    wt_dirty = add_worktree(repo, "dirty", "feat/dirty")
    (wt_dirty / "d.txt").write_text("x\n")

    res = forge_gate.sweep_worktrees(str(repo))
    removed_branches = {b for _, b, _ in res["removed"]}
    kept_branches = {item[1] for item in res["kept"]}
    assert "feat/done" in removed_branches
    assert "feat/wip" in kept_branches and "feat/dirty" in kept_branches
    # Filesystem: removed dir gone, kept dirs intact with files preserved
    assert not (repo / ".claude/worktrees/done").exists()
    assert (repo / ".claude/worktrees/wip/f.txt").exists()
    assert (repo / ".claude/worktrees/dirty/d.txt").read_text() == "x\n"
    # Merged branch deleted (safe -d); unmerged branch preserved
    branches = subprocess.run(["git", "-C", str(repo), "branch", "--format=%(refname:short)"],
                              capture_output=True, text=True).stdout.split()
    assert "feat/done" not in branches
    assert "feat/wip" in branches


def test_sweep_dry_run_removes_nothing(tmp_path):
    repo = make_main_repo(tmp_path)
    add_worktree(repo, "done", "feat/done")
    res = forge_gate.sweep_worktrees(str(repo), dry_run=True)
    assert any(b == "feat/done" for _, b, _ in res["removed"])
    assert (repo / ".claude/worktrees/done").exists()  # still there


# ── Task 3: command + reminder + scope ───────────────────────────────────────

def test_removable_orphan_count(tmp_path):
    repo = make_main_repo(tmp_path)
    add_worktree(repo, "done", "feat/done")          # removable
    wt_un = add_worktree(repo, "wip", "feat/wip")    # unmerged
    (wt_un / "f.txt").write_text("x\n"); _git(wt_un, "add", "-A"); _git(wt_un, "commit", "-q", "-m", "wip")
    assert forge_gate.removable_orphan_count(str(repo)) == 1


def test_cmd_sweep_worktrees_prints_summary(tmp_path, capsys, monkeypatch):
    repo = make_main_repo(tmp_path)
    add_worktree(repo, "done", "feat/done")
    monkeypatch.setattr(forge_gate, "get_repo_root", lambda: str(repo))
    forge_gate.cmd_sweep_worktrees(dry_run=True)
    out = capsys.readouterr().out
    assert "feat/done" in out
    assert "sweep" in out.lower()
    assert (repo / ".claude/worktrees/done").exists()  # dry-run kept it


def test_cmd_status_shows_orphan_reminder(tmp_path, capsys, monkeypatch):
    """AC8: /forge status surfaces the removable-orphan count + sweep hint."""
    repo = make_main_repo(tmp_path)
    add_worktree(repo, "done", "feat/done")  # clean + merged → removable orphan
    rdir = repo / "project-roadmap"
    rdir.mkdir(exist_ok=True)
    (rdir / "roadmap.json").write_text(json.dumps(
        {"project": {"name": "t", "north_star": "g"}, "objectives": [], "features": []}))
    monkeypatch.setattr(forge_gate, "get_repo_root", lambda: str(repo))
    forge_gate.cmd_status()
    out = capsys.readouterr().out
    assert "孤儿 worktree" in out and "sweep-worktrees" in out


def test_sweep_keeps_external_worktree(tmp_path):
    """AC5: a clean+merged worktree OUTSIDE .claude/worktrees/ is never removed."""
    repo = make_main_repo(tmp_path)
    external = tmp_path / "sibling-wt"   # NOT under .claude/worktrees/
    _git(repo, "worktree", "add", "-q", "-b", "feat/ext", str(external), "main")
    res = forge_gate.sweep_worktrees(str(repo))
    assert all(b != "feat/ext" for _, b, _ in res["removed"])
    assert external.exists()
    assert any(item[1] == "feat/ext" and item[2] == "kept-unmanaged" for item in res["kept"])


def test_classify_keeps_nonmain_current_worktree(tmp_path):
    """AC4: when the *current* worktree is a non-main feature worktree (clean+merged),
    it is still kept — git cannot remove the worktree you are standing in."""
    repo = make_main_repo(tmp_path)
    wt = add_worktree(repo, "here", "feat/here")  # clean + merged (no new commits)
    feat = next(w for w in forge_gate.list_worktrees(str(repo)) if w.get("branch") == "feat/here")
    # Simulate forge running from inside this feature worktree: current_path == wt.
    removable, status, _ = forge_gate.classify_worktree(feat, str(repo), "origin/main", str(wt))
    assert removable is False and status == "kept-current"


# ── Task 5: end-to-end real-git ──────────────────────────────────────────────

def test_e2e_three_state_repo_sweep_preserves_work(tmp_path, monkeypatch):
    """End-to-end against a REAL repo with REAL `git worktree`:
    merged+clean is removed; dirty and unmerged are preserved with files intact;
    re-running is idempotent."""
    repo = make_main_repo(tmp_path)
    add_worktree(repo, "shipped-feat", "feat/shipped")          # clean + merged
    wt_wip = add_worktree(repo, "wip-feat", "feat/wip")          # unmerged commit
    (wt_wip / "wip.py").write_text("print('keep me')\n")
    _git(wt_wip, "add", "-A"); _git(wt_wip, "commit", "-q", "-m", "wip")
    wt_dirty = add_worktree(repo, "dirty-feat", "feat/dirty")    # uncommitted edit
    (wt_dirty / "scratch.txt").write_text("unsaved work\n")

    monkeypatch.setattr(forge_gate, "get_repo_root", lambda: str(repo))
    res1 = forge_gate.sweep_worktrees(str(repo))

    assert not (repo / ".claude/worktrees/shipped-feat").exists()
    assert (repo / ".claude/worktrees/wip-feat/wip.py").read_text() == "print('keep me')\n"
    assert (repo / ".claude/worktrees/dirty-feat/scratch.txt").read_text() == "unsaved work\n"
    assert {b for _, b, _ in res1["removed"]} == {"feat/shipped"}

    # Idempotent: second run removes nothing new.
    res2 = forge_gate.sweep_worktrees(str(repo))
    assert res2["removed"] == []
