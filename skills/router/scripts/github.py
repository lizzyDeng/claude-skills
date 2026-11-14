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
UID_KEYS = {"uid", "uids", "user_id", "user_uid"}
QUOTE_KEYS = {"quote", "quotes", "text"}
QUOTE_WINDOW = 20          # 连续 20 个字符和某条原话一致才算命中（短了会误伤引用 prompt 原文的正文）


class LeakBlocked(Exception):
    def __init__(self, hits):
        super().__init__(f"正文含用户 uid：{', '.join(hits)}")
        self.hits = hits


class GhError(RuntimeError):
    pass


class GuardEmpty(Exception):
    """配了 leak_guard_globs 却一个文件都没匹配到：闸已失效，宁可不建票。"""


class Guard:
    def __init__(self):
        self.uids, self.windows, self.files = set(), set(), 0


_CACHE = {}


def _norm(s: str) -> str:
    return " ".join(s.lower().split())


def _is_uid(v) -> bool:
    # 值里的十六进制串：要求 8 位且至少含一个字母，免得把日期、时间戳当成 uid
    return isinstance(v, str) and len(v) == 8 and bool(_HEX.match(v)) and any(c in "abcdefABCDEF" for c in v)


def _walk(o, key, g: Guard):
    if isinstance(o, dict):
        for k, v in o.items():
            if isinstance(k, str) and _HEX.match(k):
                g.uids.add(k.lower())
            _walk(v, k, g)
    elif isinstance(o, list):
        for v in o:
            _walk(v, key, g)
    elif isinstance(o, str):
        if (key in UID_KEYS and _HEX.match(o)) or _is_uid(o):
            g.uids.add(o.lower())
        elif key in QUOTE_KEYS:
            t = _norm(o)
            g.windows |= {t[i:i + QUOTE_WINDOW] for i in range(0, max(len(t) - QUOTE_WINDOW + 1, 0))}


def _load_file(path: str) -> Guard:
    st = os.stat(path)
    hit = _CACHE.get(path)
    if hit and hit[0] == st.st_mtime:
        return hit[1]
    g = Guard()
    text = open(path, encoding="utf-8", errors="replace").read()
    try:
        docs = [json.loads(text)]
    except ValueError:                                  # .jsonl：一行一个
        docs = []
        for line in text.splitlines():
            try:
                docs.append(json.loads(line))
            except ValueError:
                continue
    for d in docs:
        _walk(d, None, g)
    _CACHE[path] = (st.st_mtime, g)
    return g


def load_guard(globs) -> Guard:
    """递归收集周报数据里的 uid（dict 键 + uid 类字段的值 + 任何 8 位十六进制值）和用户原话。"""
    g = Guard()
    for pat in globs:
        for path in glob.glob(os.path.expanduser(pat), recursive=True):
            if not os.path.isfile(path):
                continue
            one = _load_file(path)
            g.uids |= one.uids
            g.windows |= one.windows
            g.files += 1
    if globs and g.files == 0:
        raise GuardEmpty(f"leak_guard_globs 一个文件都没匹配到：{globs}")
    return g


GH_S = 120                     # gh / graphql 弱网上界：超时 rc=124 → GhError


