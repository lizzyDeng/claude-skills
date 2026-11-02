"""gh 适配：建票 / 挂 sub-issue / 依赖 / PR 状态 / 合并，外加防泄漏闸。

🔴 GitHub 正文里不许出现用户 uid 或用户原话（spec §10）。建票、留言前都过 leak_hits，
   命中就拒绝，不建、不发。
🔴 合并只在 CI 检查非空且全部通过时做，且不用 --auto：staging 没有必需检查，--auto 会立刻合。
sub-issue / blocked_by 吃的是 issue 的数据库 id，不是 #number（aifriends docs/agents/issue-tracker.md）。
"""
import glob
import json
import os
import re

MAP_LABEL = "wayfinder:map"
TICKET_LABEL = "wayfinder:task"
_HEX = re.compile(r"^[0-9a-fA-F]{8,}$")
OK_CONCLUSIONS = {"SUCCESS", "NEUTRAL", "SKIPPED"}


class LeakBlocked(Exception):
    def __init__(self, hits):
        super().__init__(f"正文含用户 uid：{', '.join(hits)}")
        self.hits = hits


class GhError(RuntimeError):
    pass


def _gh(ctx, args) -> str:
    r = ctx.run(["gh", *args])
    if r.returncode != 0:
        raise GhError(f"gh {' '.join(args[:3])} 失败：{(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout


def load_guard_uids(globs) -> set:
    uids = set()
    for g in globs:
        for path in glob.glob(os.path.expanduser(g)):
            try:
                data = json.load(open(path, encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict):
                uids |= {k.lower() for k in data if isinstance(k, str) and _HEX.match(k)}
    return uids


def leak_hits(text: str, uids) -> list:
    low = text.lower()
    return sorted(u for u in uids if u in low)


def _guard(text, uids):
    hits = leak_hits(text, uids)
    if hits:
        raise LeakBlocked(hits)


def _number_from_url(out: str) -> int:
    m = re.search(r"/issues/(\d+)", out)
    if not m:
        raise GhError(f"gh issue create 没返回 issue 链接：{out.strip()[:200]}")
    return int(m.group(1))


def _db_id(ctx, repo, number) -> str:
    return _gh(ctx, ["api", f"repos/{repo}/issues/{number}", "--jq", ".id"]).strip()


def ensure_map(ctx, repo, title) -> int:
    out = _gh(ctx, ["issue", "list", "-R", repo, "--label", MAP_LABEL, "--state", "open",
                    "--search", title, "--json", "number,title", "--limit", "100"])
    for it in json.loads(out or "[]"):
        if it["title"] == title:
            return it["number"]
    body = "router 的派发地图：每张派出去的票都挂在这里（sub-issue）。由 /router 维护。"
    return _number_from_url(_gh(ctx, ["issue", "create", "-R", repo, "--title", title,
                                      "--label", MAP_LABEL, "--body", body]))


def issue_body(map_no: int, t: dict) -> str:
    why, sol = t.get("why", {}), t.get("solution", {})
    return "\n".join([
        f"Part of #{map_no}", "",
        "## 为什么派",
        f"- 指标：{why.get('metric', '')}",
        f"- 人数：{why.get('users', '')}",
        f"- 证据（本地路径）：{why.get('evidence', '')}", "",
        "## 解决什么问题",
        t.get("problem", ""),
        f"- 锚点：{t.get('problem_anchor', '')}", "",
        "## router 线索（非方案）",
        sol.get("text", "") or "（无）",
        "", "解法由 worker 的方向卡提出、用户确认后才定。由 /router 派发。"])


def create_ticket_issue(ctx, repo, map_no: int, ticket: dict, uids) -> int:
    body = issue_body(map_no, ticket)
    _guard(ticket["title"] + "\n" + body, uids)
    n = _number_from_url(_gh(ctx, ["issue", "create", "-R", repo, "--title", ticket["title"],
                                   "--label", TICKET_LABEL, "--body", body]))
    _gh(ctx, ["api", "--method", "POST", f"repos/{repo}/issues/{map_no}/sub_issues",
              "-F", f"sub_issue_id={_db_id(ctx, repo, n)}"])
    return n


def add_blocked_by(ctx, repo, child_no, blocker_no) -> None:
    _gh(ctx, ["api", "--method", "POST", f"repos/{repo}/issues/{child_no}/dependencies/blocked_by",
              "-F", f"issue_id={_db_id(ctx, repo, blocker_no)}"])


def pr_for_branch(ctx, repo, branch):
    out = _gh(ctx, ["pr", "list", "-R", repo, "--head", branch, "--state", "all",
                    "--json", "number", "--limit", "1"])
    items = json.loads(out or "[]")
    return items[0]["number"] if items else None


def _classify(rollup) -> str:
    if not rollup:
        return "none"
    finished, unfinished = [], 0
    for c in rollup:
        if c.get("__typename") == "StatusContext" or "state" in c and "status" not in c:
            st = (c.get("state") or "").upper()
            if st in ("PENDING", "EXPECTED", ""):
                unfinished += 1
            else:
                finished.append(st)
        else:
            if (c.get("status") or "").upper() != "COMPLETED":
                unfinished += 1
            else:
                finished.append((c.get("conclusion") or "").upper())
    if any(x not in OK_CONCLUSIONS for x in finished):
        return "failure"                      # 已有失败就报失败（比 pending 早一步让 router 知道）
    return "pending" if unfinished else "success"


def pr_status(ctx, repo, pr) -> dict:
    d = json.loads(_gh(ctx, ["pr", "view", str(pr), "-R", repo, "--json",
                             "state,mergedAt,mergeCommit,headRefOid,statusCheckRollup"]))
    return {"state": d.get("state"), "merged": d.get("state") == "MERGED",
            "checks": _classify(d.get("statusCheckRollup") or []),
            "merge_sha": (d.get("mergeCommit") or {}).get("oid"),
            "head": d.get("headRefOid")}


def merge_if_green(ctx, repo, pr) -> bool:
    st = pr_status(ctx, repo, pr)
    if st["merged"]:
        return True
    if st["state"] != "OPEN" or st["checks"] != "success" or not st["head"]:
        return False
    r = ctx.run(["gh", "pr", "merge", str(pr), "--squash", "--match-head-commit", st["head"], "-R", repo])
    return r.returncode == 0


def comment(ctx, repo, issue, body, uids=()) -> None:
    _guard(body, uids)
    _gh(ctx, ["issue", "comment", str(issue), "-R", repo, "--body", body])
