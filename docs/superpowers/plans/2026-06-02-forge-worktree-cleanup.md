# Forge Worktree Cleanup-on-Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a safe worktree reaper to forge so a feature's git worktree is auto-cleaned once it is truly delivered (branch merged into trunk), plus a manual `/forge sweep-worktrees` sweep — never deleting dirty, unmerged, or current worktrees.

**Architecture:** All logic lives in `skills/forge/hooks/forge_gate.py` (the only forge file `forge-setup` deploys, so it propagates to target projects automatically). A pure-function core (`list_worktrees` / `detect_trunk` / `worktree_is_clean` / `branch_merged` / `classify_worktree`) is wrapped by `sweep_worktrees`. `cmd_transition` calls it best-effort on delivery transitions (`shipped`/`measuring`/`concluded`); a new `cmd_sweep_worktrees` exposes it as `/forge sweep-worktrees [--dry-run]`. Safety is enforced by three independent layers: our clean+ancestor pre-check, `git worktree remove` without `--force` (git refuses dirty), and `git branch -d` (git refuses unmerged).

**Tech Stack:** Python 3.11, `subprocess` + git plumbing (`git worktree list --porcelain`, `git merge-base --is-ancestor`, `git status --porcelain`), pytest with `tmp_path` + real `git worktree add`.

---

## File Structure

- **Modify** `skills/forge/hooks/forge_gate.py`:
  - Add git/worktree helpers after `get_current_branch` (current line ~122).
  - Add reaper functions (`classify_worktree`, `remove_worktree`, `sweep_worktrees`, `_print_sweep`, `removable_orphan_count`).
  - Add `cmd_sweep_worktrees`.
  - Hook the auto-reap into `cmd_transition` after `save_forge_state(state)` (current line 1048).
  - Add `sweep-worktrees` to the `main()` dispatch (current line ~1187).
  - Add a one-line removable-orphan reminder to `cmd_status`.
- **Create** `tests/forge/test_worktree_cleanup.py` — unit + real-git end-to-end tests for the reaper.
- **Modify** `tests/forge/test_forge_gate.py` — add `cmd_transition` delivery-integration tests (reuse existing G4 fixtures).
- **Modify** `skills/forge/SKILL.md` — document `/forge sweep-worktrees` + auto-cleanup behavior + safety semantics.
- **Modify** `.claude/commands/forge.md` — same doc sync (installed-copy mirror in this repo).

**Out of scope (protection markers):** Do NOT change fastship state schema (Forge Gate 4 field compatibility — see KNOWLEDGE.md). Do NOT modify `skills/fastship/orchestrator.py` (worktree creation stays a harness responsibility). Auto-sweep is scoped to `<root>/.claude/worktrees/` only — sibling/external worktrees are never auto-removed.

---

## Task 1: Git worktree helpers

**Files:**
- Modify: `skills/forge/hooks/forge_gate.py` (insert after `get_current_branch`, ~line 122)
- Test: `tests/forge/test_worktree_cleanup.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/forge/test_worktree_cleanup.py`:

```python
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "skills" / "forge" / "hooks"))
import forge_gate  # noqa: E402


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True,
                   capture_output=True, text=True)


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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/forge/test_worktree_cleanup.py -q`
Expected: FAIL with `AttributeError: module 'forge_gate' has no attribute 'list_worktrees'`

- [ ] **Step 3: Implement the helpers**

Insert into `skills/forge/hooks/forge_gate.py` after `get_current_branch` (~line 122):

