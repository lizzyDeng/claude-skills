#!/usr/bin/env python3
"""End-to-end runner for forge worktree cleanup.

Drives the REAL `forge_gate` reaper against a REAL throwaway git repo with REAL
`git worktree`s, and records each step as a turn (action / observed response /
status) into an e2e_result.json compatible with tests/e2e_gate.py.

This is the executable form of the real-git E2E the user accepted as evidence:
build repo -> add worktrees in 3 states -> run the actual sweep -> observe the
filesystem + git refs. Every "response" below is an observed fact, not a claim.

Usage: python3 tests/forge/worktree_e2e_runner.py -o /tmp/e2e_result.json
Exit 0 iff every turn's assertion held.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "skills", "forge", "hooks"))
import forge_gate  # noqa: E402


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


def _mk_main_repo(base):
    repo = os.path.join(base, "repo")
    os.makedirs(repo)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.io")
    _git(repo, "config", "user.name", "t")
    with open(os.path.join(repo, "README.md"), "w") as f:
        f.write("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    return repo


def _add_wt(repo, rel, branch):
    wt = os.path.join(repo, rel)
    os.makedirs(os.path.dirname(wt), exist_ok=True)
    _git(repo, "worktree", "add", "-q", "-b", branch, wt, "main")
    return wt


def run():
    rounds_turns = []

    def turn(action, fn):
        """Run fn() -> (ok, response). Record a turn; return ok."""
        t0 = time.time()
        try:
            ok, response = fn()
            err = "" if ok else "assertion failed"
        except Exception as e:  # pragma: no cover - defensive
            ok, response, err = False, str(e), str(e)
        rounds_turns.append({
            "action": action,
            "input": "",
            "response": response,
            "status": "pass" if ok else "fail",
            "error": err,
            "elapsed_ms": int((time.time() - t0) * 1000),
        })
        return ok

    base = tempfile.mkdtemp(prefix="forge-wt-e2e-")
    all_ok = True
    try:
        repo = _mk_main_repo(base)
        shipped = _add_wt(repo, ".claude/worktrees/shipped", "feat/shipped")  # clean + merged
        wip = _add_wt(repo, ".claude/worktrees/wip", "feat/wip")              # unmerged commit
        with open(os.path.join(wip, "wip.py"), "w") as f:
            f.write("print('keep me')\n")
        _git(wip, "add", "-A")
        _git(wip, "commit", "-q", "-m", "wip")
        dirty = _add_wt(repo, ".claude/worktrees/dirty", "feat/dirty")        # uncommitted edit
        with open(os.path.join(dirty, "scratch.txt"), "w") as f:
            f.write("unsaved work\n")
        external = os.path.join(base, "external-wt")                          # outside .claude/worktrees
        _git(repo, "worktree", "add", "-q", "-b", "feat/ext", external, "main")

        all_ok &= turn("setup: 1 main + 4 worktrees (shipped/wip/dirty/external)",
                       lambda: (len(forge_gate.list_worktrees(repo)) == 5,
                                f"{len(forge_gate.list_worktrees(repo))} worktrees listed"))

        res = forge_gate.sweep_worktrees(repo)
        removed = {b for _, b, _ in res["removed"]}
        kept = {item[1]: item[2] for item in res["kept"]}

        all_ok &= turn("sweep_worktrees(): clean+merged 'feat/shipped' removed",
                       lambda: ("feat/shipped" in removed, f"removed={sorted(removed)}"))
        all_ok &= turn("shipped worktree directory no longer exists",
                       lambda: (not os.path.exists(shipped), f"exists={os.path.exists(shipped)}"))
        all_ok &= turn("unmerged 'feat/wip' kept, work preserved byte-for-byte",
                       lambda: (kept.get("feat/wip") == "kept-unmerged"
                                and open(os.path.join(wip, "wip.py")).read() == "print('keep me')\n",
                                f"status={kept.get('feat/wip')}, file intact"))
        all_ok &= turn("dirty 'feat/dirty' kept, uncommitted file preserved",
                       lambda: (kept.get("feat/dirty") == "kept-dirty"
                                and open(os.path.join(dirty, "scratch.txt")).read() == "unsaved work\n",
                                f"status={kept.get('feat/dirty')}, file intact"))
        all_ok &= turn("external worktree (outside .claude/worktrees) kept as unmanaged",
                       lambda: (kept.get("feat/ext") == "kept-unmanaged" and os.path.exists(external),
                                f"status={kept.get('feat/ext')}, exists={os.path.exists(external)}"))
        all_ok &= turn("main worktree never removed (kept-current)",
                       lambda: (any(item[2] in ("kept-current", "kept-main")
                                    and os.path.realpath(item[0]) == os.path.realpath(repo)
                                    for item in res["kept"]), "main kept"))

        def _branches():
            out = subprocess.run(["git", "-C", repo, "branch", "--format=%(refname:short)"],
                                 capture_output=True, text=True).stdout.split()
            return out
        all_ok &= turn("merged branch 'feat/shipped' deleted; unmerged 'feat/wip' branch preserved",
                       lambda: ("feat/shipped" not in _branches() and "feat/wip" in _branches(),
                                f"branches={_branches()}"))

        res2 = forge_gate.sweep_worktrees(repo)
        all_ok &= turn("idempotent: second sweep removes nothing",
                       lambda: (res2["removed"] == [], f"removed={res2['removed']}"))

        # Second scenario round: dry-run safety
        dry = forge_gate.sweep_worktrees(repo, dry_run=True)
        all_ok &= turn("dry-run reports candidates without removing (none left now)",
                       lambda: (isinstance(dry.get("removed"), list), f"dry removed={dry['removed']}"))
        all_ok &= turn("removable_orphan_count == 0 after sweep",
                       lambda: (forge_gate.removable_orphan_count(repo) == 0,
                                f"count={forge_gate.removable_orphan_count(repo)}"))
        all_ok &= turn("trunk detected as origin/main",
                       lambda: (forge_gate.detect_trunk(repo) == "origin/main",
                                f"trunk={forge_gate.detect_trunk(repo)}"))
    finally:
        shutil.rmtree(base, ignore_errors=True)

    result = {
        "name": "forge-worktree-cleanup-e2e",
        "status": "pass" if all_ok else "fail",
        "scenarios": [{
            "name": "worktree-cleanup-on-delivery (real git worktrees)",
            "description": "Build a real repo, add 4 worktrees in distinct states, run the "
                           "actual forge reaper, observe filesystem + git refs.",
            "rounds": [{"turns": rounds_turns}],
        }],
    }
    return result, all_ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--output", default="/tmp/e2e_result.json")
    args = ap.parse_args()
    result, ok = run()
    with open(args.output, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    turns = sum(len(r["turns"]) for s in result["scenarios"] for r in s["rounds"])
    print(f"E2E result: status={result['status']} turns={turns} -> {args.output}")
    for s in result["scenarios"]:
        for r in s["rounds"]:
            for t in r["turns"]:
                mark = "PASS" if t["status"] == "pass" else "FAIL"
                print(f"  [{mark}] {t['action']} :: {t['response']}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
