#!/usr/bin/env python3
"""E2E runner for the upward-ported forge engine (doctor / audit-month / compaction).

Drives the REAL forge_gate CLI (subprocess, observed exit codes + stdout) and the
REAL worktree-aware compaction logic against REAL throwaway git repos/worktrees.
Every "response" is an observed fact, not a claim. Result schema is compatible
with tests/e2e_gate.py (scenarios -> rounds -> turns). Exit 0 iff every turn held.

Usage: python3 tests/forge/forge_engine_e2e_runner.py -o /tmp/e2e_result.json
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime

HOOKS = os.path.join(os.path.dirname(__file__), "..", "..", "skills", "forge", "hooks")
sys.path.insert(0, HOOKS)
import forge_gate  # noqa: E402

GATE = os.path.join(HOOKS, "forge_gate.py")
VALID_METRIC = {"metric_name": "m", "event_name": "e", "baseline": 0,
                "target": 1, "harvest_days": 7, "data_source": "db"}


def _git(cwd, *a):
    subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True, text=True)


def _run_gate(cwd, *args):
    p = subprocess.run([sys.executable, GATE, *args], cwd=cwd, capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f)


def _mk_repo(base, name):
    repo = os.path.join(base, name)
    os.makedirs(repo)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.io")
    _git(repo, "config", "user.name", "t")
    open(os.path.join(repo, "f"), "w").write("x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def _roadmap(repo, features):
    _write_json(os.path.join(repo, "project-roadmap", "roadmap.json"),
                {"project": {"name": "t"}, "objectives": [],
                 "features": [{"slug": s, "name": s, "status": st} for s, st in features]})


def _metric(repo, slug, metric=None):
    _write_json(os.path.join(repo, "project-roadmap", "features", slug, "metric.json"),
                metric or VALID_METRIC)


def _plan(repo, name):
    d = os.path.join(repo, "docs", "superpowers", "plans")
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, name), "w").write("# plan\n")


def _write_log(root, ts):
    d = os.path.join(str(root), ".claude", "checkpoints")
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "compaction.log"), "w").write(f"{ts} context compacted\n")


def _epoch(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def run():
    turns = []

    def turn(action, fn):
        t0 = time.time()
        try:
            ok, response = fn()
            err = "" if ok else "assertion failed"
        except Exception as e:  # pragma: no cover - defensive
            ok, response, err = False, str(e), str(e)
        turns.append({"action": action, "input": "", "response": response,
                      "status": "pass" if ok else "fail", "error": err,
                      "elapsed_ms": int((time.time() - t0) * 1000)})
        return ok

    base = tempfile.mkdtemp(prefix="forge-engine-e2e-")
    all_ok = True
    try:
        # ---------- doctor ----------
        repo = _mk_repo(base, "doctor")
        _roadmap(repo, [("f1", "planned")]); _metric(repo, "f1")
        rc, out = _run_gate(repo, "doctor")
        all_ok &= turn("doctor: valid roadmap -> exit 0 + passed banner",
                       lambda: (rc == 0 and "Forge doctor passed (1" in out, f"rc={rc}"))
        _roadmap(repo, [("f1", "bogus")])
        rc, out = _run_gate(repo, "doctor")
        all_ok &= turn("doctor: invalid status -> exit 1 + names 'bogus'",
                       lambda: (rc == 1 and "invalid status 'bogus'" in out, f"rc={rc}"))
        _roadmap(repo, [("f1", "planned")]); _metric(repo, "f1", {"metric_name": "m"})
        rc, out = _run_gate(repo, "doctor")
        all_ok &= turn("doctor: bad G1 metric -> exit 1 (Gate 1)",
                       lambda: (rc == 1 and "Gate 1" in out, f"rc={rc}"))
        _metric(repo, "f1"); _metric(repo, "orphan")
        rc, out = _run_gate(repo, "doctor")
        all_ok &= turn("doctor: orphan metric -> warn but exit 0",
                       lambda: (rc == 0 and "not listed in roadmap.json" in out, f"rc={rc}"))
        empty = _mk_repo(base, "doctor-empty")
        rc, out = _run_gate(empty, "doctor")
        all_ok &= turn("doctor: missing roadmap -> exit 1",
                       lambda: (rc == 1 and "roadmap.json not found" in out, f"rc={rc}"))

        # ---------- audit-month ----------
        a = _mk_repo(base, "audit")
        _roadmap(a, [("f1", "planned")]); _metric(a, "f1"); _plan(a, "2026-05-10-f1.md")
        rc, out = _run_gate(a, "audit-month", "2026-05")
        all_ok &= turn("audit: clean -> exit 0 + completed + counts",
                       lambda: (rc == 0 and "Forge audit completed" in out and "plans:   1" in out, f"rc={rc}"))
        _roadmap(a, [("f1", "planned"), ("f2", "planned")])
        rc, out = _run_gate(a, "audit-month", "2026-05")
        all_ok &= turn("audit: roadmap-without-metric -> exit 1 + names f2",
                       lambda: (rc == 1 and "roadmap feature missing metric.json" in out and "f2" in out, f"rc={rc}"))
        b = _mk_repo(base, "audit-strict")
        _roadmap(b, []); _plan(b, "2026-05-10-x.md")
        rc0, _ = _run_gate(b, "audit-month", "2026-05")
        rc1, _ = _run_gate(b, "audit-month", "2026-05", "--strict")
        all_ok &= turn("audit: --strict flips exit on plan-without-metric (0 -> 1)",
                       lambda: (rc0 == 0 and rc1 == 1, f"non_strict={rc0} strict={rc1}"))

        # ---------- worktree-aware compaction ----------
        c = _mk_repo(base, "compact")
        wt = os.path.join(base, "compact-wt")
        _git(c, "worktree", "add", "-q", wt, "-b", "feat")
        _write_log(c, "2026-06-03T12:00:00Z")  # MAIN only
        forge_gate.get_repo_root = lambda: wt
        all_ok &= turn("compaction: linked worktree reads MAIN log (exact epoch)",
                       lambda: (forge_gate._last_compaction_epoch() == _epoch("2026-06-03T12:00:00Z"),
                                f"epoch={forge_gate._last_compaction_epoch()}"))
        _write_log(wt, "2026-06-03T08:00:00Z")  # local older than main 12:00
        all_ok &= turn("compaction: max(shared, local) keeps newer MAIN",
                       lambda: (forge_gate._last_compaction_epoch() == _epoch("2026-06-03T12:00:00Z"), "max=main"))
        forge_gate.get_repo_root = lambda: c
        _write_log(c, "2026-06-03T09:30:00Z")
        all_ok &= turn("compaction: main worktree behaviour unchanged",
                       lambda: (forge_gate._last_compaction_epoch() == _epoch("2026-06-03T09:30:00Z"), "unchanged"))
    finally:
        shutil.rmtree(base, ignore_errors=True)

    result = {
        "name": "forge-engine-upward-port-e2e",
        "status": "pass" if all_ok else "fail",
        "scenarios": [{
            "name": "doctor / audit-month / worktree-compaction (real git + real CLI)",
            "description": "Drive the real forge_gate CLI + compaction logic across real "
                           "throwaway repos and a real linked worktree; observe exit codes, "
                           "stdout and epoch values.",
            "rounds": [{"turns": turns}],
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
