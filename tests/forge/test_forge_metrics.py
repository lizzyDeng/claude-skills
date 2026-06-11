import json, os, importlib.util, hashlib
SPEC = os.path.join(os.path.dirname(__file__), "..", "..", "skills", "forge", "hooks", "forge_gate.py")
def load_gate():
    s = importlib.util.spec_from_file_location("forge_gate_m", SPEC)
    m = importlib.util.module_from_spec(s); s.loader.exec_module(m); return m
def _sha(s): return hashlib.sha256(s.encode()).hexdigest()


# ===== Task 1: metrics.project.json config loading =====

def test_load_metrics_config(tmp_path, monkeypatch):
    g = load_gate(); monkeypatch.setattr(g, "get_repo_root", lambda: str(tmp_path))
    (tmp_path/".claude").mkdir()
    (tmp_path/".claude"/"metrics.project.json").write_text(json.dumps(
        {"version":1,"resolver_command":"python3 r.py --metric {metric_id} --as-of {as_of} --out {out}"}))
    cfg, err = g.load_metrics_project_config(); assert err == "" and cfg["resolver_command"]
def test_load_metrics_config_missing_placeholder(tmp_path, monkeypatch):
    g = load_gate(); monkeypatch.setattr(g, "get_repo_root", lambda: str(tmp_path))
    (tmp_path/".claude").mkdir()
    (tmp_path/".claude"/"metrics.project.json").write_text(json.dumps({"version":1,"resolver_command":"python3 r.py"}))
    cfg, err = g.load_metrics_project_config(); assert cfg is None and "{metric_id}" in err
def test_load_metrics_config_absent(tmp_path, monkeypatch):
    g = load_gate(); monkeypatch.setattr(g, "get_repo_root", lambda: str(tmp_path))
    cfg, err = g.load_metrics_project_config(); assert cfg is None and "metrics.project.json" in err


# ===== Task 2: direction-aware snapshot store + dual-end evidence verify =====

def _owner(tmp_path, kind, oid):
    d = tmp_path/"project-roadmap"/kind/oid; (d/"snapshots").mkdir(parents=True, exist_ok=True); return d
def _ev(body):
    return {"source":"wh","collected_at":"t","raw_path":"snapshots/r.json","raw_sha256":_sha(body)}

def test_is_improvement_direction():
    g = load_gate()
    assert g.is_improvement(10, 5, "up") and not g.is_improvement(5, 10, "up")
    assert g.is_improvement(5, 10, "down") and not g.is_improvement(10, 5, "down")

def test_append_snapshot_keeps_rawpath_and_regression_up(tmp_path, monkeypatch):
    g = load_gate(); monkeypatch.setattr(g, "get_repo_root", lambda: str(tmp_path))
    o = _owner(tmp_path,"features","fx"); (o/"snapshots"/"r.json").write_text("b")
    base = {"metric_id":"m1","as_of":"d1","baseline":0,"target":100,"direction":"up","evidence":_ev("b")}
    assert g.append_metric_snapshot("features","fx",{**base,"value":50})[0]
    assert g.append_metric_snapshot("features","fx",{**base,"as_of":"d2","value":40})[0]
    h = g.load_metric_history("features","fx")
    assert h[-1]["regression"] is True and h[-1]["raw_path"] == "snapshots/r.json" and h[-1]["collected_at"] == "t"

def test_append_snapshot_regression_down(tmp_path, monkeypatch):
    g = load_gate(); monkeypatch.setattr(g, "get_repo_root", lambda: str(tmp_path))
    o = _owner(tmp_path,"features","fy"); (o/"snapshots"/"r.json").write_text("b")
    base = {"metric_id":"m1","as_of":"d1","baseline":100,"target":0,"direction":"down","evidence":_ev("b")}
    assert g.append_metric_snapshot("features","fy",{**base,"value":40})[0]
    assert g.append_metric_snapshot("features","fy",{**base,"as_of":"d2","value":60})[0]  # 升=越低越好的退步
    assert g.load_metric_history("features","fy")[-1]["regression"] is True

