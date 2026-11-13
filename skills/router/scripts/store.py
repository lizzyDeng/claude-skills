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
from datetime import datetime, timedelta, timezone
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
POSTLAUNCH = "postlaunch"          # 已上线、等上线验收：代码已在 prod ⇒ 不占名额、不持锁、对下游算已解锁
DONE = "done"
PAUSED = "paused"
TAKEOVER = "takeover"
STUCK = "stuck"
HOLD = "hold"
SKIP = "skip"

STATUSES = {DRAFT, QUEUED, KICKOFF, AWAIT_CARD, EXECUTING, VERIFYING, MERGED, AWAIT_CHECK, AWAIT_PROD,
            POSTLAUNCH, DONE, PAUSED, TAKEOVER, STUCK, HOLD, SKIP}
RUNNING = {KICKOFF, EXECUTING, VERIFYING}                                  # 占名额
LOCKING = RUNNING | {AWAIT_CARD, PAUSED, TAKEOVER, STUCK, AWAIT_CHECK}      # 持锁
SETTLED = {MERGED, AWAIT_PROD, POSTLAUNCH, DONE}                            # 对下游算已解锁

QUEUE_KINDS = {"start", "card", "spend", "prod", "handson", "stuck", "conflict", "direction", "postlaunch",
               "verify_problem", "close", "harm", "return"}
# 决定单项的 topic（可选）：同一 kind 下按 topic 分派回答（router.cmd_answer）、看板文案
# assumption：核心假设被推翻的方向项（v2.1：方向归用户，router 不能 --as-router 代答）
QUEUE_TOPICS = {"base", "retarget", "dispatch", "offrepo", "requeue", "next", "assumption"}

# set_status 不传 waiting_for 时按状态给的默认值（R5：续跑路径不再把「在等什么」清空）
DEFAULT_WAIT = {KICKOFF: "写方向卡", EXECUTING: "执行中", VERIFYING: "等 CI", AWAIT_CARD: "等你确认方向卡",
                AWAIT_CHECK: "等你核对仓外改动", AWAIT_PROD: "等上 prod", POSTLAUNCH: "等上线验收"}
# topic=base 的项挡住的票：还没走完的（已合 / 等上 prod / 已上线 / 完成 / 暂缓 / 不派 的不算）
_BASE_FREE = {MERGED, AWAIT_PROD, POSTLAUNCH, DONE, HOLD, SKIP}


class StateCorrupt(Exception):
    pass


PROTECTED_BASES = {"main", "master"}


def base_branch_problem(cfg) -> str:
    """config 的 base_branch 是 main / master / 仓库默认分支（config 的 main_branch，缺省 main）⇒ 拒绝工作的原因；
    没问题 → ""。合进它 = 上 prod，归用户（RV M-7）：配置写错不能绕过这条。"""
    base, main = cfg.get("base_branch", "staging"), cfg.get("main_branch", "main")
    if not base or base in PROTECTED_BASES or base == main:
        return (f"config 的 base_branch 是 {base!r}（= main / master / 仓库默认分支 {main!r}）：router 只往 staging 这类"
                "隔离分支合，合进它就是上 prod、归用户；先改 config 的 base_branch")
    return ""


def pending_intent(t) -> bool:
    """上次派单 / 续跑落了意图、还没核实（dispatch.phase == "spawning"）：worker 可能已经在跑。"""
    return isinstance(t, dict) and (t.get("dispatch") or {}).get("phase") == "spawning"


def occupies_slot(t) -> bool:
    """占名额：在跑，或带着未核实的意图（按「可能在跑」算，RV M-5）。"""
    return t["status"] in RUNNING or pending_intent(t)


def holds_lock(t) -> bool:
    """持代码面锁：LOCKING 状态，或带着未核实的意图（RV M-5）。"""
    return t["status"] in LOCKING or pending_intent(t)


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


# ---- 业务票（v2）：一张票 = 一个用户行为问题；墙 / 尝试 / 判定 / 影响分 ----
KINDS = ("legacy", "business", "maint")
BIZ_VERDICTS = ("no_entry", "pending_sample", "useful", "wall_broken_not_retained", "useless", "harmful")
_CONFIDENCE = {"low": 0.5, "mid": 0.75, "high": 1.0}
PENDING_CAP_DAYS = 7
USELESS_RETURN_AT = 3


