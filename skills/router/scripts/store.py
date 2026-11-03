"""router 的持久状态：state.json（票 / 队列 / 游标）与 decisions.md（router 替用户做的决定）。

写一律走「临时文件 + os.replace」：router 随时可能被压缩或崩溃，半截文件比没有文件更糟。
读到坏文件就拒绝启动并留备份——用空状态覆盖等于把全部在跑的票丢掉。
"""
import fcntl
import json
import os
import re
import shutil
import tempfile
import time
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path

DRAFT = "draft"
QUEUED = "queued"
KICKOFF = "kickoff"
AWAIT_CARD = "await_card"
EXECUTING = "executing"
VERIFYING = "verifying"
MERGED = "merged_staging"
AWAIT_CHECK = "await_check"        # 仓外票改完、等用户核对：仓外没有 staging 隔离层，核对前持锁、不解依赖
AWAIT_PROD = "await_prod"
DONE = "done"
PAUSED = "paused"
TAKEOVER = "takeover"
STUCK = "stuck"
HOLD = "hold"
SKIP = "skip"

STATUSES = {DRAFT, QUEUED, KICKOFF, AWAIT_CARD, EXECUTING, VERIFYING, MERGED, AWAIT_CHECK, AWAIT_PROD,
            DONE, PAUSED, TAKEOVER, STUCK, HOLD, SKIP}
RUNNING = {KICKOFF, EXECUTING, VERIFYING}                                  # 占名额
LOCKING = RUNNING | {AWAIT_CARD, PAUSED, TAKEOVER, STUCK, AWAIT_CHECK}      # 持锁
SETTLED = {MERGED, AWAIT_PROD, DONE}                                        # 对下游算已解锁

QUEUE_KINDS = {"start", "card", "spend", "prod", "handson", "stuck", "conflict", "direction"}
# 决定单项的 topic（可选）：同一 kind 下按 topic 分派回答（router.cmd_answer）、看板文案
QUEUE_TOPICS = {"base", "retarget", "dispatch", "offrepo"}

# set_status 不传 waiting_for 时按状态给的默认值（R5：续跑路径不再把「在等什么」清空）
DEFAULT_WAIT = {KICKOFF: "写方向卡", EXECUTING: "执行中", VERIFYING: "等 CI", AWAIT_CARD: "等你确认方向卡",
                AWAIT_CHECK: "等你核对仓外改动", AWAIT_PROD: "等上 prod"}
# topic=base 的项挡住的票：还没走完的（已合 / 等上 prod / 完成 / 暂缓 / 不派 的不算）
_BASE_FREE = {MERGED, AWAIT_PROD, DONE, HOLD, SKIP}


class StateCorrupt(Exception):
    pass


def pending_intent(t) -> bool:
    """上次派单 / 续跑落了意图、还没核实（dispatch.phase == "spawning"）：worker 可能已经在跑。"""
    return isinstance(t, dict) and (t.get("dispatch") or {}).get("phase") == "spawning"


def empty_state():
    return {"version": 1,
            "batch": {"id": None, "started": False, "started_at": None},
            "seq": {"ticket": 0, "decision": 0, "queue": 0},
            "tickets": {}, "external": {}, "queue": [],
            "cursors": {"events": {}, "inbox": {}}}


def _ticket_defaults():
    return {"title": "", "issue": None, "status": DRAFT, "weight": "light", "origin": "weekly",
            "batch": None, "start_ok": False, "priority": 100,
            "why": {"metric": "", "users": "", "evidence": ""},
            "problem": "", "problem_anchor": "",
            "solution": {"kind": "clue", "text": "", "source": ""},
            "surfaces": [], "tags": [], "blocked_by": [], "waiting_for": "", "review_flag": False,
            "worker": {"session_id": None, "pid": None, "worktree": None, "branch": None,
                       "log": None, "started_at": None, "kickoff_rounds": 0, "runs": 0},
            "pr": None, "stops": 0, "fail_streak": {"step": "", "count": 0}, "history": []}