def test_append_snapshot_rejects_bad_sha(tmp_path, monkeypatch):
    g = load_gate(); monkeypatch.setattr(g, "get_repo_root", lambda: str(tmp_path))
    o = _owner(tmp_path,"features","fz"); (o/"snapshots"/"r.json").write_text("b")
    bad = {"metric_id":"m1","as_of":"d1","value":1,"baseline":0,"target":1,"direction":"up",
           "evidence":{"source":"w","collected_at":"t","raw_path":"snapshots/r.json","raw_sha256":"0"*64}}
    ok, err = g.append_metric_snapshot("features","fz",bad)
    assert not ok and "sha256" in err.lower() and g.load_metric_history("features","fz") == []

def test_verify_history_evidence_detects_tamper(tmp_path, monkeypatch):
    g = load_gate(); monkeypatch.setattr(g, "get_repo_root", lambda: str(tmp_path))
    o = _owner(tmp_path,"features","fw"); (o/"snapshots"/"r.json").write_text("b")
    g.append_metric_snapshot("features","fw",{"metric_id":"m1","as_of":"d1","value":1,"baseline":0,"target":1,"direction":"up","evidence":_ev("b")})
    assert g.verify_history_evidence("features","fw")[0]
    (o/"snapshots"/"r.json").write_text("TAMPERED")  # 改证据文件
    assert not g.verify_history_evidence("features","fw")[0]


# ===== Task 3: metric definition enrich + forge track (argv, anti-injection) =====

def _resolver(tmp_path):  # stub：只产 value+evidence，无 baseline/target
    (tmp_path/"tools").mkdir(exist_ok=True)
    (tmp_path/"tools"/"res.py").write_text(
        "import sys,json,os,hashlib\n"
        "a=dict(zip(sys.argv[1::2],sys.argv[2::2]))\n"
        "od=os.path.join('project-roadmap','features','fx','snapshots'); os.makedirs(od,exist_ok=True)\n"
        "raw=os.path.join(od,a['--as-of']+'.json'); body='{\"v\":42}'; open(raw,'w').write(body)\n"
        "json.dump({'metric_id':a['--metric'],'value':42,'as_of':a['--as-of'],'evidence':{'source':'stub','collected_at':a['--as-of'],'raw_path':os.path.relpath(raw,os.path.join('project-roadmap','features','fx')),'raw_sha256':hashlib.sha256(body.encode()).hexdigest()}}, open(a['--out'],'w'))\n")
    return "python3 tools/res.py --metric {metric_id} --as-of {as_of} --out {out}"

def _setup_track(tmp_path, monkeypatch):
    g=load_gate(); monkeypatch.setattr(g,"get_repo_root",lambda:str(tmp_path)); monkeypatch.chdir(tmp_path)
    fd=tmp_path/"project-roadmap"/"features"/"fx"; fd.mkdir(parents=True)
    (fd/"metric.json").write_text(json.dumps({"metric_name":"M","event_name":"e","baseline":0,"target":100,
        "harvest_days":7,"data_source":"manual","metric_id":"m1","direction":"up"}))
    (tmp_path/".claude").mkdir()
    (tmp_path/".claude"/"metrics.project.json").write_text(json.dumps({"version":1,"resolver_command":_resolver(tmp_path)}))
    return g

def test_track_enriches_from_metricjson(tmp_path, monkeypatch):
    g=_setup_track(tmp_path,monkeypatch)
    assert g.cmd_track("features","fx","m1","2026-06-01")==0
    h=g.load_metric_history("features","fx")
    assert h[-1]["value"]==42 and h[-1]["baseline"]==0 and h[-1]["target"]==100 and h[-1]["direction"]=="up"

def test_track_rejects_shell_injection(tmp_path, monkeypatch):
    g=_setup_track(tmp_path,monkeypatch)
    rc=g.cmd_track("features","fx","m1; touch /tmp/pwned","2026-06-01")
    assert rc==1 and not os.path.exists("/tmp/pwned")