def ticket_kind(t: dict) -> str:
    """缺字段 ⇒ legacy（老 state 不迁移）。"""
    return t.get("kind") or "legacy"


def _iso(s) -> datetime:
    d = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def new_business_ticket(state: dict, *, title, segment, want, walls, metric, evidence, impact,
                        origin, absorbs=(), surfaces=(), weight=None) -> str:
    """surfaces 空 ⇒ 按重票（weight=heavy）：没声明代码面就不知道会不会编译 Rust，先占重名额、过磁盘闸；
    方向卡给出代码面后 router 用 surfaces --set 改成实际的。"""
    at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    surfaces = list(surfaces)
    tid = new_ticket(state, title=title, origin=origin, kind="business", authorized=True,
                     surfaces=surfaces, weight=weight or ("heavy" if not surfaces else "light"),
                     segment=segment, want=want, metric=metric, evidence=list(evidence),
                     impact=impact, absorbs=list(absorbs), parent=None, tasks=[], attempts=[],
                     no_use_streak=0,
                     walls=[{"id": f"W{i}", "text": w, "status": "open", "source": origin, "at": at}
                            for i, w in enumerate(walls, 1)])
    return tid


def attempt_inflight(t: dict) -> bool:
    """业务票有尝试已上线、还没判完（判定为空或待样本）：一次只验一个尝试（spec §4.4a）。历史尝试不算。"""
    return any(a.get("verdict") in (None, "pending_sample") for a in t.get("attempts") or [] if not a.get("historical"))


def record_shipped(state: dict, tid: str, at: str, t0: str) -> None:
    """改动随这次发版上了 prod，但票不进上线验收（停 / 接管 / 关前已撤回）：history 记一条 shipped_at，不改状态。
    这一条不带 status（不是状态变化，按状态读 history 的地方照旧）；staging_pending / offrepo_pending 看到它就算这次改动已出 staging。"""
    state["tickets"][tid]["history"].append({"at": at, "shipped_at": t0, "note": "随发版上 prod（状态不变、不进上线验收）"})


def staging_pending(t: dict) -> bool:
    """这张票的改动合进了 staging、之后还没 released（合 main 发版会把它一起带上去）。不看票现在的状态：
    stop / takeover / close 都不会让它离开发版批次（终审残留 #2a）。
    依据 = merge_sha（_settle 记的合并 commit）+ history：最后一次进 merged_staging / await_prod 之后没有 postlaunch，
    也没有 shipped_at（停 / 接管 / 关前已撤回的票随别的票发版上去时，released 只记这一条，残留 2 #3）。
    老票、维护票之后直接是 done 的也算 released（老版 released 直接记 done）；业务票之后是 done = 关了票，代码还在 staging。
    没有 history（手写的 state）就按状态算。"""
    if not t.get("merge_sha"):
        return False
    return _pending_since(t, (MERGED, AWAIT_PROD), done_releases=ticket_kind(t) != "business")


def offrepo_pending(t: dict) -> bool:
    """仓外改动还在真身上、这次还没上线判定：最后一次进 await_check 之后没有 postlaunch（业务票核对过 = 记成尝试进
    postlaunch）/ shipped_at。和 staging_pending 一样不看现在的状态：stop / takeover 都不会让改动消失（残留 2 #1）。"""
    return _pending_since(t, (AWAIT_CHECK,), done_releases=ticket_kind(t) != "business")


def _pending_since(t: dict, marks_of: tuple, done_releases: bool) -> bool:
    hist = t.get("history") or []
    marks = [i for i, x in enumerate(hist) if x.get("status") in marks_of]
    if not marks:
        return t.get("status") in marks_of
    after = hist[marks[-1] + 1:]
    if any(x.get("status") == POSTLAUNCH or x.get("shipped_at") for x in after):
        return False
    return not (done_releases and any(x.get("status") == DONE for x in after))


def current_attempt(t: dict):
    """业务票的「当前尝试」= 最后一条非历史的尝试（历史尝试是并进来的老票，不参与判定）；没有 → None。
    daily 的票况、verdict、released 的在途检查、看板 / status 的最近判定都按它。"""
    return next((a for a in reversed(t.get("attempts") or []) if not a.get("historical")), None)