```python
def _git_out(args, cwd=None):
    """Run git; return (returncode, stdout-stripped-trailing). Never raises."""
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
        return r.returncode, r.stdout
    except Exception:
        return 1, ""


def list_worktrees(root=None):
    """Parse `git worktree list --porcelain` into dicts:
    {path, head, branch, detached, is_main}. First entry is the main worktree."""
    root = root or get_repo_root()
    if not root:
        return []
    rc, out = _git_out(["-C", root, "worktree", "list", "--porcelain"], cwd=root)
    if rc != 0:
        return []
    worktrees, cur = [], None
    for line in out.splitlines():
        if line.startswith("worktree "):
            if cur:
                worktrees.append(cur)
            cur = {"path": line[len("worktree "):], "head": None,
                   "branch": None, "detached": False, "is_main": False}
        elif cur is None:
            continue
        elif line.startswith("HEAD "):
            cur["head"] = line[len("HEAD "):]
        elif line.startswith("branch "):
            ref = line[len("branch "):]
            cur["branch"] = ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
        elif line.strip() == "detached":
            cur["detached"] = True
    if cur:
        worktrees.append(cur)
    if worktrees:
        worktrees[0]["is_main"] = True
    return worktrees


def detect_trunk(root=None):
    """Trunk ref to test merges against: origin/main, main, origin/master,
    master — first that resolves. None if none exist."""
    root = root or get_repo_root()
    if not root:
        return None
    for ref in ("origin/main", "main", "origin/master", "master"):
        rc, _ = _git_out(["-C", root, "rev-parse", "--verify", "--quiet", ref], cwd=root)
        if rc == 0:
            return ref
    return None


def worktree_is_clean(path):
    """True iff no uncommitted or untracked changes in the worktree."""
    rc, out = _git_out(["-C", path, "status", "--porcelain"], cwd=path)
    return rc == 0 and out.strip() == ""


def branch_merged(root, head_sha, trunk):
    """Conservative: True iff head_sha is an ancestor of trunk (a real merge).
    Squash-merges are intentionally NOT detected → returns False (kept safe)."""
    if not head_sha or not trunk:
        return False
    rc, _ = _git_out(["-C", root, "merge-base", "--is-ancestor", head_sha, trunk], cwd=root)
    return rc == 0


def managed_worktrees_root(root=None):
    root = root or get_repo_root()
    return os.path.realpath(os.path.join(root, ".claude", "worktrees")) if root else ""


def is_managed_worktree(wt_path, root=None):
    """True iff the worktree lives under <root>/.claude/worktrees/."""
    base = managed_worktrees_root(root)
    return bool(base) and os.path.realpath(wt_path).startswith(base + os.sep)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/forge/test_worktree_cleanup.py -q`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add skills/forge/hooks/forge_gate.py tests/forge/test_worktree_cleanup.py
git commit -m "feat(forge): worktree inspection helpers (list/trunk/clean/merged/managed)"
```

---

## Task 2: Reaper core — classify + remove + sweep

**Files:**
- Modify: `skills/forge/hooks/forge_gate.py` (append reaper functions after Task 1 helpers)
- Test: `tests/forge/test_worktree_cleanup.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/forge/test_worktree_cleanup.py`:

```python
def _merge_branch_into_origin_main(repo, branch):
    """Fast-forward origin/main to include branch tip (simulate a real merge)."""
    tip = subprocess.run(["git", "-C", str(repo), "rev-parse", branch],
                         capture_output=True, text=True).stdout.strip()
    _git(repo, "update-ref", "refs/remotes/origin/main", tip)


def test_classify_removable_when_clean_and_merged(tmp_path):
    repo = make_main_repo(tmp_path)
    wt = add_worktree(repo, "feat-a", "feat/a")  # no new commits → already ancestor
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/forge/test_worktree_cleanup.py -q`
Expected: FAIL with `AttributeError: module 'forge_gate' has no attribute 'classify_worktree'`

- [ ] **Step 3: Implement the reaper**

Append to `skills/forge/hooks/forge_gate.py` after the Task 1 helpers:

```python
def classify_worktree(wt, main_root, trunk, current_path):
    """Decide whether one worktree may be reaped.
    `main_root` MUST be the MAIN worktree path (anchors the managed-scope check),
    NOT the current worktree — otherwise running from inside a linked worktree
    mis-derives the managed base and skips every sibling.
    Returns (removable: bool, status: str, reason: str)."""
    rpath = os.path.realpath(wt["path"])
    if rpath == os.path.realpath(current_path):
        return (False, "kept-current", "当前工作区，不能删除自身")
    if wt.get("is_main"):
        return (False, "kept-main", "主工作区")
    if not is_managed_worktree(rpath, main_root):
        return (False, "kept-unmanaged", "不在 .claude/worktrees/ 下，跳过")
    if not os.path.isdir(rpath):
        return (False, "kept-missing", "worktree 路径不存在")
    if wt.get("detached") or not wt.get("branch"):
        return (False, "kept-detached", "detached HEAD，无法判定合并")
    if not worktree_is_clean(rpath):
        return (False, "kept-dirty", "有未提交/未跟踪改动")
    if not branch_merged(main_root, wt.get("head"), trunk):
        return (False, "kept-unmerged", f"分支未并入 {trunk}（squash-merge 保守保留）")
    return (True, "removable", f"干净且已并入 {trunk}")


