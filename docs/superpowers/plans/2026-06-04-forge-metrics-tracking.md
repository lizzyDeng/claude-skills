# Forge 项目级 Metrics 持续追踪与对齐（阶段 A：forge core）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 forge 增加「连续指标快照追踪 + objective 对齐 rollup + 趋势分析 + 对抗归因 dynamic workflow」核心能力，把一次性 harvest 升级为持续、方向感知、证据可复验的可观测，且 forge 自身零 SQL、零 shell 注入。

**Architecture:** forge 定契约（`.claude/metrics.project.json` 声明 resolver 命令）+ 编排（`forge track` 经 argv 调 resolver 取「数字+证据」、`forge analyze` 算方向感知趋势 + 跑 dynamic workflow 做对抗归因）+ 校验（快照 evidence sha256 在 append 与 analyze 两端复验）。**指标定义（metric_id/baseline/target/direction）人工 curate 在 metric.json（feature）/ objective.target_metric（objective）；resolver 只产 value+evidence**（呼应 "curate, don't auto-generate"）。

**Tech Stack:** Python 3.11 stdlib（`shlex`/`subprocess` argv、`hashlib`）、pytest、forge_gate.py 现有 helpers（`_load_json`/`_sha256_file`/`get_repo_root`/`load_roadmap`/`generate_roadmap_md`）；dynamic workflow 为纯 JS（Workflow 运行时）；纯 Python skill E2E（`*_e2e_runner.py`，scenarios→rounds→turns）。

---

## P0 合同（不可破，逐条可审）

- **P0-1 forge 自身零 SQL、零 shell 注入**：取数只经 `metrics.project.json.resolver_command`，**用 `shlex.split` + argv 列表执行（禁 `shell=True`）**，占位符值（metric_id/as_of）经白名单校验（`^[A-Za-z0-9._:-]+$`），非法即拒。forge 代码不含任何 DB/SQL；resolver 是否查库属消费方治理。
- **P0-2 证据双端绑定**：每条 snapshot 的 `evidence.raw_path` 落在 owner 目录下、`raw_sha256` 在 **append 期** 与 **analyze 期** 各复算一次；history row **保留 raw_path + collected_at + evidence_sha256**，被手改即报。
- **P0-3 向后兼容**：现有 metric.json/harvest.json/Gate 6/`data_source:"manual"`/`generate_roadmap_md`/`forge_dashboard.py` 不破坏；`target_metric` dict 与旧 string 双兼容；`metric_id`/`direction` 为带默认的可选字段，不进 `METRIC_REQUIRED_FIELDS`。
- **P0-4 方向感知**：metric.json + objective.target_metric 带 `direction`（`"up"`默认=越高越好｜`"down"`=越低越好）；regression 与 projection 全部经 `is_improvement(cur,prev,direction)`，对 latency/churn/error-rate 正确。
- **P0-5 analyze 真跑 dynamic workflow（非降级）**：阶段 A 交付真实 `skills/forge/workflows/analyze.workflow.js`（fan-out 趋势 + 对抗归因 reviewer + 合成带 provenance footer 的 cited verdict，消费 analysis.json）。边界：CLI 产确定性 spine + provenance（pytest 硬门）；workflow 脚本经**结构 lint**（合法 JS / `meta` 块 / prompt 模板串无裸反引号）；workflow 的 LLM 执行不进 pytest 门（运行时由 `/forge analyze` 触发），此切法在 1.6 显式确认。

## 验收清单（AC）→ E2E 映射（每条 AC 必有 E2E turn）

