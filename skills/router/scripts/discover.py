"""认出用户自己开着的 session（外部 worktree），登记它们正在改的代码面。

router 只登记、只转交信息，不给它们下指令（spec §10）。
活动时间 = max(conductor ledger mtime, 分支自己的最新 commit 时间, 未提交改动文件的 mtime)：
  active  = 活动在 stale_min 分钟内 → 占重票名额
  locking = 活动在 lock_hours 小时内 → 持锁（等用户回答的 session 不该丢锁；
            squash 合并后留下的陈旧 worktree 也不该永久占锁）
判活不看 lsof（memory：lsof 空 ≠ 死）。

代码面 = 三部分的并集：
  - 已提交：分支自分叉点起改过、且至今仍与 base 不同的文件（squash 合进 base 后自动不再占锁）
  - 未提交改动
  - 计划要改：近期 conductor ledger / plan 里写到的文件路径（还在出方案、没动代码的 session 也要先占上）
    只认本 session 新建或改过的 ledger：仓里已提交、原样检出的旧 ledger 不算（新 worktree 检出时 mtime 全是新的）
分支 tip == 某个已合并 PR 的 head（squash 后判已合的唯一可靠办法），且没有 .claude/ 以外的未提交改动
⇒ 这个 session 的活已经合进去了：不占锁、不占重票名额。
"""
import json
import re
import time
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


_PATH = re.compile(r"[\w.~/-]*/[\w.-]+\.(?:rs|ts|tsx|js|py|sql|json|toml|sh)\b")


def _planned_files(ledgers, since_ts):
    files = set()
    for p in ledgers:
        if p.stat().st_mtime >= since_ts:
            files |= set(_PATH.findall(p.read_text(encoding="utf-8", errors="replace")))
    return files


_MERGED = {}                     # (branch, head) → (查询时间, PR 号或 None)；合过的结果不会变，没合的 10 分钟后重查


def _merged_pr(ctx, repo_slug, path, branch):
    head = ctx.run(["git", "-C", path, "rev-parse", "HEAD"]).stdout.strip()
    if not head or not branch:
        return None
    hit = _MERGED.get((branch, head))
    if hit and (hit[1] or time.time() - hit[0] < 600):
        return hit[1]
    r = ctx.run(["gh", "pr", "list", "-R", repo_slug, "--head", branch, "--state", "merged",
                 "--json", "number,headRefOid", "--limit", "5"])
    try:
        prs = json.loads(r.stdout or "[]") if r.returncode == 0 else []
    except ValueError:
        prs = []
    num = next((p["number"] for p in prs if p.get("headRefOid", "").startswith(head) or head.startswith(p.get("headRefOid") or "-")), None)
    _MERGED[(branch, head)] = (time.time(), num)
    return num


def _iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if ts else None


def scan(ctx, repo: Path, base_ref: str, table, router_branches: set, stale_min: int,
         lock_hours: int = 24, repo_slug: str = None) -> dict:
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
        since_fork = set(ctx.run(["git", "-C", path, "diff", "--name-only", f"{base_ref}...HEAD"]).stdout.split())
        still_differs = set(ctx.run(["git", "-C", path, "diff", "--name-only", base_ref, "HEAD"]).stdout.split())
        files = since_fork & still_differs
        dirty = set(_status_files(ctx.run(["git", "-C", path, "status", "--porcelain"]).stdout))
        files |= dirty
        dirty_ts = max([Path(path, f).stat().st_mtime for f in dirty if Path(path, f).is_file()] or [0])
        # 仓里已提交、本 session 没动过的 ledger 是建 worktree 时一起检出的（mtime = 检出时间），不是它的计划
        checked_out = set(ctx.run(["git", "-C", path, "-c", "core.quotePath=false", "ls-files", "--",
                                   ".claude/conductor/runs"]).stdout.splitlines()) - files
        ledgers = sorted((p for p in Path(path, ".claude", "conductor", "runs").glob("*.md")
                          if str(p.relative_to(path)) not in checked_out),
                         key=lambda p: p.stat().st_mtime)
        ledger_ts = ledgers[-1].stat().st_mtime if ledgers else None
        commit = ctx.run(["git", "-C", path, "log", "-1", "--format=%ct", f"{base_ref}..HEAD"]).stdout.strip()
        commit_ts = int(commit) if commit.isdigit() else None
        last = max([t for t in (ledger_ts, commit_ts, dirty_ts) if t] or [0])
        planned = surf_mod.surfaces_for_files(sorted(_planned_files(ledgers, now - lock_hours * 3600)), table)
        surfs = surf_mod.surfaces_for_files(sorted(files), table) | planned
        merged_pr = _merged_pr(ctx, repo_slug, path, branch) if repo_slug else None
        real_dirty = [f for f in dirty if not f.startswith(".claude/")]
        if merged_pr and not real_dirty:
            surfs, planned = set(), set()
        found[path] = {
            "branch": branch,
            "ledger": str(ledgers[-1]) if ledgers else None,
            "ledger_mtime": _iso(ledger_ts),
            "last_activity": _iso(last),
            "active": bool(last) and now - last <= stale_min * 60,
            "locking": bool(last) and now - last <= lock_hours * 3600 and bool(surfs),
            "merged_pr": merged_pr,
            "surfaces": sorted(surfs),
            "planned_surfaces": sorted(planned),
            "weight": surf_mod.weight_for(surfs, table),
        }
    return found


def conflicts_with_router(state, external) -> list:
    """外部 session 动了 router 票锁着的代码面 → [(worktree, surface, 持锁票号)]。"""
    owners = {}
    for tid, t in state["tickets"].items():
        if store.holds_lock(t):
            for s in t["surfaces"]:
                owners.setdefault(s, tid)
    return [(p, s, owners[s]) for p, e in sorted(external.items()) if e.get("locking", True)
            for s in e.get("surfaces", []) if s in owners]