def remove_worktree(root, wt, delete_branch=True):
    """Remove a worktree WITHOUT --force (git refuses if dirty). Optionally
    delete its branch with -d (git refuses if not fully merged).
    PRECONDITION: callers MUST run classify_worktree() first — this function does
    NOT re-validate merged/clean/managed/current status. The only safety it adds on
    its own is git's refusal to remove a dirty worktree (no --force) or delete an
    unmerged branch (-d). All exposed paths (sweep_worktrees) classify before calling."""
    rc, out = _git_out(["-C", root, "worktree", "remove", wt["path"]], cwd=root)
    if rc != 0:
        return (False, f"git worktree remove 拒绝：{out.strip()[:120]}")
    if delete_branch and wt.get("branch"):
        _git_out(["-C", root, "branch", "-d", wt["branch"]], cwd=root)
    return (True, "")


def sweep_worktrees(root=None, dry_run=False, prune=False):
    """Reap ALL managed orphan worktrees (clean + truly merged into trunk).
    - prune=True: also run `git worktree prune` (clears admin entries whose working dir
      was manually deleted — safe, only acts on missing dirs, never loses committed work).
    Returns {removed:[(path,branch,reason)], kept:[(path,branch,status,reason)], trunk, error?}.

    Note: there is intentionally no per-feature targeting. git forbids removing the
    worktree you are standing in, so a per-feature reap run from inside `/forge ship`
    would always no-op; a full sweep instead reaps every other delivered feature's
    orphan and converges to zero across deliveries."""
    root = root or get_repo_root()           # current worktree (for never-remove-self)
    res = {"removed": [], "kept": [], "trunk": None}
    if not root:
        res["error"] = "不在 git 仓库中"
        return res
    wts = list_worktrees(root)
    if not wts:
        res["error"] = "无 worktree 列表"
        return res
    # Anchor the managed-scope check and all git ops on the MAIN worktree (always
    # the first `git worktree list` entry), NOT the current worktree — so a sweep
    # run from inside a linked feature worktree still sees siblings under
    # <main>/.claude/worktrees/ and reaps them.
    main_root = os.path.realpath(wts[0]["path"])
    trunk = detect_trunk(main_root)
    res["trunk"] = trunk
    if not trunk:
        res["error"] = "未找到 trunk (origin/main|main|origin/master|master)，跳过清理"
        return res
    for wt in wts:
        removable, status, reason = classify_worktree(wt, main_root, trunk, root)
        if not removable:
            res["kept"].append((wt["path"], wt.get("branch"), status, reason))
            continue
        if dry_run:
            res["removed"].append((wt["path"], wt.get("branch"), "DRY-RUN: 干净且已合并"))
            continue
        ok, err = remove_worktree(main_root, wt)
        if ok:
            res["removed"].append((wt["path"], wt.get("branch"), reason))
        else:
            res["kept"].append((wt["path"], wt.get("branch"), "kept-remove-failed", err))
    if prune and not dry_run:
        _git_out(["-C", main_root, "worktree", "prune"], cwd=main_root)
    return res
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/forge/test_worktree_cleanup.py -q`
Expected: PASS (11 tests total)

- [ ] **Step 5: Commit**

```bash
git add skills/forge/hooks/forge_gate.py tests/forge/test_worktree_cleanup.py
git commit -m "feat(forge): safe worktree reaper (classify/remove/sweep, never deletes dirty/unmerged/current)"
```

---

## Task 3: CLI command `sweep-worktrees` + dispatch + status reminder

**Files:**
- Modify: `skills/forge/hooks/forge_gate.py` (add `_print_sweep`, `cmd_sweep_worktrees`, `removable_orphan_count`; edit `main()` + `cmd_status`)
- Test: `tests/forge/test_worktree_cleanup.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/forge/test_worktree_cleanup.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/forge/test_worktree_cleanup.py -q`
Expected: FAIL with `AttributeError: module 'forge_gate' has no attribute 'removable_orphan_count'`

- [ ] **Step 3: Implement command + dispatch + reminder**

Append to `skills/forge/hooks/forge_gate.py` after the reaper:

```python
def removable_orphan_count(root=None):
    """Count managed worktrees that are clean + merged (safe to remove)."""
    root = root or get_repo_root()
    if not root:
        return 0
    wts = list_worktrees(root)
    if not wts:
        return 0
    main_root = os.path.realpath(wts[0]["path"])
    trunk = detect_trunk(main_root)
    if not trunk:
        return 0
    n = 0
    for wt in wts:
        removable, _, _ = classify_worktree(wt, main_root, trunk, root)
        if removable:
            n += 1
    return n


