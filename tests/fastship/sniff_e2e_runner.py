#!/usr/bin/env python3
"""sniff E2E runner — 真实驱动 start→真实推进→fixture 注入→subprocess sniff，零 mock。
15 个 scenario 与 plan ac_mapping 的 e2e 名称一一对应。每 turn 记录真实命令真实输出。"""
import hashlib, json, os, re, subprocess, sys, tempfile, time
from datetime import datetime, timedelta

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
ORCH = os.path.join(ROOT, "skills", "fastship", "orchestrator.py")
GATE = os.path.join(ROOT, "skills", "fastship", "hooks", "ship_verify_gate.py")

scenarios = []
_cur = None

def scenario(name, desc=""):
    global _cur
    _cur = {"name": name, "description": desc, "rounds": [{"turns": []}]}
    scenarios.append(_cur)

def turn(action, cond, detail=""):
    _cur["rounds"][0]["turns"].append(
        {"action": action, "status": "pass" if cond else "fail", "passed": bool(cond),
         "response": "ok" if cond else "FAILED", "detail": str(detail)[:300]})

def run_orch(env, *args, script=ORCH):
    return subprocess.run([sys.executable, script, *args], env=env,
                          capture_output=True, text=True, timeout=60)

def parse_sniff(stdout):
    lines = [l for l in stdout.splitlines() if l.startswith("[FASTSHIP_SNIFF]")]
    if len(lines) != 1:
        return None
    d = {}
    for tok in lines[0].split()[1:]:
        if "=" in tok:
            k, v = tok.split("=", 1)
            d[k] = v
    return d