def _gh(ctx, args, timeout=GH_S) -> str:
    r = ctx.run(["gh", *args], timeout=timeout)
    if r.returncode != 0:
        raise GhError(f"gh {' '.join(args[:3])} 失败：{(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout


def leak_hits(text: str, guard) -> list:
    """guard 可以是 Guard，也可以是 uid 集合。命中的 uid 原样返回；原话命中返回「原话：<前 12 字>…」。"""
    low = text.lower()
    uids = guard.uids if isinstance(guard, Guard) else set(guard)
    hits = sorted(u for u in uids if u in low)
    if isinstance(guard, Guard) and guard.windows:
        t = _norm(text)
        for i in range(0, max(len(t) - QUOTE_WINDOW + 1, 0)):
            w = t[i:i + QUOTE_WINDOW]
            if w in guard.windows:
                hits.append(f"原话：{w[:12]}…")
                break
    return hits


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
    """业务票：business_issue_body（含「当前的墙和备注」，之后由巡检同步）；其余票：建票时写一次的三段正文。"""
    if t.get("kind") == "business":
        return business_issue_body(map_no, t)
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


WALL_STATUS = {"open": "没拆", "broken": "已拆"}


def business_issue_body(map_no: int, t: dict) -> str:
    """业务票的 issue 正文：只由票上相对稳定的业务字段算出来（不含时间戳、不含方向卡），
    墙 / 备注 / 标题 / 并进来的老票一变，哈希就变，巡检据此 gh issue edit（tick.Run.step5b_issue_sync）。"""
    import store
    m = t.get("metric") or {}
    m = m if isinstance(m, dict) else {"name": str(m)}
    mtxt = m.get("name", "") + "".join(f"（{k}：{m[k]}）" for k in ("now", "direction") if m.get(k) not in (None, ""))
    walls = []
    for w in t.get("walls") or []:
        tail = f" · 占流失 {store.wall_share(w)}" if store.wall_share(w) else ""
        walls.append(f"- {w.get('id', '')}（{WALL_STATUS.get(w.get('status'), w.get('status') or '')}）"
                     f"{w.get('text', '')}{tail}")
        walls += [f"  - 症状：{s}" for s in store.wall_symptoms(w)]
    notes = t.get("notes") or []
    notes = [notes] if isinstance(notes, str) else [str(x) for x in notes if str(x).strip()]
    lines = [f"Part of #{map_no}", "",
             "## 为什么派",
             f"- 指标：{mtxt}",
             f"- 人群：{t.get('segment', '')}",
             f"- 证据（本地路径）：{'；'.join(t.get('evidence') or [])}", "",
             "## 解决什么问题",
             f"{t.get('segment', '')}：{t.get('want', '')}", "",
             "## 当前的墙和备注",
             "墙是根因，不是方案；按用户路径先后排，一次尝试对准第一道没拆的墙。",
             *(walls or ["- （无）"])]
    if notes:
        lines += ["", "备注（影响面不到 10%、不成墙，或还说不清根因）：", *[f"- {x}" for x in notes]]
    if t.get("absorbs"):
        lines += ["", f"并进来的老票：{'、'.join(t['absorbs'])}"]
    lines += ["", "解法由 worker 的方向卡提出、用户确认后才定。由 /router 派发；墙和备注由 /router 自动同步。"]
    return "\n".join(lines)


def body_hash(title: str, body: str) -> str:
    """issue 标题 + 正文的哈希：票上存一份，巡检比对，变了才 gh issue edit。"""
    import hashlib
    return hashlib.sha256(f"{title}\0{body}".encode("utf-8")).hexdigest()[:16]


def edit_issue(ctx, repo, n, body, guard, title=None) -> None:
    """改 issue 正文（title 给了就连标题一起改）。先过防泄漏闸：命中抛 LeakBlocked，一个字都不发。"""
    _guard(((title or "") + "\n" + body), guard)
    _gh(ctx, ["issue", "edit", str(n), "-R", repo, *(["--title", title] if title is not None else []), "--body", body])


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


def pr_for_branch(ctx, repo, branch, base=None):
    out = _gh(ctx, ["pr", "list", "-R", repo, "--head", branch, "--state", "open",
                    *(["--base", base] if base else []), "--json", "number", "--limit", "1"])
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
    if unfinished:
        return "pending"
    return "success" if "SUCCESS" in finished else "none"    # 全是 SKIPPED / NEUTRAL = 没有真跑过的检查


def pr_status(ctx, repo, pr) -> dict:
    d = json.loads(_gh(ctx, ["pr", "view", str(pr), "-R", repo, "--json",
                             "state,mergedAt,mergeCommit,headRefOid,baseRefName,mergeable,statusCheckRollup"]))
    return {"state": d.get("state"), "merged": d.get("state") == "MERGED",
            "checks": _classify(d.get("statusCheckRollup") or []),
            "merge_sha": (d.get("mergeCommit") or {}).get("oid"),
            "head": d.get("headRefOid"), "base": d.get("baseRefName"), "mergeable": d.get("mergeable")}


def pr_text(ctx, repo, pr) -> str:
    """PR 标题 + 正文 + diff：合并前再过一遍防泄漏闸。"""
    d = json.loads(_gh(ctx, ["pr", "view", str(pr), "-R", repo, "--json", "title,body"]) or "{}")
    return "\n".join([d.get("title") or "", d.get("body") or "", _gh(ctx, ["pr", "diff", str(pr), "-R", repo])])


PROTECTED_BASES = {"main", "master"}


def merge_if_green(ctx, repo, pr, base=None, main=None) -> bool:
    """🔴 base 不对（例如 worker 漏写 --base，PR 指向 main）绝不合：合进 main = 上 prod，归用户。
    硬拦（RV M-7）：要合进的 base（传进来的，或 PR 自己的）是 main / master / 仓库默认分支 main ⇒ 一律不合，
    配置写错也一样；传进来的 base 不对时连 gh 都不调。"""
    guarded = PROTECTED_BASES | ({main} if main else set())
    if base is not None and (not base or base in guarded):
        return False
    st = pr_status(ctx, repo, pr)
    if (st["base"] or "") in guarded:
        return False
    if base and st["base"] != base:
        return False
    if st["merged"]:
        return True
    if st["state"] != "OPEN" or st["checks"] != "success" or not st["head"]:
        return False
    r = ctx.run(["gh", "pr", "merge", str(pr), "--squash", "--match-head-commit", st["head"], "-R", repo])
    return r.returncode == 0


def comment(ctx, repo, issue, body, uids=()) -> None:
    _guard(body, uids)
    _gh(ctx, ["issue", "comment", str(issue), "-R", repo, "--body", body])


# ---- R1：被 GitHub 自动改指的 PR ----------------------------------------------

_TIMELINE_Q = """query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){
pullRequest(number:$number){timelineItems(last:20,itemTypes:[AUTOMATIC_BASE_CHANGE_SUCCEEDED_EVENT,BASE_REF_CHANGED_EVENT]){
nodes{__typename ... on AutomaticBaseChangeSucceededEvent{oldBase newBase createdAt}
... on BaseRefChangedEvent{previousRefName currentRefName createdAt actor{login}}}}}}}"""


def last_base_change(ctx, repo, pr):
    """PR 时间线里最近一次改 base 的事件（GitHub 自动 / 人手）；没有 → {}；查不了 → None。"""
    owner, _, name = repo.partition("/")
    try:
        out = _gh(ctx, ["api", "graphql", "-f", f"query={_TIMELINE_Q}", "-f", f"owner={owner}",
                        "-f", f"name={name}", "-F", f"number={int(pr)}"])
        nodes = json.loads(out)["data"]["repository"]["pullRequest"]["timelineItems"]["nodes"]
    except (GhError, ValueError, KeyError, TypeError):
        return None
    nodes = [n for n in nodes or [] if isinstance(n, dict)]
    return nodes[-1] if nodes else {}


def is_auto_retarget(ev, base) -> bool:
    return bool(ev) and ev.get("__typename") == "AutomaticBaseChangeSucceededEvent" and ev.get("oldBase") == base


def auto_retargeted(ctx, repo, pr, base):
    """最近一次改 base 是 GitHub 自动把它从 <base> 改走的（base 分支被删）→ True；
    最近一次是人手改的、或从没改过 → False（人手改过的不碰）；查不了 → None（走原来的 stuck，绝不合）。"""
    ev = last_base_change(ctx, repo, pr)
    return None if ev is None else is_auto_retarget(ev, base)


def retarget_pr(ctx, repo, pr, base) -> None:
    _gh(ctx, ["pr", "edit", str(pr), "-R", repo, "--base", base])


# ---- R6：逐目标幂等留言 / 关 issue ---------------------------------------------

AUTO_SIGNATURE = "由 /router 自动留言"


def comment_once(ctx, repo, n, body, guard, marker, legacy=None) -> dict:
    """先查后做：issue 上已有带 marker 的评论（或旧格式的 router 留言）就不再留。投递语义「至少一次」，
    靠 marker 去重做到「恰好一次」。→ {"result": "commented"|"already", "state": issue 状态}。
    legacy：旧格式正文的正则，用 re.match 锚定评论开头，并要求带 router 签名（引用块、人手写的都不算）。"""
    view = json.loads(_gh(ctx, ["issue", "view", str(n), "-R", repo, "--json", "state,comments"]) or "{}")
    bodies = [c.get("body") or "" for c in view.get("comments") or [] if isinstance(c, dict)]
    state = view.get("state")
    if any(marker in b for b in bodies) or (
            legacy and any(re.match(legacy, b) and AUTO_SIGNATURE in b for b in bodies)):
        return {"result": "already", "state": state}
    text = f"{body}\n\n{marker}"
    _guard(text, guard)
    _gh(ctx, ["issue", "comment", str(n), "-R", repo, "--body", text])
    return {"result": "commented", "state": state}


def legacy_downstream_re(tid) -> str:
    """修复前 _notify_downstream 的正文开头「上游 T1 #944「…」：T1 不会命中 T10（后面必须是 " #" 或「）。"""
    return rf"上游 {re.escape(tid)}(?: #\d+)?「"


def close_issue(ctx, repo, n, body, guard, marker) -> dict:
    """留一条收尾评论（marker 去重）再关。不用 `issue close --comment`：它在 gh 内部也是先留言再关的两次调用，
    关失败时下次会重复留言。已关的不再关。→ {"comment": …, "closed": "closed"|"already"}"""
    c = comment_once(ctx, repo, n, body, guard, marker)
    if (c.get("state") or "").upper() == "CLOSED":
        return {"comment": c["result"], "closed": "already"}
    _gh(ctx, ["issue", "close", str(n), "-R", repo, "--reason", "completed"])
    return {"comment": c["result"], "closed": "closed"}
