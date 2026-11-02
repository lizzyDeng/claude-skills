"""router 的持久状态：state.json（票 / 队列 / 游标）与 decisions.md（router 替用户做的决定）。

写一律走「临时文件 + os.replace」：router 随时可能被压缩或崩溃，半截文件比没有文件更糟。
读到坏文件就拒绝启动并留备份——用空状态覆盖等于把全部在跑的票丢掉。
"""
import json
import os
import re
import shutil
import tempfile
import time
from copy import deepcopy
from pathlib import Path

DRAFT = "draft"
QUEUED = "queued"
KICKOFF = "kickoff"
AWAIT_CARD = "await_card"
EXECUTING = "executing"
VERIFYING = "verifying"
MERGED = "merged_staging"
AWAIT_PROD = "await_prod"
DONE = "done"
PAUSED = "paused"
TAKEOVER = "takeover"
STUCK = "stuck"
HOLD = "hold"
SKIP = "skip"

STATUSES = {DRAFT, QUEUED, KICKOFF, AWAIT_CARD, EXECUTING, VERIFYING, MERGED, AWAIT_PROD,
            DONE, PAUSED, TAKEOVER, STUCK, HOLD, SKIP}
RUNNING = {KICKOFF, EXECUTING, VERIFYING}                     # 占名额
LOCKING = RUNNING | {AWAIT_CARD, PAUSED, TAKEOVER, STUCK}      # 持锁
SETTLED = {MERGED, AWAIT_PROD, DONE}                           # 对下游算已解锁

QUEUE_KINDS = {"start", "card", "spend", "prod", "handson", "stuck", "conflict", "direction"}


class StateCorrupt(Exception):
    pass


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
    return state


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


def set_status(state: dict, tid: str, status: str, at: str, waiting_for: str = "") -> None:
    if status not in STATUSES:
        raise ValueError(f"未知状态 {status}")
    t = state["tickets"][tid]
    t["status"] = status
    t["waiting_for"] = waiting_for
    t["history"].append({"at": at, "status": status})


def push_queue(state, ticket, kind, summary, blocks, at) -> str:
    if kind not in QUEUE_KINDS:
        raise ValueError(f"未知队列类型 {kind}")
    state["seq"]["queue"] += 1
    qid = f"Q{state['seq']['queue']}"
    state["queue"].append({"id": qid, "ticket": ticket, "kind": kind, "summary": summary,
                           "blocks": blocks, "created_at": at})
    return qid


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


def append_decision(path: Path, state: dict, ticket: str, decision: str, why: str, basis: str, at: str) -> str:
    # 编号取 state 与文件两者的最大值：写完文件、没来得及存 state 就崩，下一条也不会撞号
    in_file = [int(m.group(1)[1:]) for m in map(_DEC.match, _read_lines(path)) if m]
    n = max([state["seq"]["decision"], *in_file]) + 1
    state["seq"]["decision"] = n
    did = f"D{n}"
    _append_line(path, f"- {did} | {_clean(at)} | {_clean(ticket)} | {_clean(decision)} | "
                       f"理由：{_clean(why)} | 依据：{_clean(basis)}")
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