def _print_sweep(res):
    if res.get("error"):
        print(f"🧹 worktree 清理跳过：{res['error']}")
        return
    trunk = res.get("trunk")
    for path, branch, reason in res["removed"]:
        print(f"🧹 已清理 worktree: {path} [{branch}] — {reason}")
    for item in res["kept"]:
        path, branch, status = item[0], item[1], item[2]
        why = item[3] if len(item) > 3 else ""
        print(f"   保留 {path} [{branch}] — {status}{(': ' + why) if why else ''}")
    print(f"🧹 worktree sweep: 清理 {len(res['removed'])} 个，保留 {len(res['kept'])} 个 (trunk={trunk})")


def cmd_sweep_worktrees(dry_run=False):
    root = get_repo_root()
    if not root:
        print("❌ 不在 git 仓库中。")
        sys.exit(1)
    # Manual sweep targets all managed worktrees and also prunes orphan admin dirs.
    _print_sweep(sweep_worktrees(root, dry_run=dry_run, prune=True))
```

Edit `cmd_transition`: after `save_forge_state(state)` (current line 1048) and before `print(f"✅ {slug}: ...")`, insert a best-effort FULL sweep:

```python
    # On delivery transitions, sweep ALL managed orphan worktrees (clean + truly
    # merged). Best-effort — wrapped so a cleanup error never blocks the transition.
    # Full sweep (not per-feature) because git forbids removing the worktree you're
    # standing in: reaping every *other* delivered feature's orphan here converges
    # to zero across deliveries. The just-shipped feature's own worktree (if you're
    # inside it) is caught later by `/forge status` + manual `/forge sweep-worktrees`.
    if target_status in ("shipped", "measuring", "concluded"):
        try:
            res = sweep_worktrees(root)
            if res.get("removed"):
                _print_sweep(res)
        except Exception as e:
            print(f"⚠️  worktree 自动清理跳过（非致命）：{e}")
```

Edit `main()`: add a branch before the final `else` (current line 1187):

```python
    elif action == "sweep-worktrees":
        cmd_sweep_worktrees(dry_run="--dry-run" in sys.argv)
```

Edit `cmd_status` (forge_gate.py:870): append at the very end of the function, immediately after the `🎯 Active` block (`if active: print(f"\n  🎯 Active: {active}")`), as the last statements in `cmd_status`:

```python
    n_orphans = removable_orphan_count()
    if n_orphans:
        print(f"\n🧹 {n_orphans} 个可清理的孤儿 worktree（干净+已合并）→ run /forge sweep-worktrees")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `python3 -m pytest tests/forge/test_worktree_cleanup.py -q`
