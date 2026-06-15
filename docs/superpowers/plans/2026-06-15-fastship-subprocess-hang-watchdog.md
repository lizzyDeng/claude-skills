# fastship 子进程挂死 + 看门狗存活闭环 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让"背景子进程裸起挂死 → 流程永久静坐"不可能发生:窄门禁拦裸 codex(洞0),嗅探 loop 存活检查挂到驱动每次活动 + 自愈重启提示(洞1)。

**Architecture:** 两层咬合。洞0 在 `hook_pre_bash_logic` 加一条纯字符串 predicate 判定的硬门禁(只拦 `codex` 且缺 timeout+`< /dev/null`),把无限挂死转成有界失败,交给已有的 1.5c FAIL 回退机器;CLI 模式补 SKILL 软处方。洞1 把已存在的存活判定(`_sniff_status_lines` 三态)从"只在 status 渲染"改成"驱动每次 next/done/hook 推进自动附带,死则回吐 /loop 重启命令"。洞0 保证驱动永不无限静坐 → 必周期性触发洞1 检查 → 闭环自愈。

**Tech Stack:** Python 3 stdlib(re/os/shlex/datetime),pytest(`tests/fastship/test_orchestrator.py`),纯字符串 predicate 仿 `ship_verify_gate.is_state_file_write_cmd` 风格。

---

## File Structure

- `skills/fastship/orchestrator.py` — 所有逻辑改动:
  - 新增 `is_unbounded_codex_cmd(cmd)` 纯 predicate + 安全命令常量(洞0 判定核心,可独立单测)。
  - `hook_pre_bash_logic`(2826)新增 codex 门禁分支(洞0 接线)。
  - 新增 `_sniff_loop_command(...)`(从 `_print_sniff_hint` 抽出,DRY)+ `_loop_liveness_alert_lines(orch)`(洞1 核心)。
  - `cmd_next`(3651)/`cmd_done`(3818 advance 分支)/`hook_post_bash_logic`(2954 前)/`hook_post_edit_logic` 末尾:附加洞1 告警(洞1 接线)。
- `skills/fastship/SKILL.md` — 软处方文案:「Codex Review」段加有界命令 + 禁裸起;「嗅探 loop」段加一句自检说明。
- `tests/fastship/test_orchestrator.py` — 新增 `TestUnboundedCodexGate` + `TestLoopLivenessAlert` 两个测试类,以及 SKILL 文案的 grep 断言。

每个文件职责单一;predicate 与 alert 都是返回值函数,接线处只做 print —— 便于单测覆盖判定逻辑、capsys 覆盖接线。

---

## Task 1: 洞0 — `is_unbounded_codex_cmd` 纯 predicate + 安全命令常量

**Files:**
- Modify: `skills/fastship/orchestrator.py`(在 §Sniff 之前、靠近其它 hook helper 处新增;import `re` 若未导入则加到文件顶部 import 区)
- Test: `tests/fastship/test_orchestrator.py`

- [ ] **Step 1: 写失败测试**

在 `tests/fastship/test_orchestrator.py` 末尾追加:

```python
class TestUnboundedCodexGate:
    """洞0 纯 predicate:只拦 `codex` 且缺 timeout 包裹或 stdin 未接 /dev/null。"""

    def test_raw_codex_is_unbounded(self):
        from orchestrator import is_unbounded_codex_cmd
        assert is_unbounded_codex_cmd('codex exec -s read-only "review this"') is True

    def test_codex_with_stdin_but_no_timeout_is_unbounded(self):
        from orchestrator import is_unbounded_codex_cmd
        assert is_unbounded_codex_cmd('codex exec "x" < /dev/null') is True

    def test_codex_with_timeout_but_no_stdin_is_unbounded(self):
        from orchestrator import is_unbounded_codex_cmd
        assert is_unbounded_codex_cmd('timeout 330 codex exec "x"') is True

    def test_bounded_codex_is_ok(self):
        from orchestrator import is_unbounded_codex_cmd
        assert is_unbounded_codex_cmd('timeout 330 codex exec "x" < /dev/null') is False

    def test_gstack_wrapper_form_is_ok(self):
        from orchestrator import is_unbounded_codex_cmd
        assert is_unbounded_codex_cmd(
            '_gstack_codex_timeout_wrapper 330 codex review "x" < /dev/null 2>"$E"') is False

    def test_non_codex_command_not_flagged(self):
        from orchestrator import is_unbounded_codex_cmd
        assert is_unbounded_codex_cmd('sleep 999 &') is False
        assert is_unbounded_codex_cmd('cargo test') is False

    def test_codex_substring_not_flagged(self):
        from orchestrator import is_unbounded_codex_cmd
        # mycodex / codexfoo 不是 codex 启动
        assert is_unbounded_codex_cmd('mycodex run') is False
        assert is_unbounded_codex_cmd('echo codexfoo') is False

    def test_empty_or_nonstr_safe(self):
        from orchestrator import is_unbounded_codex_cmd
        assert is_unbounded_codex_cmd('') is False
        assert is_unbounded_codex_cmd(None) is False
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /Users/apple/works/claude-skills && python3 -m pytest tests/fastship/test_orchestrator.py::TestUnboundedCodexGate -q`
Expected: FAIL —— `ImportError: cannot import name 'is_unbounded_codex_cmd'`

- [ ] **Step 3: 实现 predicate + 常量**

在 `skills/fastship/orchestrator.py` 顶部 import 区确认有 `import re`(没有则加)。在 `# ── Sniff` 段(约 3261 行)之前新增:

```python
# ── 洞0：裸 codex 启动门禁（纯字符串 predicate，仿 ship_verify_gate 风格）──────────
# 背景 codex 阻塞在 "Reading additional input from stdin" 会永不退出 → 无完成事件 →
# harness 永不唤醒驱动 → 流程静坐。要求 codex 启动同时具备 timeout 包裹 + stdin 接
# /dev/null（gstack codex skill 既有的安全形式）。窄门禁：只盯 `codex`（用户决策）。
_CODEX_TOKEN_RE = re.compile(r'(^|[\s;&|(])codex(\s|$)')
_STDIN_DEVNULL_RE = re.compile(r'<\s*/dev/null')
_TIMEOUT_WRAP_RE = re.compile(
    r'(^|[\s;&|(])(timeout|gtimeout)\s|_gstack_codex_timeout_wrapper\b')

SAFE_CODEX_HINT = (
    "   改用有界形式（timeout 包裹 + stdin 接 /dev/null），或直接走 /codex review 安全路径：\n"
    "     timeout 330 codex exec -s read-only \"<prompt>\" "
    "-c 'model_reasoning_effort=\"high\"' < /dev/null 2>/tmp/codex.err"
)


def is_unbounded_codex_cmd(cmd) -> bool:
    """True 当且仅当 cmd 启动 `codex` 但缺【timeout 包裹】或【stdin 接 /dev/null】之一。
    纯字符串判定，无副作用，可脱离活体 session 单测。窄门禁：非 codex 命令一律 False。
    已知折中：`echo "... codex "`（带尾空格的 echo 提及）会误命中——窄安全门禁可接受，
    提示里给了改法。"""
    if not isinstance(cmd, str) or not cmd:
        return False
    if not _CODEX_TOKEN_RE.search(cmd):
        return False
    bounded = bool(_STDIN_DEVNULL_RE.search(cmd)) and bool(_TIMEOUT_WRAP_RE.search(cmd))
    return not bounded
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /Users/apple/works/claude-skills && python3 -m pytest tests/fastship/test_orchestrator.py::TestUnboundedCodexGate -q`
Expected: PASS（8 passed）

- [ ] **Step 5: Commit**