def test_track_rejects_path_traversal(tmp_path, monkeypatch):
    g=_setup_track(tmp_path,monkeypatch)
    for bad in ("../../etc", "..", ".hidden", "a/b"):  # 含纯 ".." 与前导点
        assert g.cmd_track("features",bad,"m1","2026-06-01")==1, bad  # _safe_owner_id 拦


# ===== Task 4: objective alignment + roadmap.md section + dict-safe render =====

def _seed_obj(g, tmp_path, vals, direction="up", base=1000, target=2000):
    od=tmp_path/"project-roadmap"/"objectives"/"obj-1"/"snapshots"; od.mkdir(parents=True,exist_ok=True)
    (od/"r.json").write_text("b"); ev={"source":"s","collected_at":"t","raw_path":"snapshots/r.json","raw_sha256":_sha("b")}
    for v in vals: g.append_metric_snapshot("objectives","obj-1",{"metric_id":"m1","as_of":"d","value":v,"baseline":base,"target":target,"direction":direction,"evidence":ev})

def test_alignment_direction_up(tmp_path, monkeypatch):
    g=load_gate(); monkeypatch.setattr(g,"get_repo_root",lambda:str(tmp_path)); _seed_obj(g,tmp_path,[1200,1500])
    a=g.compute_objective_alignment({"id":"obj-1","target_metric":{"metric_id":"m1","baseline":1000,"target":2000,"direction":"up"}})
    assert a["current"]==1500 and a["pct"]==50.0 and a["status"]=="at_risk"

def test_alignment_legacy_string_safe(tmp_path, monkeypatch):
    g=load_gate(); monkeypatch.setattr(g,"get_repo_root",lambda:str(tmp_path))
    assert g.compute_objective_alignment({"id":"obj-1","target_metric":"reach 2000 DAU"}) is None  # 旧 string 不崩、不渲染

def test_roadmap_md_alignment_and_legacy(tmp_path, monkeypatch):
    g=load_gate(); monkeypatch.setattr(g,"get_repo_root",lambda:str(tmp_path)); _seed_obj(g,tmp_path,[1800])
    rm={"project":{"name":"P","north_star":"NS"},
        "objectives":[{"id":"obj-1","name":"G","target_metric":{"metric_id":"m1","baseline":1000,"target":2000,"direction":"up"}},
                      {"id":"obj-2","name":"Legacy","target_metric":"reach X"}],"features":[]}
    md=g.generate_roadmap_md(rm)
    assert "Objective 对齐" in md and "obj-1" in md and "1800" in md and "{'metric_id'" not in md  # dict 不被 stringify


# ===== Task 5: forge analyze (evidence re-verify + direction-aware projection + provenance) =====

def test_analyze_emits_with_provenance_and_verify(tmp_path, monkeypatch):
    g=load_gate(); monkeypatch.setattr(g,"get_repo_root",lambda:str(tmp_path))
    od=tmp_path/"project-roadmap"/"features"/"fx"/"snapshots"; od.mkdir(parents=True)
    (od/"r.json").write_text("b"); ev={"source":"wh","collected_at":"t","raw_path":"snapshots/r.json","raw_sha256":_sha("b")}
    for i,v in enumerate((1000,1200,1400)):
        g.append_metric_snapshot("features","fx",{"metric_id":"m1","as_of":f"d{i}","value":v,"baseline":1000,"target":2000,"direction":"up","evidence":ev})
    assert g.cmd_analyze("features","fx")==0
    ana=json.loads((tmp_path/"project-roadmap"/"features"/"fx"/"analysis.json").read_text())
    assert ana["samples"]==3 and ana["latest"]==1400 and ana["trend"]=="up" and ana["projected"]["reachable"] is True
    assert ana["provenance"]["raw_path"]=="snapshots/r.json" and ana["provenance"]["evidence_sha256"]==_sha("b")