def shown_attempt(t: dict):
    """给人看的「最近一次」：有当前尝试就是它；只有历史尝试时取最后一条历史的；都没有 → None。"""
    atts = t.get("attempts") or []
    return current_attempt(t) or (atts[-1] if atts else None)


def impact_score(t: dict) -> float:
    if ticket_kind(t) != "business":
        return 0.0
    return impact_of_dict(t.get("impact"))


def impact_of_dict(imp) -> float:
    """影响分公式只此一处：size × gap × 置信权重；缺字段 ⇒ 0。"""
    imp = imp or {}
    if not imp.get("size") or not imp.get("gap"):
        return 0.0
    return float(imp["size"]) * float(imp["gap"]) * _CONFIDENCE.get(imp.get("confidence"), 0.5)


def queue_impact(state: dict, q: dict) -> float:
    """决定单项的业务影响：verify_problem 取提议里的 impact；带票号的取该票的 impact_score；其余 0。"""
    if q.get("kind") == "verify_problem":
        return impact_of_dict((state.get("proposals", {}).get(q.get("proposal") or "") or {}).get("impact"))
    t = state["tickets"].get(q.get("ticket") or "")
    return impact_score(t) if t else 0.0


def attach_wall(state: dict, tid: str, text: str, source: str, at: str) -> str:
    walls = state["tickets"][tid]["walls"]
    nums = [int(w["id"][1:]) for w in walls if str(w.get("id", "")).startswith("W") and w["id"][1:].isdigit()]
    wid = f"W{max(nums, default=0) + 1}"
    walls.append({"id": wid, "text": text, "status": "open", "source": source, "at": at})
    return wid


def first_open_wall(t: dict):
    """第一道没拆的墙的编号；没有 → None。"""
    return next((w.get("id") for w in t.get("walls") or [] if w.get("status") == "open"), None)


def add_attempt(state: dict, tid: str, direction: str, at: str, rollback: str) -> int:
    """记一次尝试；wall = 这次尝试对准的墙（上线那一刻第一道没拆的墙），判定拆的就是它（v2.1：并票后墙的顺序会变）。"""
    t = state["tickets"][tid]
    attempts = t["attempts"]
    n = max((a.get("n", 0) for a in attempts), default=0) + 1
    attempts.append({"n": n, "direction": direction, "at": at, "rollback": rollback, "verdict": None,
                     "wall": first_open_wall(t)})
    return n


def _break_wall(t: dict, wid=None) -> None:
    """拆这次尝试对准的那道墙（wid，还没拆的话）；没记 wid（v2.1 之前的尝试）或它已经拆了 ⇒ 拆第一道没拆的墙。"""
    for w in t["walls"]:
        if wid and w.get("id") == wid and w["status"] == "open":
            w["status"] = "broken"
            return
    for w in t["walls"]:
        if w["status"] == "open":
            w["status"] = "broken"
            return


