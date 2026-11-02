"""认出用户自己开着的 session（外部 worktree），登记它们正在改的代码面。

router 只登记、只转交信息，不给它们下指令（spec §10）。
活动时间 = max(conductor ledger mtime, 分支自己的最新 commit 时间, 未提交改动文件的 mtime)：
  active  = 活动在 stale_min 分钟内 → 占重票名额
  locking = 活动在 lock_hours 小时内 → 持锁（等用户回答的 session 不该丢锁；
            squash 合并后留下的陈旧 worktree 也不该永久占锁）
判活不看 lsof（memory：lsof 空 ≠ 死）。
"""
from datetime import datetime, timezone
from pathlib import Path

import store
import surfaces as surf_mod


def _worktrees(porcelain: str):
    block = {}
    for line in porcelain.splitlines() + [""]:
        if not line.strip():
            if block:
                yield block
            block = {}
            continue
        key, _, val = line.partition(" ")
        block[key] = val


def _status_files(out: str):
    for line in out.splitlines():
        if len(line) < 4:
            continue
        path = line[3:]
        yield path.split(" -> ")[-1].strip('"')


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ts else None


def scan(ctx, repo: Path, base_ref: str, table, router_branches: set, stale_min: int,
         lock_hours: int = 24) -> dict:
    out = ctx.run(["git", "-C", str(repo), "worktree", "list", "--porcelain"]).stdout
    blocks = list(_worktrees(out))
    now = ctx.now().timestamp()
    found = {}
    for i, b in enumerate(blocks):
        path = b.get("worktree")
        branch = b.get("branch", "").removeprefix("refs/heads/")
        if i == 0 or not path or "prunable" in b or not Path(path).exists():
            continue                                  # 主仓 checkout / 已失效的 worktree
        if branch.startswith("router/") or branch in router_branches:
            continue
        files = set(ctx.run(["git", "-C", path, "diff", "--name-only", f"{base_ref}...HEAD"]).stdout.split())
        dirty = set(_status_files(ctx.run(["git", "-C", path, "status", "--porcelain"]).stdout))
        files |= dirty
        dirty_ts = max([Path(path, f).stat().st_mtime for f in dirty if Path(path, f).is_file()] or [0])
        ledgers = sorted(Path(path, ".claude", "conductor", "runs").glob("*.md"),
                         key=lambda p: p.stat().st_mtime)
        ledger_ts = ledgers[-1].stat().st_mtime if ledgers else None
        commit = ctx.run(["git", "-C", path, "log", "-1", "--format=%ct", f"{base_ref}..HEAD"]).stdout.strip()
        commit_ts = int(commit) if commit.isdigit() else None
        last = max([t for t in (ledger_ts, commit_ts, dirty_ts) if t] or [0])
        surfs = surf_mod.surfaces_for_files(sorted(files), table)
        found[path] = {
            "branch": branch,
            "ledger": str(ledgers[-1]) if ledgers else None,
            "ledger_mtime": _iso(ledger_ts),
            "last_activity": _iso(last),
            "active": bool(last) and now - last <= stale_min * 60,
            "locking": bool(last) and now - last <= lock_hours * 3600,
            "surfaces": sorted(surfs),
            "weight": surf_mod.weight_for(surfs, table),
        }
    return found


def conflicts_with_router(state, external) -> list:
    """外部 session 动了 router 票锁着的代码面 → [(worktree, surface, 持锁票号)]。"""
    owners = {}
    for tid, t in state["tickets"].items():
        if t["status"] in store.LOCKING:
            for s in t["surfaces"]:
                owners.setdefault(s, tid)
    return [(p, s, owners[s]) for p, e in sorted(external.items()) if e.get("locking", True)
            for s in e.get("surfaces", []) if s in owners]