Expected: PASS (16 tests total: Task 1 adds 5, Task 2 adds 6, Task 3 adds 5)

- [ ] **Step 5: Verify no regression in the full forge suite**

Run: `python3 -m pytest tests/forge/ -q`
Expected: PASS (existing test_forge_gate.py still green — confirms no schema/dispatch breakage)

- [ ] **Step 6: Commit**

```bash
git add skills/forge/hooks/forge_gate.py tests/forge/test_worktree_cleanup.py
git commit -m "feat(forge): /forge sweep-worktrees command + delivery auto-reap + status reminder"
```

---

## Task 4: cmd_transition delivery integration (AC7)

**Files:**
- Modify: `tests/forge/test_worktree_cleanup.py`? No — these reuse the G4 fixtures (`make_fastship_done_state`) that live in `tests/forge/test_forge_gate.py`, so add them there.

- [ ] **Step 1: Write the failing tests**

Append to `tests/forge/test_forge_gate.py` (reuses existing module-level helpers `make_fastship_done_state`; uses `forge_gate.X` style per the file's reload rule; `os`/`subprocess`/`json`/`patch` already imported):

```python
def _init_repo_with_origin_main(tmp_path):
    def g(*a):
        subprocess.run(["git", "-C", str(tmp_path), *a], check=True, capture_output=True, text=True)
    g("init", "-q", "-b", "main"); g("config", "user.email", "t@t.io"); g("config", "user.name", "t")
    (tmp_path / "README.md").write_text("base\n"); g("add", "-A"); g("commit", "-q", "-m", "base")
    g("update-ref", "refs/remotes/origin/main", "HEAD")


def _roadmap_in_progress(tmp_path, slug="test-feature"):
    fdir = tmp_path / "project-roadmap" / "features" / slug
    fdir.mkdir(parents=True)
    (fdir / "metric.json").write_text(json.dumps(
        {"metric_name": "C", "event_name": "c", "baseline": 0.3, "target": 0.5,
         "harvest_days": 7, "data_source": "manual"}))
    return {"project": {"name": "t", "north_star": "g", "created_at": "2026-05-06"},
            "objectives": [{"id": "obj-1", "name": "O", "target_metric": "c>=0.5", "features": [slug]}],
            "features": [{"slug": slug, "name": "F", "objective_id": "obj-1", "status": "in_progress",
                          "created_at": "2026-05-06", "shipped_at": None, "harvest_due": None,
                          "concluded_at": None, "previous_feature": None, "next_feature": None}]}


def _write_g4_state(tmp_path, slug="test-feature"):
    gate, orch = make_fastship_done_state(tmp_path, slug)
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python3 -m pytest tests/forge/test_forge_gate.py -k cmd_transition -q`
Expected: FAIL — `test_cmd_transition_triggers_sweep_and_reaps_orphan` fails because the worktree still exists (no sweep wired into `cmd_transition` yet).

- [ ] **Step 3: Verify implementation already added in Task 3**

The `cmd_transition` sweep hook was added in Task 3 Step 3. Re-run:
Run: `python3 -m pytest tests/forge/test_forge_gate.py -k cmd_transition -q`
Expected: PASS (3 tests).

- [ ] **Step 4: Commit**

```bash
git add tests/forge/test_forge_gate.py
git commit -m "test(forge): cmd_transition delivery triggers worktree sweep + never blocks on cleanup failure"
```

---

## Task 5: End-to-end real-git verification

**Files:**
- Test: `tests/forge/test_worktree_cleanup.py`

- [ ] **Step 1: Write the E2E scenario test**

Append to `tests/forge/test_worktree_cleanup.py`:

```python
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
```

- [ ] **Step 2: Run the E2E test**

Run: `python3 -m pytest tests/forge/test_worktree_cleanup.py::test_e2e_three_state_repo_sweep_preserves_work -v`
Expected: PASS — observable filesystem proof that unmerged/dirty work survives and merged-clean is reaped.

- [ ] **Step 3: Commit**

```bash
git add tests/forge/test_worktree_cleanup.py
git commit -m "test(forge): e2e three-state worktree sweep preserves unmerged/dirty work"
```

---

## Task 6: Documentation

**Files:**
- Modify: `skills/forge/SKILL.md`
- Modify: `.claude/commands/forge.md`

- [ ] **Step 1: Document the command + behavior in `skills/forge/SKILL.md`**

Add a section describing:
- `/forge sweep-worktrees [--dry-run]` — clean all orphan worktrees under `.claude/worktrees/`.
- Auto-cleanup: on `/forge ship` / transition to shipped/measuring/concluded, forge runs a full managed-orphan sweep (clean + truly merged). The worktree you are shipping *from* is retained (git can't remove the current worktree); it is reaped at the next delivery from elsewhere or via `/forge sweep-worktrees`.
- Safety contract (verbatim): "只删除『工作区干净 + 分支已真合并进 trunk(origin/main…)』的 worktree；脏的 / 未合并的 / 当前的 / 不在 .claude/worktrees/ 下的一律保留。squash-merge 用 ancestor 判定检测不到，保守保留，请手动处理。绝不丢失代码。"
- Add to the red-flags / notes list: cleanup never uses `--force` and uses `git branch -d` (safe deletes only).

- [ ] **Step 2: Mirror the doc into `.claude/commands/forge.md`**

Apply the equivalent section so this repo's installed command doc matches the source SKILL.md.

- [ ] **Step 3: Commit**

```bash
git add skills/forge/SKILL.md .claude/commands/forge.md
git commit -m "docs(forge): document sweep-worktrees + delivery auto-cleanup safety contract"
```

---

## Acceptance Criteria

- **AC1 (P0):** A worktree under `.claude/worktrees/` whose branch is an ancestor of trunk AND has no uncommitted/untracked changes is removed by `sweep_worktrees`. The merged branch deletion is best-effort via safe `git branch -d` (refuses unmerged; may no-op when the branch is merged into trunk but not into the local HEAD — the worktree, the actual disk/memory cost, is still removed). → `test_sweep_removes_only_merged_clean`, `test_e2e_three_state_repo_sweep_preserves_work`.
- **AC2 (P0, safety):** A worktree with uncommitted/untracked changes is NEVER removed; files preserved. → `test_classify_keeps_dirty`, E2E `dirty-feat`.
- **AC3 (P0, safety):** A worktree whose branch is not an ancestor of trunk (incl. squash-merge) is NEVER removed; commits preserved. → `test_classify_keeps_unmerged`, E2E `wip-feat`.
- **AC4 (P0, safety):** The current worktree (incl. a non-main feature worktree you are standing in) and the main worktree are NEVER removed. → `test_classify_keeps_current_and_unmanaged`, `test_classify_keeps_nonmain_current_worktree`.
- **AC5 (P0, scope):** Worktrees outside `<root>/.claude/worktrees/` are never removed by a sweep — even when clean+merged. → `test_is_managed_worktree`, `test_sweep_keeps_external_worktree`.
- **AC6 (P1):** `/forge sweep-worktrees` prints a per-worktree removed/kept summary with reasons; `--dry-run` removes nothing. → `test_cmd_sweep_worktrees_prints_summary`, `test_sweep_dry_run_removes_nothing`.
- **AC7 (P1):** A delivery transition (`shipped`/`measuring`/`concluded`) runs a best-effort full sweep of managed orphans through the real `cmd_transition` path — including reaping a clean+merged SIBLING even when shipping from inside a linked worktree — and a cleanup error never blocks the transition. → `test_cmd_transition_triggers_sweep_and_reaps_orphan`, `test_cmd_transition_from_linked_worktree_reaps_sibling_keeps_self`, `test_cmd_transition_cleanup_failure_does_not_block`.
- **AC8 (P1):** `/forge status` surfaces a count of removable orphan worktrees. → `test_removable_orphan_count` (helper) + `test_cmd_status_shows_orphan_reminder` (the printed reminder through `cmd_status`).
- **AC9 (P0, no-regression):** fastship state schema unchanged; existing `tests/forge/test_forge_gate.py` stays green. → `python3 -m pytest tests/forge/`.
- **AC10 (P1, completeness):** `/forge sweep-worktrees` also runs `git worktree prune` to clear admin entries whose working dir was manually deleted (never loses committed work). → manual sweep path passes `prune=True`.

**Known limitation (documented, not a bug):** git forbids removing the worktree you are currently inside. When `/forge ship` runs from within the feature's own worktree, the full sweep correctly no-ops on *that* worktree (`kept-current`) while still reaping every *other* delivered feature's orphan. The just-shipped worktree is reaped at the next delivery run from elsewhere, or immediately via `/forge sweep-worktrees` from the main worktree. The `/forge status` reminder (AC8) surfaces the pending orphan so it is never silently forgotten.

## E2E Verification Plan

This is a Python skills repo with no HTTP service, so the end-to-end check is a **real-git** scenario, not an HTTP scenario. Two complementary E2E paths:
1. **Reaper safety E2E** — `test_e2e_three_state_repo_sweep_preserves_work` builds a real repo with three real `git worktree`s (merged-clean / unmerged / dirty), runs the actual `sweep_worktrees`, and asserts on real filesystem + git ref state — observable business outcome (worktree dir removed) and safety outcome (unmerged/dirty files byte-for-byte preserved). Mapped to AC1–AC3, AC5.
2. **Delivery-path E2E** — `test_cmd_transition_triggers_sweep_and_reaps_orphan` drives the real `cmd_transition` (with G4-complete fastship state on disk) and asserts a clean+merged orphan worktree is removed as a side effect of shipping, plus `test_cmd_transition_cleanup_failure_does_not_block` proves cleanup never blocks delivery. Mapped to AC7. This closes the gap of the reaper E2E bypassing the dispatch/transition path.

For the fastship Phase-3 E2E step, configure `.claude/fastship.project.json` so `runner_command`/`gate_command` run these pytest E2E tests, OR pause and confirm this real-git pytest suite is the accepted E2E evidence for this feature (exemption is the user's call, not the agent's).

## Self-Review

- **Spec coverage:** requirement = "交付后自动清理 worktree, 无未提交且已并入主干, 绝不丢失代码" → AC1 (cleanup), AC2/AC3 (safety gates), AC7 (delivery trigger via real `cmd_transition`), AC4/AC5 (never-touch current/main/external). User decisions: auto+manual sweep → Task 3 (`cmd_sweep_worktrees`) + Task 3/4 (full sweep in `cmd_transition`); conservative ancestor → `branch_merged` via `merge-base --is-ancestor`. All covered.
- **Codex P1/P2 resolution:** P1#1 (per-feature auto-reap is a no-op from inside the worktree) → delivery transition now runs a *full* sweep that reaps every other orphan + documented limitation + status reminder. P1#2 (branch read from wrong state) → eliminated by dropping per-feature matching. P1#3 (AC7 untested) → Task 4 tests `cmd_transition` directly incl. never-blocks. P2 (AC4/AC5 shallow, branch-deletion overstated, E2E bypassed transition) → added `test_classify_keeps_nonmain_current_worktree`, `test_sweep_keeps_external_worktree`, softened AC1 wording, added delivery-path E2E.
- **Placeholder scan:** every step has real code/commands; no TBD/TODO.
- **Type consistency:** `classify_worktree` returns `(bool, str, str)` everywhere; `sweep_worktrees(root, dry_run, prune)` returns `{removed, kept, trunk, error?}` consumed consistently by `_print_sweep`, `cmd_sweep_worktrees`, the `cmd_transition` hook, and tests; worktree dict keys `path/head/branch/detached/is_main` consistent across `list_worktrees`/`classify_worktree`/`remove_worktree`. No `_matches_feature`/`feature=`/`_auto_reap_feature_worktree` references remain.
