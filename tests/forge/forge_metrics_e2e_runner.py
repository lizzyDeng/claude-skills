#!/usr/bin/env python3
"""E2E runner for forge metrics tracking (track / analyze / alignment / guards).

Drives the REAL forge_gate CLI (subprocess, observed exit codes + stdout) and the
REAL forge_gate / forge_dashboard module functions against REAL throwaway git
repos with a REAL stub resolver. Every turn records an explicit `expect` and the
observed `actual` — no claim is self-certifying. Covers AC1-AC12 of the plan
(docs/superpowers/plans/2026-06-04-forge-metrics-tracking.md). Result schema is
compatible with tests/e2e_gate.py (scenarios -> rounds -> turns). Exit 0 iff
every turn held.

Usage: python3 tests/forge/forge_metrics_e2e_runner.py -o /tmp/forge_metrics_e2e.json
"""
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(_HERE, "..", ".."))
HOOKS = os.path.join(ROOT, "skills", "forge", "hooks")
DASH = os.path.join(ROOT, "skills", "forge")
sys.path.insert(0, HOOKS)
sys.path.insert(0, DASH)
import forge_gate          # noqa: E402
import forge_dashboard     # noqa: E402

GATE = os.path.join(HOOKS, "forge_gate.py")

# Stub resolver: owner-agnostic (derives owner dir from --out's dir), emits ONLY
# value + evidence (NEVER baseline/target — those are curated, proving enrich).
# Value is read from tools/next_value.txt (cwd=repo root) so the test controls it.
GOOD_RESOLVER = (
    "import sys, json, os, hashlib\n"
    "a = dict(zip(sys.argv[1::2], sys.argv[2::2]))\n"
    "owner = os.path.dirname(os.path.abspath(a['--out']))\n"
    "od = os.path.join(owner, 'snapshots'); os.makedirs(od, exist_ok=True)\n"
    "raw = os.path.join(od, a['--as-of'] + '.json')\n"
    "v = float(open('tools/next_value.txt').read().strip())\n"
    "v = int(v) if v == int(v) else v\n"
    "body = json.dumps({'value': v})\n"
    "open(raw, 'w').write(body)\n"
    "json.dump({'metric_id': a['--metric'], 'value': v, 'as_of': a['--as-of'],\n"
    "           'evidence': {'source': 'warehouse:stub', 'collected_at': a['--as-of'] + 'T00:00',\n"
    "                        'raw_path': os.path.relpath(raw, owner),\n"
    "                        'raw_sha256': hashlib.sha256(body.encode()).hexdigest()}},\n"
    "          open(a['--out'], 'w'))\n"
)
# Bad-sha resolver: writes raw file but reports a sha of DIFFERENT content -> reject.
BAD_SHA_RESOLVER = (
    "import sys, json, os, hashlib\n"
    "a = dict(zip(sys.argv[1::2], sys.argv[2::2]))\n"
    "owner = os.path.dirname(os.path.abspath(a['--out']))\n"
    "od = os.path.join(owner, 'snapshots'); os.makedirs(od, exist_ok=True)\n"
    "raw = os.path.join(od, a['--as-of'] + '.json'); open(raw, 'w').write('REAL')\n"
    "json.dump({'metric_id': a['--metric'], 'value': 1, 'as_of': a['--as-of'],\n"
    "           'evidence': {'source': 's', 'collected_at': 't',\n"
    "                        'raw_path': os.path.relpath(raw, owner),\n"
    "                        'raw_sha256': hashlib.sha256(b'DIFFERENT').hexdigest()}},\n"
    "          open(a['--out'], 'w'))\n"
)


def _git(cwd, *a):
    subprocess.run(["git", "-C", str(cwd), *a], check=True, capture_output=True, text=True)


def _snip(out):
    return " ⏎ ".join(line for line in str(out).strip().splitlines() if line.strip())[:240]


def _run_gate(cwd, *args):
    env = {**os.environ, "FORGE_REPO_ROOT": str(cwd)}
    for k in ("FASTSHIP_REPO_ROOT", "FASTSHIP_STATE_HOME", "FASTSHIP_SESSION"):
        env.pop(k, None)
    p = subprocess.run([sys.executable, GATE, *args], cwd=cwd, capture_output=True, text=True, env=env)
    return p.returncode, p.stdout + p.stderr


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _mk_repo(base, name):
    repo = os.path.join(base, name)
    os.makedirs(repo)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t.io")
    _git(repo, "config", "user.name", "t")
    _write(os.path.join(repo, "f"), "x")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def _feature_metric(repo, slug, *, baseline, target, direction, metric_id="nsm_dau"):
    _write(os.path.join(repo, "project-roadmap", "features", slug, "metric.json"), json.dumps({
        "metric_name": "M", "event_name": "e", "baseline": baseline, "target": target,
        "harvest_days": 7, "data_source": "warehouse", "metric_id": metric_id, "direction": direction}))