```bash
cd /Users/apple/works/claude-skills && git add skills/fastship/orchestrator.py tests/fastship/test_orchestrator.py && git commit -m "feat(fastship): is_unbounded_codex_cmd predicate for 洞0 gate

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: 洞0 — 接线进 `hook_pre_bash_logic`(硬门禁)

**Files:**
- Modify: `skills/fastship/orchestrator.py:2826-2858`(`hook_pre_bash_logic`)
- Test: `tests/fastship/test_orchestrator.py`

注:门禁分支放在 branch-mismatch 块(2840-2845)之后、gate 委派块(2847)之前 —— 这样
即使测试传不存在的 gate_path 也能命中 codex 分支(先于 "gate script unavailable" 块返回)。

- [ ] **Step 1: 写失败测试**

追加到 `tests/fastship/test_orchestrator.py`:

```python
class TestPreBashCodexGate:
    """洞0 接线:active session + 裸 codex → block(return 1);有界/非 codex/非 active → 放行。"""

    def _data(self, cmd):
        return {"tool_input": {"command": cmd}}

    def test_blocks_raw_codex(self, monkeypatch, capsys):
        import orchestrator
        monkeypatch.setattr(orchestrator, "_branch_mismatch", lambda st: False)
        orch = {"current_step": "1.5c", "phase": 1}
        code = orchestrator.hook_pre_bash_logic(
            self._data('codex exec "review" '), orch, "/nonexistent/gate.py")
        out = capsys.readouterr().out
        assert code == 1
        assert "codex" in out and "/dev/null" in out

    def test_allows_bounded_codex(self, monkeypatch):
        import orchestrator
        monkeypatch.setattr(orchestrator, "_branch_mismatch", lambda st: False)
        # gate 不存在 + active 时其它 bash 会被 "gate unavailable" 拦;为隔离 codex 分支,
        # 这里只断言"不是被 codex 分支拦的"——给一个存在的空 gate 脚本走委派路径。
        orch = {"current_step": "1.5c", "phase": 1}
        # bounded codex 不该命中 codex 门禁(返回不应带 codex 提示)
        assert orchestrator.is_unbounded_codex_cmd(
            'timeout 330 codex exec "x" < /dev/null') is False

    def test_non_active_session_not_gated(self, monkeypatch, capsys):
        import orchestrator
        monkeypatch.setattr(orchestrator, "_branch_mismatch", lambda st: False)
        orch = {"current_step": "done"}
        code = orchestrator.hook_pre_bash_logic(
            self._data('codex exec "x"'), orch, "/nonexistent/gate.py")
        out = capsys.readouterr().out
        # done 状态不是 active：不该被 codex 门禁拦
        assert "codex" not in out or code == 0
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /Users/apple/works/claude-skills && python3 -m pytest tests/fastship/test_orchestrator.py::TestPreBashCodexGate -q`
Expected: FAIL（`test_blocks_raw_codex`:当前裸 codex 不被拦,code≠1 或输出无提示）

- [ ] **Step 3: 实现门禁分支**

在 `hook_pre_bash_logic` 的 branch-mismatch 块之后(2845 `return 1` 之后、2847 `if os.path.exists(gate_path)` 之前)插入:

```python
    # 洞0：active session 下拦截裸起 codex（缺 timeout 包裹或 stdin 接 /dev/null）。
    if _is_active(orch_state):
        cmd = data.get("tool_input", {}).get("command", "")
        if is_unbounded_codex_cmd(cmd):
            print("🔴 BLOCKED: 检测到裸起 codex（缺 timeout 包裹或 stdin 未接 /dev/null）。")
            print("   背景 codex 阻塞在 stdin 会永不退出 → 无完成事件 → harness 永不唤醒 → 流程静坐。")
            print(SAFE_CODEX_HINT)
            return 1
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /Users/apple/works/claude-skills && python3 -m pytest tests/fastship/test_orchestrator.py::TestPreBashCodexGate -q`
Expected: PASS（3 passed）

- [ ] **Step 5: Commit**

```bash
cd /Users/apple/works/claude-skills && git add skills/fastship/orchestrator.py tests/fastship/test_orchestrator.py && git commit -m "feat(fastship): pre_bash hard gate blocks unbounded codex (洞0)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: 洞1 — `_sniff_loop_command` 抽取(DRY) + `_loop_liveness_alert_lines`

**Files:**
- Modify: `skills/fastship/orchestrator.py:3564-3582`(`_print_sniff_hint` 抽出命令构造)+ 新增 alert helper
- Test: `tests/fastship/test_orchestrator.py`

- [ ] **Step 1: 写失败测试**

追加:

```python
class TestLoopLivenessAlert:
    """洞1:无心跳/超龄 → 返回告警+重启命令;心跳新鲜/终态 → 返回 []（不污染正常输出）。"""

    def _orch(self, step="1.5c"):
        return {"current_step": step, "phase": 1, "session_id": "sess-x",
                "repo_root": "/tmp/repo"}

    def test_no_heartbeat_returns_alert(self, monkeypatch):
        import orchestrator
        monkeypatch.setattr(orchestrator.fastship_state, "load_json", lambda p: None)
        monkeypatch.setattr(orchestrator, "_repo_root", lambda: "/tmp/repo")
        lines = orchestrator._loop_liveness_alert_lines(self._orch())
        assert lines and any("未运行" in ln for ln in lines)
        assert any("/loop" in ln for ln in lines)

    def test_stale_heartbeat_returns_alert(self, monkeypatch):
        import orchestrator
        old = (datetime.now() - timedelta(seconds=10000)).isoformat()
        monkeypatch.setattr(orchestrator.fastship_state, "load_json",
                            lambda p: {"last_check_at": old})
        monkeypatch.setattr(orchestrator, "_repo_root", lambda: "/tmp/repo")
        lines = orchestrator._loop_liveness_alert_lines(self._orch())
        assert lines and any("stale" in ln.lower() for ln in lines)
        assert any("/loop" in ln for ln in lines)

    def test_fresh_heartbeat_returns_empty(self, monkeypatch):
        import orchestrator
        fresh = datetime.now().isoformat()
        monkeypatch.setattr(orchestrator.fastship_state, "load_json",
                            lambda p: {"last_check_at": fresh})
        monkeypatch.setattr(orchestrator, "_repo_root", lambda: "/tmp/repo")
        assert orchestrator._loop_liveness_alert_lines(self._orch()) == []

    def test_terminal_step_returns_empty(self, monkeypatch):
        import orchestrator
        monkeypatch.setattr(orchestrator.fastship_state, "load_json", lambda p: None)
        assert orchestrator._loop_liveness_alert_lines(self._orch(step="done")) == []
        assert orchestrator._loop_liveness_alert_lines(self._orch(step="stopped")) == []

    def test_sniff_loop_command_shared_shape(self):
        import orchestrator
        cmd = orchestrator._sniff_loop_command("sess-x", "/tmp/repo", 240)
        assert cmd.startswith("/loop 240s 跑 `")
        assert "sniff" in cmd and "--session sess-x" in cmd
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /Users/apple/works/claude-skills && python3 -m pytest tests/fastship/test_orchestrator.py::TestLoopLivenessAlert -q`
Expected: FAIL（`AttributeError: module 'orchestrator' has no attribute '_loop_liveness_alert_lines'`）

- [ ] **Step 3: 实现 — 抽 `_sniff_loop_command` + 加 alert helper**

(a) 把 `_print_sniff_hint`(3564)里命令构造抽成共享函数。新增于 `_print_sniff_hint` 之前:

```python
def _sniff_loop_command(session_id: str, state_repo: str, interval: int = None) -> str:
    """`/loop <interval>s 跑 \`<sniff_cmd>\`` 单行。单源:start hint(_print_sniff_hint)
    与驱动活动存活告警(_loop_liveness_alert_lines)共用，避免命令构造漂移。"""
    if interval is None:
        interval = _sniff_interval_s()
    env_bits = []
    if os.environ.get("FASTSHIP_STATE_HOME"):
        env_bits.append(f"FASTSHIP_STATE_HOME={shlex.quote(os.environ['FASTSHIP_STATE_HOME'])}")
    env_bits.append(f"FASTSHIP_SESSION={shlex.quote(session_id)}")
    sniff_cmd = (f"cd {shlex.quote(state_repo)} && " + " ".join(env_bits)
                 + f" python3 {shlex.quote(os.path.abspath(__file__))} sniff"
                 + f" --session {shlex.quote(session_id)}")
    return f"/loop {interval}s 跑 `{sniff_cmd}`"
