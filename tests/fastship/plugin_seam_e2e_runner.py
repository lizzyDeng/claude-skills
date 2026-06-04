#!/usr/bin/env python3
"""Plugin-mode E2E runner — proves forge/fastship engine location independence.

The engine stays in its real source dir (the "plugin"); a throwaway git repo is
the "project". Each turn drives the REAL engine via a python subprocess with
CLAUDE_PROJECT_DIR / FASTSHIP_REPO_ROOT / FORGE_REPO_ROOT / cwd toggled, and
asserts the resolved path (repo_root / forge.get_repo_root / _e2e_result_path /
ship_verify_gate.e2e_result_path / gate_script_path). Result schema is compatible
with tests/e2e_gate.py. Exit 0 iff every turn holds.

Usage: python3 tests/fastship/plugin_seam_e2e_runner.py -o .claude/fastship-e2e-result.json
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
FASTSHIP_DIR = os.path.abspath(os.path.join(_HERE, "..", "..", "skills", "fastship"))
FASTSHIP_HOOKS_DIR = os.path.join(FASTSHIP_DIR, "hooks")
FORGE_HOOKS_DIR = os.path.abspath(os.path.join(_HERE, "..", "..", "skills", "forge", "hooks"))
ORCH = os.path.join(FASTSHIP_DIR, "orchestrator.py")


def _git(cwd, *a):
    subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True, text=True)


def _mk_repo(base, name):
    repo = os.path.join(base, name)
    os.makedirs(repo)
    _git(repo, "init", "-q")
    return os.path.realpath(repo)


def _run(snippet, env_overrides, cwd):
    """Run a python snippet with a clean-ish env + overrides; return (rc, stdout)."""
    env = dict(os.environ)
    for k in ("CLAUDE_PROJECT_DIR", "FASTSHIP_REPO_ROOT", "FORGE_REPO_ROOT",
              "FASTSHIP_STATE_HOME", "FASTSHIP_SESSION"):
        env.pop(k, None)
    env.update(env_overrides)
    p = subprocess.run([sys.executable, "-c", snippet], cwd=cwd, env=env,
                       capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr).strip()


def _run_orch(args, env_overrides, cwd):
    """Invoke the packaged command path (python3 orchestrator.py ...) directly."""
    env = dict(os.environ)
    for k in ("CLAUDE_PROJECT_DIR", "FASTSHIP_REPO_ROOT", "FORGE_REPO_ROOT",
              "FASTSHIP_STATE_HOME", "FASTSHIP_SESSION"):
        env.pop(k, None)
    env.update(env_overrides)
    p = subprocess.run([sys.executable, ORCH, *args], cwd=cwd, env=env,
                       capture_output=True, text=True)
    return p.returncode, (p.stdout + p.stderr).strip()


# snippet builders
def _s_repo_root(force_installed=False):
    extra = "fastship_state._is_installed_tool_dir = lambda: True\n" if force_installed else ""
    return (f"import sys; sys.path.insert(0, {FASTSHIP_DIR!r})\n"
            f"import fastship_state\n{extra}print(fastship_state.repo_root())")


def _s_forge_root():
    return (f"import sys; sys.path.insert(0, {FORGE_HOOKS_DIR!r})\n"
            f"import forge_gate\nprint(forge_gate.get_repo_root())")


def _s_e2e_path():
    return (f"import sys; sys.path.insert(0, {FASTSHIP_DIR!r})\n"
            f"import orchestrator\nprint(orchestrator._e2e_result_path())")


def _s_svg_e2e_path():
    return (f"import sys; sys.path.insert(0, {FASTSHIP_HOOKS_DIR!r})\n"
            f"import ship_verify_gate\nprint(ship_verify_gate.e2e_result_path())")


def _s_gate_script():
    return (f"import sys; sys.path.insert(0, {FASTSHIP_DIR!r})\n"
            f"import fastship_state\nprint(fastship_state.gate_script_path())")


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

    base = tempfile.mkdtemp(prefix="plugin-seam-e2e-")
    all_ok = True
    try:
        project = _mk_repo(base, "project")
        elsewhere = _mk_repo(base, "elsewhere")
        override = _mk_repo(base, "override")
        e2e_default = os.path.join(project, ".claude", "fastship-e2e-result.json")

        cases = [
            ("repo_root CLAUDE_PROJECT_DIR wins over cwd",
             f"== {project}",
             lambda: _eq(_run(_s_repo_root(), {"CLAUDE_PROJECT_DIR": project}, elsewhere), project)),
            ("repo_root FASTSHIP_REPO_ROOT beats CLAUDE_PROJECT_DIR",
             f"== {override}",
             lambda: _eq(_run(_s_repo_root(), {"CLAUDE_PROJECT_DIR": project, "FASTSHIP_REPO_ROOT": override}, project), override)),
            ("repo_root nonexistent CLAUDE_PROJECT_DIR falls through to cwd",
             f"== {project}",
             lambda: _eq(_run(_s_repo_root(), {"CLAUDE_PROJECT_DIR": os.path.join(base, "nope")}, project), project)),
            ("repo_root no env, cwd=project",
             f"== {project}",
             lambda: _eq(_run(_s_repo_root(), {}, project), project)),
            ("repo_root engine-by-abspath, cwd=elsewhere, CLAUDE_PROJECT_DIR=project (location independence)",
             f"== {project}",
             lambda: _eq(_run(_s_repo_root(), {"CLAUDE_PROJECT_DIR": project}, elsewhere), project)),
            ("forge get_repo_root CLAUDE_PROJECT_DIR",
             f"== {project}",
             lambda: _eq(_run(_s_forge_root(), {"CLAUDE_PROJECT_DIR": project}, elsewhere), project)),
            ("forge get_repo_root FORGE_REPO_ROOT beats CLAUDE_PROJECT_DIR",
             f"== {override}",
             lambda: _eq(_run(_s_forge_root(), {"CLAUDE_PROJECT_DIR": project, "FORGE_REPO_ROOT": override}, project), override)),
            ("orchestrator _e2e_result_path under CLAUDE_PROJECT_DIR (retired /tmp)",
             f"== {e2e_default}",
             lambda: _eq(_run(_s_e2e_path(), {"CLAUDE_PROJECT_DIR": project}, elsewhere), e2e_default)),
            ("orchestrator _e2e_result_path NOT under /tmp",
             "not startswith /tmp/",
             lambda: _not_tmp(_run(_s_e2e_path(), {"CLAUDE_PROJECT_DIR": project}, elsewhere))),
            ("repo_root is stable across two calls",
             "call1 == call2",
             lambda: _stable(project)),
            ("forge get_repo_root no env, cwd=project",
             f"== {project}",
             lambda: _eq(_run(_s_forge_root(), {}, project), project)),
            ("repo_root CLAUDE_PROJECT_DIR beats installed-tool fallback (AC1)",
             f"== {project}",
             lambda: _eq(_run(_s_repo_root(force_installed=True), {"CLAUDE_PROJECT_DIR": project}, elsewhere), project)),
            ("ship_verify_gate e2e_result_path under CLAUDE_PROJECT_DIR (retired /tmp, both engines)",
             f"== {e2e_default}",
             lambda: _eq(_run(_s_svg_e2e_path(), {"CLAUDE_PROJECT_DIR": project}, elsewhere), e2e_default)),
            ("packaged command path is invocable (python3 orchestrator.py, NOT the broken fastship wrapper)",
             "usage printed; no 'No such file' / ModuleNotFound",
             lambda: _invocable(_run_orch([], {"CLAUDE_PROJECT_DIR": project}, elsewhere))),
        ]
        for action, expect, fn in cases:
            all_ok &= turn(action, expect, fn)
    finally:
        shutil.rmtree(base, ignore_errors=True)

    result = {
        "name": "plugin-seam-engine-location-independence-e2e",
        "status": "pass" if all_ok else "fail",
        "exitCode": 0 if all_ok else 1,
        "scenarios": [{
            "name": "engine location independence (real engine subprocess + real git repos)",
            "description": "Toggle CLAUDE_PROJECT_DIR/FASTSHIP_REPO_ROOT/FORGE_REPO_ROOT/cwd and assert "
                           "repo_root / forge.get_repo_root / _e2e_result_path / ship_verify_gate.e2e_result_path.",
            "rounds": [{"turns": turns}],
        }],
    }
    return result, all_ok


def _eq(run_out, expected):
    rc, out = run_out
    return (rc == 0 and out == expected, f"rc={rc} | {out}")


def _not_tmp(run_out):
    rc, out = run_out
    return (rc == 0 and not out.startswith("/tmp/"), f"rc={rc} | {out}")


def _invocable(run_out):
    """The packaged command path must run the orchestrator (usage banner), not fail
    like the broken wrapper would (missing fastship_orchestrator.py / import error)."""
    rc, out = run_out
    bad = ("No such file" in out or "ModuleNotFoundError" in out
           or "can't open file" in out)
    has_usage = ("Usage" in out or "CLI mode" in out or "start" in out)
    return (not bad and has_usage, f"rc={rc} | {out[:160]}")


def _stable(project):
    rc1, o1 = _run(_s_repo_root(), {"CLAUDE_PROJECT_DIR": project}, project)
    rc2, o2 = _run(_s_repo_root(), {"CLAUDE_PROJECT_DIR": project}, project)
    return (rc1 == 0 and rc2 == 0 and o1 == o2 == project, f"call1={o1} call2={o2}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--output", default=".claude/fastship-e2e-result.json")
    args = ap.parse_args()
    result, ok = run()
    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
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