def _install_resolver(repo, src=GOOD_RESOLVER):
    _write(os.path.join(repo, "tools", "res.py"), src)
    _write(os.path.join(repo, ".claude", "metrics.project.json"), json.dumps({
        "version": 1, "resolver_command": "python3 tools/res.py --metric {metric_id} --as-of {as_of} --out {out}"}))


def _set_val(repo, v):
    _write(os.path.join(repo, "tools", "next_value.txt"), str(v))


def _history(repo, kind, oid):
    p = os.path.join(repo, "project-roadmap", kind, oid, "metric-history.jsonl")
    if not os.path.exists(p):
        return []
    return [json.loads(ln) for ln in open(p, encoding="utf-8") if ln.strip()]


def _with_root(repo, fn):
    """Run a direct forge_gate/forge_dashboard call with FORGE_REPO_ROOT bound to repo."""
    old = os.environ.get("FORGE_REPO_ROOT")
    os.environ["FORGE_REPO_ROOT"] = str(repo)
    try:
        return fn()
    finally:
        if old is None:
            os.environ.pop("FORGE_REPO_ROOT", None)
        else:
            os.environ["FORGE_REPO_ROOT"] = old


def run():
    turns = []

    def turn(action, expect, fn):
        t0 = time.time()
        try:
            ok, actual = fn()
            err = "" if ok else "assertion failed"
        except Exception as e:  # pragma: no cover - defensive
            ok, actual, err = False, repr(e), repr(e)
        turns.append({"action": action, "input": "", "expect": expect, "actual": actual,
                      "response": actual, "status": "pass" if ok else "fail", "error": err,
                      "elapsed_ms": int((time.time() - t0) * 1000)})
        return ok

    base = tempfile.mkdtemp(prefix="forge-metrics-e2e-")
    all_ok = True
    try:
        # ---- AC1: no metrics.project.json -> track fails clearly ----
        r1 = _mk_repo(base, "ac1")
        _feature_metric(r1, "demo", baseline=1000, target=2000, direction="up")
        rc, out = _run_gate(r1, "track", "demo", "nsm_dau", "2026-06-01")
        all_ok &= turn("AC1 track without metrics.project.json", "exit!=0 + 'metrics.project.json' in error",
                       lambda: (rc != 0 and "metrics.project.json" in out, f"rc={rc} | {_snip(out)}"))

        # ---- main lifecycle repo (AC2/AC4/AC8/AC9/AC11) ----
        rL = _mk_repo(base, "life")
        _feature_metric(rL, "demo", baseline=1000, target=2000, direction="up")
        _install_resolver(rL)

        # AC2: first snapshot + enrich from metric.json (not resolver)
        _set_val(rL, 1100)
        rc, out = _run_gate(rL, "track", "demo", "nsm_dau", "2026-06-01")
        h = _history(rL, "features", "demo")
        rout = os.path.join(rL, "project-roadmap", "features", "demo", ".resolver-out.json")
        rdata = json.load(open(rout)) if os.path.exists(rout) else {}
        all_ok &= turn(
            "AC2 first snapshot + curate-enrich", "rc==0; 1 row; value=1100; baseline/target/direction from metric.json; resolver emitted no baseline/target",
            lambda: (rc == 0 and len(h) == 1 and h[-1]["value"] == 1100 and h[-1]["baseline"] == 1000
                     and h[-1]["target"] == 2000 and h[-1]["direction"] == "up"
                     and "baseline" not in rdata and "target" not in rdata,
                     f"rc={rc} rows={len(h)} row={h[-1] if h else None} resolver_keys={sorted(rdata.keys())}"))

        # AC4: direction up, value drops -> regression True
        _set_val(rL, 1050)
        rc, out = _run_gate(rL, "track", "demo", "nsm_dau", "2026-06-08")
        h = _history(rL, "features", "demo")
        all_ok &= turn("AC4 regression (direction up, value fell)", "rc==0; 2 rows; last regression True",
                       lambda: (rc == 0 and len(h) == 2 and h[-1]["regression"] is True,
                                f"rc={rc} rows={len(h)} last_regression={h[-1]['regression'] if h else None}"))

        # AC8: analyze emits analysis.json with trend/slope/projected
        rc, out = _run_gate(rL, "analyze", "demo")
        ana_p = os.path.join(rL, "project-roadmap", "features", "demo", "analysis.json")
        ana = json.load(open(ana_p)) if os.path.exists(ana_p) else {}
        all_ok &= turn(
            "AC8 analyze -> analysis.json", "rc==0; samples=2; latest=1050; trend=down; projected.reachable=False (declining)",
            lambda: (rc == 0 and ana.get("samples") == 2 and ana.get("latest") == 1050
                     and ana.get("trend") == "down" and ana.get("projected", {}).get("reachable") is False,
                     f"rc={rc} samples={ana.get('samples')} latest={ana.get('latest')} trend={ana.get('trend')} projected={ana.get('projected')}"))

        # AC9: provenance footer fields present + re-verifiable
        prov = ana.get("provenance", {})
        all_ok &= turn(
            "AC9 provenance footer", "provenance has source_tier + as_of + owner + raw_path + evidence_sha256",
            lambda: (all(prov.get(k) for k in ("source_tier", "as_of", "owner", "raw_path", "evidence_sha256")),
                     f"provenance={prov}"))

        # AC11a: shell-injection metric_id rejected, no side effect
        marker = os.path.join(rL, "pwned_marker")
        rc, out = _run_gate(rL, "track", "demo", "nsm_dau; touch pwned_marker", "2026-06-09")
        h = _history(rL, "features", "demo")
        all_ok &= turn(
            "AC11a shell-injection guard", "rc==1; no pwned_marker; history still 2 rows",
            lambda: (rc == 1 and not os.path.exists(marker) and len(h) == 2,
                     f"rc={rc} marker_exists={os.path.exists(marker)} rows={len(h)}"))

        # AC11b: path-traversal owner id rejected
        rc, out = _run_gate(rL, "track", "../../etc", "nsm_dau", "2026-06-09")
        all_ok &= turn("AC11b path-traversal guard", "rc==1 (owner id '../../etc' rejected)",
                       lambda: (rc == 1, f"rc={rc} | {_snip(out)}"))

        # ---- AC8-tamper: analyze blocks on tampered history evidence ----
        rT = _mk_repo(base, "tamper")
        _feature_metric(rT, "demo", baseline=0, target=10, direction="up")
        _install_resolver(rT)
        _set_val(rT, 5)
        _run_gate(rT, "track", "demo", "nsm_dau", "2026-06-01")
        raw_t = os.path.join(rT, "project-roadmap", "features", "demo", "snapshots", "2026-06-01.json")
        _write(raw_t, "TAMPERED")  # mutate evidence file after the fact
        rc, out = _run_gate(rT, "analyze", "demo")
        all_ok &= turn("AC8-tamper analyze blocks on tampered evidence", "rc!=0 (evidence check failed)",
                       lambda: (rc != 0 and "evidence" in out.lower(), f"rc={rc} | {_snip(out)}"))

        # ---- AC3: resolver sha mismatch rejected ----
        r3 = _mk_repo(base, "ac3")
        _feature_metric(r3, "demo", baseline=0, target=10, direction="up")
        _install_resolver(r3, src=BAD_SHA_RESOLVER)
        rc, out = _run_gate(r3, "track", "demo", "nsm_dau", "2026-06-01")
        h = _history(r3, "features", "demo")
        all_ok &= turn("AC3 resolver sha mismatch rejected", "rc!=0; 'sha256' in error; 0 rows",
                       lambda: (rc != 0 and "sha256" in out.lower() and len(h) == 0,
                                f"rc={rc} rows={len(h)} | {_snip(out)}"))

        # ---- AC5: direction-down regression semantics ----
        r5 = _mk_repo(base, "ac5")
        _feature_metric(r5, "lat", baseline=100, target=0, direction="down")
        _install_resolver(r5)
        _set_val(r5, 60)
        _run_gate(r5, "track", "lat", "nsm_dau", "2026-06-01")
        _set_val(r5, 80)  # rose -> worse for "down" -> regression
        rc, out = _run_gate(r5, "track", "lat", "nsm_dau", "2026-06-02")
        h = _history(r5, "features", "lat")
        all_ok &= turn("AC5a direction-down: value rose -> regression True", "2 rows; last regression True",
                       lambda: (len(h) == 2 and h[-1]["regression"] is True,
                                f"rows={len(h)} last={h[-1]['value'] if h else None} reg={h[-1]['regression'] if h else None}"))
        _set_val(r5, 50)  # fell -> better for "down" -> NOT regression
        rc, out = _run_gate(r5, "track", "lat", "nsm_dau", "2026-06-03")
        h = _history(r5, "features", "lat")
        all_ok &= turn("AC5b direction-down: value fell -> regression False", "3 rows; last regression False",
                       lambda: (len(h) == 3 and h[-1]["regression"] is False,
                                f"rows={len(h)} last={h[-1]['value'] if h else None} reg={h[-1]['regression'] if h else None}"))

        # ---- AC6 + AC7: objective alignment + roadmap.md render ----
        rO = _mk_repo(base, "obj")
        _install_resolver(rO)
        obj1 = {"id": "obj-1", "name": "Grow", "target_metric": {"metric_id": "nsm_dau", "baseline": 1000, "target": 2000, "direction": "up"}}
        _write(os.path.join(rO, "project-roadmap", "roadmap.json"), json.dumps({
            "project": {"name": "P", "north_star": "NS"},
            "objectives": [obj1, {"id": "obj-2", "name": "Legacy", "target_metric": "reach 2000 DAU"}],
            "features": []}))
        _set_val(rO, 1500)
        rc, out = _run_gate(rO, "track", "--objective", "obj-1", "nsm_dau", "2026-06-01")
        align = _with_root(rO, lambda: forge_gate.compute_objective_alignment(obj1))
        all_ok &= turn("AC6 objective alignment (direction up)", "rc==0; current=1500; pct=50.0; status=at_risk",
                       lambda: (rc == 0 and align and align["current"] == 1500 and align["pct"] == 50.0 and align["status"] == "at_risk",
                                f"rc={rc} align={align}"))

        rm = {"project": {"name": "P", "north_star": "NS"},
              "objectives": [obj1, {"id": "obj-2", "name": "Legacy", "target_metric": "reach 2000 DAU"}],
              "features": []}
        md = _with_root(rO, lambda: forge_gate.generate_roadmap_md(rm))
        _dict_token = "{'metric_id'"
        _md_stringified = _dict_token in md
        all_ok &= turn(
            "AC7 roadmap.md alignment section + legacy-string safe",
            "md has 'Objective 对齐' + 'obj-1' + '1500'; no stringified dict; legacy string present",
            lambda: ("Objective 对齐" in md and "obj-1" in md and "1500" in md
                     and not _md_stringified and "reach 2000 DAU" in md,
                     f"has_section={'Objective 对齐' in md} has_1500={'1500' in md} stringified_dict={_md_stringified}"))

        # ---- AC10: analyze.workflow.js structural lint passes ----
        env = {**os.environ}
        for k in ("FASTSHIP_REPO_ROOT", "FASTSHIP_STATE_HOME", "FASTSHIP_SESSION"):
            env.pop(k, None)
        p = subprocess.run([sys.executable, "-m", "pytest", "tests/forge/test_forge_metrics.py",
                            "-k", "workflow_script_lint", "-q", "-p", "no:cacheprovider"],
                           cwd=ROOT, capture_output=True, text=True, env=env)
        all_ok &= turn("AC10 analyze.workflow.js lint", "pytest -k workflow_script_lint exit 0",
                       lambda: (p.returncode == 0 and "passed" in (p.stdout + p.stderr),
                                f"rc={p.returncode} | {_snip(p.stdout + p.stderr)}"))

        # ---- AC12: dashboard normalizes structured target_metric (no [object Object]) ----
        rD = _mk_repo(base, "dash")
        _write(os.path.join(rD, "project-roadmap", "roadmap.json"), json.dumps({
            "project": {"name": "P", "north_star": "NS"},
            "objectives": [{"id": "obj-1", "name": "G", "target_metric": {"metric_id": "nsm_dau", "baseline": 1000, "target": 2000, "direction": "up"}},
                           {"id": "obj-2", "name": "L", "target_metric": "reach X"}],
            "features": []}))
        snap = forge_dashboard.build_snapshot(rD)
        o1, o2 = snap["objectives"][0], snap["objectives"][1]
        html = forge_dashboard.render_html()
        all_ok &= turn(
            "AC12 dashboard dict-safe target_metric_display", "o1 display 'nsm_dau: 1000→2000'; o2 'reach X'; no [object Object]; JS uses display field",
            lambda: (o1["target_metric_display"] == "nsm_dau: 1000→2000" and o2["target_metric_display"] == "reach X"
                     and "[object Object]" not in json.dumps(snap, ensure_ascii=False)
                     and "o.target_metric_display" in html and "esc(o.target_metric)" not in html,
                     f"o1={o1['target_metric_display']!r} o2={o2['target_metric_display']!r} js_uses_display={'o.target_metric_display' in html}"))
    finally:
        shutil.rmtree(base, ignore_errors=True)

    result = {
        "name": "forge-metrics-tracking-e2e",
        "status": "pass" if all_ok else "fail",
        "exitCode": 0 if all_ok else 1,
        "scenarios": [{
            "name": "forge metrics tracking — real CLI track/analyze + alignment + guards (real git + stub resolver)",
            "description": "Drive the real forge_gate CLI and forge_gate/forge_dashboard module functions across "
                           "real throwaway git repos with a real stub resolver; observe exit codes, jsonl rows, "
                           "regression flags, analysis.json fields, alignment status, roadmap.md text, injection/"
                           "path-traversal rejection, and dashboard display normalization. Covers AC1-AC12.",
            "rounds": [{"turns": turns}],
        }],
    }
    return result, all_ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--output", default="/tmp/forge_metrics_e2e.json")
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