def main():
    tmp = tempfile.mkdtemp(prefix="sniff-e2e-")
    home, repo, repo2, jobs = (os.path.join(tmp, d) for d in ("home", "repoA", "repoC", "jobs"))
    for d in (repo, repo2, jobs):
        os.makedirs(d)
    subprocess.run(["git", "init", "-q", repo], check=True)
    subprocess.run(["git", "init", "-q", repo2], check=True)
    base = {k: v for k, v in os.environ.items()
            if k not in ("FASTSHIP_SESSION", "FASTSHIP_STATE_HOME",
                         "FASTSHIP_REPO_ROOT", "CLAUDE_PROJECT_DIR")}
    env = {**base, "FASTSHIP_STATE_HOME": home, "FASTSHIP_REPO_ROOT": repo}

    # ── e2e_start_hint_executable (AC-START-1)
    scenario("e2e_start_hint_executable", "start hint 内嵌命令抠出并原样真实执行")
    r = run_orch(env, "start", "--no-worktree", "sniff e2e fixture")
    turn("start exit 0", r.returncode == 0, r.stderr[-150:])
    sid = (re.search(r"Session: (\S+)", r.stdout) or [None, ""])[1]
    turn("hint has /loop + interval + stop rule", "/loop" in r.stdout and "240" in r.stdout
         and "session_done" in r.stdout and "sniff" in r.stdout)
    hint = re.search(r"`(cd .+? sniff --session \S+)`", r.stdout)  # FIX5: 显式 --session 兜底
    turn("hint embeds full command with real session id",
         bool(hint) and sid in hint.group(1) and home in hint.group(1))
    if hint:
        hr = subprocess.run(["bash", "-c", hint.group(1)], env=base,
                            capture_output=True, text=True, timeout=60)
        d = parse_sniff(hr.stdout)
        turn("hint command executes verbatim → valid verdict line",
             hr.returncode == 0 and d is not None and d.get("session") == sid
             and d.get("verdict") == "ok", hr.stdout[-200:])
    env_s = {**env, "FASTSHIP_SESSION": sid}

    # ── e2e_healthy_sniff (AC-SNIFF-1)
    scenario("e2e_healthy_sniff", "健康 session：恰好一行 verdict=ok")
    r = run_orch(env_s, "sniff", "--jobs-dir", jobs)
    d = parse_sniff(r.stdout)
    turn("exactly one line, exit 0", r.returncode == 0 and d is not None)
    turn("verdict=ok session/step match", d and d["verdict"] == "ok"
         and d["session"] == sid and d["step"] == "1.0" and d["action"] == "none")

    # ── e2e_step_ts_monotonic (AC-TS-1) —— 真实推进通道（classify+done，断言 rc 与位移）
    scenario("e2e_step_ts_monotonic", "classify+done 真实推进，entered_at 落盘且单调")
    opath = os.path.join(home, "sessions", sid, "orchestrator.json")
    rc1 = run_orch(env_s, "classify", "--type", "feature", script=GATE)
    rd = run_orch(env_s, "done")
    st = json.load(open(opath))
    turn("classify+done rc=0 and step actually advanced",
         rc1.returncode == 0 and rd.returncode == 0 and st["current_step"] == "1.1",
         f"step={st['current_step']}")
    ts = st.get("step_entered_at", {})
    turn("new step stamped, monotonic vs 1.0",
         "1.1" in ts and ts["1.0"] <= ts["1.1"])

    # ── e2e_bg_classify_no_mtime (AC-SNIFF-2) —— mtime 双向证伪 + unknown 可观察
    #    本 scenario 恰好一次 sniff（消耗 j1 链的 resume 一格），notify 转换留给
    #    e2e_evidence_chain 场景自含完成（codex round-2：场景顺序自洽）。
    scenario("e2e_bg_classify_no_mtime", "blocked/unknown/working 分类只依赖 state 字段")
    fixtures = {"j1": {"state": "blocked", "intent": "cargo build", "cwd": repo,
                       "updatedAt": "2026-06-10T00:00:00"},
                "j2": {"cwd": repo},                                  # 有归属但无 state → unknown
                "j3": {"state": "active", "intent": "y", "cwd": repo}}
    for jid, body in fixtures.items():
        os.makedirs(os.path.join(jobs, jid))
        open(os.path.join(jobs, jid, "state.json"), "w").write(json.dumps(body))
    old = time.time() - 86400
    os.utime(os.path.join(jobs, "j1", "state.json"), (old, old))   # blocked+旧 mtime
    os.utime(os.path.join(jobs, "j3", "state.json"), (old, old))   # active+旧 mtime
    r = run_orch(env_s, "sniff", "--jobs-dir", jobs)
    d = parse_sniff(r.stdout)
    turn("old-mtime blocked still detected (state-only)", d and d.get("signal") == "bg_state"
         and d.get("job") == "j1" and d["verdict"] == "stalled" and d["action"] == "resume")
    turn("old-mtime active not dead; stateless observable as jobs_unknown=1",
         d and d.get("jobs_checked") == "3" and d.get("jobs_unknown") == "1", str(d))

    # ── e2e_evidence_chain (AC-RESUME-2, bg_state 信号源) —— notify 转换发生在本场景内
    scenario("e2e_evidence_chain", "bg_state notify 证据链字段值与 fixture 对账")
    r = run_orch(env_s, "sniff", "--jobs-dir", jobs)   # j1 事件第二次 → notify_user
    d = parse_sniff(r.stdout)
    turn("bg notify evidence: signal/since==fixture/resume_at",
         d and d["action"] == "notify_user" and d["signal"] == "bg_state"
         and d["stalled_since"] == "2026-06-10T00:00:00" and "resume_at" in d
         and int(d["stalled_s"]) > 86000, str(d))

    # ── e2e_notify_dedup (AC-NOTIFY-1) —— 静默 + 🔴 starvation 反向（j1 保持在场）
    scenario("e2e_notify_dedup", "notified 后静默；旧 blocked job 在场时新 job 仍重开完整链")
    r = run_orch(env_s, "sniff", "--jobs-dir", jobs)
    d = parse_sniff(r.stdout)
    turn("third round silent stalled_notified", d and d["verdict"] == "stalled_notified"
         and d["action"] == "none")
    os.makedirs(os.path.join(jobs, "j4"))              # j1 不删！必须证明不被旧事件饿死
    open(os.path.join(jobs, "j4", "state.json"), "w").write(json.dumps(
        {"state": "blocked", "intent": "psql migrate", "cwd": repo}))
    r = run_orch(env_s, "sniff", "--jobs-dir", jobs)
    d = parse_sniff(r.stdout)
    turn("REVERSE: j4 opens fresh chain while notified j1 still blocked",
         d and d["action"] == "resume" and d.get("job") == "j4", str(d))
    r = run_orch(env_s, "sniff", "--jobs-dir", jobs)
    d = parse_sniff(r.stdout)
    turn("j4 chain completes its own notify", d and d["action"] == "notify_user"
         and d.get("job") == "j4", str(d))
    spath = os.path.join(home, "sessions", sid, "sniff-state.json")
    ss = json.load(open(spath))
    turn("events keyed per job: j1 and j4 chains independent",
         any("|bg_state|j1" in k for k in ss["events"])
         and any("|bg_state|j4" in k for k in ss["events"]))
    # 🔴 防回归（codex round-4）：j4 已 notified，刷新其 updatedAt（模拟 daemon 心跳）
    # → 绝不重开链（updatedAt 不在事件键，否则 resume 风暴回归）
    open(os.path.join(jobs, "j4", "state.json"), "w").write(json.dumps(
        {"state": "blocked", "intent": "psql migrate", "cwd": repo,
         "updatedAt": datetime.now().isoformat()}))
    r = run_orch(env_s, "sniff", "--jobs-dir", jobs)
    d = parse_sniff(r.stdout)
    turn("ANTI-STORM: churned updatedAt on notified j4 stays silent",
         d and d["verdict"] == "stalled_notified" and d["action"] == "none", str(d))
    for jid in ("j1", "j2", "j4"):                     # 清场给 step_stale 场景
        os.remove(os.path.join(jobs, jid, "state.json"))

    # ── e2e_step_stale_evidence + e2e_escalation_dedup + e2e_readonly_hash_sandwich
    #    单一基线 sandwich：回拨 fixture 写完后取基线，跨 resume/notify/silent 全部
    #    5 次 sniff 后一次性断言（中途零基线重取 —— codex round-2）。
    scenario("e2e_step_stale_evidence", "回拨 entered_at → stalled 四 evidence 字段")
    gpath = os.path.join(home, "sessions", sid, "gate.json")
    if not os.path.exists(gpath):
        json.dump({}, open(gpath, "w"))
    h = lambda p: hashlib.sha256(open(p, "rb").read()).hexdigest()
    st = json.load(open(opath))
    step = st["current_step"]
    st["step_entered_at"][step] = (datetime.now() - timedelta(seconds=99999)).isoformat()
    json.dump(st, open(opath, "w"))                  # fixture builder 身份回拨（非 sniff 写）
    sandwich_base = (h(opath), h(gpath))             # 🔴 唯一基线，此后不再重取
    r = run_orch(env_s, "sniff", "--jobs-dir", jobs)
    d = parse_sniff(r.stdout)
    turn("stalled with full evidence", d and d["verdict"] == "stalled"
         and d["action"] == "resume" and d["signal"] == "step_stale"
         and "entered_at" in d and "threshold_s" in d and int(d["stalled_s"]) > 3600, str(d))

    scenario("e2e_escalation_dedup", "同事件 5 连跑（独立进程）：resume→notify→none×3, attempts 恒 1")
    seq = [d]
    for _ in range(4):
        r = run_orch(env_s, "sniff", "--jobs-dir", jobs)
        seq.append(parse_sniff(r.stdout) or {})
    turn("action sequence", [x.get("action") for x in seq]
         == ["resume", "notify_user", "none", "none", "none"], str([x.get("action") for x in seq]))
    turn("step_stale notify evidence carries resume_at + stalled_since",
         seq[1].get("signal") == "step_stale" and "resume_at" in seq[1]
         and "stalled_since" in seq[1], str(seq[1]))
    ss = json.load(open(spath))
    key = f"{step}|step_stale|{st['step_entered_at'][step]}"
    turn("persisted attempts==1 after 5 independent processes",
         ss["events"][key]["resume_attempts"] == 1 and ss["events"][key]["notified"] is True)

    scenario("e2e_readonly_hash_sandwich", "跨 resume/notify/silent 路径引擎 state 零写入")
    turn("orchestrator.json+gate.json sha256 unchanged across all 3 action paths",
         (h(opath), h(gpath)) == sandwich_base)
    turn("sniff-state.json exists as the only sniff-written file", os.path.exists(spath))
    # 反向（sandwich 断言之后才动 fixture）：entered_at 刷新（loop rewind 语义）→ 新事件键 → 链重开
    st["step_entered_at"][step] = (datetime.now() - timedelta(seconds=88888)).isoformat()
    json.dump(st, open(opath, "w"))
    r = run_orch(env_s, "sniff", "--jobs-dir", jobs)
    d = parse_sniff(r.stdout)
    turn("REVERSE: refreshed entered_at reopens chain", d and d["action"] == "resume")

    # ── e2e_heartbeat_advances (AC-HB-1)
    scenario("e2e_heartbeat_advances", "两次独立进程夹 sleep，心跳严格递增")
    t1 = json.load(open(spath))["last_check_at"]
    time.sleep(1.1)
    run_orch(env_s, "sniff", "--jobs-dir", jobs)
    t2 = json.load(open(spath))["last_check_at"]
    turn("heartbeat strictly advances", t2 > t1, f"{t1} -> {t2}")

    # ── e2e_status_heartbeat_3states (AC-HB-2)
    scenario("e2e_status_heartbeat_3states", "status 露出 健康/超龄/未启动 三态")
    r = run_orch(env_s, "status")
    turn("healthy heartbeat shown", "嗅探心跳" in r.stdout)
    ss = json.load(open(spath))
    ss["last_check_at"] = (datetime.now() - timedelta(seconds=2 * 240 + 120)).isoformat()
    json.dump(ss, open(spath, "w"))
    r = run_orch(env_s, "status")
    turn("stale watchdog flagged", "watchdog stale" in r.stdout)
    os.remove(spath)
    r = run_orch(env_s, "status")
    turn("not-started hint shown", "嗅探未启动" in r.stdout)

    # ── e2e_scope_isolation (AC-SCOPE-1)
    scenario("e2e_scope_isolation", "同根保守跳过 + 异根互不可见 + 自我排除（带对照）")
    st = json.load(open(opath))
    st["step_entered_at"][st["current_step"]] = datetime.now().isoformat()  # A 恢复健康
    json.dump(st, open(opath, "w"))
    rb = run_orch(env, "start", "--no-worktree", "--shared", "shared-root session B")
    sid_b = (re.search(r"Session: (\S+)", rb.stdout) or [None, ""])[1]
    os.makedirs(os.path.join(jobs, "jamb"))
    open(os.path.join(jobs, "jamb", "state.json"), "w").write(json.dumps(
        {"state": "blocked", "intent": "ambiguous task", "cwd": repo}))
    r = run_orch(env_s, "sniff", "--jobs-dir", jobs)
    d = parse_sniff(r.stdout)
    turn("shared root → conservative skip with note", d and d["verdict"] == "ok"
         and "bg_shared_root" in d.get("note", ""), str(d))
    turn("A output contains zero B id", sid_b and sid_b not in r.stdout)
    # 对照组（codex round-2）：B 终结后同根不再算共享 → A 恢复正常 bg 告警
    bpath = os.path.join(home, "sessions", sid_b, "orchestrator.json")
    bst = json.load(open(bpath))
    bst["current_step"] = "done"
    json.dump(bst, open(bpath, "w"))
    r = run_orch(env_s, "sniff", "--jobs-dir", jobs)
    d = parse_sniff(r.stdout)
    turn("CONTRAST: done same-root session no longer blocks bg attribution",
         d and d.get("job") == "jamb" and d["action"] == "resume", str(d))
    os.remove(os.path.join(jobs, "jamb", "state.json"))   # 清场
    env_c = {**base, "FASTSHIP_STATE_HOME": home, "FASTSHIP_REPO_ROOT": repo2}
    rc_ = run_orch(env_c, "start", "--no-worktree", "--shared", "isolated session C")
    sid_c = (re.search(r"Session: (\S+)", rc_.stdout) or [None, ""])[1]
    os.makedirs(os.path.join(jobs, "jc"))
    open(os.path.join(jobs, "jc", "state.json"), "w").write(json.dumps(
        {"state": "blocked", "intent": "c-only task", "cwd": repo2}))
    r = run_orch(env_s, "sniff", "--jobs-dir", jobs)
    turn("A never sees C-root job", "jc" not in r.stdout)
    r = run_orch({**env_c, "FASTSHIP_SESSION": sid_c}, "sniff", "--jobs-dir", jobs)
    d = parse_sniff(r.stdout)
    turn("C's own sniff alarms on its job (control group)",
         d and d.get("job") == "jc" and d["signal"] == "bg_state", str(d))
    os.makedirs(os.path.join(jobs, "jself"))
    open(os.path.join(jobs, "jself", "state.json"), "w").write(json.dumps(
        {"state": "blocked", "intent": "FASTSHIP_SNIFF watch loop", "cwd": repo2}))
    r = run_orch({**env_c, "FASTSHIP_SESSION": sid_c}, "sniff", "--jobs-dir", jobs)
    turn("self-signed job invisible while control job visible",
         "jself" not in r.stdout and "jc" in r.stdout)

    # ── e2e_session_done_stops (AC-STOP-1)
    scenario("e2e_session_done_stops", "终态 → session_done/stop_loop（终态优先于旧 stalled）")
    st = json.load(open(opath))
    st["current_step"] = "done"
    st["step_entered_at"]["done"] = "2000-01-01T00:00:00"   # 终态+极旧戳并存
    json.dump(st, open(opath, "w"))
    r = run_orch(env_s, "sniff", "--jobs-dir", jobs)
    d = parse_sniff(r.stdout)
    turn("terminal state wins over staleness", d and d["verdict"] == "session_done"
         and d["action"] == "stop_loop", str(d))

    # ── e2e_docs_anchor (AC-START-2)
    scenario("e2e_docs_anchor", "双文档自动启动指示锚点")
    for rel in ("skills/fastship/SKILL.md", ".claude/commands/fastship.md"):
        text = open(os.path.join(ROOT, rel), encoding="utf-8").read()
        turn(f"anchors in {rel}", "start 成功后" in text and "自动" in text
             and "启动嗅探 loop" in text and "手动粘贴" in text and "interval_s" in text)

    # ── e2e_liveness_parity (AC-REUSE-1)
    scenario("e2e_liveness_parity", "与 session-radar liveness 五类输入逐项一致")
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "sd", os.path.join(ROOT, "skills", "session-radar", "session_dashboard.py"))
    sd = importlib.util.module_from_spec(spec); spec.loader.exec_module(sd)
    sys.path.insert(0, os.path.dirname(ORCH))  # orchestrator.py does `import fastship_state` (sibling)
    spec2 = importlib.util.spec_from_file_location("orch_mod", ORCH)
    om = importlib.util.module_from_spec(spec2); spec2.loader.exec_module(om)
    for s in ["active", "blocked", "done", None, "wibble"]:
        turn(f"parity on {s!r}",
             om._classify_bg_state(s) == sd.liveness(0, is_bg=True, bg_state=s))

    all_turns = [t for sc in scenarios for t in sc["rounds"][0]["turns"]]
    passed = sum(1 for t in all_turns if t["passed"])
    result = {"scenarios": scenarios, "turns": len(all_turns), "passed": passed,
              "failed": len(all_turns) - passed, "timestamp": datetime.now().isoformat()}
    out = sys.argv[sys.argv.index("-o") + 1] if "-o" in sys.argv else "/tmp/sniff_e2e_result.json"
    json.dump(result, open(out, "w"), ensure_ascii=False, indent=1)
    print(f"{passed}/{len(all_turns)} turns passed across {len(scenarios)} scenarios → {out}")
    return 0 if passed == len(all_turns) else 1

if __name__ == "__main__":
    sys.exit(main())