```

(b) 把 `_print_sniff_hint` 内部改为复用它(替换 3568-3579 的 interval/env_bits/sniff_cmd 构造 + 那行 print）:

```python
def _print_sniff_hint(session_id: str, state_repo: str):
    """start 后输出可复制的 /loop 嗅探命令（AC-START-1）。"""
    loop_cmd = _sniff_loop_command(session_id, state_repo)
    print("🕵️  嗅探 loop（后台存活监控）— Claude Code 下驱动 agent 应立即后台自动启动；CLI 手动粘贴：")
    print(f"   {loop_cmd} 并解析 [FASTSHIP_SNIFF] 行：")
    print("      action=resume → 向 fastship 驱动 session 注入一次「继续当前 step」软唤醒（绝不 kill 进程）；")
    print("      action=notify_user → 立即用最醒目可用通道通知用户，原样附上整行证据；")
    print("      verdict=session_done / no_session → 停止本 loop。判定纯本地零 LLM。\n")
```

(c) 新增 alert helper(放在 `_sniff_status_lines` 附近,约 3829 之后):

```python
def _loop_liveness_alert_lines(orch) -> list:
    """洞1:驱动每次活动(next/done/hook 推进)附带的存活告警。loop 未起/超龄 → 返回
    [告警, 重启命令];心跳新鲜或流程终态 → []（保持安静,不污染正常输出）。纯只读。"""
    step = (orch or {}).get("current_step")
    if step in ("done", "stopped", None):
        return []
    sid = (orch or {}).get("session_id") or fastship_state.current_session_id()
    if not sid:
        return []
    data = fastship_state.load_json(fastship_state.sniff_state_path(sid))
    interval = _sniff_interval_s()
    state_repo = ((orch.get("worktree") or {}).get("path")
                  or orch.get("repo_root") or _repo_root())
    restart = "   重启嗅探 loop: " + _sniff_loop_command(sid, state_repo, interval)
    if not data or not data.get("last_check_at"):
        return ["🕵️  嗅探 loop 未运行 — 全流程无存活监控,重启:", restart]
    age = _iso_age_s(datetime.now(), data["last_check_at"])
    if age > 2 * interval:
        return [f"⚠️  watchdog stale: 嗅探最后心跳 {age}s 前（>2×{interval}s）,loop 可能已死,重启:",
                restart]
    return []
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /Users/apple/works/claude-skills && python3 -m pytest tests/fastship/test_orchestrator.py::TestLoopLivenessAlert -q`
Expected: PASS（5 passed）

- [ ] **Step 5: 跑既有 sniff/status 测试确认无回归**

Run: `cd /Users/apple/works/claude-skills && python3 -m pytest tests/fastship/test_orchestrator.py -q -k "Sniff or sniff or status or Status or hint"`
Expected: PASS（既有 `_print_sniff_hint` 行为不变）

- [ ] **Step 6: Commit**

```bash
cd /Users/apple/works/claude-skills && git add skills/fastship/orchestrator.py tests/fastship/test_orchestrator.py && git commit -m "feat(fastship): loop liveness alert helper + DRY sniff-loop command (洞1)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: 洞1 — 把告警接到驱动活动(next/done/hook)

**Files:**
- Modify: `skills/fastship/orchestrator.py` — `cmd_next`(3651)、`cmd_done`(3818 advance 分支)、`hook_post_bash_logic`(2954 前)、`hook_post_edit_logic`(末尾)
- Test: `tests/fastship/test_orchestrator.py`

- [ ] **Step 1: 写失败测试(capsys 验接线)**

追加:

```python
class TestLoopLivenessWired:
    """洞1 接线:loop 死时 cmd_next / hook_post_bash 的输出含告警;新鲜时不含。"""

    def test_cmd_next_surfaces_dead_loop(self, monkeypatch, capsys):
        import orchestrator
        monkeypatch.setattr(orchestrator, "load_orch_state",
                            lambda *a, **k: {"current_step": "1.5c", "phase": 1,
                                             "session_id": "s", "repo_root": "/tmp/r"})
        monkeypatch.setattr(orchestrator, "format_next", lambda st: "NEXT-STEP-TEXT")
        monkeypatch.setattr(orchestrator.fastship_state, "load_json", lambda p: None)
        monkeypatch.setattr(orchestrator, "_repo_root", lambda: "/tmp/r")
        rc = orchestrator.cmd_next()
        out = capsys.readouterr().out
        assert rc == 0
        assert "NEXT-STEP-TEXT" in out
        assert "嗅探 loop 未运行" in out and "/loop" in out

    def test_cmd_next_quiet_when_loop_fresh(self, monkeypatch, capsys):
        import orchestrator
        fresh = datetime.now().isoformat()
        monkeypatch.setattr(orchestrator, "load_orch_state",
                            lambda *a, **k: {"current_step": "1.5c", "phase": 1,
                                             "session_id": "s", "repo_root": "/tmp/r"})
        monkeypatch.setattr(orchestrator, "format_next", lambda st: "NEXT")
        monkeypatch.setattr(orchestrator.fastship_state, "load_json",
                            lambda p: {"last_check_at": fresh})
        monkeypatch.setattr(orchestrator, "_repo_root", lambda: "/tmp/r")
        orchestrator.cmd_next()
        out = capsys.readouterr().out
        assert "未运行" not in out and "watchdog stale" not in out
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /Users/apple/works/claude-skills && python3 -m pytest tests/fastship/test_orchestrator.py::TestLoopLivenessWired -q`
Expected: FAIL（`test_cmd_next_surfaces_dead_loop`:输出无 "嗅探 loop 未运行"）

- [ ] **Step 3: 接线四处**

(a) `cmd_next`(3651-3657)改为:

```python
def cmd_next() -> int:
    st = load_orch_state()
    if not st:
        print("❌ 没有活跃 session。先 start。")
        return 1
    print(format_next(st))
    for line in _loop_liveness_alert_lines(st):
        print(line)
    return 0
```

(b) `cmd_done` 主 advance 分支(3816-3818 `if next_step:` 块,在 `print(format_next(st))` 之后)追加:

```python
    if next_step:
        print()
        print(format_next(st))
        for line in _loop_liveness_alert_lines(st):
            print(line)
        if step.id == "1.6" and st.get("current_step") == "2.0":
            _print_goal_hint(st)
```

(c) `hook_post_bash_logic`(2954 `return 0` 之前)追加:

```python
    for line in _loop_liveness_alert_lines(orch):
        print(line)
    return 0
```

(d) `hook_post_edit_logic`(2957)用的变量名就是 `orch`(2961 载入、2962 active 守卫,已核实)。在该函数**末尾的终态 `return 0`** 之前追加(不是 validation-fail 早退分支里的 return,是函数最后那个):

```python
    for line in _loop_liveness_alert_lines(orch):
        print(line)
    return 0
```

> 注:alert helper 在心跳新鲜/终态时返回 [],四处接线在正常情况都安静,只有 loop 真死/未起才出声。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /Users/apple/works/claude-skills && python3 -m pytest tests/fastship/test_orchestrator.py::TestLoopLivenessWired -q`
Expected: PASS（2 passed）

- [ ] **Step 5: 跑既有 hook/CLI 测试确认无回归**

Run: `cd /Users/apple/works/claude-skills && python3 -m pytest tests/fastship/test_orchestrator.py -q -k "Hook or CLI or Detection or IntegrationFullFlow"`
Expected: PASS（既有 next/done/hook 推进行为不变）

- [ ] **Step 6: Commit**

```bash
cd /Users/apple/works/claude-skills && git add skills/fastship/orchestrator.py tests/fastship/test_orchestrator.py && git commit -m "feat(fastship): surface loop liveness alert on next/done/hook activity (洞1)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: SKILL.md 软处方(洞0 CLI 兜底 + 洞1 说明)

**Files:**
- Modify: `skills/fastship/SKILL.md`(「嗅探 loop」段 ≈L22-43;新增/补「Codex Review」处方)
- Test: `tests/fastship/test_orchestrator.py`(grep 断言)

- [ ] **Step 1: 写失败测试**

追加:

```python
class TestSkillSoftPrescription:
    """洞0 软处方 + 洞1 说明必须落进 SKILL.md（CLI 模式无 hook,靠文案约束）。"""

    def _skill_text(self):
        p = os.path.join(os.path.dirname(__file__), '..', '..',
                         'skills', 'fastship', 'SKILL.md')
        with open(p, encoding='utf-8') as f:
            return f.read()

    def test_codex_bounded_prescription_present(self):
        t = self._skill_text()
        assert "< /dev/null" in t and "timeout" in t
        assert "codex" in t

    def test_forbids_raw_background_codex(self):
        t = self._skill_text()
        assert "禁止" in t and "codex" in t  # 明文禁裸起背景 codex

    def test_loop_self_check_note_present(self):
        t = self._skill_text()
        assert "存活自检" in t or "存活检查" in t
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd /Users/apple/works/claude-skills && python3 -m pytest tests/fastship/test_orchestrator.py::TestSkillSoftPrescription -q`
Expected: FAIL（SKILL.md 暂无这些字样）

