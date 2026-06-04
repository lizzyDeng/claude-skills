#!/usr/bin/env python3
"""E2E runner for the /forge activate compaction-policy config knob (A4 seam).

Drives the REAL forge_gate CLI `activate` against a REAL throwaway git repo,
toggling FORGE_ACTIVATE_REQUIRES_COMPACT and the presence of a recent
compaction.log. Every turn records expect + observed actual (exit code + stdout).
Result schema compatible with tests/e2e_gate.py. Exit 0 iff every turn held.

Usage: python3 tests/forge/forge_activate_e2e_runner.py -o /tmp/e2e_result.json
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone

_HERE = os.path.dirname(os.path.abspath(__file__))
HOOKS = os.path.abspath(os.path.join(_HERE, "..", "..", "skills", "forge", "hooks"))
GATE = os.path.join(HOOKS, "forge_gate.py")


def _git(cwd, *a):
    subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True, text=True)


def _snip(out):
    return " ⏎ ".join(l for l in out.strip().splitlines() if l.strip())[:200]


def _mk_repo(base):
    repo = os.path.join(base, "repo")
    os.makedirs(repo)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.io")
    _git(repo, "config", "user.name", "t")
    fdir = os.path.join(repo, "project-roadmap", "features", "f1")
    os.makedirs(fdir)
    json.dump({"metric_name": "m", "event_name": "e", "baseline": 0, "target": 1,
               "harvest_days": 7, "data_source": "db"}, open(os.path.join(fdir, "metric.json"), "w"))
    json.dump({"project": {"name": "t"}, "objectives": [],
               "features": [{"slug": "f1", "name": "f1", "status": "planned"}]},
              open(os.path.join(repo, "project-roadmap", "roadmap.json"), "w"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def _set_recent(repo, recent):
    d = os.path.join(repo, ".claude", "checkpoints")
    os.makedirs(d, exist_ok=True)
    log = os.path.join(d, "compaction.log")
    if recent:
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        open(log, "w").write(ts + " context compacted\n")
    elif os.path.exists(log):
        os.remove(log)


def _activate(repo, env_val):
    env = dict(os.environ)
    env.pop("FORGE_ACTIVATE_REQUIRES_COMPACT", None)
    if env_val is not None:
        env["FORGE_ACTIVATE_REQUIRES_COMPACT"] = env_val
    p = subprocess.run([sys.executable, GATE, "activate", "f1"], cwd=repo,
                       capture_output=True, text=True, env=env)
    return p.returncode, p.stdout + p.stderr


def run():
    turns = []

    def turn(action, expect, fn):
        t0 = time.time()
        try:
            ok, actual = fn()
            err = "" if ok else "assertion failed"
        except Exception as e:  # pragma: no cover
            ok, actual, err = False, str(e), str(e)
        turns.append({"action": action, "input": "", "expect": expect, "actual": actual,
                      "response": actual, "status": "pass" if ok else "fail", "error": err,
                      "elapsed_ms": int((time.time() - t0) * 1000)})
        return ok

    base = tempfile.mkdtemp(prefix="forge-activate-e2e-")
    all_ok = True
    try:
        repo = _mk_repo(base)

        # (env_val, recent, expect_rc, must_contain, expect_desc)
        cases = [
            (None, False, 0, "SUGGESTION", "env unset + not-recent → advisory exit 0"),
            ("true", False, 1, "BLOCKED", "knob=true + not-recent → BLOCKED exit 1"),
            ("true", True, 0, "Active feature set", "knob=true + recent → not blocked exit 0"),
            ("1", False, 1, "BLOCKED", "knob=1 + not-recent → exit 1"),
            ("yes", False, 1, "BLOCKED", "knob=yes + not-recent → exit 1"),
            ("on", False, 1, "BLOCKED", "knob=on + not-recent → exit 1"),
            ("TRUE", False, 1, "BLOCKED", "knob=TRUE (case-insensitive) → exit 1"),
            ("0", False, 0, "Active feature set", "knob=0 + not-recent → advisory exit 0"),
            ("false", False, 0, "Active feature set", "knob=false → advisory exit 0"),
            ("", False, 0, "Active feature set", "knob='' (empty) → advisory exit 0"),
            (None, True, 0, "Active feature set", "env unset + recent → exit 0, no suggestion"),
        ]
        for env_val, recent, exp_rc, needle, desc in cases:
            _set_recent(repo, recent)
            rc, out = _activate(repo, env_val)
            label = f"activate FORGE_ACTIVATE_REQUIRES_COMPACT={env_val!r} recent={recent}"
            all_ok &= turn(label, f"exit {exp_rc} + '{needle}'",
                           lambda rc=rc, out=out, exp_rc=exp_rc, needle=needle:
                           (rc == exp_rc and needle in out, f"rc={rc} | {_snip(out)}"))
    finally:
        shutil.rmtree(base, ignore_errors=True)

    result = {
        "name": "forge-activate-config-knob-e2e",
        "status": "pass" if all_ok else "fail",
        "exitCode": 0 if all_ok else 1,
        "scenarios": [{
            "name": "/forge activate compaction policy knob (real git + real CLI)",
            "description": "Run the real forge_gate CLI activate with FORGE_ACTIVATE_REQUIRES_COMPACT "
                           "and compaction.log toggled; observe exit codes + stdout.",
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
    print(f"E2E result: status={result['status']} exitCode={result['exitCode']} turns={turns} -> {args.output}")
    for s in result["scenarios"]:
        for r in s["rounds"]:
            for t in r["turns"]:
                mark = "PASS" if t["status"] == "pass" else "FAIL"
                print(f"  [{mark}] {t['action']} | expect: {t['expect']} | actual: {t['actual']}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
