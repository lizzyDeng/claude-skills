import json
import os
import time

import pytest

from discover import conflicts_with_router, scan

BASE = "origin/staging"


@pytest.fixture
def repo(tmp_path, fake):
    main, a, b, r = (tmp_path / n for n in ("main", "wt-a", "wt-b", "wt-r"))
    for d in (main, a, b, r):
        d.mkdir()
    porcelain = "\n".join([
        f"worktree {main}", "HEAD 111", "branch refs/heads/feat/831", "",
        f"worktree {a}", "HEAD 222", "branch refs/heads/feat/her-attention-by-stage", "",
        f"worktree {b}", "HEAD 333", "branch refs/heads/fix/weekly", "",
        f"worktree {r}", "HEAD 444", "branch refs/heads/router/T1", "",
        f"worktree {tmp_path / 'gone'}", "HEAD 555", "branch refs/heads/old", "prunable gitdir file points to non-existent location", ""])
    fake.script(["git", "-C", str(main), "worktree", "list", "--porcelain"], porcelain)
    fake.script(["git", "-C", str(a), "diff", "--name-only", f"{BASE}...HEAD"],
                "services/api-server/src/services/her_chat/turn.rs\n")
    fake.script(["git", "-C", str(a), "diff", "--name-only", BASE, "HEAD"],      # 仍与 staging 不同
                "services/api-server/src/services/her_chat/turn.rs\nREADME.md\n")
    fake.script(["git", "-C", str(a), "status", "--porcelain"], " M crates/persona-engine/src/prompt_builder.rs\n?? notes.txt\n")
    fake.script(["git", "-C", str(b), "status", "--porcelain"], "R  old.py -> services/api-server/migrations/20261102_llm_config_x.sql\n")
    return tmp_path, main, a, b, r


def _ledger(wt, minutes_ago, fake):
    d = wt / ".claude" / "conductor" / "runs"; d.mkdir(parents=True, exist_ok=True)
    f = d / "run.md"; f.write_text("x")
    t = fake.now().timestamp() - minutes_ago * 60
    os.utime(f, (t, t))
    return f


def test_scan_skips_router_owned_and_main_checkout(fake, repo, table):
    tmp, main, a, b, r = repo
    out = scan(fake, main, BASE, table, set(), 45)
    assert sorted(out) == sorted([str(a), str(b)])


def test_changed_files_become_surfaces_and_weight(fake, repo, table):
    tmp, main, a, b, r = repo
    out = scan(fake, main, BASE, table, set(), 45)
    assert out[str(a)]["surfaces"] == ["her-reply-prompt"] and out[str(a)]["weight"] == "heavy"
    assert out[str(a)]["branch"] == "feat/her-attention-by-stage"
    assert out[str(b)]["surfaces"] == ["llm-config"] and out[str(b)]["weight"] == "light"


def test_activity_from_latest_ledger_mtime(fake, repo, table):
    tmp, main, a, b, r = repo
    _ledger(a, 10, fake)                     # 10 分钟前 → active + 持锁
    _ledger(b, 300, fake)                    # 5 小时前 → 不 active，但 24h 内仍持锁
    out = scan(fake, main, BASE, table, set(), 45)
    assert out[str(a)]["active"] is True and out[str(a)]["locking"] is True
    assert out[str(a)]["ledger"].endswith("run.md")
    assert out[str(b)]["active"] is False and out[str(b)]["locking"] is True


def test_long_idle_external_releases_lock(fake, repo, table):
    tmp, main, a, b, r = repo
    _ledger(a, 60 * 30, fake)                # 30 小时没动 → 不再持锁
    out = scan(fake, main, BASE, table, set(), 45)
    assert out[str(a)]["locking"] is False and out[str(a)]["active"] is False


def test_own_commit_counts_as_activity(fake, repo, table):
    tmp, main, a, b, r = repo
    fake.script(["git", "-C", str(b), "log", "-1", "--format=%ct", f"{BASE}..HEAD"],
                str(int(fake.now().timestamp()) - 120) + "\n")
    out = scan(fake, main, BASE, table, set(), 45)
    assert out[str(b)]["active"] is True and out[str(b)]["ledger"] is None


def test_external_taking_router_lock_is_reported(state_with):
    st = state_with(T2=("executing", ["her-reply-prompt"]), T3=("merged_staging", ["llm-config"]),
                    external={"/wt/x": ["her-reply-prompt", "llm-config"]})
    assert conflicts_with_router(st, st["external"]) == [("/wt/x", "her-reply-prompt", "T2")]


def test_uncommitted_file_mtime_counts_as_activity(fake, repo, table):
    tmp, main, a, b, r = repo
    f = a / "crates" / "persona-engine" / "src"; f.mkdir(parents=True)
    (f / "prompt_builder.rs").write_text("x")
    t = fake.now().timestamp() - 5 * 60
    os.utime(f / "prompt_builder.rs", (t, t))
    out = scan(fake, main, BASE, table, set(), 45)
    assert out[str(a)]["active"] is True and out[str(a)]["ledger"] is None


def test_planned_surfaces_from_ledger_text(fake, repo, table):
    tmp, main, a, b, r = repo
    f = _ledger(b, 10, fake)
    f.write_text("plan v3：改 `crates/persona-engine/src/prompt_builder.rs:691` 和 services/api-server/src/services/her_chat/turn.rs")
    os.utime(f, (fake.now().timestamp() - 600,) * 2)
    out = scan(fake, main, BASE, table, set(), 45)
    assert out[str(b)]["planned_surfaces"] == ["her-reply-prompt"]
    assert out[str(b)]["surfaces"] == ["her-reply-prompt", "llm-config"] and out[str(b)]["weight"] == "heavy"


def test_squash_merged_files_do_not_lock(fake, repo, table):
    tmp, main, a, b, r = repo
    _ledger(a, 10, fake)
    # 分支改过 turn.rs，但它和 staging 现在一模一样（已 squash 合入）
    fake.script(["git", "-C", str(a), "diff", "--name-only", BASE, "HEAD"], "crates/persona-engine/src/prompt_builder.rs\n")
    fake.script(["git", "-C", str(a), "status", "--porcelain"], "")
    out = scan(fake, main, BASE, table, set(), 45)
    assert out[str(a)]["surfaces"] == []


def test_branch_merged_by_pr_head_releases_locks(fake, repo, table):  # 分诊发现：合完的 session 还占锁
    tmp, main, a, b, r = repo
    _ledger(a, 10, fake)
    fake.script(["git", "-C", str(a), "rev-parse", "HEAD"], "5e846ee08019\n")
    fake.script(["git", "-C", str(a), "status", "--porcelain"], "?? .claude/conductor/runs/x.md\n")   # 只有 ledger 没提交
    fake.script(["gh", "pr", "list"], json.dumps([{"number": 941, "headRefOid": "5e846ee08019"}]))
    out = scan(fake, main, BASE, table, set(), 45, repo_slug="o/r")
    assert out[str(a)]["merged_pr"] == 941 and out[str(a)]["surfaces"] == [] and out[str(a)]["locking"] is False
    fake.script(["git", "-C", str(a), "status", "--porcelain"], " M crates/persona-engine/src/prompt_builder.rs\n")
    assert scan(fake, main, BASE, table, set(), 45, repo_slug="o/r")[str(a)]["locking"] is True   # 合完又在改 → 照样占锁