| AC | 可观察断言 | E2E scenario.round |
|----|-----------|--------------------|
| AC1 | 无 `metrics.project.json` → `forge track` 非零退出 + 明确报错 | S.no-config |
| AC2 | `forge track <slug> <metric_id> <as_of>` 经 resolver 把快照追加进 `metric-history.jsonl`，行数 +1、末行 value 正确、且 baseline/target/direction 由 metric.json enrich（非 resolver） | S.first-snapshot |
| AC3 | resolver evidence sha256 不匹配真文件 → 拒收，jsonl 不增长、非零退出 | S.tampered-evidence |
| AC4 | `direction:up` 下 value 跌破上条 → `regression:true` | S.regression-up |
| AC5 | `direction:down`（越低越好）下 value **升**破上条 → `regression:true`；下降不算 | S.regression-down |
| AC6 | objective `target_metric` 结构化 + 有快照 → `compute_objective_alignment` 出 `current/status(on_track\|at_risk\|off_track)`，方向感知正确 | S.objective-status |
| AC7 | `generate_roadmap_md` 渲染「Objective 对齐」段含 current/target/status；**旧 string target_metric 不崩** | S.roadmap-render |
| AC8 | `forge analyze <slug>` 先复验 history evidence（被改报错），再产 `analysis.json`（trend/slope/projected/provenance.raw_path），projected 方向正确 | S.analyze + S.analyze-tamper |
| AC9 | analyze 的 provenance footer 含 source_tier + as_of + owner + evidence_sha256 + raw_path | S.analyze |
| AC10 | `skills/forge/workflows/analyze.workflow.js` 存在、合法 JS、有 `meta`、prompt 模板无裸反引号（lint 通过） | S.workflow-lint |
| AC11 | forge 调 resolver 用 argv（无 shell=True）；`metric_id`/`as_of`/**`oid`** 含 shell 元字符或 `../` → 白名单拒、不执行、不逃出 owner 目录 | S.injection-guard（含 path-traversal turn） |
| AC12 | 结构化 `target_metric` dict 经 dashboard `build_snapshot` 产 `target_metric_display` 串、rendered HTML 无 `[object Object]` | S.dashboard-compat（E2E turn + pytest 回归） |

E2E 主断言均为业务结果（jsonl 行数/值/regression、status 派生、md 文本、analysis.json 字段、注入被拒）。runner=`tests/forge/forge_metrics_e2e_runner.py`（匹配 `e2e[_-]?runner`），≥12 turns，无需 fastship.project.json。

---

## File Structure

- **Modify** `skills/forge/hooks/forge_gate.py` — metrics 区（config 加载、is_improvement、snapshot 存储+双端校验、metric 定义 enrich、track/analyze CLI、objective 对齐、roadmap.md 对齐段 + dict 安全渲染）。
- **Modify** `skills/forge/forge_dashboard.py` — `target_metric` dict/str 双兼容渲染。
- **Create** `skills/forge/templates/metrics.project.json` — resolver 契约模板。
- **Modify** `skills/forge/templates/metric.json` — 加 `metric_id` + `direction`。
- **Create** `skills/forge/workflows/analyze.workflow.js` — 对抗归因 dynamic workflow。
- **Create** `tests/forge/test_forge_metrics.py` — 单测（config/snapshot/direction/track/enrich/injection/alignment/analyze/history-verify）。
- **Create** `tests/forge/forge_metrics_e2e_runner.py` — 全 AC 覆盖 E2E。
- **Modify** `tests/forge/test_forge_dashboard.py` — 加结构化 target_metric 回归（AC12）。
- **Modify** `skills/forge/SKILL.md` + `.claude/commands/forge.md` — track/analyze + workflow + P0-4 supersede 注记。

数据形态：
- `.claude/metrics.project.json`：`{"version":1,"resolver_command":"python3 tools/forge_metric_resolver.py --metric {metric_id} --as-of {as_of} --out {out}","notes":[...]}`
- resolver→`{out}`：`{"metric_id":"nsm_dau","value":1450,"as_of":"2026-06-04","evidence":{"source":"warehouse:metrics.dau","collected_at":"2026-06-04T10:00:00","raw_path":"snapshots/2026-06-04-nsm_dau.json","raw_sha256":"<hex64>"}}`（**无 baseline/target**）
- metric.json（feature 定义）：增 `"metric_id":"nsm_dau","direction":"up"`
- objective.target_metric：`{"metric_id":"nsm_dau","baseline":1000,"target":2000,"direction":"up"}`（旧 string 兼容）
- history row：`{"metric_id","as_of","value","baseline","target","direction","source","raw_path","collected_at","evidence_sha256","regression","recorded_at"}`

---

### Task 1: metrics.project.json 契约 + 加载校验

**Files:** Create `skills/forge/templates/metrics.project.json`；Modify `forge_gate.py`；Test `tests/forge/test_forge_metrics.py`

- [ ] **Step 1: 失败测试**（三例：合法 / 缺占位符 / 缺文件）

```python
import json, os, importlib.util, hashlib
SPEC = os.path.join(os.path.dirname(__file__), "..", "..", "skills", "forge", "hooks", "forge_gate.py")
def load_gate():
    s = importlib.util.spec_from_file_location("forge_gate_m", SPEC)
    m = importlib.util.module_from_spec(s); s.loader.exec_module(m); return m
def _sha(s): return hashlib.sha256(s.encode()).hexdigest()

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
```

- [ ] **Step 2: 跑失败** `env -u FASTSHIP_REPO_ROOT -u FASTSHIP_STATE_HOME -u FASTSHIP_SESSION python3 -m pytest tests/forge/test_forge_metrics.py -q -p no:cacheprovider` → FAIL（无 `load_metrics_project_config`）
- [ ] **Step 3: 实现**

```python
# forge_gate.py 新增「Metrics Tracking」区（check_g6_harvest 之后）
import shlex
METRICS_PROJECT_REL = os.path.join(".claude", "metrics.project.json")
RESOLVER_PLACEHOLDERS = ("{metric_id}", "{as_of}", "{out}")
SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9._:-]+$")  # 白名单：占位符值禁 shell 元字符
def _safe_owner_id(oid):  # owner id 进文件路径，比 token 更严：禁 ".."、"/"、前导点（堵 path traversal）
    return bool(oid) and ".." not in oid and "/" not in oid and not oid.startswith(".") and SAFE_TOKEN_RE.match(oid) is not None

def metrics_project_path():
    root = get_repo_root(); return os.path.join(root, METRICS_PROJECT_REL) if root else None
def load_metrics_project_config():
    p = metrics_project_path()
    if not p or not os.path.exists(p): return (None, f"metrics.project.json not found at {p}")
    data = _load_json(p); cmd = data.get("resolver_command")
    if not isinstance(cmd, str) or not cmd.strip(): return (None, "resolver_command missing/not a string")
    missing = [ph for ph in RESOLVER_PLACEHOLDERS if ph not in cmd]
    if missing: return (None, "resolver_command missing placeholders: " + ", ".join(missing))
    return (data, "")
```

- [ ] **Step 4: 跑通** 同上命令 → 3 passed
- [ ] **Step 5: 模板 + commit**

```bash
cat > skills/forge/templates/metrics.project.json <<'JSON'
{
  "version": 1,
  "resolver_command": "python3 tools/forge_metric_resolver.py --metric {metric_id} --as-of {as_of} --out {out}",
  "notes": ["forge never runs SQL and never shell-interpolates; it splits resolver_command with shlex and execs argv. The resolver reads the project's governed analytics and writes {value, as_of, evidence} to --out"]
}
JSON
git add skills/forge/templates/metrics.project.json skills/forge/hooks/forge_gate.py tests/forge/test_forge_metrics.py
git commit -m "feat(forge): metrics.project.json resolver contract + loader (Task 1)"
```

---

### Task 2: 方向感知快照存储 + 双端 evidence 校验

**Files:** Modify `forge_gate.py`；Test `tests/forge/test_forge_metrics.py`

- [ ] **Step 1: 失败测试**（方向感知 regression + 双端校验 + history 保留 raw_path）

```python
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
```

- [ ] **Step 2: 跑失败** → FAIL
- [ ] **Step 3: 实现**

```python
def is_improvement(cur, prev, direction):
    return cur > prev if (direction or "up") == "up" else cur < prev

def metric_owner_dir(repo_root, kind, oid): return os.path.join(repo_root, "project-roadmap", kind, oid)
def metric_history_path(kind, oid):
    root = get_repo_root(); return os.path.join(metric_owner_dir(root,kind,oid),"metric-history.jsonl") if root else None
def load_metric_history(kind, oid):
    p = metric_history_path(kind,oid); 
    if not p or not os.path.exists(p): return []
    out=[]
    with open(p,encoding="utf-8") as f:
        for ln in f:
            ln=ln.strip()
            if ln:
                try: out.append(json.loads(ln))
                except json.JSONDecodeError: continue
    return out

def _resolve_owner_raw(repo_root, kind, oid, raw_path):
    owner = os.path.realpath(metric_owner_dir(repo_root,kind,oid))
    raw = os.path.realpath(os.path.join(owner, raw_path))
    return (owner, raw)
def _verify_snapshot_evidence(repo_root, kind, oid, ev):
    for f in ("source","collected_at","raw_path","raw_sha256"):
        if not ev.get(f): return (False, f"evidence.{f} missing")
    owner, raw = _resolve_owner_raw(repo_root,kind,oid,ev["raw_path"])
    if not raw.startswith(owner+os.sep) or not os.path.exists(raw): return (False,f"evidence raw_path bad: {ev.get('raw_path')}")
    digest,size=_sha256_file(raw)
    if size<=0: return (False,"evidence raw_path empty")
    if digest!=ev["raw_sha256"]: return (False,"evidence raw_sha256 mismatch")
    return (True,"")

def append_metric_snapshot(kind, oid, snap):
    root=get_repo_root()
    if not root: return (False,"no repo root")
    for f in ("metric_id","as_of","value","baseline","target","direction","evidence"):
        if snap.get(f) is None: return (False,f"snapshot.{f} missing (caller must enrich definition)")
    if not isinstance(snap["value"],(int,float)): return (False,"value must be numeric")
    ok,err=_verify_snapshot_evidence(root,kind,oid,snap["evidence"])
    if not ok: return (False,err)
    prev=load_metric_history(kind,oid)
    regression = bool(prev) and isinstance(prev[-1].get("value"),(int,float)) and not is_improvement(snap["value"],prev[-1]["value"],snap["direction"]) and snap["value"]!=prev[-1]["value"]
    row={"metric_id":snap["metric_id"],"as_of":snap["as_of"],"value":snap["value"],
         "baseline":snap["baseline"],"target":snap["target"],"direction":snap["direction"],
         "source":snap["evidence"]["source"],"raw_path":snap["evidence"]["raw_path"],
         "collected_at":snap["evidence"]["collected_at"],"evidence_sha256":snap["evidence"]["raw_sha256"],
         "regression":regression,"recorded_at":datetime.now().isoformat()}
    p=metric_history_path(kind,oid); os.makedirs(os.path.dirname(p),exist_ok=True)
    with open(p,"a",encoding="utf-8") as f: f.write(json.dumps(row,ensure_ascii=False)+"\n")
    return (True,"")

def verify_history_evidence(kind, oid):
    """Re-hash every row's raw_path vs stored evidence_sha256. (False, idx) on first mismatch."""
    root=get_repo_root()
    for i,row in enumerate(load_metric_history(kind,oid)):
        rp=row.get("raw_path")
        if not rp: return (False, f"row {i} missing raw_path")
        _,raw=_resolve_owner_raw(root,kind,oid,rp)
        if not os.path.exists(raw): return (False,f"row {i} raw_path gone: {rp}")
        digest,_=_sha256_file(raw)
        if digest!=row.get("evidence_sha256"): return (False,f"row {i} evidence tampered: {rp}")
    return (True,"")
```

- [ ] **Step 4: 跑通** → all passed  - [ ] **Step 5: commit** `feat(forge): direction-aware evidence-bound snapshot store + history re-verify (Task 2)`

---

### Task 3: metric 定义 enrich + `forge track`（argv，防注入）

**Files:** Modify `forge_gate.py` + `skills/forge/templates/metric.json`；Test `test_forge_metrics.py`

- [ ] **Step 1: 失败测试**（enrich from metric.json + 注入拒绝 + 端到端 track）

```python
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
```

- [ ] **Step 2: 跑失败** → FAIL
- [ ] **Step 3: 实现**

```python
def _owner_metric_def(kind, oid, metric_id):
    """Curated definition: feature → metric.json; objective → roadmap objective.target_metric. Returns (def, err)."""
    root=get_repo_root()
    if kind=="features":
        mp=os.path.join(root,"project-roadmap","features",oid,"metric.json")
        if not os.path.exists(mp): return (None,f"metric.json not found for feature {oid}")
        m=_load_json(mp)
        return ({"metric_id":m.get("metric_id") or metric_id,"baseline":m.get("baseline"),
                 "target":m.get("target"),"direction":m.get("direction","up")},"")
    rm=load_roadmap() or {}
    for obj in rm.get("objectives",[]):
        if obj.get("id")==oid:
            tm=obj.get("target_metric")
            if isinstance(tm,dict):
                return ({"metric_id":tm.get("metric_id") or metric_id,"baseline":tm.get("baseline"),
                         "target":tm.get("target"),"direction":tm.get("direction","up")},"")
            return (None,f"objective {oid} target_metric not structured (legacy string)")
    return (None,f"objective {oid} not found")

def cmd_track(kind, oid, metric_id, as_of):
    if not (_safe_owner_id(oid) and SAFE_TOKEN_RE.match(metric_id) and SAFE_TOKEN_RE.match(as_of)):
        print("🚫 forge track: unsafe owner/metric_id/as_of (owner blocks '..' and '/'; tokens whitelist-only)"); return 1
    cfg,err=load_metrics_project_config()
    if cfg is None: print(f"🚫 forge track: {err}"); return 1
    mdef,e1=_owner_metric_def(kind,oid,metric_id)
    if mdef is None: print(f"🚫 forge track: {e1}"); return 1
    if mdef["metric_id"] and mdef["metric_id"]!=metric_id:   # 防"取 X 记成 Y"
        print(f"🚫 forge track: CLI metric_id '{metric_id}' != curated '{mdef['metric_id']}'"); return 1
    root=get_repo_root()
    out=os.path.join(metric_owner_dir(root,kind,oid),".resolver-out.json"); os.makedirs(os.path.dirname(out),exist_ok=True)
    argv=[ (metric_id if t=="{metric_id}" else as_of if t=="{as_of}" else out if t=="{out}" else t)
           for t in shlex.split(cfg["resolver_command"]) ]   # argv 替换，禁 shell=True
    try:
        r=subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=120)
    except Exception as e: print(f"🚫 forge track: resolver run failed — {e}"); return 1
    if r.returncode!=0: print(f"🚫 forge track: resolver exit {r.returncode} — {(r.stderr or '').strip()[:200]}"); return 1
    out_data=_load_json(out)
    if not out_data or out_data.get("value") is None: print("🚫 forge track: resolver bad output"); return 1
    snap={**out_data,"baseline":mdef["baseline"],"target":mdef["target"],"direction":mdef["direction"],
          "metric_id":mdef["metric_id"]}   # enrich：定义来自 curate，不来自 resolver
    ok,e2=append_metric_snapshot(kind,oid,snap)
    if not ok: print(f"🚫 forge track: snapshot rejected — {e2}"); return 1
    last=load_metric_history(kind,oid)[-1]
    print(f"✅ forge track {kind}/{oid} {last['metric_id']}={last['value']} @ {last['as_of']}" + ("  ⚠️ regression" if last['regression'] else "")); return 0
```

main() 分发（与 Task 5 共用 argv 解析）：

```python
    elif action == "track":
        a=sys.argv[2:]
        kind,oid,rest = ("objectives",a[1],a[2:]) if a[:1]==["--objective"] else ("features",a[0],a[1:])
        if len(rest)<2: print("Usage: track <slug> <metric_id> <as_of> | track --objective <id> <metric_id> <as_of>"); sys.exit(1)
        sys.exit(cmd_track(kind,oid,rest[0],rest[1]))
```

metric.json 模板加字段：

```bash
python3 - <<'PY'
import json,io
p="skills/forge/templates/metric.json"; d=json.load(open(p))
d["metric_id"]=""; d["direction"]="up"
json.dump(d, open(p,"w"), indent=2, ensure_ascii=False)
PY
```

- [ ] **Step 4: 跑通** → passed（含注入被拒、`/tmp/pwned` 不存在）
- [ ] **Step 5: commit** `feat(forge): forge track — curate-enriched snapshot via argv resolver (no shell injection) (Task 3)`

---

### Task 4: objective 对齐（方向感知）+ roadmap.md 段 + dict 安全渲染

**Files:** Modify `forge_gate.py`；Test `test_forge_metrics.py`

- [ ] **Step 1: 失败测试**

```python
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
```

- [ ] **Step 2: 跑失败** → FAIL
- [ ] **Step 3: 实现**

```python
ALIGN_ON_TRACK=80.0; ALIGN_AT_RISK=40.0
def _target_metric_struct(obj):
    tm=obj.get("target_metric"); return tm if isinstance(tm,dict) else None
def compute_objective_alignment(obj):
    tm=_target_metric_struct(obj)
    if not tm: return None  # 旧 string → 不渲染对齐（向后兼容）
    hist=load_metric_history("objectives",obj.get("id",""))
    cur=hist[-1]["value"] if hist else None; as_of=hist[-1]["as_of"] if hist else None
    base,target=tm.get("baseline"),tm.get("target"); pct,status=None,"no_data"
    if cur is not None and isinstance(base,(int,float)) and isinstance(target,(int,float)) and target!=base:
        pct=round((cur-base)/(target-base)*100.0,1)  # 方向无关（base/target 编码方向）
        status="on_track" if pct>=ALIGN_ON_TRACK else ("at_risk" if pct>=ALIGN_AT_RISK else "off_track")
    return {"metric_id":tm.get("metric_id"),"baseline":base,"target":target,"current":cur,"pct":pct,"status":status,"as_of":as_of}
```

`generate_roadmap_md` 中：①对现有 `obj.get('target_metric')` 渲染处改 **dict-safe**（dict 时显示 `tm['metric_id']: base→target`，str 时原样）；②`## Summary` 前插对齐段：

```python
    def _tm_display(tm):
        return f"{tm.get('metric_id')}: {tm.get('baseline')}→{tm.get('target')}" if isinstance(tm,dict) else str(tm)
    # （把原 line ~1106 `obj.get('target_metric')` 的渲染替换为 _tm_display(obj.get('target_metric'))）
    align=[]
    for obj in objectives:
        a=compute_objective_alignment(obj)
        if a:
            cur="—" if a["current"] is None else a["current"]; pct="—" if a["pct"] is None else f"{a['pct']}%"
            align.append(f"| {obj.get('id')} | {a['metric_id']} | {a['baseline']}→{a['target']} | {cur} | {pct} | {a['status']} | {a['as_of'] or '—'} |")
    if align:
        lines+=["## Objective 对齐","| Objective | Metric | Baseline→Target | Current | Progress | Status | As-of |",
                "|---|---|---|---|---|---|---|", *align, ""]
```

- [ ] **Step 4: 跑通** → passed - [ ] **Step 5: roadmap 回归** `... pytest tests/forge/test_forge_gate.py -k roadmap -q -p no:cacheprovider` → PASS - [ ] **commit** `feat(forge): direction-aware objective alignment + dict-safe roadmap.md (Task 4)`

---

### Task 5: `forge analyze`（先复验证据 + 方向感知 projection + provenance）

**Files:** Modify `forge_gate.py`；Test `test_forge_metrics.py`

- [ ] **Step 1: 失败测试**（含 evidence 复验门 + projection 方向）

```python
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
```

- [ ] **Step 2: 跑失败** → FAIL
- [ ] **Step 3: 实现**

```python
def _linear_slope(vals):
    n=len(vals)
    if n<2: return 0.0
    xs=list(range(n)); mx=sum(xs)/n; my=sum(vals)/n; den=sum((x-mx)**2 for x in xs)
    return 0.0 if den==0 else round(sum((xs[i]-mx)*(vals[i]-my) for i in range(n))/den,4)

def cmd_analyze(kind, oid):
    if not _safe_owner_id(oid): print("🚫 forge analyze: unsafe owner id (blocks '..' and '/')"); return 1
    ok,why=verify_history_evidence(kind,oid)          # F6：先复验，篡改即拒
    if not ok: print(f"🚫 forge analyze: history evidence check failed — {why}"); return 1
    root=get_repo_root(); hist=load_metric_history(kind,oid)
    if not hist: print(f"🚫 forge analyze: no history for {kind}/{oid}"); return 1
    vals=[h["value"] for h in hist if isinstance(h.get("value"),(int,float))]
    last=hist[-1]; slope=_linear_slope(vals); trend="up" if slope>0 else ("down" if slope<0 else "flat")
    target=last.get("target"); direction=last.get("direction","up"); projected=None
    if isinstance(target,(int,float)):
        if not is_improvement(target, vals[-1], direction):   # latest 已达/越过 target（含相等）
            projected={"periods_to_target":0,"reachable":True,"achieved":True}
        elif slope!=0:
            periods=round((target-vals[-1])/slope,1)
            reachable = is_improvement(vals[-1]+slope, vals[-1], direction)  # 朝改善方向移动即可达
            projected={"periods_to_target":periods,"reachable":reachable,"achieved":False}
    analysis={"owner":f"{kind}/{oid}","metric_id":last.get("metric_id"),"samples":len(vals),
              "first":vals[0],"latest":vals[-1],"baseline":last.get("baseline"),"target":target,
              "direction":direction,"slope":slope,"trend":trend,"regressions":sum(1 for h in hist if h.get("regression")),
              "projected":projected,
              "provenance":{"source_tier":last.get("source") or "resolver","as_of":last.get("as_of"),
                            "owner":f"{kind}/{oid}","raw_path":last.get("raw_path"),"evidence_sha256":last.get("evidence_sha256"),
                            "note":"deterministic trend over re-verified evidence-bound snapshots; adversarial attribution via skills/forge/workflows/analyze.workflow.js"}}
    out=os.path.join(metric_owner_dir(root,kind,oid),"analysis.json")
    with open(out,"w",encoding="utf-8") as f: json.dump(analysis,f,indent=2,ensure_ascii=False)
    print(f"✅ forge analyze {kind}/{oid}: {trend} (slope={slope}, {vals[-1]}/{target}) → {out}"); return 0
```

main() 分发：`elif action=="analyze": a=sys.argv[2:]; sys.exit(cmd_analyze("objectives",a[1]) if a[:1]==["--objective"] else cmd_analyze("features",a[0]) if a else (print("Usage: analyze <slug> | analyze --objective <id>") or 1))`

- [ ] **Step 4: 跑通** → passed - [ ] **Step 5: commit** `feat(forge): forge analyze — evidence re-verify + direction-aware projection + provenance (Task 5)`

---

### Task 6: 真实 analyze dynamic workflow 脚本 + 结构 lint

**Files:** Create `skills/forge/workflows/analyze.workflow.js`；Test `test_forge_metrics.py`

- [ ] **Step 1: 失败 lint 测试**（AC10：存在 + 合法 JS 形状 + 无裸反引号 + 有 meta/对抗 review）

```python
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
        r=subprocess.run([node,"--check",p],capture_output=True,text=True)
        assert r.returncode==0, f"node --check failed: {r.stderr}"
```

- [ ] **Step 2: 跑失败** → FAIL（脚本不存在）
- [ ] **Step 3: 写脚本**（消费 analysis.json：fan-out 趋势解读 + 对抗归因 reviewer + 合成 cited verdict + provenance footer）

```javascript
export const meta = {
  name: 'forge-analyze-attribution',
  description: 'Adversarial attribution over a metric trend: trend readers + refuters + cited synthesis with provenance footer',
  phases: [{ title: 'Read' }, { title: 'Refute' }, { title: 'Synthesize' }],
}
// args: { analysisPath, shippedFeatures }  — analysis.json produced by `forge analyze`
const A = args && args.analysis ? args.analysis : {}
const shipped = (args && args.shippedFeatures) || []
phase('Read')
const readers = await parallel([
  () => agent('Read this metric analysis JSON and state, in 2 sentences, what the trend is and how strong it is. Be literal; do not invent direction. ANALYSIS: ' + JSON.stringify(A), { label: 'trend-read', phase: 'Read' }),
  () => agent('Given the metric trend and these shipped features ' + JSON.stringify(shipped) + ', list the 2 most plausible CAUSAL drivers of the movement. ANALYSIS: ' + JSON.stringify(A), { label: 'driver-hypo', phase: 'Read' }),
])
phase('Refute')
const refutes = await parallel(readers.filter(Boolean).map((r, i) => () =>
  agent('Adversarially REFUTE this attribution claim. Default to refuted=true if the move could be seasonality, a confounder, or noise rather than the shipped features. Preserve the trend direction faithfully; never reverse it. CLAIM: ' + r, { label: 'refute-' + i, phase: 'Refute' })
))
phase('Synthesize')
const verdict = await agent(
  'Synthesize a cited attribution verdict from the readings and refutations. Output must end with a PROVENANCE FOOTER line copying source_tier, as_of, owner, raw_path, evidence_sha256 from the analysis verbatim. Faithfully preserve the trend direction. READINGS: ' + JSON.stringify(readers) + ' REFUTATIONS: ' + JSON.stringify(refutes) + ' ANALYSIS: ' + JSON.stringify(A),
  { label: 'synthesize', phase: 'Synthesize' }
)
return { verdict, provenance: A.provenance || null }
```

> 注：模板串内只用普通引号 + 字符串拼接，**无裸反引号**（KNOWLEDGE 坑）。LLM 执行由 `/forge analyze` 在 skill 层用 Workflow 工具触发（喂 analysis.json）；本步只交付脚本 + 结构门，不进 pytest 执行门（P0-5 边界，1.6 确认）。

- [ ] **Step 4: 跑通 lint** → passed - [ ] **Step 5: commit** `feat(forge): real analyze.workflow.js dynamic workflow (adversarial attribution) + lint (Task 6)`

---

### Task 7: dashboard dict 兼容 + 回归（AC12）

**Files:** Modify `skills/forge/forge_dashboard.py`；Test `tests/forge/test_forge_dashboard.py`

> 修法（根因）：JS 端 `objCard` 做 `esc(o.target_metric)`，dict 会渲染成 `[object Object]`。**在数据层归一化**：`build_snapshot` 给每个 objective 加 `target_metric_display`（dict→`"id: base→target"`、str→原样），JS 改渲 `o.target_metric_display`（永远是 string）。既修 JS 又不依赖 JS 执行测试。

- [ ] **Step 1: 失败测试**（数据层有 display 串 + HTML 不裸渲 dict + JS 用 display 字段）

```python
def test_dashboard_normalizes_target_metric_display(tmp_path):
    import importlib.util, os, json
    DP=os.path.join(os.path.dirname(__file__),"..","..","skills","forge","forge_dashboard.py")
    s=importlib.util.spec_from_file_location("fd",DP); fd=importlib.util.module_from_spec(s); s.loader.exec_module(fd)
    rmdir=tmp_path/"project-roadmap"; rmdir.mkdir()
    (rmdir/"roadmap.json").write_text(json.dumps({"project":{"name":"P","north_star":"NS"},
        "objectives":[{"id":"obj-1","name":"G","target_metric":{"metric_id":"m1","baseline":0,"target":100,"direction":"up"}},
                      {"id":"obj-2","name":"L","target_metric":"reach X"}],"features":[]}))
    snap=fd.build_snapshot(str(tmp_path))
    o1,o2=snap["objectives"][0],snap["objectives"][1]
    assert o1["target_metric_display"]=="m1: 0→100" and o2["target_metric_display"]=="reach X"
    src=open(DP,encoding="utf-8").read()  # JS 模板内嵌在 .py 源里，读源断言（避开 render_html 签名）
    assert "o.target_metric_display" in src and "esc(o.target_metric)" not in src  # JS 渲 display 字段，不裸渲 dict
```

- [ ] **Step 2: 跑失败** → FAIL（dict 当 str 渲染 / 无 display 字段 / JS 仍 `esc(o.target_metric)`）
- [ ] **Step 3: 实现** — ① `build_snapshot` 给每个 objective 注入 `target_metric_display`：`tm=obj.get("target_metric"); display = f"{tm['metric_id']}: {tm.get('baseline')}→{tm.get('target')}" if isinstance(tm,dict) else str(tm or "")`；② JS `objCard` 把 `esc(o.target_metric)` 改成 `esc(o.target_metric_display)`。
- [ ] **Step 4: 跑通 + dashboard 全回归** `... pytest tests/forge/test_forge_dashboard.py -q -p no:cacheprovider` → PASS - [ ] **Step 5: commit** `fix(forge): dashboard normalizes structured target_metric to display string (no [object Object]) (Task 7)`

---

### Task 8: 全 AC E2E runner + 文档

**Files:** Create `tests/forge/forge_metrics_e2e_runner.py`；Modify `skills/forge/SKILL.md` + `.claude/commands/forge.md`

- [ ] **Step 1: 写 runner**（真 CLI + stub resolver；**全 AC1-12 各 ≥1 turn**；≥14 turns；每 turn 业务断言）。结构同现有 `forge_engine_e2e_runner.py`：`run(repo,*args)` 设 `FORGE_REPO_ROOT`、`turn(action,expect,ok,detail)`，turns：
  - AC1 no-config→非零；AC2 first-snapshot→jsonl 1 行+value+baseline/target/direction enrich（断言 enrich 来自 metric.json 非 resolver）；AC3 tampered-evidence→拒收+行数不变；AC4 regression-up→regression True；AC5 direction-down 序列→升=regression True、降不算；AC6 objective track→`compute_objective_alignment` status 正确；AC7 `forge generate-view` 后读 roadmap.md 含「Objective 对齐」段+current；AC8 analyze→analysis.json 字段+projected 方向；AC8-tamper 改证据后 analyze→非零；AC9 provenance footer 含 source_tier/as_of/owner/raw_path/evidence_sha256；**AC10 `python3 -m pytest tests/forge/test_forge_metrics.py -k workflow_script_lint` 子进程退出 0（workflow.js lint）**；AC11 injection（`m1;touch`）+ path-traversal（`../../etc`）→拒+无副作用；**AC12 `build_snapshot` 对结构化 target_metric 产 `target_metric_display` 且 rendered HTML 无 `[object Object]`**。
  - 末尾写 `{scenarios:[{name,rounds:[{turns}]}],turns,turn_count,passed}`，`passed=all turn.status==pass`，退出码 0/1。
- [ ] **Step 2: 跑 runner** `env -u FASTSHIP_REPO_ROOT -u FASTSHIP_STATE_HOME -u FASTSHIP_SESSION python3 tests/forge/forge_metrics_e2e_runner.py -o /tmp/forge_metrics_e2e.json` → `PASS — ≥14 turns`
- [ ] **Step 3: 过 e2e_gate** `python3 skills/fastship/e2e/e2e_gate.py --result /tmp/forge_metrics_e2e.json --min-turns 14` → PASS
- [ ] **Step 4: 全套回归** `env -u FASTSHIP_REPO_ROOT -u FASTSHIP_STATE_HOME -u FASTSHIP_SESSION python3 -m pytest tests/forge/ -q -p no:cacheprovider` → all PASS
- [ ] **Step 5: 文档**（SKILL.md + forge.md 增 track/analyze + workflow + supersede 注记，含 token `forge track`/`forge analyze`/`metrics.project.json`/`dynamic workflow`/`provenance`/`direction`），扩 `test_command_docs.py`，commit `docs+test(forge): metrics E2E runner (all AC) + track/analyze docs (Task 8)`

---

## Self-Review

**1. Spec coverage：** AC1→T1/T8；AC2→T3/T8；AC3→T2/T8；AC4→T2/T8；AC5→T2/T8；AC6→T4/T8；AC7→T4/T8；AC8→T5/T8；AC9→T5/T8；AC10→T6+T8(E2E lint turn)；AC11→T3/T8(含 path-traversal)；AC12→T7+T8(E2E dashboard turn)。P0-1（argv+白名单，覆盖 oid 防 `../`）T3/T1；P0-2（双端 evidence）T2/T5；P0-3（dict/str+可选字段）T4/T7；P0-4（direction）T2/T4/T5；P0-5（真 workflow 脚本+lint）T6。codex R1 7 findings：F1→T6，F2→T2/T4/T5，F3→T3，F4→T8，F5→T3，F6→T2/T5，F7→T7。codex R2 4 findings：path-traversal→T3（oid 白名单）, F4 残留 AC10/AC12→T8, F7 残留 JS render→T7（target_metric_display）, AC9 footer raw_path→T6。全闭。
**2. Placeholder scan：** 无 TBD/TODO；每 code step 给完整函数+测试体；T8 runner 给出 turn 清单与断言（按现有 runner 范式逐字落）。
**3. Type consistency：** `is_improvement(cur,prev,direction)` / `append_metric_snapshot(kind,oid,snap)`（snap 必含 baseline/target/direction）/ `verify_history_evidence(kind,oid)` / `_owner_metric_def(kind,oid,metric_id)` / `cmd_track(kind,oid,metric_id,as_of)` / `compute_objective_alignment(obj)` / `cmd_analyze(kind,oid)` 跨任务签名一致；history row 字段（含 raw_path/collected_at/direction）在 T2 定义、T5/T7/T8 一致引用；`kind∈{"features","objectives"}` 统一。

## 风险与缓解
- workflow 脚本 LLM 执行不进 pytest（P0-5 显式边界，1.6 确认）；CJK/裸反引号/staticmethod 三坑由 T6 lint + 普通引号拼接规避。
- 测试 env 污染 → 全程 `env -u FASTSHIP_REPO_ROOT -u FASTSHIP_STATE_HOME -u FASTSHIP_SESSION` + `-p no:cacheprovider`（防 stale .pyc）。
- dashboard 改动面 → T7 专跑 dashboard 全回归。