def _atomic_write(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


@contextmanager
def locked(home: Path):
    """<home>/.lock 上的排他锁：后台 tick 与前台命令不会互相覆盖 state.json。不可嵌套。"""
    Path(home).mkdir(parents=True, exist_ok=True)
    with open(Path(home) / ".lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def load_state(path: Path) -> dict:
    path = Path(path)
    if not path.exists():
        return empty_state()
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(state, dict):
            raise ValueError("顶层不是对象")
    except ValueError as e:
        backup = path.with_name(f"{path.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}")
        shutil.copy2(path, backup)
        raise StateCorrupt(f"{path} 解析失败（{e}），已备份到 {backup}；原文件未动") from e
    base = empty_state()
    for k, v in base.items():                    # 老文件缺字段 → 补默认，不覆盖已有
        if isinstance(v, dict):
            state.setdefault(k, {})
            for kk, vv in v.items():
                state[k].setdefault(kk, vv)
        else:
            state.setdefault(k, v)
    _migrate(state)
    return state


def _migrate(state: dict) -> None:
    """读时迁移，幂等，只改状态：
    旧 _settle 把仓外票（显式 merge_sha=None）也标成 merged_staging → await_check。
    不写任何「已通知 / 已收尾」标记：下游通知、关 issue 由核对之后的收尾逐目标先查后做。
    判据：只有旧的仓外 _settle 会显式写 None；仓内路径总是 sha 或 "?"。"""
    for t in state.get("tickets", {}).values():
        if isinstance(t, dict) and t.get("status") == MERGED and "merge_sha" in t and t["merge_sha"] is None:
            t["status"] = AWAIT_CHECK


def save_state(path: Path, state: dict) -> None:
    _atomic_write(path, json.dumps(state, ensure_ascii=False, indent=2) + "\n")


def new_ticket(state: dict, **fields) -> str:
    state["seq"]["ticket"] += 1
    tid = f"T{state['seq']['ticket']}"
    t = _ticket_defaults()
    for k, v in fields.items():
        t[k] = {**t[k], **v} if isinstance(t.get(k), dict) and isinstance(v, dict) else v
    t["id"] = tid
    state["tickets"][tid] = t
    return tid


def set_status(state: dict, tid: str, status: str, at: str, waiting_for: str = None) -> None:
    """waiting_for 不传（None）⇒ 按状态取 DEFAULT_WAIT；显式传 "" 仍可清空。"""
    if status not in STATUSES:
        raise ValueError(f"未知状态 {status}")
    t = state["tickets"][tid]
    t["status"] = status
    t["waiting_for"] = DEFAULT_WAIT.get(status, "") if waiting_for is None else waiting_for
    t["history"].append({"at": at, "status": status})


def push_queue(state, ticket, kind, summary, at, topic=None) -> str:
    """「挡几张」「几张」不存：读时由 queue_view 现算（R3）。notified_at=None ⇒ 下一轮 tick 推 Lark（R4）。"""
    if kind not in QUEUE_KINDS:
        raise ValueError(f"未知队列类型 {kind}")
    if topic is not None and topic not in QUEUE_TOPICS:
        raise ValueError(f"未知队列 topic {topic}")
    state["seq"]["queue"] += 1
    qid = f"Q{state['seq']['queue']}"
    item = {"id": qid, "ticket": ticket, "kind": kind, "summary": summary, "created_at": at, "notified_at": None}
    if topic:
        item["topic"] = topic
    state["queue"].append(item)
    return qid


# ---- 仓外票收尾（R6）：closeout = {"downstream": {票号: {"issue", "state"}}, "issue", "worktree", "branch"} ----
# 每栏 "pending"（下一轮 tick 做）| "done" | "n/a"（没有 issue）| "kept: 原因"（有意保留，要人处理）
CLOSEOUT_COLS = ("issue", "worktree", "branch")


def closeout_open(co) -> dict:
    """还没做完的栏 → {"downstream": [票号…], "cols": [栏名…]}；kept 不算没做完（要人处理，不再重试）。"""
    if not isinstance(co, dict):
        return {"downstream": [], "cols": []}
    down = [oid for oid, d in (co.get("downstream") or {}).items() if isinstance(d, dict) and d.get("state") == "pending"]
    return {"downstream": down, "cols": [c for c in CLOSEOUT_COLS if co.get(c) == "pending"]}


def closeout_pending(co) -> bool:
    o = closeout_open(co)
    return bool(o["downstream"] or o["cols"])


def in_queue(state, qid) -> bool:
    return bool(qid) and any(q["id"] == qid for q in state["queue"])


def _draft_count(state) -> int:
    batch = state.get("batch", {}).get("id")
    return sum(1 for t in state["tickets"].values()
               if t.get("batch") == batch and t.get("origin", "weekly") == "weekly" and t["status"] == DRAFT)


def queue_blocks(state, q) -> int:
    """这一项挡住了几张票（读时现算，不存）。被挡的票按票号集合去重：同一张票可能既依赖本票、又共享代码面。
    - start：本批 weekly 草稿张数；prod：等上 prod 的张数；topic=base：还没走完的票数
    - 带票号的项：本票自己（没完成 / 没放弃就算 1 张）+ 被它挡住的其它票：
      本票没 settle ⇒ 依赖它的；本票持锁 ⇒ 排队等它代码面的"""
    tickets = state["tickets"]
    if q.get("kind") == "start":
        return _draft_count(state)
    if q.get("kind") == "prod":
        return sum(1 for t in tickets.values() if t["status"] == AWAIT_PROD)
    if q.get("topic") == "base":
        return sum(1 for t in tickets.values() if t["status"] not in _BASE_FREE)
    t = tickets.get(q.get("ticket") or "")
    if not t:
        return 0
    tid, mine = t["id"], set(t.get("surfaces", []))
    blocked = {o["id"] for o in tickets.values()
               if o["id"] != tid and o["status"] not in SETTLED | {SKIP}
               and ((t["status"] not in SETTLED and tid in o.get("blocked_by", []))
                    or (t["status"] in LOCKING and o["status"] == QUEUED and mine & set(o.get("surfaces", []))))}
    return (1 if t["status"] not in (DONE, SKIP) else 0) + len(blocked)


def queue_view(state) -> list:
    """决定单的读视图：每项浅拷贝 + 现算 blocks；start 项的「几张」按当前草稿重写；按 (-blocks, 序号) 排。
    只读：不改 state 本身（status 这类只读命令不存盘）。"""
    out = []
    for q in state["queue"]:
        v = dict(q)
        v["blocks"] = queue_blocks(state, q)
        if q.get("kind") == "start":
            v["summary"] = f"派发单已备好（{v['blocks']} 张）"
        out.append(v)
    return sorted(out, key=lambda v: (-v["blocks"], int(v["id"][1:])))


def pop_queue(state, qid) -> dict:
    for i, q in enumerate(state["queue"]):
        if q["id"] == qid:
            return state["queue"].pop(i)
    raise KeyError(qid)


# ---- decisions.md ----------------------------------------------------------

_DEC = re.compile(r"^- (D\d+) \| (.*?) \| (.*?) \| (.*?) \| 理由：(.*?) \| 依据：(.*)$")
_REV = re.compile(r"^- (D\d+) 已撤回 \| (.*?) \| (.*)$")


def _clean(s) -> str:
    return " ".join(str(s).split("\n")).replace("|", "｜")


def _read_lines(path: Path) -> list:
    path = Path(path)
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []


def _append_line(path: Path, line: str) -> None:
    lines = _read_lines(path)
    _atomic_write(path, "\n".join(lines + [line]) + "\n")


def _key_tag(key) -> str:
    return f"[key:{_clean(key)}]"


def decision_with_key(path: Path, key: str):
    """带 [key:<key>] 的那条决定的编号；没有就 None。"""
    tag = _key_tag(key)
    for line in _read_lines(path):
        m = _DEC.match(line)
        if m and tag in m.group(6):
            return m.group(1)
    return None


def append_decision(path: Path, state: dict, ticket: str, decision: str, why: str, basis: str, at: str,
                    key: str = None) -> str:
    """key 给了：同一件事只记一条（重复调用、崩溃重放都返回已有的 Dn，不写新行）。
    查和写都在 router home 的 flock 内（tick 整轮持锁，前台命令也持锁），所以查完再写不会撞车。"""
    if key:
        found = decision_with_key(path, key)
        if found:
            return found
    # 编号取 state 与文件两者的最大值：写完文件、没来得及存 state 就崩，下一条也不会撞号
    in_file = [int(m.group(1)[1:]) for m in map(_DEC.match, _read_lines(path)) if m]
    n = max([state["seq"]["decision"], *in_file]) + 1
    state["seq"]["decision"] = n
    did = f"D{n}"
    tail = f" {_key_tag(key)}" if key else ""
    _append_line(path, f"- {did} | {_clean(at)} | {_clean(ticket)} | {_clean(decision)} | "
                       f"理由：{_clean(why)} | 依据：{_clean(basis)}{tail}")
    return did


def revoke_decision(path, did: str, reason: str, at: str) -> None:
    if not any((m := _DEC.match(l)) and m.group(1) == did for l in _read_lines(path)):
        raise KeyError(did)
    _append_line(path, f"- {did} 已撤回 | {_clean(at)} | {_clean(reason)}")


def recent_decisions(path, n: int = 10) -> list:
    """最新的在前；撤回行合并进被撤回的那条。"""
    out, by_id = [], {}
    for line in _read_lines(path):
        m = _DEC.match(line)
        if m:
            d = {"id": m.group(1), "at": m.group(2), "ticket": m.group(3), "decision": m.group(4),
                 "why": m.group(5), "basis": m.group(6), "revoked": False,
                 "revoked_at": None, "revoke_reason": None}
            out.append(d)
            by_id[d["id"]] = d
            continue
        m = _REV.match(line)
        if m and m.group(1) in by_id:
            by_id[m.group(1)].update(revoked=True, revoked_at=m.group(2), revoke_reason=m.group(3))
    return deepcopy(out[::-1][:n])
