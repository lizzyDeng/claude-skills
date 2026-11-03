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


# ---- plan v4：R1 git 原语、R6 收尾清理 ---------------------------------------
import subprocess  # noqa: E402

import pytest  # noqa: E402
from conftest import FakeCtx  # noqa: E402

import launcher  # noqa: E402

M = "a" * 40          # origin/main 的 sha
Z = "f" * 40          # 认不出的 tip


def _ls(repo, branch):
    return ["git", "-C", str(repo), "ls-remote", "--exit-code", "origin", f"refs/heads/{branch}"]


def test_remote_branch_classifies_present_missing_unknown(tmp_path):
    f = FakeCtx().script(_ls(tmp_path, "staging"), f"{M}\trefs/heads/staging\n")
    assert launcher.remote_branch(f, tmp_path, "staging") == ("present", M)
    assert f.calls[-1] == _ls(tmp_path, "staging")                   # 完整 ref 名，不会撞上 staging-old
    assert f.timeouts[-1] is not None and f.timeouts[-1] <= 60       # 弱网不能挂住整轮
    assert launcher.remote_branch(FakeCtx().script(_ls(tmp_path, "staging"), "", returncode=2),
                                  tmp_path, "staging") == ("missing", "")
    for rc, err in ((128, "fatal: unable to access 'https://x/': Could not resolve host"), (124, "timeout")):
        state, detail = launcher.remote_branch(
            FakeCtx().script(_ls(tmp_path, "staging"), "", returncode=rc, stderr=err), tmp_path, "staging")
        assert state == "unknown" and err[:20] in detail               # 网络坏了 ≠ 分支不存在


def test_rebuild_branch_pushes_main_sha_with_lease_and_no_verify(tmp_path):
    f = FakeCtx()
    res = launcher.rebuild_branch(f, tmp_path, "staging", "main", M)
    assert res["ok"] is True and res["sha"] == M and not res.get("by_other")
    g = lambda *a: ["git", "-C", str(tmp_path), *a]
    push = g("push", "--no-verify", "--force-with-lease=refs/heads/staging:", "origin", f"{M}:refs/heads/staging")
    assert f.calls.index(g("fetch", "origin", "main")) < f.calls.index(push) < f.calls.index(g("fetch", "origin", "staging"))
    assert f.timeout_of("push") is not None
    # base == main：拒绝（绝不拿 main 覆盖 main）
    f2 = FakeCtx()
    assert launcher.rebuild_branch(f2, tmp_path, "main", "main", M)["ok"] is False and f2.calls == []
    # fetch main 失败 ⇒ 重建失败，不推
    f3 = FakeCtx().script(g("fetch", "origin", "main"), "", returncode=128, stderr="fatal: network")
    r3 = launcher.rebuild_branch(f3, tmp_path, "staging", "main", M)
    assert r3["ok"] is False and not f3.calls_with("push")
    # 推成功、随后 fetch base 失败 ⇒ 仍算成功，带 warn
    f4 = FakeCtx().script(g("fetch", "origin", "staging"), "", returncode=128, stderr="fatal: flaky")
    r4 = launcher.rebuild_branch(f4, tmp_path, "staging", "main", M)
    assert r4["ok"] is True and r4.get("warn")


def test_rebuild_lease_rejected_branch_at_main_sha_is_ok(tmp_path):
    f = FakeCtx().script(["git", "-C", str(tmp_path), "push"], "", returncode=1, stderr="! [rejected] (stale info)")
    f.script(_ls(tmp_path, "staging"), f"{M}\trefs/heads/staging\n")          # 别人刚好也从 main 重建了
    res = launcher.rebuild_branch(f, tmp_path, "staging", "main", M)
    assert res["ok"] is True and res["by_other"] is True and res["sha"] == M


def test_rebuild_lease_rejected_with_foreign_tip_is_refused(tmp_path):
    f = FakeCtx().script(["git", "-C", str(tmp_path), "push"], "", returncode=1, stderr="! [rejected] (stale info)")
    f.script(_ls(tmp_path, "staging"), f"{Z}\trefs/heads/staging\n")          # 同名分支，但指向任意 tip
    res = launcher.rebuild_branch(f, tmp_path, "staging", "main", M)
    assert res == {**res, "ok": False, "reason": "foreign_tip", "sha": Z}
    # lease 拒了、再查仍然不在 ⇒ 普通推送失败
    g = FakeCtx().script(["git", "-C", str(tmp_path), "push"], "", returncode=1, stderr="pre-receive hook declined")
    g.script(_ls(tmp_path, "staging"), "", returncode=2)
    r = launcher.rebuild_branch(g, tmp_path, "staging", "main", M)
    assert r["ok"] is False and r["reason"] == "push" and "declined" in r["error"]


def _git(*a, cwd=None):
    return subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True).stdout