def test_analyze_blocks_on_tampered_history(tmp_path, monkeypatch):
    g=load_gate(); monkeypatch.setattr(g,"get_repo_root",lambda:str(tmp_path))
    od=tmp_path/"project-roadmap"/"features"/"ft"/"snapshots"; od.mkdir(parents=True)
    (od/"r.json").write_text("b"); ev={"source":"wh","collected_at":"t","raw_path":"snapshots/r.json","raw_sha256":_sha("b")}
    g.append_metric_snapshot("features","ft",{"metric_id":"m1","as_of":"d0","value":1,"baseline":0,"target":2,"direction":"up","evidence":ev})
    (od/"r.json").write_text("TAMPER")
    assert g.cmd_analyze("features","ft")==1  # 证据被改 → 拒绝出 analysis


# ===== Task 6: analyze.workflow.js script lint =====

def test_analyze_workflow_script_lint():
    import os, subprocess, shutil
    p=os.path.join(os.path.dirname(__file__),"..","..","skills","forge","workflows","analyze.workflow.js")
    assert os.path.exists(p), "analyze.workflow.js missing"
    src=open(p).read()
    assert "export const meta" in src and "agent(" in src and "parallel(" in src
    assert "provenance" in src and ("adversar" in src.lower() or "refute" in src.lower())
    # KNOWLEDGE 坑：prompt 模板串里禁裸反引号
    import re
    for m in re.findall(r"`[^`]*`", src):
        assert "`" not in m[1:-1], "bare backtick inside template literal"
    node=shutil.which("node")
    if node:  # 有 node 才做语法门，无则跳过（lint 仍验形状）
        # workflow 脚本不是独立 ESM 文件：运行时 body 跑在 async 函数上下文（顶层
        # return/await 合法），meta 由 harness 提取。lint 须模拟该形态——否则
        # node --check 把 .js 当 CommonJS 在 'export' 挂掉，或当 ESM 在顶层 return 挂掉
        lint_src="(async () => {\n"+src.replace("export const meta","const meta",1)+"\n})()"
        r=subprocess.run([node,"--check","--input-type=module"],input=lint_src,capture_output=True,text=True)
        assert r.returncode==0, f"node --check failed: {r.stderr}"


# --- Phase 2.5 hardening (G1/G2): analyze-side containment re-check + --objective missing-id guard ---

def test_verify_history_rejects_escaping_rawpath(tmp_path, monkeypatch):
    g = load_gate(); monkeypatch.setattr(g, "get_repo_root", lambda: str(tmp_path))
    o = _owner(tmp_path, "features", "fesc"); (o/"snapshots"/"r.json").write_text("b")
    assert g.append_metric_snapshot("features", "fesc", {"metric_id":"m1","as_of":"d1","value":1,"baseline":0,"target":1,"direction":"up","evidence":_ev("b")})[0]
    hp = g.metric_history_path("features", "fesc")
    rows = [json.loads(l) for l in open(hp) if l.strip()]
    rows[-1]["raw_path"] = "../../../../etc/passwd"          # hand-edit jsonl to escape owner dir
    with open(hp, "w") as f:
        for r in rows: f.write(json.dumps(r) + "\n")
    ok, why = g.verify_history_evidence("features", "fesc")
    assert not ok and "escapes" in why                       # analyze-side containment now enforced

def test_cli_objective_missing_id_no_crash(tmp_path):
    import subprocess, sys
    GATE = os.path.join(os.path.dirname(__file__), "..", "..", "skills", "forge", "hooks", "forge_gate.py")
    for args in (["track", "--objective"], ["analyze", "--objective"]):
        r = subprocess.run([sys.executable, GATE, *args], capture_output=True, text=True,
                           env={**os.environ, "FORGE_REPO_ROOT": str(tmp_path)})
        assert r.returncode == 1, (args, r.returncode, r.stderr)
        assert "Traceback" not in r.stderr, (args, r.stderr)   # no IndexError
        assert "Usage" in (r.stdout + r.stderr), (args, r.stdout, r.stderr)
