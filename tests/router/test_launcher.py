import sys
import time
from pathlib import Path

from ctx import Ctx
from launcher import claude_cmd, create_worktree, read_result, remove_worktree, spawn, stop_now

FAKE = str(Path(__file__).resolve().parent / "fake_claude.py")
CFG = {"model": "claude-opus-5-5", "permission_mode": "bypassPermissions", "allowed_tools": [],
       "disallowed_tools": ["Bash(supercli db execute*)", "Bash(gh pr merge*)"]}


def wait_until(cond, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.05)
    raise AssertionError("timeout")


def test_first_run_uses_session_id_and_resume_reuses_it():
    c = claude_cmd(CFG, "p", "s1", resume=False)
    assert c[:3] == ["claude", "-p", "p"] and c[c.index("--session-id") + 1] == "s1" and "--resume" not in c
    c = claude_cmd(CFG, "p", "s1", resume=True)
    assert c[c.index("--resume") + 1] == "s1" and "--session-id" not in c
    assert c[c.index("--output-format") + 1] == "json" and c[c.index("--model") + 1] == "claude-opus-5-5"


def test_cmd_carries_permission_and_disallowed_tools():
    c = claude_cmd(CFG, "p", "s1", resume=False)
    assert c[c.index("--permission-mode") + 1] == "bypassPermissions"
    i = c.index("--disallowedTools")
    assert c[i + 1:] == ["Bash(supercli db execute*)", "Bash(gh pr merge*)"]   # 变长参数放最后，不吞 prompt
    assert "--allowedTools" not in c
    c2 = claude_cmd({**CFG, "allowed_tools": ["Read"], "claude_bin": [sys.executable, FAKE]}, "p", "s", False)
    assert c2[:2] == [sys.executable, FAKE] and c2[c2.index("--allowedTools") + 1] == "Read"


def test_spawned_worker_outlives_parent_and_writes_result(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDECODE", "1")                  # router 自己的会话变量不能漏给 worker
    pid = spawn(Ctx(), tmp_path, [sys.executable, FAKE, "-p", "x", "--session-id", "s9"], tmp_path / "w.log",
                env={"ROUTER_TICKET": "T9"})
    wait_until(lambda: not Ctx().pid_alive(pid))
    r = read_result(tmp_path / "w.log")
    assert r["session_id"] == "s9" and r["router_ticket"] == "T9" and r["claudecode"] is None


def test_stop_now_kills_process_group(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_SLEEP", "30")
    pid = spawn(Ctx(), tmp_path, [sys.executable, FAKE, "-p", "x", "--session-id", "s1"], tmp_path / "w.log")
    assert Ctx().pid_alive(pid)
    stop_now(pid)
    wait_until(lambda: not Ctx().pid_alive(pid))
    assert read_result(tmp_path / "w.log") is None
    stop_now(pid)                                           # 已经死了再停一次不报错


def test_read_result_none_when_log_has_no_json(tmp_path):
    (tmp_path / "w.log").write_text("Warning: no stdin data received in 3s\nkilled\n")
    assert read_result(tmp_path / "w.log") is None
    assert read_result(tmp_path / "missing.log") is None


def test_read_result_takes_last_result_object(tmp_path):
    (tmp_path / "w.log").write_text('{"type":"result","result":"one"}\nnoise {x\n{"a":1}\n{"type":"result","result":"two"}\n')
    assert read_result(tmp_path / "w.log")["result"] == "two"


def test_create_and_remove_worktree_commands(fake, tmp_path):
    wt, branch = create_worktree(fake, tmp_path, "staging", ".claude/worktrees", "T3")
    assert branch == "router/T3" and wt == tmp_path / ".claude/worktrees" / "router-T3"
    assert fake.calls == [["git", "-C", str(tmp_path), "fetch", "origin", "staging"],
                          ["git", "-C", str(tmp_path), "rev-parse", "--verify", "--quiet", "refs/heads/router/T3"],
                          ["git", "-C", str(tmp_path), "worktree", "add", "-b", "router/T3", str(wt), "origin/staging"]]
    remove_worktree(fake, tmp_path, wt)
    assert fake.last_call() == ["git", "-C", str(tmp_path), "worktree", "remove", "--force", str(wt)]


def test_create_worktree_failure_raises(fake, tmp_path):
    import pytest
    fake.script(["git", "-C", str(tmp_path), "worktree", "add"], "", returncode=128, stderr="already exists")
    with pytest.raises(RuntimeError, match="already exists"):
        create_worktree(fake, tmp_path, "staging", ".claude/worktrees", "T3")


def test_create_worktree_reuses_existing(fake, tmp_path):             # I4：上次派到一半崩了，再派要能接上
    wt = tmp_path / ".claude/worktrees" / "router-T3"; wt.mkdir(parents=True)
    assert create_worktree(fake, tmp_path, "staging", ".claude/worktrees", "T3") == (wt, "router/T3")
    assert not fake.calls_with("worktree", "add")
    fake2 = type(fake)().script(["git", "-C", str(tmp_path), "rev-parse", "--verify", "--quiet", "refs/heads/router/T4"], "abc\n")
    wt4, _ = create_worktree(fake2, tmp_path, "staging", ".claude/worktrees", "T4")
    assert ["git", "-C", str(tmp_path), "worktree", "add", str(wt4), "router/T4"] in fake2.calls


def test_worker_alive_checks_process_identity(fake):                  # I9：pid 被复用不能当成 worker 还活着
    from launcher import worker_alive
    fake.alive.add(55)
    assert worker_alive(fake, {"pid": 55, "pid_start": fake.proc_start(55)}) is True
    assert worker_alive(fake, {"pid": 55, "pid_start": "Mon Nov  2 01:00:00 2026"}) is False
    assert worker_alive(fake, {"pid": 55}) is True and worker_alive(fake, {"pid": None}) is False