def record_verdict(state: dict, tid: str, n: int, verdict: str, at: str, *, touched: int,
                   hit_wall: int, continued: int, note: str, new_wall: str = None) -> dict:
    if verdict not in BIZ_VERDICTS:
        raise ValueError(f"未知判定 {verdict}")
    t = state["tickets"][tid]
    a = next((x for x in t["attempts"] if x.get("n") == n), None)
    if a is None:
        raise ValueError(f"{tid} 没有尝试 {n}")
    if a.get("historical"):
        raise ValueError(f"尝试 {n} 是历史记录，不能记判定")
    if verdict == "wall_broken_not_retained" and not new_wall:
        raise ValueError("wall_broken_not_retained 必须给 new_wall")
    if verdict == "pending_sample":
        since = a.get("pending_since")
        if since and _iso(at) - _iso(since) >= timedelta(days=PENDING_CAP_DAYS):
            if hit_wall:                          # 有人走到墙：有几个按几个判，不许再待样本
                raise ValueError(f"待样本已满 {PENDING_CAP_DAYS} 天：走到墙的有几个按几个判")
            # 满 7 天、碰到改动的人里没人走到墙（含碰到 0 人）：按没入口判，走 no_entry（终审裁决 #13）
            verdict = "no_entry"
            note = f"{note}（待样本满 {PENDING_CAP_DAYS} 天，碰到 {touched} 人、没人走到墙：按没入口判）"
    a.update(verdict=verdict, verdict_at=at, touched=touched, hit_wall=hit_wall, continued=continued, note=note)
    if verdict == "pending_sample":
        a.setdefault("pending_since", at)
    else:
        a.pop("pending_since", None)
    actions = []
    if verdict == "no_entry":
        actions = ["check_entry"]
    elif verdict == "pending_sample":
        actions = ["wait"]
    elif verdict == "useful":
        _break_wall(t, a.get("wall")); t["no_use_streak"] = 0; actions = ["next_wall"]
    elif verdict == "wall_broken_not_retained":
        _break_wall(t, a.get("wall")); t["no_use_streak"] = 0
        attach_wall(state, tid, new_wall, "verdict", at); actions = ["next_wall"]
    elif verdict == "useless":
        t["no_use_streak"] += 1
        actions = ["change_direction"] + (["return_to_user"] if t["no_use_streak"] >= USELESS_RETURN_AT else [])
    elif verdict == "harmful":
        actions = ["rollback"]
    return {"actions": actions, "verdict": verdict}


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
    """还没做完的栏 → {"downstream": [票号…], "cols": [栏名…], "old_branches": [分支…]}；kept 不算没做完
    （要人处理，不再重试）。old_branches = 业务票旧尝试已合并的本地分支（关业务票时列出，老票没有这一栏）。"""
    if not isinstance(co, dict):
        return {"downstream": [], "cols": [], "old_branches": []}
    down = [oid for oid, d in (co.get("downstream") or {}).items() if isinstance(d, dict) and d.get("state") == "pending"]
    old = [b for b, v in (co.get("old_branches") or {}).items() if v == "pending"]
    return {"downstream": down, "cols": [c for c in CLOSEOUT_COLS if co.get(c) == "pending"], "old_branches": old}


def closeout_pending(co) -> bool:
    o = closeout_open(co)
    return bool(o["downstream"] or o["cols"] or o["old_branches"])


# ---- 上线验收（postlaunch）：released 之后 t0 + 1 / 3 / 7 天各查一次；票的终点是上线验收，不是 released ----
ISO = "%Y-%m-%dT%H:%M:%SZ"
POSTLAUNCH_CHECKS = (("T+1", 1), ("T+3", 3), ("T+7", 7))
VERDICTS = ("生效", "没入口", "没用", "有害", "看不出来")
BAD_VERDICTS = ("没入口", "没用", "有害")          # 必须带当天动作，进决定单
TICKET_TYPES = ("增长型", "风险型")                 # 增长型：拿出行为收益才算生效；风险型：行为不 worse 即可，写明代价
# --behavior 的判定词：照周报 progress.<指标>.v 的口径
BEHAVIOR_VERDICTS = ("better", "worse", "flat", "unclear", "small", "up_unconfirmed", "down_unconfirmed")


def parse_iso(s) -> datetime:
    return datetime.strptime(s, ISO).replace(tzinfo=timezone.utc)


def postlaunch_plan(t0: str) -> dict:
    base = parse_iso(t0)
    return {"t0": t0, "checks": [{"label": label, "at": (base + timedelta(days=d)).strftime(ISO)}
                                 for label, d in POSTLAUNCH_CHECKS],
            "results": []}


def postlaunch_effective(pl) -> dict:
    """检查点 → 当前有效的那条结果（同一检查点提交多次：最后一次覆盖前面的）。"""
    out = {}
    for r in (pl or {}).get("results", []):
        out[r.get("check")] = r
    return out


def postlaunch_next(t):
    """下一个还没有结果的检查点（按 T+1 → T+7 的顺序）；都有结果了 → None。"""
    pl = t.get("postlaunch") or {}
    done = postlaunch_effective(pl)
    return next((c for c in pl.get("checks", []) if c["label"] not in done), None)


def postlaunch_due(t, now: str) -> list:
    """到期、还没记结果的检查点（now ≥ at）。只有 postlaunch 的票会到期；漏掉的早检查点照样交。"""
    if t.get("status") != POSTLAUNCH:
        return []
    pl = t.get("postlaunch") or {}
    done, n = postlaunch_effective(pl), parse_iso(now)
    return [c["label"] for c in pl.get("checks", []) if c["label"] not in done and n >= parse_iso(c["at"])]