def _repo_with_origin(tmp_path):
    origin, root = tmp_path / "origin.git", tmp_path / "proj"
    _git("init", "-q", "--bare", "-b", "staging", str(origin))
    _git("clone", "-q", str(origin), str(root))
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        _git("config", k, v, cwd=root)
    (root / "a.txt").write_text("a\n")
    _git("add", "a.txt", cwd=root); _git("commit", "-qm", "init", cwd=root); _git("push", "-q", "origin", "HEAD:staging", cwd=root)
    return root


def _worktree(root, tid):
    wt = root / ".claude" / "worktrees" / f"router-{tid}"
    _git("worktree", "add", "-q", "-b", f"router/{tid}", str(wt), "origin/staging", cwd=root)
    return wt


def _branch_exists(root, b):
    return bool(_git("branch", "--list", b, cwd=root).strip())


def test_cleanup_deletes_branch_only_without_unpushed_commits(tmp_path):
    root = _repo_with_origin(tmp_path)
    wt1 = _worktree(root, "T1")                                         # 分支上没有自己的提交
    res = launcher.cleanup_ticket_checkout(Ctx(), root, str(wt1), "router/T1")
    assert res == {"worktree": "done", "branch": "done"}
    assert not wt1.exists() and not _branch_exists(root, "router/T1")
    wt2 = _worktree(root, "T2")
    (wt2 / "b.txt").write_text("b\n"); _git("add", "b.txt", cwd=wt2); _git("commit", "-qm", "local", cwd=wt2)
    res = launcher.cleanup_ticket_checkout(Ctx(), root, str(wt2), "router/T2")
    assert res["worktree"] == "done" and not wt2.exists()
    assert res["branch"].startswith("kept") and "1" in res["branch"]     # 有 1 个未推送提交：不删
    assert _branch_exists(root, "router/T2")
    # 再跑一遍：worktree 已删、分支已在「保留」⇒ 幂等，不报错
    assert launcher.cleanup_ticket_checkout(Ctx(), root, str(wt1), "router/T1") == {"worktree": "done", "branch": "done"}


def test_cleanup_keeps_dirty_worktree(tmp_path):
    root = _repo_with_origin(tmp_path)
    wt = _worktree(root, "T1")
    (wt / "dirty.txt").write_text("还没提交\n")
    res = launcher.cleanup_ticket_checkout(Ctx(), root, str(wt), "router/T1")
    assert res["worktree"].startswith("kept") and (wt / "dirty.txt").exists()
    # 路径不在、但 git 还登记着 ⇒ prune 后算 done
    wt2 = _worktree(root, "T2")
    subprocess.run(["rm", "-rf", str(wt2)], check=True)
    assert launcher.cleanup_ticket_checkout(Ctx(), root, str(wt2), "router/T2")["worktree"] == "done"
    assert str(wt2) not in _git("worktree", "list", "--porcelain", cwd=root)


def test_cleanup_keeps_branch_when_worktree_kept(tmp_path):
    root = _repo_with_origin(tmp_path)
    wt = _worktree(root, "T1")
    (wt / "dirty.txt").write_text("x\n")
    res = launcher.cleanup_ticket_checkout(Ctx(), root, str(wt), "router/T1")
    assert res["branch"].startswith("kept") and _branch_exists(root, "router/T1")


# ---- FX：终审后修复轮 ------------------------------------------------------------------------

def test_receipt_wrapper_does_not_record_partial_ps_output_as_start_time(tmp_path, monkeypatch):   # F5 同类：ps rc≠0
    import json
    import exec_receipt
    monkeypatch.setattr(exec_receipt.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
        cmd, 124, "Mon Nov  2 10:00:00 2026\n", "timeout"))
    r = tmp_path / "T1-1.receipt.json"
    exec_receipt._write_receipt(r, "sid-a")
    rec = json.loads(r.read_text())
    assert rec["session_id"] == "sid-a" and rec["pid_start"] is None    # 查不到 ⇒ None（对账按「认不出」处理），不记半截输出


def test_worker_liveness_is_three_state(fake):                                       # F5（RV M-2）
    fake.alive.add(55)
    assert launcher.worker_liveness(fake, {"pid": 55, "pid_start": fake.proc_start(55)}) == "alive"
    assert launcher.worker_liveness(fake, {"pid": 55, "pid_start": "Mon Nov  2 01:00:00 2026"}) == "dead"   # pid 被复用
    assert launcher.worker_liveness(fake, {"pid": 56, "pid_start": "x"}) == "dead"
    fake.starts = {55: None}                                                          # ps 失败 / 超时
    assert launcher.worker_liveness(fake, {"pid": 55, "pid_start": "start-55"}) == "unknown"
    assert launcher.worker_alive(fake, {"pid": 55, "pid_start": "start-55"}) is False
