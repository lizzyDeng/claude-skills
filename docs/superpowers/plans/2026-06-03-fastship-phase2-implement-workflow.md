# Fastship Phase 2 Implement-as-Dynamic-Workflow + Multi-Session Hook Isolation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Turn fastship's Phase 2 implement step (2.0) into a dependency-aware dynamic workflow where Claude reads the confirmed plan and autonomously fans out implement subagents (file-disjoint groups in parallel, dependent chains sequential), and make fastship's hook/state layer multi-session safe so parallel work can never corrupt or mis-advance another session's state.

**Architecture:** Two layers.

**(A) Isolation/safety layer** in `fastship_state.py`, the hook entry points, and the gate. It rests on three *proven* facts (frozen by Task 1's characterization tests, not assumed): (1) `state_home` is per-worktree (`{git-dir}/fastship`), so different git worktrees are fully isolated; (2) at step 2.0 an edit to a **code file** is a state-no-op in **both** the orchestrator (`detect_completion_post_edit` has no 2.0 case) and the gate (`gate_post_edit` only writes for plan/KNOWLEDGE files); (3) the gate delegates all state IO to `fastship_state` (`save_json`, `set_current_session_id`), so hardening `fastship_state` automatically covers the gate subprocess. On top of those facts we add: atomic state writes (temp+rename), a reentrant cross-process `state_lock()` wrapping every registry/gate read-modify-write, an "ambiguous session" guard that makes hooks fail safe when ≥2 sessions are active in one state-home and none is pinned (post hooks refuse to auto-advance; pre hooks fail *open* — skip session-specific blocks, keep the session-independent state-file-write block), and a `start` that refuses to create a second concurrent session in one worktree unless `--shared`/`--session` is given.

**(B) Contract layer** in Step 2.0's instruction. Implement subagents run in the **shared session worktree** (no per-agent worktrees, no merge-back): Claude partitions the plan's tasks into file-disjoint groups (run in parallel via a dynamic Workflow, **edit-only** — agents do not `git commit`, the main thread commits per group) vs dependent chains (run sequentially in-place). Parallel implement agents may only edit + compile-check (`cargo check`/`tsc`); running the project test suite or E2E during implement is forbidden (those are steps 3.1/3.2, main-thread, serial) — this keeps the gate's bash-triggered state writes from ever racing. Per-task adversarial review verdicts accumulate to a **session-scoped** ledger (`{git-dir}/fastship/sessions/<sid>/implement-verdicts.md`) that feeds the existing Step 2.5 code-review gate. A Workflow is only spun up when there are ≥2 file-disjoint groups; otherwise the step degrades to sequential subagent-driven implementation.

**Why this is safe across every harness behavior:** Whether or not Claude Code fires PostToolUse hooks for Workflow-subagent tool calls, and whatever cwd it uses, the design holds. At step 2.0 a code-file edit writes no state in either the orchestrator or the gate (fact 2). Implement agents write code files (no artifact owner), so the pre_edit out-of-order/phase blocks don't fire. The lock + atomic writes + ambiguity guard close the residual races for any step that *does* write state and for the deliberate `--shared` multi-session mode.

**Tech Stack:** Python 3.11 (stdlib `fcntl`, `threading`, `contextlib`, `tempfile`, `os.replace`), pytest (`tmp_path` + `monkeypatch`), git worktrees. Target repo: `claude-skills` (the fastship source). Files under `skills/fastship/`, `skills/fastship/hooks/`, `tests/fastship/`.

**Decisions locked in during grilling (2026-06-03):**
1. **Implement isolation unit** = the shared session worktree; parallel agents edit file-disjoint groups, **do not commit** (main thread commits per group); per-agent worktrees + merge-back dropped.
2. **Gate concurrency** closed two ways: contract forbids parallel test/E2E during 2.0 (edit + compile-check only); gate RMW also wrapped in `state_lock`.
3. **`orch_path is None`** is the correct production discriminator for the post-hook guard (verified: production calls `hook_post_*_logic(data)` with no path; existing tests pass an explicit path).
4. **Pre-hook guard** added with **fail-open** (skip session-specific blocks under ambiguity, keep state-file-write block, placed before the branch-mismatch check).
5. **`start`** refuses a bare second concurrent session in one worktree; requires `--shared` or `--session`.
6. **Verdict ledger** is persisted and **session-directory-scoped**, not a hard gate; feeds 2.5.
7. **Workflow opt-in** is instruction-level (valid per the tool's own rules: a skill instruction directing a Workflow call), conditional on ≥2 disjoint groups, with sequential fallback.

**Out of scope (do NOT do here):**
- Deploying into the *installed* aifriends copy. `aifriends/.claude/tools/fastship` is a bidirectional fork (memory `project_forge_aifriends_divergence`); deployment is a separate surgical port, never `cp`.
- Adding a new hard gate on Step 2.0. `validate_execute` stays `True, "sequencing"`; real gates remain 2.5 (code review) and 3.0 (smoke).
- **Session-scoping the existing `.claude/.fastship-*.md` artifacts** (brief / grill / codex-review / code-review). They are per-worktree but not per-session, so they collide only under deliberate `--shared`. Fixing them touches every validator + hook filename match — tracked as a follow-up below, NOT done here. The *new* verdict ledger is session-scoped from the start.

**Follow-up (file an issue, do not implement here):** session-scope the existing `.claude/.fastship-*.md` artifacts (or relocate them under `sessions/<sid>/`) so `--shared` mode is fully isolated.

---

## File Structure

| File | Responsibility | Change |
|------|----------------|--------|
| `skills/fastship/fastship_state.py` | State location, session registry, JSON IO | Atomic `save_json`; reentrant `state_lock()`; wrap `set_current_session_id`/`unregister_session` RMW in the lock; `active_session_ids()`; `implement_verdicts_path()` |
| `skills/fastship/hooks/ship_verify_gate.py` | Verify gate subprocess | Wrap `gate_post_edit`/`gate_post_bash` RMW in `fastship_state.state_lock()` |
| `skills/fastship/orchestrator.py` | Hook entry + step machine | `_hook_session_ambiguous()`; post-hook no-advance guard; pre-hook fail-open guard; `cmd_start` refuses 2nd concurrent session unless `--shared`/`--session`; rewrite Step 2.0 `instruction` |
| `skills/fastship/SKILL.md` | Skill doc | Document the shared-worktree edit-only implement contract, the one-session-per-worktree invariant + `--shared`, the session-scoped verdict ledger |
| `tests/fastship/test_orchestrator.py` | Test suite (pytest) | New test classes for every change |

**Test run command (from repo root) used throughout:**
```bash
cd /Users/apple/works/claude-skills && python3 -m pytest tests/fastship/test_orchestrator.py -v
```

---

## Task 1: Characterize the two safety premises

Freeze the facts the isolation design rests on, with deterministic tests that need **no** real Workflow run.

**Files:**
- Test: `tests/fastship/test_orchestrator.py` (new classes `TestWorktreeStateIsolation`, `TestStep20StateNoop`)
- Reference (no change): `skills/fastship/fastship_state.py:102` (`state_home`); `skills/fastship/orchestrator.py:1247` (`detect_completion_post_edit`); `skills/fastship/hooks/ship_verify_gate.py:656` (`gate_post_edit`)

- [ ] **Step 1: Add the worktree-isolation characterization test**

Append to `tests/fastship/test_orchestrator.py`:

```python
class TestWorktreeStateIsolation:
    """Premise 1: state_home is per-worktree — different worktrees are isolated."""

    def _git(self, *args, cwd):
        subprocess.run(["git", "-C", str(cwd), *args],
                       check=True, capture_output=True, text=True)

    def test_worktree_resolves_to_separate_state_home(self, tmp_path, monkeypatch):
        import fastship_state

        monkeypatch.delenv("FASTSHIP_STATE_HOME", raising=False)
        monkeypatch.delenv("FASTSHIP_REPO_ROOT", raising=False)
        monkeypatch.delenv("FASTSHIP_SESSION", raising=False)

        main = tmp_path / "main"
        main.mkdir()
        self._git("init", "-q", cwd=main)
        self._git("config", "user.email", "t@t.io", cwd=main)
        self._git("config", "user.name", "t", cwd=main)
        (main / "README.md").write_text("x")
        self._git("add", "-A", cwd=main)
        self._git("commit", "-qm", "init", cwd=main)

        wt = tmp_path / "wt"
        self._git("worktree", "add", "-q", str(wt), "-b", "feat", cwd=main)

        monkeypatch.chdir(main)
        home_main = fastship_state.state_home()
        monkeypatch.chdir(wt)
        home_wt = fastship_state.state_home()

        assert home_wt != home_main
        assert "worktrees" in home_wt
```

- [ ] **Step 2: Add the 2.0 state-no-op characterization test**

Append to `tests/fastship/test_orchestrator.py`:

```python
class TestStep20StateNoop:
    """Premise 2: at step 2.0 a code-file edit writes no state in EITHER the
    orchestrator or the gate. This is what makes same-worktree parallel
    implement safe."""

    def test_orchestrator_post_edit_noop_for_code_at_2_0(self):
        from orchestrator import detect_completion_post_edit
        data = {"tool_input": {"file_path": "services/api/src/handlers/chat.rs"}}
        assert detect_completion_post_edit("2.0", data) is None

    def test_gate_does_not_treat_code_file_as_artifact(self):
        import sys, os
        sys.path.insert(0, os.path.join(
            os.path.dirname(__file__), "..", "..", "skills", "fastship", "hooks"))
        import ship_verify_gate as gate
        code = "services/api/src/handlers/chat.rs"
        assert gate.is_plan_file(code) is False
        assert gate.is_knowledge_file(code) is False
```

- [ ] **Step 3: Run both — expected PASS on current code (characterization)**

Run: `cd /Users/apple/works/claude-skills && python3 -m pytest tests/fastship/test_orchestrator.py::TestWorktreeStateIsolation tests/fastship/test_orchestrator.py::TestStep20StateNoop -v`
Expected: **PASS** on current code. If either FAILS, STOP — a core premise is wrong and the plan must be revisited.

- [ ] **Step 4: Commit**

```bash
cd /Users/apple/works/claude-skills
git add tests/fastship/test_orchestrator.py
git commit -m "test(fastship): freeze worktree isolation + 2.0 state-no-op premises"
```

---

## Task 2: Atomic `save_json` (covers the gate too)

`save_json` truncates-then-writes, risking torn reads. Make it temp-file + `os.replace`. Because the gate's `save_state` calls `fastship_state.save_json` (`ship_verify_gate.py:156`), this fix covers gate writes automatically.

**Files:**
- Modify: `skills/fastship/fastship_state.py:344-347` (`save_json`); imports near line 25
- Test: `tests/fastship/test_orchestrator.py` (new class `TestAtomicSaveJson`)

- [ ] **Step 1: Write the failing test**

```python
class TestAtomicSaveJson:
    def test_save_json_no_leftover_temp_files(self, tmp_path):
        import fastship_state
        target = tmp_path / "state.json"
        fastship_state.save_json(str(target), {"n": 1})
        leftovers = [p.name for p in tmp_path.iterdir() if p.name != "state.json"]
        assert leftovers == [], f"unexpected leftover files: {leftovers}"

    def test_save_json_uses_atomic_replace(self, tmp_path, monkeypatch):
        import fastship_state
        calls = []
        real_replace = os.replace
        monkeypatch.setattr(os, "replace",
                            lambda a, b: calls.append((a, b)) or real_replace(a, b))
        target = tmp_path / "s.json"
        fastship_state.save_json(str(target), {"k": "v"})
        assert calls, "save_json must use os.replace for atomicity"
        assert calls[0][1].endswith("s.json")
        assert json.loads(target.read_text())["k"] == "v"
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /Users/apple/works/claude-skills && python3 -m pytest tests/fastship/test_orchestrator.py::TestAtomicSaveJson -v`
Expected: `test_save_json_uses_atomic_replace` FAILS (`AssertionError: save_json must use os.replace`).

- [ ] **Step 3: Implement atomic write**

Add `import tempfile` to the imports block of `skills/fastship/fastship_state.py` (after `import shutil`, line 25). Replace `save_json` (lines 344-347):

```python
def save_json(path: str, data: dict) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd /Users/apple/works/claude-skills && python3 -m pytest tests/fastship/test_orchestrator.py::TestAtomicSaveJson -v`
Expected: PASS.

- [ ] **Step 5: Full suite + commit**

```bash
cd /Users/apple/works/claude-skills
python3 -m pytest tests/fastship/test_orchestrator.py -q
git add skills/fastship/fastship_state.py tests/fastship/test_orchestrator.py
git commit -m "fix(fastship): atomic save_json via temp+os.replace (covers gate)"
```

---

## Task 3: Reentrant cross-process `state_lock()`

Exclusive across processes (`fcntl.flock` on `{state_home}/.lock`), reentrant within a thread (depth counter) so nested locked sections never self-deadlock.

**Files:**
- Modify: `skills/fastship/fastship_state.py` (imports + new function above `save_json`)
- Test: `tests/fastship/test_orchestrator.py` (new class `TestStateLock`)

- [ ] **Step 1: Write the failing test**

```python
class TestStateLock:
    def test_lock_serializes_concurrent_increments(self, tmp_path, monkeypatch):
        import threading
        import fastship_state
        monkeypatch.setenv("FASTSHIP_STATE_HOME", str(tmp_path))
        counter = tmp_path / "counter.json"
        counter.write_text(json.dumps({"n": 0}))

        def bump():
            for _ in range(50):
                with fastship_state.state_lock():
                    d = json.loads(counter.read_text())
                    d["n"] += 1
                    counter.write_text(json.dumps(d))

        threads = [threading.Thread(target=bump) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert json.loads(counter.read_text())["n"] == 200

    def test_lock_is_reentrant_within_thread(self, tmp_path, monkeypatch):
        import fastship_state
        monkeypatch.setenv("FASTSHIP_STATE_HOME", str(tmp_path))
        with fastship_state.state_lock():
            with fastship_state.state_lock():
                assert True
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /Users/apple/works/claude-skills && python3 -m pytest tests/fastship/test_orchestrator.py::TestStateLock -v`
Expected: FAIL (`module 'fastship_state' has no attribute 'state_lock'`).

- [ ] **Step 3: Implement the lock**

Add `import fcntl`, `import threading`, `import contextlib` to the imports block (after `import re`, line 24). Add above `def save_json`:

```python
_LOCAL = threading.local()


@contextlib.contextmanager
def state_lock():
    """Exclusive across processes (fcntl.flock on {state_home}/.lock), reentrant
    within a thread. Wrap registry/gate read-modify-write in this."""
    depth = getattr(_LOCAL, "depth", 0)
    if depth > 0:
        _LOCAL.depth = depth + 1
        try:
            yield
        finally:
            _LOCAL.depth -= 1
        return

    home = ensure_state_home()
    f = open(os.path.join(home, ".lock"), "w")
    fcntl.flock(f, fcntl.LOCK_EX)
    _LOCAL.depth = 1
    _LOCAL.fd = f
    try:
        yield
    finally:
        _LOCAL.depth = 0
        try:
            fcntl.flock(f, fcntl.LOCK_UN)
        finally:
            f.close()
            _LOCAL.fd = None
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd /Users/apple/works/claude-skills && python3 -m pytest tests/fastship/test_orchestrator.py::TestStateLock -v`
Expected: PASS (both). The increment test reliably fails *without* the lock (n<200).

- [ ] **Step 5: Commit**

```bash
cd /Users/apple/works/claude-skills
git add skills/fastship/fastship_state.py tests/fastship/test_orchestrator.py
git commit -m "feat(fastship): reentrant cross-process state_lock()"
```

---

## Task 4: Lock the registry read-modify-write

`set_current_session_id` does `load_registry()` → mutate → `save_registry()` unlocked, so concurrent updates drop sessions. Wrap in `state_lock()`. The gate's `save_state` calls `set_current_session_id`, so this covers the gate's registry RMW cross-process too.

**Files:**
- Modify: `skills/fastship/fastship_state.py:203-238` (`set_current_session_id`, `unregister_session`)
- Test: `tests/fastship/test_orchestrator.py` (new class `TestRegistryConcurrency`)

- [ ] **Step 1: Write the failing test**

```python
class TestRegistryConcurrency:
    def test_concurrent_session_registration_keeps_all(self, tmp_path, monkeypatch):
        import threading
        import fastship_state
        monkeypatch.setenv("FASTSHIP_STATE_HOME", str(tmp_path))
        ids = [f"sess-{i:03d}" for i in range(20)]

        def register(sid):
            fastship_state.set_current_session_id(
                sid, f"req {sid}", {"current_step": "1.0"})

        threads = [threading.Thread(target=register, args=(sid,)) for sid in ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        sessions = fastship_state.list_sessions()
        assert set(sessions) == set(ids), f"lost: {set(ids) - set(sessions)}"
```

- [ ] **Step 2: Run to verify it fails (race)**

Run: `cd /Users/apple/works/claude-skills && python3 -m pytest tests/fastship/test_orchestrator.py::TestRegistryConcurrency -v`
Expected: FAIL most runs with `lost: {...}`.

- [ ] **Step 3: Wrap the RMW**

In `set_current_session_id`, wrap lines 205-225 (`registry = load_registry()` … `save_registry(registry)`) in `with state_lock():`:

```python
def set_current_session_id(session_id: str, requirement: str = None, state: dict = None) -> str:
    sid = normalize_session_id(session_id) or DEFAULT_SESSION_ID
    with state_lock():
        registry = load_registry()
        sessions = registry.setdefault("sessions", {})
        rec = dict(sessions.get(sid) or {})
        rec.update({
            "id": sid,
            "updated_at": _now_iso(),
            "repo_root": repo_root(),
        })
        if requirement:
            rec["requirement"] = requirement
        if state:
            rec["current_step"] = state.get("current_step")
            rec["phase"] = state.get("phase")
            rec["branch"] = state.get("branch")
            rec["status"] = _status_from_state(state)
            if state.get("requirement"):
                rec["requirement"] = state.get("requirement")
        rec.setdefault("created_at", rec["updated_at"])
        sessions[sid] = rec
        registry["current_session"] = sid
        save_registry(registry)
    return sid
```

And `unregister_session` (lines 232-238):

```python
def unregister_session(session_id: str) -> None:
    sid = normalize_session_id(session_id)
    if not sid:
        return
    with state_lock():
        registry = load_registry()
        registry.get("sessions", {}).pop(sid, None)
        if registry.get("current_session") == sid:
            remaining = sorted(registry.get("sessions", {}).keys())
            registry["current_session"] = remaining[0] if len(remaining) == 1 else None
        save_registry(registry)
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd /Users/apple/works/claude-skills && python3 -m pytest tests/fastship/test_orchestrator.py::TestRegistryConcurrency -v`
Expected: PASS deterministically.

- [ ] **Step 5: Full suite + commit**

```bash
cd /Users/apple/works/claude-skills
python3 -m pytest tests/fastship/test_orchestrator.py -q
git add skills/fastship/fastship_state.py tests/fastship/test_orchestrator.py
git commit -m "fix(fastship): lock registry RMW (covers gate set_current_session_id)"
```

---

## Task 5: Harden the gate's state RMW with the lock

Defense-in-depth: wrap the `load_state → mutate → save_state` regions of `gate_post_edit` and `gate_post_bash` in `fastship_state.state_lock()` so the gate's per-session `gate.json` RMW can't lose updates under concurrency. (Already unreachable during 2.0 by contract — Task 9 forbids parallel test/E2E — but this makes `gate.json` safe under any future concurrency.)

**Files:**
- Modify: `skills/fastship/hooks/ship_verify_gate.py:656-685` (`gate_post_edit`), `:1190+` (`gate_post_bash`)
- Test: `tests/fastship/test_orchestrator.py` (new class `TestGateStateLocking`)

- [ ] **Step 1: Confirm the gate imports `fastship_state`**

Run: `cd /Users/apple/works/claude-skills && grep -n "import fastship_state\|^import\|^from" skills/fastship/hooks/ship_verify_gate.py | head -20`
Expected: a line importing `fastship_state` (the gate already calls `fastship_state.save_json`/`set_current_session_id`). Note how it's imported for Step 3.

- [ ] **Step 2: Write the failing concurrency test**

```python
class TestGateStateLocking:
    def _import_gate(self):
        import sys, os
        sys.path.insert(0, os.path.join(
            os.path.dirname(__file__), "..", "..", "skills", "fastship", "hooks"))
        import ship_verify_gate as gate
        return gate

    def test_gate_post_edit_rmw_serialized(self, tmp_path, monkeypatch):
        import threading
        import fastship_state
        gate = self._import_gate()
        monkeypatch.setenv("FASTSHIP_STATE_HOME", str(tmp_path))
        monkeypatch.setattr(gate, "get_current_branch", lambda: "main")
        monkeypatch.setattr(gate, "require_branch_match", lambda st, br: True)
        monkeypatch.setattr(gate, "is_plan_file", lambda p: p.endswith("plan.md"))
        monkeypatch.setattr(gate, "is_knowledge_file",
                            lambda p: os.path.basename(p).upper() == "KNOWLEDGE.MD")

        tl = threading.local()
        monkeypatch.setattr(gate, "read_stdin", lambda: getattr(tl, "data", {}))

        def worker(file_path):
            tl.data = {"tool_input": {"file_path": file_path}}
            gate.gate_post_edit()

        threads = [
            threading.Thread(target=worker, args=("docs/plan.md",)),
            threading.Thread(target=worker, args=("KNOWLEDGE.md",)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        st = gate.ensure_branch_state(gate.load_state(), "main")
        # Both concurrent writers' fields survive — no lost update.
        assert st.get("plan_ready") is True
        assert st.get("knowledge_acknowledged") is True
```

- [ ] **Step 3: Run to verify it fails (or is flaky) without the lock**

Run: `cd /Users/apple/works/claude-skills && python3 -m pytest tests/fastship/test_orchestrator.py::TestGateStateLocking -v`
Expected: FAILS (one field lost) at least intermittently, because the two `load_state → save_state` RMWs interleave. If the import path needs adjusting, fix it per Step 1's output before proceeding.

- [ ] **Step 4: Wrap the gate RMW in the lock**

In `skills/fastship/hooks/ship_verify_gate.py`, add to the imports (it already imports `fastship_state`): ensure `import fastship_state` is present (from Step 1). Then wrap `gate_post_edit` (lines 656-685) so the load→mutate→save is inside the lock:

```python
def gate_post_edit():
    """PostToolUse: Edit/Write — 检测 plan 文件 / KNOWLEDGE.md 写入"""
    data = read_stdin()
    file_path = data.get("tool_input", {}).get("file_path", "")
    branch = get_current_branch()
    now = datetime.now().isoformat()

    with fastship_state.state_lock():
        st = ensure_branch_state(load_state(), branch)
        changed = False

        if is_plan_file(file_path):
            st["plan_ready"] = True
            st["plan_file"] = normalize_path(file_path)
            st["plan_ts"] = now
            changed = True
            print(f"✅ Gate: 检测到 plan 文件已写入，plan_ready=true ({file_path})")

        if is_knowledge_file(file_path):
            st["knowledge_acknowledged"] = True
            st["knowledge_file"] = normalize_path(file_path)
            st["knowledge_ts"] = now
            st["knowledge_skip_reason"] = None
            changed = True
            print(f"✅ Gate: 检测到 KNOWLEDGE.md 更新，knowledge_acknowledged=true ({file_path})")

        if changed:
            if not require_branch_match(st, branch):
                return 0
            save_state(st)
    return 0
```

For `gate_post_bash` (starting line 1190): wrap from `st = ensure_branch_state(load_state(), branch)` (line 1198) through the function's final `save_state(...)` in `with fastship_state.state_lock():` (indent the existing body). Keep the early `branch_mismatch` return *inside* the lock (it only reads). Do not change any detection logic — this is a pure `with`-wrap + reindent.

- [ ] **Step 5: Run to verify it passes**

Run: `cd /Users/apple/works/claude-skills && python3 -m pytest tests/fastship/test_orchestrator.py::TestGateStateLocking -v`
Expected: PASS.

- [ ] **Step 6: Sanity-check the gate still runs as a script**

Run: `cd /Users/apple/works/claude-skills && echo '{}' | python3 skills/fastship/hooks/ship_verify_gate.py status; echo "exit=$?"`
Expected: runs without import/syntax error.

- [ ] **Step 7: Commit**

```bash
cd /Users/apple/works/claude-skills
git add skills/fastship/hooks/ship_verify_gate.py tests/fastship/test_orchestrator.py
git commit -m "fix(fastship): wrap gate post_edit/post_bash RMW in state_lock"
```

---

## Task 6: Ambiguous-session guard — post hooks no-advance, pre hooks fail-open

When ≥2 sessions are active in one state-home and none is pinned via `FASTSHIP_SESSION`, the hook can't attribute the edit. Post hooks must refuse to auto-advance; pre hooks must fail *open* (skip session-specific blocks, keep the session-independent state-file-write block) so they don't wrongly halt legitimate work.

**Files:**
- Add: `skills/fastship/fastship_state.py` (`active_session_ids()`)
- Add: `skills/fastship/orchestrator.py` (`_hook_session_ambiguous()`)
- Modify: `skills/fastship/orchestrator.py` — guard in `hook_post_bash_logic`, `hook_post_edit_logic`, `hook_pre_edit_logic`, `hook_pre_bash_logic`
- Test: `tests/fastship/test_orchestrator.py` (new class `TestAmbiguousSessionGuard`)

- [ ] **Step 1: Write the failing tests**

```python
class TestAmbiguousSessionGuard:
    def _seed_two_active(self):
        import fastship_state
        fastship_state.set_current_session_id("alpha", "feature alpha", {"current_step": "2.0"})
        fastship_state.set_current_session_id("beta", "feature beta", {"current_step": "1.4"})

    def test_active_session_ids_excludes_done(self, tmp_path, monkeypatch):
        import fastship_state
        monkeypatch.setenv("FASTSHIP_STATE_HOME", str(tmp_path))
        fastship_state.set_current_session_id("a", "ra", {"current_step": "2.0"})
        fastship_state.set_current_session_id("b", "rb", {"current_step": "done"})
        assert fastship_state.active_session_ids() == ["a"]

    def test_ambiguous_when_two_active_no_pin(self, tmp_path, monkeypatch):
        import orchestrator
        monkeypatch.setenv("FASTSHIP_STATE_HOME", str(tmp_path))
        monkeypatch.delenv("FASTSHIP_SESSION", raising=False)
        self._seed_two_active()
        assert orchestrator._hook_session_ambiguous() is True

    def test_not_ambiguous_when_pinned(self, tmp_path, monkeypatch):
        import orchestrator
        monkeypatch.setenv("FASTSHIP_STATE_HOME", str(tmp_path))
        monkeypatch.setenv("FASTSHIP_SESSION", "alpha")
        self._seed_two_active()
        assert orchestrator._hook_session_ambiguous() is False

    def test_post_bash_no_advance_when_ambiguous(self, tmp_path, monkeypatch, capsys):
        import orchestrator
        monkeypatch.setenv("FASTSHIP_STATE_HOME", str(tmp_path))
        monkeypatch.delenv("FASTSHIP_SESSION", raising=False)
        self._seed_two_active()
        monkeypatch.setattr("orchestrator.detect_completion_post_bash",
                            lambda *a, **k: "1.0")
        rc = orchestrator.hook_post_bash_logic(
            {"tool_input": {"command": "x"}}, hook_state={"request_classified": True})
        out = capsys.readouterr().out
        assert rc == 0
        assert "多个活跃 session" in out

    def test_pre_edit_failopen_skips_phase_block_but_keeps_state_block(self, tmp_path, monkeypatch, capsys):
        import orchestrator
        monkeypatch.setenv("FASTSHIP_STATE_HOME", str(tmp_path))
        monkeypatch.delenv("FASTSHIP_SESSION", raising=False)
        self._seed_two_active()
        # Wrong-session orch_state says phase 1; a code edit would normally be blocked.
        orch_state = {"current_step": "1.4", "phase": 1, "branch": None}
        # Session-specific block is skipped (fail-open) -> allowed.
        rc_code = orchestrator.hook_pre_edit_logic(
            {"tool_input": {"file_path": "src/app.rs"}}, orch_state, "/nonexistent-gate.py")
        assert rc_code == 0
        # Session-independent block still fires: editing fastship state is blocked.
        rc_state = orchestrator.hook_pre_edit_logic(
            {"tool_input": {"file_path": "x/fastship/orchestrator.json"}},
            orch_state, "/nonexistent-gate.py")
        assert rc_state == 1
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /Users/apple/works/claude-skills && python3 -m pytest tests/fastship/test_orchestrator.py::TestAmbiguousSessionGuard -v`
Expected: FAIL — helpers don't exist.

- [ ] **Step 3: Add `active_session_ids()` to `fastship_state.py`**

After `list_sessions` (line 199-200):

```python
def active_session_ids() -> list:
    """Session ids whose flow is still active (not done/stopped)."""
    out = []
    for sid, rec in (list_sessions() or {}).items():
        if (rec or {}).get("status") not in ("done", "stopped"):
            n = normalize_session_id(sid)
            if n:
                out.append(n)
    return sorted(out)
```

- [ ] **Step 4: Add `_hook_session_ambiguous()` and the post-hook guards in `orchestrator.py`**

Above `hook_post_bash_logic` (line 1530):

```python
def _hook_session_ambiguous() -> bool:
    """True when ≥2 sessions are active in this state-home and none is pinned via
    FASTSHIP_SESSION — the editing context can't be mapped to one session."""
    if os.environ.get(fastship_state.SESSION_ENV):
        return False
    return len(fastship_state.active_session_ids()) >= 2


_AMBIGUOUS_HINT = (
    "⚠️ fastship: 检测到多个活跃 session 且未用 FASTSHIP_SESSION 锁定，为避免串台，"
    "本次 hook 不应用 session 专属逻辑。\n"
    "   并行需求请放各自 git worktree，或用 "
    "\"$(git rev-parse --show-toplevel)/.claude/tools/fastship\" use <session> 指定。"
)
```

At the top of `hook_post_bash_logic`, right after its `orch = load_orch_state(orch_path)` / early-return block (before `detected = detect_completion_post_bash(...)`, line 1539):

```python
    if orch_path is None and _hook_session_ambiguous():
        print(_AMBIGUOUS_HINT)
        return 0
```

At the top of `hook_post_edit_logic`, after its `orch = load_orch_state(orch_path)` / early-return block (line 1588-1589, before `current = orch.get("current_step")`):

```python
    if orch_path is None and _hook_session_ambiguous():
        print(_AMBIGUOUS_HINT)
        return 0
```

- [ ] **Step 5: Add the pre-hook fail-open guards**

In `hook_pre_edit_logic`: the **state-file-write block** (lines 1437-1449) must run first (session-independent — keep it), then the fail-open guard, then the rest. Move/confirm ordering so the guard sits **after** the state-file block and **before** the branch-mismatch check (line 1431) — i.e. relocate the branch-mismatch check to after the guard. Concretely, restructure the top of `hook_pre_edit_logic` to:

```python
def hook_pre_edit_logic(data: dict, orch_state: Optional[dict],
                        gate_path: str) -> int:
    file_path = data.get("tool_input", {}).get("file_path", "")

    if not orch_state:
        if os.path.exists(gate_path):
            code, stdout = delegate_to_gate(gate_path, "pre_edit", data)
            if stdout:
                print(stdout, end="")
            return code
        return 0

    # Session-INDEPENDENT block: editing fastship state files is always forbidden.
    normalized = _normalize(file_path)
    if (
        any(pat in normalized for pat in (
            "fastship/gate.json",
            "fastship/orchestrator.json",
            "fastship/registry.json",
            ".fastship-orchestrator-state.json",
            ".ship-verify-state.json",
        ))
        or ("fastship/sessions/" in normalized and normalized.endswith(("/gate.json", "/orchestrator.json")))
    ):
        print("🔴 BLOCKED: fastship state 由系统管理，禁止手动编辑")
        return 1

    # Fail-open under ambiguous multi-session: skip session-specific blocks.
    if _hook_session_ambiguous():
        print(_AMBIGUOUS_HINT)
        return 0

    if _is_active(orch_state) and _branch_mismatch(orch_state):
        print("🔴 BLOCKED: Fastship branch mismatch")
        print(_branch_mismatch_text(orch_state))
        return 1

    # ... (the existing out-of-order artifact block + phase-1 code block + gate
    #      delegation follow unchanged, with the now-removed duplicate state-file
    #      block deleted from its old position)
```

Delete the original state-file block at its old location (old lines 1436-1449) and the original branch-mismatch check at old line 1431 (now relocated below the guard) so each appears exactly once.

In `hook_pre_bash_logic`, add the fail-open guard right after the `if not orch_state:` block (before the branch-mismatch check at line 1509):

```python
    if _hook_session_ambiguous():
        print(_AMBIGUOUS_HINT)
        return 0
```

- [ ] **Step 6: Run to verify it passes**

Run: `cd /Users/apple/works/claude-skills && python3 -m pytest tests/fastship/test_orchestrator.py::TestAmbiguousSessionGuard -v`
Expected: PASS (all).

- [ ] **Step 7: Full suite to confirm existing hook tests unaffected**

Run: `cd /Users/apple/works/claude-skills && python3 -m pytest tests/fastship/test_orchestrator.py -q`
Expected: all pass. Existing pre/post hook tests register ≤1 session (or pass `orch_path`), so `_hook_session_ambiguous()` is False and the guards are inert.

- [ ] **Step 8: Commit**

```bash
cd /Users/apple/works/claude-skills
git add skills/fastship/fastship_state.py skills/fastship/orchestrator.py tests/fastship/test_orchestrator.py
git commit -m "feat(fastship): ambiguous-session guard (post no-advance, pre fail-open)"
```

---

## Task 7: `start` refuses a second concurrent session unless `--shared`/`--session`

Make the one-session-per-worktree invariant the default: a bare `start "<req>"` is rejected when another active session already lives in this state-home; the user must pass `--shared` (or an explicit `--session <id>`, which is consumed globally before `cmd_start` and sets `FASTSHIP_SESSION`) to opt into a second concurrent session.

**Files:**
- Modify: `skills/fastship/orchestrator.py` (`cmd_start` + arg parsing for `--shared`)
- Add: `skills/fastship/orchestrator.py` (`_other_active_sessions`, `_blocking_active_session_msg`)
- Test: `tests/fastship/test_orchestrator.py` (new class `TestStartSecondSessionRefusal`)

- [ ] **Step 1: Locate `cmd_start` and the boolean-flag set**

Run: `cd /Users/apple/works/claude-skills && grep -n "def cmd_start\|BOOLEAN_FLAGS\|--user-confirmed" skills/fastship/orchestrator.py`
Expected: note `cmd_start`'s line and the `BOOLEAN_FLAGS` set (line 1778) to register `--shared`.

- [ ] **Step 2: Write the failing test**

```python
class TestStartSecondSessionRefusal:
    def test_other_active_sessions_excludes_self_and_done(self, tmp_path, monkeypatch):
        import orchestrator, fastship_state
        monkeypatch.setenv("FASTSHIP_STATE_HOME", str(tmp_path))
        fastship_state.set_current_session_id("self", "mine", {"current_step": "2.0"})
        fastship_state.set_current_session_id("other", "theirs", {"current_step": "1.4"})
        fastship_state.set_current_session_id("old", "done", {"current_step": "done"})
        assert orchestrator._other_active_sessions("self") == ["other"]

    def test_blocking_message_lists_other_and_mentions_shared(self, tmp_path, monkeypatch):
        import orchestrator, fastship_state
        monkeypatch.setenv("FASTSHIP_STATE_HOME", str(tmp_path))
        fastship_state.set_current_session_id("other", "theirs", {"current_step": "1.4"})
        msg = orchestrator._blocking_active_session_msg("newcomer")
        assert msg is not None
        assert "other" in msg
        assert "--shared" in msg
        assert "worktree" in msg.lower()

    def test_no_block_when_no_other_active(self, tmp_path, monkeypatch):
        import orchestrator, fastship_state
        monkeypatch.setenv("FASTSHIP_STATE_HOME", str(tmp_path))
        fastship_state.set_current_session_id("solo", "only", {"current_step": "1.0"})
        assert orchestrator._blocking_active_session_msg("solo") is None
```

- [ ] **Step 3: Run to verify it fails**

Run: `cd /Users/apple/works/claude-skills && python3 -m pytest tests/fastship/test_orchestrator.py::TestStartSecondSessionRefusal -v`
Expected: FAIL — helpers don't exist.

- [ ] **Step 4: Register the `--shared` flag and add the helpers**

Add `"--shared"` to `BOOLEAN_FLAGS` (line 1778):

```python
BOOLEAN_FLAGS = {"--grill-complete", "--user-confirmed", "--shared"}
```

Add near `_hook_session_ambiguous`:

```python
def _other_active_sessions(current_sid: str) -> list:
    cur = fastship_state.normalize_session_id(current_sid)
    return [s for s in fastship_state.active_session_ids() if s != cur]


def _blocking_active_session_msg(current_sid: str):
    """Return a refusal message if another active session shares this
    state-home, else None."""
    others = _other_active_sessions(current_sid)
    if not others:
        return None
    cli = '"$(git rev-parse --show-toplevel)/.claude/tools/fastship"'
    return (
        f"🔴 本 worktree 已有活跃 session: {', '.join(others)}\n"
        f"   一 session 一 worktree 是默认隔离方式。请二选一：\n"
        f"     • 在新的 git worktree 里 start（推荐，隔离最干净）\n"
        f"     • 确需同 worktree 内并行：加 --shared 或 --session <id> 重新 start\n"
        f"   （同 worktree 多 session 时 hook 会停止自动推进以防串台，"
        f"且 .claude/.fastship-*.md 评审产物会共享。）"
    )
```

- [ ] **Step 5: Enforce in `cmd_start`**

In `cmd_start`, after the new session id is resolved (the local id variable, commonly `session_id`/`sid`) **but before** writing the orchestrator state / registering it, insert the refusal — unless `--shared` was passed or an explicit session was pinned via env:

```python
    shared = "--shared" in argv or bool(os.environ.get(fastship_state.SESSION_ENV))
    if not shared:
        msg = _blocking_active_session_msg(session_id)
        if msg:
            print(msg)
            return 1
```

Use the actual local variable name `cmd_start` uses for the session id and the actual args list name in scope (commonly `argv`). The check must come *before* the new session is registered so `_other_active_sessions` sees only the pre-existing ones.

- [ ] **Step 6: Run to verify it passes**

Run: `cd /Users/apple/works/claude-skills && python3 -m pytest tests/fastship/test_orchestrator.py::TestStartSecondSessionRefusal -v`
Expected: PASS.

- [ ] **Step 7: Full suite (existing start tests start only one session → unaffected) + commit**

```bash
cd /Users/apple/works/claude-skills
python3 -m pytest tests/fastship/test_orchestrator.py -q
git add skills/fastship/orchestrator.py tests/fastship/test_orchestrator.py
git commit -m "feat(fastship): start refuses 2nd concurrent session unless --shared"
```

---

## Task 8: Session-scoped verdict-ledger path helper

Add `implement_verdicts_path()` so the implement workflow's per-task verdicts live under the session dir (`{git-dir}/fastship/sessions/<sid>/implement-verdicts.md`) — isolated by both worktree and session. A generic `.claude/` filename would collide between sessions under `--shared`.

**Files:**
- Add: `skills/fastship/fastship_state.py` (`implement_verdicts_path`)
- Test: `tests/fastship/test_orchestrator.py` (new class `TestImplementVerdictsPath`)

- [ ] **Step 1: Write the failing test**

```python
class TestImplementVerdictsPath:
    def test_path_is_under_session_dir(self, tmp_path, monkeypatch):
        import fastship_state
        monkeypatch.setenv("FASTSHIP_STATE_HOME", str(tmp_path))
        p_a = fastship_state.implement_verdicts_path("alpha")
        p_b = fastship_state.implement_verdicts_path("beta")
        assert p_a.endswith("sessions/alpha/implement-verdicts.md")
        assert p_b.endswith("sessions/beta/implement-verdicts.md")
        assert p_a != p_b

    def test_path_follows_current_session_when_unspecified(self, tmp_path, monkeypatch):
        import fastship_state
        monkeypatch.setenv("FASTSHIP_STATE_HOME", str(tmp_path))
        monkeypatch.setenv("FASTSHIP_SESSION", "gamma")
        assert fastship_state.implement_verdicts_path().endswith(
            "sessions/gamma/implement-verdicts.md")
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /Users/apple/works/claude-skills && python3 -m pytest tests/fastship/test_orchestrator.py::TestImplementVerdictsPath -v`
Expected: FAIL — function missing.

- [ ] **Step 3: Implement the helper**

After `gate_state_path` (line 289-290) in `fastship_state.py`:

```python
def implement_verdicts_path(session_id: str = None) -> str:
    return os.path.join(session_state_dir(session_id), "implement-verdicts.md")
```

- [ ] **Step 4: Run to verify it passes + commit**

Run: `cd /Users/apple/works/claude-skills && python3 -m pytest tests/fastship/test_orchestrator.py::TestImplementVerdictsPath -v`
Expected: PASS.

```bash
cd /Users/apple/works/claude-skills
git add skills/fastship/fastship_state.py tests/fastship/test_orchestrator.py
git commit -m "feat(fastship): session-scoped implement_verdicts_path()"
```

---

## Task 9: Rewrite Step 2.0 instruction — shared-worktree dependency-aware implement workflow

Replace Step 2.0's `instruction` to specify the contract: dependency-aware partition, shared-worktree edit-only parallel groups (no per-agent worktree, no merge-back, agents don't commit), edit + compile-check only (no parallel test/E2E), session-scoped verdict ledger feeding 2.5, conditional Workflow (≥2 disjoint groups) with sequential fallback.

**Files:**
- Modify: `skills/fastship/orchestrator.py:1105-1124` (Step 2.0 `instruction`)
- Test: `tests/fastship/test_orchestrator.py` (new class `TestStep20Contract`)

- [ ] **Step 1: Write the failing test**

```python
class TestStep20Contract:
    def _instr(self):
        from orchestrator import STEPS
        s = next(s for s in STEPS if s.id == "2.0")
        return s.instruction({}) if callable(s.instruction) else s.instruction

    def test_dependency_aware_partition(self):
        i = self._instr()
        assert "不相交" in i and "parallel" in i

    def test_shared_worktree_edit_only_no_commit(self):
        i = self._instr()
        assert "不各自 commit" in i or "不要各自 commit" in i
        assert "merge" not in i.lower()  # merge-back removed

    def test_no_parallel_tests_during_implement(self):
        i = self._instr()
        assert "编译检查" in i
        assert "测试套件" in i or "E2E" in i

    def test_conditional_workflow_and_sequential_fallback(self):
        i = self._instr()
        assert "≥2" in i or ">=2" in i
        assert "串行" in i

    def test_session_scoped_verdict_ledger_feeds_2_5(self):
        i = self._instr()
        assert "implement-verdicts" in i
        assert "2.5" in i
```

- [ ] **Step 2: Run to verify it fails**

Run: `cd /Users/apple/works/claude-skills && python3 -m pytest tests/fastship/test_orchestrator.py::TestStep20Contract -v`
Expected: FAIL — current instruction lacks these clauses.

- [ ] **Step 3: Replace the Step 2.0 instruction (lines 1106-1124)**

```python
         instruction="""🎯 向用户展示 /goal 命令，进入自主执行模式（Phase 2+3 一气呵成）：
  运行: "$(git rev-parse --show-toplevel)/.claude/tools/fastship" goal
  将输出的 /goal 命令呈现给用户，请用户执行。

/goal 模式下用 dynamic workflow（ultracode）执行 plan —— 由你读 plan 自主决定扇出：
  1. 选择开发方式（worktree / 新分支 / 当前分支）。整个 implement 在【同一个 session worktree】里跑。
  2. 读已确认 plan 的 task 列表，做【依赖感知拆分】：
       - 文件【不相交】的 task → 可并行组；有先后依赖或改同一批文件的 → 串行链。
       - 不相交组【数量 ≥2】才用 Workflow parallel() 并行实现；只有一条链时直接
         subagent-driven 串行实现（不开 Workflow，省开销）。
       - 并行 agent 在【同一 worktree】内只编辑各自不相交的文件，【不各自 commit】
         （commit 由主线程逐组统一发，避免并行写 git index）。不开 per-agent worktree。
       - 并行 implement agent 只允许【编辑 + 编译检查】（cargo check / tsc）；
         【禁止跑项目测试套件 / E2E】——那是 step 3.1/3.2、主线程、串行干的
         （并行跑测试会撞 gate 状态写，语义上也错：要的是全部完成后跑全量）。
  3. implement→review pipeline：每个 task 实现完立刻被对抗性 review
       （设计稿保真度 / spec 合同 / 质量三视角），review 不过当场打回重做。
  4. 逐 task 的结构化 verdict（task / files_changed / 三视角结论）写入【session 绑定】的
       ledger：{git-dir}/fastship/sessions/<sid>/implement-verdicts.md
       （路径用 fastship_state.implement_verdicts_path() 解析；非门禁，作 2.5 的输入证据）。
       Step 2.5 读这个 ledger，合成 .claude/.fastship-code-review.md gate。
  5. 每步完成后运行 status，让 /goal 评估器看到 [FASTSHIP_GOAL] 进度。

🔴 一 session 一 worktree：多个并行需求 = 多个 git worktree。同一 worktree 内并行多 session 时，
   hook 会停止自动推进以防串台，须用 FASTSHIP_SESSION / use <session> 显式锁定。
🔴 禁止主线程凭直觉写代码；禁止并行 agent 改重叠文件或各自 commit。

执行完成 → done 进入 2.5 Code Review 合并 gate。
手动模式（不用 /goal）: "$(git rev-parse --show-toplevel)/.claude/tools/fastship" done"""),
```

- [ ] **Step 4: Run to verify it passes**

Run: `cd /Users/apple/works/claude-skills && python3 -m pytest tests/fastship/test_orchestrator.py::TestStep20Contract -v`
Expected: PASS (all five).

- [ ] **Step 5: Full suite + commit**

```bash
cd /Users/apple/works/claude-skills
python3 -m pytest tests/fastship/test_orchestrator.py -q
git add skills/fastship/orchestrator.py tests/fastship/test_orchestrator.py
git commit -m "feat(fastship): Step 2.0 shared-worktree dependency-aware implement workflow"
```

---

## Task 10: Document the contract + isolation invariant in SKILL.md

**Files:**
- Modify: `skills/fastship/SKILL.md` (流程概览 2.0 line + 核心红线)
- Verify: grep assertion (no unit test for docs)

- [ ] **Step 1: Update the 2.0 flow line**

Replace the 流程概览 line:
```
  2.0  执行计划         [/goal 自主驱动] ultracode implement→review pipeline（执行+并发对抗 review）
```
with:
```
  2.0  执行计划         [/goal 自主驱动] dynamic workflow：依赖感知扇出（≥2 不相交组 parallel，同 worktree 只编辑不 commit）→ implement→review pipeline
```

- [ ] **Step 2: Add isolation red lines** in `## 核心红线`, after the `主线程禁止亲自 grep/find` bullet:

```
- 一 session 一 worktree：并行需求放各自 git worktree。同 state-home 内 start 第二个活跃 session 默认被拒，须 --shared / --session 显式开；多活跃 session 时 hook 停止自动推进以防串台。
- Phase 2 implement 扇出由 Claude 读 plan 决定：文件不相交的 task 才在同 worktree 内并行编辑（不各自 commit，主线程逐组 commit），相交/依赖的串行；禁止并行跑测试套件/E2E（只编辑+编译检查）。
- Implement verdict 落 session 绑定的 ledger（sessions/<sid>/implement-verdicts.md），喂 Step 2.5。
```

- [ ] **Step 3: Verify the doc edits landed**

Run:
```bash
cd /Users/apple/works/claude-skills && \
  grep -q "依赖感知扇出" skills/fastship/SKILL.md && \
  grep -q "一 session 一 worktree" skills/fastship/SKILL.md && \
  grep -q "implement-verdicts.md" skills/fastship/SKILL.md && \
  echo "DOC OK"
```
Expected: `DOC OK`.

- [ ] **Step 4: Commit**

```bash
cd /Users/apple/works/claude-skills
git add skills/fastship/SKILL.md
git commit -m "docs(fastship): document implement-workflow contract + worktree isolation"
```

---

## Task 11: Final full-suite verification

- [ ] **Step 1: Run the entire suite**

Run: `cd /Users/apple/works/claude-skills && python3 -m pytest tests/fastship/test_orchestrator.py -v`
Expected: all pass, including the new classes: `TestWorktreeStateIsolation`, `TestStep20StateNoop`, `TestAtomicSaveJson`, `TestStateLock`, `TestRegistryConcurrency`, `TestGateStateLocking`, `TestAmbiguousSessionGuard`, `TestStartSecondSessionRefusal`, `TestImplementVerdictsPath`, `TestStep20Contract`.

- [ ] **Step 2: Sanity-check both scripts import + run as CLIs**

Run:
```bash
cd /Users/apple/works/claude-skills
python3 skills/fastship/orchestrator.py status; echo "orch_exit=$?"
echo '{}' | python3 skills/fastship/hooks/ship_verify_gate.py status; echo "gate_exit=$?"
```
Expected: both run without import/syntax error (new `fcntl`/`threading`/`contextlib`/`tempfile` imports + edited functions load cleanly).

- [ ] **Step 3: Confirm the diff scope**

Run: `cd /Users/apple/works/claude-skills && git log --oneline -10 && git diff --stat HEAD~10`
Expected: ten implementation commits; diffstat touches only `skills/fastship/fastship_state.py`, `skills/fastship/orchestrator.py`, `skills/fastship/hooks/ship_verify_gate.py`, `skills/fastship/SKILL.md`, `tests/fastship/test_orchestrator.py`.

---

## Self-Review

**Spec coverage (vs grilling decisions):**
- D1 shared-worktree edit-only fan-out → Task 9 (contract), premise frozen in Task 1.
- D2 gate concurrency (contract + lock) → Task 9 (contract clause) + Task 5 (gate RMW lock).
- D3 `orch_path is None` discriminator → Task 6 post-hook guard.
- D4 pre-hook fail-open guard → Task 6.
- D5 start refuses 2nd session unless `--shared` → Task 7.
- D6 session-scoped verdict ledger → Task 8 (path) + Task 9 (contract uses it).
- D7 instruction-level conditional opt-in + sequential fallback → Task 9.
- Concurrency safety substrate → Tasks 2 (atomic), 3 (lock), 4 (registry RMW).
- Docs → Task 10. Verification → Task 11.

**Placeholder scan:** none — every code step shows full code; every test step gives the exact `pytest` command + expected PASS/FAIL.

**Type/name consistency:** `state_lock` (T3) used by T4, T5, T6. `active_session_ids` (T6, `fastship_state`) used by `_hook_session_ambiguous` (T6) and `_other_active_sessions` (T7). `_hook_session_ambiguous` + `_AMBIGUOUS_HINT` (T6) used by all four hook guards. `implement_verdicts_path` (T8) referenced by the Task 9 instruction. Task 9 contract tokens (`不相交`, `parallel`, `不各自 commit`, no `merge`, `编译检查`, `测试套件`/`E2E`, `≥2`, `串行`, `implement-verdicts`, `2.5`) all present in the Task 9 instruction text.

**Known limitation (documented, out of scope):** existing `.claude/.fastship-*.md` artifacts are per-worktree but not per-session; under deliberate `--shared` they're shared. Tracked as a follow-up; the new verdict ledger is session-scoped.