- [ ] **Step 3: 改 SKILL.md**

在 SKILL.md「双模工作方式 → Codex / 其他 Agent（CLI 模式）」段附近(或 19 步表 1.5c 行下方)新增一小节:

```markdown
### 🔴 启动 codex 评审的唯一安全方式（洞0）

1.5c Codex Review 必须以**有界形式**启动,禁止裸起背景 codex(背景 codex 阻塞在
"Reading additional input from stdin" 会永不退出 → 无完成事件 → harness 永不唤醒 →
流程静坐)。canonical 形式(timeout 包裹 + stdin 接 /dev/null):

    timeout 330 codex exec -s read-only "<prompt>" -c 'model_reasoning_effort="high"' < /dev/null 2>/tmp/codex.err

或直接走 `/codex review` 安全路径(已自带 timeout+stdin 重定向)。Claude Code hook 模式下
pre_bash 会硬拦裸起 codex;CLI/Codex 模式无 hook,须靠本处方自律。
```

在「嗅探 loop」段末尾(L43 之后)补一句:

```markdown

🔴 **loop 存活自检（洞1）**：驱动每次 `next` / `done` / hook 推进时,orchestrator 会自动
检查嗅探 loop 心跳——未运行或超龄(>2×interval)即在输出末尾打印告警 + 可复制的 /loop
重启命令。配合洞0(子进程必有界 → 驱动必周期性活动)形成闭环:loop 死掉会在下一次驱动
活动时被发现并提示重启,不再依赖人工去看 `fastship status`。
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd /Users/apple/works/claude-skills && python3 -m pytest tests/fastship/test_orchestrator.py::TestSkillSoftPrescription -q`
Expected: PASS（3 passed）

- [ ] **Step 5: Commit**

```bash
cd /Users/apple/works/claude-skills && git add skills/fastship/SKILL.md tests/fastship/test_orchestrator.py && git commit -m "docs(fastship): SKILL bounded-codex prescription + loop self-check note (洞0/洞1)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: 全量回归 + 收尾

**Files:** 无(仅验证)

- [ ] **Step 1: 跑 fastship 全量测试**

Run: `cd /Users/apple/works/claude-skills && python3 -m pytest tests/fastship/ -q`
Expected: PASS（全绿;新增 21 条 + 既有全部通过,无回归）

- [ ] **Step 2: 静态自检 orchestrator 可导入**

Run: `cd /Users/apple/works/claude-skills && python3 -c "import sys; sys.path.insert(0,'skills/fastship'); import orchestrator; print('import OK')"`
Expected: `import OK`

- [ ] **Step 3: 端到端冒烟 — 裸 codex 真被拦(可选,非阻断)**

构造一个 active orch_state,手动调 pre_bash 验证返回 1。若 Task 2 测试已绿可跳过。

- [ ] **Step 4: 最终确认无遗留**

Run: `cd /Users/apple/works/claude-skills && git log --oneline -6 && git status --short`
Expected: 5 个 feat/docs commit + 工作区干净

---

## 验收映射(spec §6 AC → task)

- AC-0-1/0-2/0-3/0-4 → Task 1(predicate)+ Task 2(接线)
- AC-0-5 → Task 5(SKILL grep)
- AC-1-1/1-2/1-3/1-4 → Task 3(helper)+ Task 4(接线 capsys)
- 回归 → Task 3.5 / Task 4.5 / Task 6.1

## 明确不做(同 spec §5)

- 洞2 豁免外层上限(被洞0 取代)
- 拦 codex 以外的背景子进程(窄门禁)
- daemon / 进程表扫描 / mtime 轮询
- 改 codex 评审内容契约 / JSON gate / 回退逻辑