def postlaunch_view(t, now: str) -> dict:
    """看板 / status 用：{"next": {"label", "at", "due"} | None, "last": 最近一次结果 | None}。
    next 只对还在验收中的票给（完成的票不再有下一次检查）。"""
    pl = t.get("postlaunch") or {}
    nxt = postlaunch_next(t) if t.get("status") == POSTLAUNCH else None
    if nxt:
        nxt = {**nxt, "due": parse_iso(now) >= parse_iso(nxt["at"])}
    results = pl.get("results") or []
    return {"next": nxt, "last": results[-1] if results else None}


def hhmm(ts) -> str:
    """ISO → 「11-13 08:30」（UTC）。"""
    return ts[5:16].replace("T", " ") if ts and len(ts) >= 16 else (ts or "")


def postlaunch_wait(state, t, now: str) -> str:
    """postlaunch 票的「在等什么」：到期 → router 跑检查；有没回的决定单 → 等你回；否则 → 下一个检查点的时间。"""
    due = postlaunch_due(t, now)
    if due:
        return f"上线验收 {'、'.join(due)} 到期：router 跑检查"
    v = postlaunch_view(t, now)
    parts = []
    items = [q for q in state.get("queue", []) if q.get("kind") == "postlaunch" and q.get("ticket") == t.get("id")]
    if items and v["last"]:
        parts.append(f"上线验收 {v['last']['check']} {v['last']['verdict']}：等你回 {items[-1]['id']}")
    if v["next"]:
        parts.append(f"等 {v['next']['label']} 上线验收（{hhmm(v['next']['at'])} UTC）")
    elif not items:
        parts.append("上线验收检查点已走完")
    return "；".join(parts)


def in_queue(state, qid) -> bool:
    return bool(qid) and any(q["id"] == qid for q in state["queue"])


def _draft_count(state) -> int:
    batch = state.get("batch", {}).get("id")
    return sum(1 for t in state["tickets"].values()
               if t.get("batch") == batch and t.get("origin", "weekly") == "weekly" and t["status"] == DRAFT)


def router_owned(t: dict) -> bool:
    """业务票且已授权：走查 / 上 prod 由 router 推进（spec §5.5）。其余（含 legacy）一律照旧交用户。
    v2.1：方向卡不在内——业务票的方向卡也交用户确认；router 只上用户确认过的方案（card_confirmed_by=user）。"""
    return ticket_kind(t) == "business" and bool(t.get("authorized"))


def queue_blocks(state, q) -> int:
    """这一项挡住了几张票（读时现算，不存）。被挡的票按票号集合去重：同一张票可能既依赖本票、又共享代码面。
    - start：本批 weekly 草稿张数；prod：等上 prod 的张数；topic=base：还没走完的票数
    - 带票号的项：本票自己（没完成 / 没放弃就算 1 张）+ 被它挡住的其它票：
      本票没 settle ⇒ 依赖它的；本票持锁 ⇒ 排队等它代码面的"""
    tickets = state["tickets"]
    if q.get("kind") == "start":
        return _draft_count(state)
    if q.get("kind") == "prod":
        return sum(1 for t in tickets.values() if t["status"] == AWAIT_PROD and not router_owned(t))
    if q.get("topic") == "base":
        return sum(1 for t in tickets.values() if t["status"] not in _BASE_FREE)
    t = tickets.get(q.get("ticket") or "")
    if not t:
        return 0
    tid, mine = t["id"], set(t.get("surfaces", []))
    blocked = {o["id"] for o in tickets.values()
               if o["id"] != tid and o["status"] not in SETTLED | {SKIP}
               and ((t["status"] not in SETTLED and tid in o.get("blocked_by", []))
                    or (holds_lock(t) and o["status"] == QUEUED and mine & set(o.get("surfaces", []))))}
    return (1 if t["status"] not in (DONE, SKIP) else 0) + len(blocked)


def queue_view(state) -> list:
    """决定单的读视图：每项浅拷贝 + 现算 blocks；start 项的「几张」按当前草稿重写；按 (-impact, -blocks, 序号) 排。
    只读：不改 state 本身（status 这类只读命令不存盘）。"""
    out = []
    for q in state["queue"]:
        v = dict(q)
        v["blocks"] = queue_blocks(state, q)
        if q.get("kind") == "start":
            v["summary"] = f"派发单已备好（{v['blocks']} 张）"
        v["impact"] = queue_impact(state, q)
        out.append(v)
    return sorted(out, key=lambda v: (-v["impact"], -v["blocks"], int(v["id"][1:])))


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
