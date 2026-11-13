#!/usr/bin/env python3
"""router CLI：router session 和 worker 对状态的唯一入口。

  python3 router.py [--root DIR] <cmd> …

配置：<root>/.claude/router/config.json；状态目录：<root>/.claude/router/
（worker 进程里由环境变量 ROUTER_HOME 指定）。每个子命令往 stdout 打一个 JSON：{"ok": bool, …}。
改状态的子命令都在 <状态目录>/.lock 上加排他锁：后台 watch 的 tick 和前台命令不会互相覆盖。
"""
import argparse
import copy
import json
import os
import re
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import analyst  # noqa: E402
import discover  # noqa: E402
import events  # noqa: E402
import github  # noqa: E402
import launcher  # noqa: E402
import store  # noqa: E402
import surfaces  # noqa: E402
from ctx import Ctx  # noqa: E402

RESUME_KINDS = {"card", "spend", "direction", "stuck", "conflict"}
# 只能由用户 / router session 跑的子命令：worker 进程（设了 ROUTER_TICKET）不许自己放行自己
ROUTER_ONLY = {"draft", "add", "start", "answer", "submit-card", "decide", "revoke", "tell", "stop", "takeover",
               "handback", "prioritize", "walkthrough-done", "prod-batch", "released", "postlaunch-result", "surfaces",
               "ack-slow", "review-pack", "review-canary", "propose", "verdict", "apply-daily", "analyst", "maint", "impact",
               "redirect", "close", "absorb", "migrate-apply", "merge"}
# answer --as-router 只收这几类、且只收票级授权的业务票：方向卡（card）、花钱 / 撤不回 / 发商店包（spend）、判真、关票、
# 有害 / 退回（harm、return）都归用户；方向项里问「要下一个方向吗」（next）和「核心假设被推翻」（assumption）也归用户（v2.1 第 1 条）
AS_ROUTER_KINDS = {"direction", "stuck", "conflict"}
USER_TOPICS = {"next", "assumption"}
ROUTER_LABEL = "router 决定"
ROUTER_RELAY = "router 转述 / 决定"           # tell --as-router：router 自己的话（转述 / 决定），不冒充用户原话
USER_LABEL = "用户原话（逐字）"
VERDICT_ZH = {"useful": "有用", "wall_broken_not_retained": "墙拆了、人没留住", "useless": "没用", "harmful": "有害",
              "no_entry": "没入口", "pending_sample": "待样本"}
KIND_HINT = {
    "card": "方向卡已确认（用户原话里如有修改，以修改为准）。进入 worker.md 第 2 段「执行」。",
    "reject": "方向卡被用户驳回（原话见上）。回到第 1 段：按原话重新查根因、重写方向卡到同一路径，"
              "然后 progress card_ready 退出。不要开始实现。",
    "spend": "这是花钱放行的回答：照原话执行；没放行就换不花钱的做法，做不到就再 emit decision 停下。",
    "direction": "照原话继续当前阶段。",
    "stuck": "router 发现你上次没留下结果就退出了。先看 git status / git log 和你的笔记，确认做到哪，再照原话继续。",
    "conflict": "照原话处理代码面冲突后继续。",
    "offrepo_reject": "用户核对你的仓外改动后不满意（原话见上）。先读 inbox；按原话改，或用 "
                      "$ROUTER_HOME/backups/$ROUTER_TICKET/ 恢复后重做；改完重写 cards/$ROUTER_TICKET.diff、跑验收，"
                      "再 progress impl_done 退出。",
}


# 续跑后「在等什么」（R5）：按续跑的原因写，不留空
RETURN_WAIT = "连续 3 次没用，退回用户定夺"
NO_WALL_WAIT = "墙已全部拆完，等分析 agent 提议结束"
REQUEUE_WAIT = "判定已记，等调度器派新 worker 接着做（读收件箱和尝试记录）"
REDIRECT_WAIT = "已换方向，等调度器派新 worker（读收件箱和尝试记录）"
RESUME_WAIT = {"card": "执行中（方向卡已确认）", "reject": "按驳回重写方向卡", "spend": "照放行结论继续",
               "stuck": "按回答续跑", "conflict": "按回答处理代码面冲突", "offrepo_reject": "按你的意见改仓外改动",
               "handback": "你交还后继续"}


def _resume_wait(kind, status) -> str:
    if kind in RESUME_WAIT:
        return RESUME_WAIT[kind]
    return "按 router 转来的意见改方向卡" if status == store.KICKOFF else "按 router 转来的话继续"   # direction / tell --resume


class Fail(Exception):
    def __init__(self, msg, code=1, **extra):
        super().__init__(msg)
        self.code, self.extra = code, extra


# ---- 环境 -------------------------------------------------------------------

class Env:
    def __init__(self, root: Path, ctx):
        self.root = Path(root).resolve()
        self.home = Path(os.environ.get("ROUTER_HOME") or self.root / ".claude" / "router")
        self.ctx = ctx
        cfg_path = self.home / "config.json"
        self.cfg = json.loads(cfg_path.read_text(encoding="utf-8")) if cfg_path.exists() else {}
        sp = self.home / "surfaces.json"
        self.table = surfaces.load_surfaces(sp) if sp.exists() else {}
        self.state_path = self.home / "state.json"
        self.decisions = self.home / "decisions.md"

    def now(self) -> str:
        return self.ctx.now().strftime("%Y-%m-%dT%H:%M:%SZ")

    def locked(self):
        return store.locked(self.home)

    @contextmanager
    def state(self):
        """load → 改 → save → 重画看板，全程持锁。出异常就不存。
        中途为了起进程先落过一次盘（续跑意图）、随后起进程失败：rollback() 把盘恢复成这条命令之前的样子。"""
        with self.locked():
            st = store.load_state(self.state_path)
            self._snapshot = copy.deepcopy(st)
            yield st
            store.save_state(self.state_path, st)
            render_board(self, st)

    def rollback(self) -> None:
        snap = getattr(self, "_snapshot", None)
        if snap is not None:
            try:
                store.save_state(self.state_path, snap)
            except Exception:  # noqa: BLE001 — 盘上留着没起成的意图：下一轮过了宽限期按「没起过」处理
                pass

    def ticket(self, st, tid):
        if tid not in st["tickets"]:
            raise Fail(f"没有票 {tid}")
        return st["tickets"][tid]


def render_board(env, st) -> None:
    try:
        import board
    except ImportError:          # 看板模块未接入时不画
        return
    board.write(env.home / "board.html",
                board.render(st, store.recent_decisions(env.decisions),
                             {tid: events.read_all(env.home, tid)[-5:] for tid in st["tickets"]},
                             env.ctx.now()))


def dispatch(env, st, run=None):
    """→ (派出去的票, 结构化的派发失败 [{ticket, phase, error}], 其余错误)。
    run 由 cmd_start 传入：它先用同一个 Run 查过 base，这里复用结果，不再查第二次。"""
    import tick
    run = run or tick.Run(env.ctx, env.root, env.cfg, st)
    done = tick.dispatch(env.ctx, env.root, env.cfg, st, run=run)
    return done, run.res["dispatch_failed"], [e for e in run.res["errors"] if e not in run.fail_errors]


def _csv(s):
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def _resume_prompt(kind, text, inbox, as_router=False, relay=False):
    if relay:
        head = [f"{ROUTER_RELAY}（不是用户原话）：", text]
    else:
        head = [f"{ROUTER_LABEL}（不是用户原话）：", text] if as_router else [f"{USER_LABEL}：", text]
    parts = ["【router】你之前停下等的事有了回答。", "", *head, ""]
    if inbox:
        parts += ["你停下期间收件箱收到的：",
                  *[f"- [{m.get('type')}] {m.get('summary', '')}（来自 {m.get('ticket', 'router')}）" for m in inbox], ""]
    if relay:
        hint = "照上面 router 转来的话继续当前阶段（这是 router 的话，不是用户原话）。"
    elif as_router:
        hint = f"照上面的{ROUTER_LABEL}继续当前阶段（票级授权，router 代为回答）。"
    else:
        hint = KIND_HINT.get(kind, KIND_HINT["direction"])
    parts += [hint, "先按 worker.md 读一次 inbox，再继续。"]
    return "\n".join(parts)


def _check_surfaces(env, names):
    unknown = [n for n in names if n not in env.table]
    if unknown:
        raise Fail(f"未知代码面 {unknown}；只能用 surfaces.json 里的：{sorted(env.table)}")


def _refuse_pending(t, tid, ctx=None) -> None:
    """会起 / 停 / 交出 worker 的前台命令，在认不出 worker 时一律不做：
    上次派单 / 续跑的意图还没核实（worker 可能已经在跑，RV M-1）；给了 ctx 时，pid 在但 ps 查不到启动时间（RV M-2）。"""
    if store.pending_intent(t):
        why = (t.get("dispatch") or {}).get("unverified") or "上次起 worker 没落完盘"
        raise Fail(f"{tid} 派单待核实（{why}），下一轮巡检后再试")
    if ctx is not None and launcher.worker_liveness(ctx, t["worker"]) == "unknown":
        raise Fail(f"{tid} 的 worker（pid {t['worker'].get('pid')}）还在，但查不到它的启动时间（ps 失败 / 超时），"
                   "认不出是不是它：稍后再试")


def _resume(env, st, tid, kind, text, status, force_restart=False, waiting=None, as_router=False, relay=False):
    """续跑 worker → "resumed"；worker 还活着 → 只投递收件箱、不起第二个进程 → "delivered"。
    force_restart：用户回答的是「卡住」，旧进程可能挂死，先停再续。接管中的票一律不碰。
    waiting：续跑后的 waiting_for；不传就按 kind 给（RESUME_WAIT），不会被清空。
    续跑走与派单相同的事务：意图落盘 → 起进程（带启动回执）→ 命令结束时存盘；那次存盘失败，下一轮按回执认领。"""
    t = st["tickets"][tid]
    if t["status"] == store.TAKEOVER:
        raise Fail(f"{tid} 在你接管中：先「还给 router」（handback）再续跑")
    _refuse_pending(t, tid, env.ctx)
    if not (t["worker"].get("session_id") and t["worker"].get("worktree")):
        raise Fail(f"{tid} 没有可续跑的 worker session")
    if launcher.worker_alive(env.ctx, t["worker"]):
        if not force_restart:
            said = (f"{ROUTER_RELAY}（不是用户原话）：{text}" if relay else
                    f"{ROUTER_LABEL}（不是用户原话）：{text}" if as_router else text)
            events.deliver(env.home, tid, {"ts": env.now(), "ticket": "router", "type": "tell", "summary": said})
            return "delivered"
        launcher.stop_worker(env.ctx, t["worker"])
    inbox = events.read_inbox(env.home, st, tid)
    wait = waiting if waiting is not None else _resume_wait(kind, status)
    try:
        launcher.resume_with_intent(env.ctx, env.home, env.cfg, st, tid, _resume_prompt(kind, text, inbox, as_router, relay),
                                    status,
                                    wait, env.now(), lambda: store.save_state(env.state_path, st))
    except launcher.IntentNotSaved as e:
        raise Fail(f"{tid} 没续跑：起进程前存盘失败（{e}）")
    except Exception as e:  # noqa: BLE001 — 意图已落盘、进程没起：盘恢复成这条命令之前
        env.rollback()
        raise Fail(f"{tid} 没续跑：起不了 worker（{e}）")
    store.set_status(st, tid, status, env.now(), wait)
    return "resumed"


def _append_local(path: Path, line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def _card_direction(text: str) -> str:
    lines, take, out = text.splitlines(), False, []
    for line in lines:
        bare = line.strip().strip("*#").strip()
        if re.match(r"^3[\.\、]", bare) or bare.startswith("方案方向"):
            take = True
            continue
        if take and (re.match(r"^4[\.\、]", bare) or bare.startswith("为什么不选")):
            break
        if take and line.strip():
            out.append(line.strip())
    summary = "\n".join(out) or text.strip()[:300]
    return summary[:600]


# ---- 子命令 -----------------------------------------------------------------

def cmd_draft(a, env):
    tri = json.loads(Path(a.source).read_text(encoding="utf-8"))
    batch = tri.get("batch")
    if not batch or not isinstance(tri.get("tickets"), list):
        raise Fail("triage.json 缺 batch 或 tickets")
    with env.state() as st:
        mine = [tid for tid, t in st["tickets"].items() if t["batch"] == batch and t["origin"] == "weekly"]
        if mine and not a.replace:
            raise Fail(f"批次 {batch} 已有草稿（{' '.join(mine)}）；要重出请加 --replace")
        for tid in mine:
            if st["tickets"][tid]["status"] in (store.DRAFT, store.HOLD, store.SKIP):
                del st["tickets"][tid]
        st["queue"] = [q for q in st["queue"] if q["kind"] != "start"]
        for item in tri["tickets"]:
            _check_surfaces(env, item.get("surfaces", []))
        by_title, created = {}, []
        for item in tri["tickets"]:
            surf = list(item.get("surfaces", []))
            weight = "heavy" if surfaces.weight_for(set(surf), env.table) == "heavy" else item.get("weight", "light")
            tid = store.new_ticket(
                st, title=item["title"], weight=weight, origin="weekly", batch=batch,
                priority=item.get("priority", 100), why=item.get("why", {}),
                problem=item.get("problem", ""), problem_anchor=item.get("problem_anchor", ""),
                solution={"kind": "clue", "text": item.get("clue", ""), "source": "router 分诊"},
                surfaces=surf, tags=list(item.get("tags", [])), covered_by=item.get("covered_by", ""))
            decision, reason = item.get("decision", "dispatch"), item.get("reason", "")
            if decision == "hold":
                store.set_status(st, tid, store.HOLD, env.now(), reason)
            elif decision == "skip":
                cov = item.get("covered_by")
                store.set_status(st, tid, store.SKIP, env.now(), f"已由 {cov} 覆盖：{reason}" if cov else reason)
            else:
                st["tickets"][tid]["history"].append({"at": env.now(), "status": store.DRAFT})
            by_title[item["title"]] = tid
            created.append(tid)
        unknown = []
        for item, tid in zip(tri["tickets"], created):
            for dep in item.get("depends_on", []):
                if dep in by_title:
                    st["tickets"][tid]["blocked_by"].append(by_title[dep])
                else:
                    unknown.append({"ticket": tid, "depends_on": dep})
        st["batch"] = {"id": batch, "started": False, "started_at": None}
        n = sum(1 for t in created if st["tickets"][t]["status"] == store.DRAFT)
        qid = store.push_queue(st, None, "start", f"派发单已备好（{n} 张）", env.now())
    # 不在这里发 Lark：start 项 notified_at=None，下一轮巡检统一推（R4）
    return {"ok": True, "batch": batch, "tickets": created, "queue": qid, "unknown_depends": unknown}


def _biz_issue_fields(t) -> None:
    """业务票建 issue 时 issue_body 读 why / problem：从业务字段填上，不另造一套 body。"""
    m = t.get("metric") or {}
    t["why"] = {"metric": m.get("name", "") if isinstance(m, dict) else str(m),
                "users": t.get("segment", ""), "evidence": "；".join(t.get("evidence") or [])}
    t["problem"] = f"{t.get('segment', '')}：{t.get('want', '')}"
    t["solution"] = {"kind": "clue", "text": "；".join(w["text"] for w in t.get("walls", [])),
                     "source": "分析 agent 提议 / 用户判真（墙，非方案）"}


def _cmd_add_business(a, env):
    missing = [f for f, v in (("--segment", a.segment), ("--want", a.want), ("--wall", a.wall),
                              ("--metric", a.metric)) if not v]
    if missing:
        raise Fail(f"add --business 缺 {' '.join(missing)}")
    surf = _csv(a.surfaces)
    _check_surfaces(env, surf)
    with env.state() as st:
        tid = store.new_business_ticket(
            st, title=a.title, segment=a.segment, want=a.want, walls=list(a.wall), metric={"name": a.metric},
            evidence=[a.evidence] if a.evidence else [], impact={}, origin="user",   # 用户直接提 = 判过真
            surfaces=surf, weight=surfaces.ticket_weight("business", surf, env.table))
        _biz_issue_fields(st["tickets"][tid])
        st["tickets"][tid]["history"].append({"at": env.now(), "status": store.DRAFT})
    return {"ok": True, "ticket": tid}


def cmd_add(a, env):
    if a.business:
        return _cmd_add_business(a, env)
    surf = _csv(a.surfaces)
    _check_surfaces(env, surf)
    with env.state() as st:
        tid = store.new_ticket(
            st, title=a.title, origin="user", batch="user", priority=a.priority,
            weight=a.weight or surfaces.weight_for(set(surf), env.table),
            why={"metric": a.metric or "", "users": a.users or "", "evidence": a.evidence or ""},
            problem=a.problem or "", problem_anchor=a.anchor or "",
            solution={"kind": "clue", "text": a.clue or "", "source": "用户提出"},
            surfaces=surf, tags=_csv(a.tags), blocked_by=_csv(a.depends))
        st["tickets"][tid]["history"].append({"at": env.now(), "status": store.DRAFT})
    return {"ok": True, "ticket": tid}


def cmd_start(a, env):
    bad = store.base_branch_problem(env.cfg)
    if bad:                                           # 在任何 GitHub 写入、任何 git 操作之前
        raise Fail(bad)
    with env.locked():
        st = store.load_state(env.state_path)
        if a.all:
            sel = [tid for tid, t in st["tickets"].items()
                   if t["batch"] == st["batch"]["id"] and t["origin"] == "weekly" and t["status"] == store.DRAFT]
        else:
            sel = a.tickets
        if not sel:
            raise Fail("没有可 start 的票")
        return _start_locked(env, st, sel)


def _start_locked(env, st, sel):
    """start 的本体：调用方已持锁、已载入 st。cmd_start 和「判真后当场 start」共用（不复制逻辑）。"""
    import tick
    bad = store.base_branch_problem(env.cfg)
    if bad:
        raise Fail(bad)
    repo = env.cfg["repo"]
    blocked, errors, warnings, started = [], [], [], []
    for tid in sel:
        if env.ticket(st, tid)["status"] not in (store.DRAFT, store.HOLD):
            raise Fail(f"{tid} 状态是 {st['tickets'][tid]['status']}，不能 start")
    # 与其它 GitHub 写路径同一道闸（uid + 20 字原话窗口）；闸失效 ⇒ 什么都不做（fail-closed，RV I-3）
    try:
        guard = github.load_guard(env.cfg.get("leak_guard_globs", []))
    except github.GuardEmpty as e:
        raise Fail(f"防泄漏闸失效，没有建任何 issue：{e}")
    # 在任何 GitHub 写入（map / issue）之前先查远端 base：base 不对，一张 issue 都不建
    run = tick.Run(env.ctx, env.root, env.cfg, st)
    base = run.ensure_base()
    if not base["ok"]:
        if not base["transient"]:                    # 进了决定单：项先落盘，再 Fail
            store.save_state(env.state_path, st)
            render_board(env, st)
        raise Fail(f"远端 {env.cfg.get('base_branch', 'staging')} 不可用，没有建任何 issue：{base['error']}",
                   base_wait=base["wait"])
    if not st.get("map"):
        st["map"] = github.ensure_map(env.ctx, repo, env.cfg["map_title"])
        store.save_state(env.state_path, st)
    for tid in sorted(sel, key=lambda x: (st["tickets"][x]["priority"], int(x[1:]))):
        t = st["tickets"][tid]
        if t["issue"] is None:
            try:
                t["issue"] = github.create_ticket_issue(env.ctx, repo, st["map"], t, guard)
            except github.LeakBlocked as e:
                blocked.append({"ticket": tid, "hits": e.hits})
                continue
            except github.GhError as e:
                errors.append({"ticket": tid, "error": str(e)})
                continue
            store.save_state(env.state_path, st)          # 建一张存一次：中途崩了重跑不会重复建票
        t["start_ok"] = True
        store.set_status(st, tid, store.QUEUED, env.now())
        started.append(tid)
    for tid in started:
        t = st["tickets"][tid]
        for b in t["blocked_by"]:
            bi = st["tickets"].get(b, {}).get("issue")
            if bi and t["issue"]:
                try:
                    github.add_blocked_by(env.ctx, repo, t["issue"], bi)
                except github.GhError as e:
                    warnings.append({"ticket": tid, "blocked_by": b, "error": str(e)})
    if started:
        st["batch"]["started"] = True
        st["batch"]["started_at"] = st["batch"]["started_at"] or env.now()
    if not any(t["batch"] == st["batch"]["id"] and t["origin"] == "weekly" and t["status"] == store.DRAFT
               for t in st["tickets"].values()):
        st["queue"] = [q for q in st["queue"] if q["kind"] != "start"]
    store.save_state(env.state_path, st)
    dispatched, failed, other = dispatch(env, st, run)
    errors += failed                                  # [{ticket, phase, error}]：哪张票、卡在哪一步
    warnings += [{"ticket": None, "error": e} for e in other]
    store.save_state(env.state_path, st)
    render_board(env, st)
    res = {"ok": not blocked and not errors, "started": started, "dispatched": dispatched,
           "issues": {tid: st["tickets"][tid]["issue"] for tid in started},
           "blocked": blocked, "errors": errors, "warnings": warnings}
    if not res["ok"]:
        raise Fail("部分票没能 start", **res)
    return res


def _check_as_router(st, q, reject) -> None:
    """answer --as-router：只收票级授权的业务票、只收 AS_ROUTER_KINDS（其余归用户）。拒 ⇒ Fail，不存盘：项留在决定单里。"""
    if reject:
        raise Fail("--as-router 不能和 --reject 一起用")
    t = st["tickets"].get(q["ticket"] or "")
    if not t or not store.router_owned(t):
        raise Fail(f"{q['id']} 不是票级授权的业务票的项：只能由用户回（去掉 --as-router，照用户原话答）")
    if q["kind"] not in AS_ROUTER_KINDS:
        raise Fail(f"{q['id']} 是 {q['kind']} 项：归用户定（方向卡、花钱 / 撤不回 / 发商店包、判真、关票、有害、退回），不能代答")
    if q.get("topic") in USER_TOPICS:
        raise Fail(f"{q['id']} 问的是方向（{q['topic']}）：方向归用户定，router 不能代答。要出新方向卡交用户："
                   f"router.py redirect {q['ticket']} --new-card；用户给了方向：照原话 answer（不加 --as-router）")


def cmd_answer(a, env):
    as_router = bool(getattr(a, "as_router", False))
    with env.state() as st:
        try:
            q = store.pop_queue(st, a.qid)
        except KeyError:
            raise Fail(f"决定单里没有 {a.qid}")
        if as_router:
            _check_as_router(st, q, a.reject)
        if q["kind"] in ("verify_problem", "close"):      # 格式不对 ⇒ Fail，不存盘：项留在决定单里
            return _answer_business(env, st, q, a.text)
        said = " ".join(a.text.splitlines())
        _append_local(env.home / "answers.md",
                      f"- {env.now()} | {q['id']} | {q['kind']} | {q['ticket'] or '-'} | "
                      f"{f'{ROUTER_LABEL}：{said}' if as_router else said}")
        resumed, delivered, hint, extra = None, None, "", {}
        t = st["tickets"].get(q["ticket"]) if q["ticket"] else None
        topic = q.get("topic")
        # 判定顺序：topic（base / retarget / dispatch）→ 接管中 → 续跑 worker → 待你动手（走查 / 核对）
        if topic == "base":
            bp = st.get("base_problem")
            if bp and bp.get("qid") == q["id"]:
                if bp.get("expected_sha"):               # H1：回答 ≠ 接受当前 tip；保留预期 sha，下一轮照样重验
                    bp.update(qid=None, answered_at=env.now())
                else:
                    st.pop("base_problem")
            hint = "下一轮巡检重查远端 base（仍不对会重新进决定单）"
        elif topic == "retarget":
            if t:
                t.pop("retarget_fail", None)
                t.pop("wrong_base_reported", None)
            hint = "下一轮重验 PR（不唤醒 worker）"
        elif topic == "dispatch":
            if t:
                t.pop("dispatch_fail", None)
            hint = "下一轮重派"
        elif topic == "requeue":                         # 判定后没排上队：再排一次（不在旧 worker 上续跑）
            if not t:
                raise Fail(f"{q['id']} 指向的票不在了")
            said = f"{ROUTER_LABEL if as_router else USER_LABEL}：{a.text}"     # 回答的原话原样带给新 worker
            _requeue_business(env, st, t["id"], f"{q['next']}\n{said}" if q.get("next") else said, REQUEUE_WAIT)
            hint = "已重新排队，调度器派新 worker"
        elif topic == "next":                            # 「要下一个方向吗」：用户的回答 = 下一个方向（同 redirect --as-user）
            if not t:
                raise Fail(f"{q['id']} 指向的票不在了")
            extra = _redirect_core(env, st, t["id"], a.text, "user", record_answer=False)
            extra.pop("ok", None)
            hint = "已换方向、重新排队，调度器派新 worker"
        elif q["kind"] == "postlaunch":
            hint, extra = _postlaunch_answered(env, st, t, q)
        elif t and t["status"] == store.TAKEOVER:
            hint = f"{t['id']} 在你接管中，回答已存档；还给 router 后再续跑"
        elif t and q["kind"] in RESUME_KINDS and t["worker"].get("session_id"):
            kind = q["kind"]
            if kind == "card" and a.reject:
                kind = "reject"
                t["card_confirmed"] = False
                t.pop("card_confirmed_by", None)
                t["solution"]["source"] = re.sub(r"（.*?）$", "（被驳回）", t["solution"]["source"])
            elif kind == "card":                     # 方向卡只由用户确认（--as-router 在 _check_as_router 已拒）
                t["card_confirmed"] = True
                t["card_confirmed_by"] = "user"
                t["solution"]["source"] = t["solution"]["source"].replace("待你确认", "已确认")
            status = store.EXECUTING if t.get("card_confirmed") else store.KICKOFF
            how = _resume(env, st, t["id"], kind, a.text, status, force_restart=(kind == "stuck"), as_router=as_router)
            resumed, delivered = (t["id"], None) if how == "resumed" else (None, t["id"])
            if kind == "spend":                      # 原话照样原样转给 worker；这里只报 router 怎么读的，供核对
                extra = {"go": _said_go(a.text)}
        elif t and q["kind"] == "handson":
            if a.reject:
                if t["status"] == store.MERGED:
                    raise Fail(f"{t['id']} 是仓内票、已合 staging，没有驳回通道：不满意请 add 一张跟进票")
                if t["status"] != store.AWAIT_CHECK:
                    raise Fail(f"{t['id']} 状态是 {t['status']}，不在等你核对")
                wt = t["worker"].get("worktree")
                if not wt or not Path(wt).is_dir():
                    raise Fail(f"{t['id']} 的 worktree 已不在，不能续跑：不满意请 add 一张跟进票")
                how = _resume(env, st, t["id"], "offrepo_reject", a.text, store.EXECUTING)
                resumed, delivered = (t["id"], None) if how == "resumed" else (None, t["id"])
            else:
                extra = _checked(env, st, t)
                hint = extra.pop("hint", "")
        elif q["kind"] == "start":
            hint = "派发单要用 start 子命令"
        elif q["kind"] == "prod" and q.get("batch"):   # 整批项（有业务票等上 prod）：放行 = 整批一起上
            hint, extra = _prod_answered(env, st, q, a.text)
        elif q["kind"] == "prod":
            hint = "按 aifriends 发版流程上 prod，发完用 released"
    return {"ok": True, "queue": q["id"], "kind": q["kind"], "resumed": resumed, "delivered": delivered, "hint": hint,
            **extra}


# ---- 业务票：提议 → 判真 -----------------------------------------------------

VERIFY_HINT = "格式：「真」/「真，先放着」/「不是：理由」"
CLOSE_HINT = "格式：「关」/「不关」"
_CONF = ("low", "mid", "high")
_END = r"(?:[\s，,。.！!；;、]*)$"                       # 词后只许空白 / 标点
_CLOSE = re.compile(r"^关" + _END)
_NOT_CLOSE = re.compile(r"^不关" + _END)
_TRUE = re.compile(r"^真(?:\s*[，,]\s*先放着)?" + _END)
_NO = re.compile(r"^不是[：:]\s*(\S.*)$", re.S)


def _unit(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and 0 <= x <= 1


# 产品诊断（v2.1 第 3 条）：每条问题先回答这五问，再写技术卡点（walls）
PRODUCT_KEYS = {"want_now": "他此刻想要什么", "we_gave": "我们给了什么", "not_worth": "哪里无聊 / 不值得",
                "why_pay": "凭什么值得他付钱或回来", "peers": "同类产品通常怎么做（可写「待查」）"}


def _check_product(i, p, required) -> None:
    prod = p.get("product")
    if prod is None and not required:
        return
    if not isinstance(prod, dict):
        bad = list(PRODUCT_KEYS)
    else:
        bad = [k for k in PRODUCT_KEYS if not isinstance(prod.get(k), str) or not prod[k].strip()]
    if bad:
        raise Fail(f"problems[{i}]（{p.get('key')}）product（产品诊断）缺 {'、'.join(bad)}："
                   + "；".join(f"{k} = {v}" for k, v in PRODUCT_KEYS.items()))


def _product_line(prod) -> str:
    """产品诊断 → 一行（派单、判真项用）；没有 → 空串。"""
    if not isinstance(prod, dict):
        return ""
    return "；".join(f"{v.split('（')[0]}：{prod[k]}" for k, v in PRODUCT_KEYS.items() if str(prod.get(k, "")).strip())


def _check_problem(i, p, need_product=False) -> None:
    if not isinstance(p, dict):
        raise Fail(f"problems[{i}] 不是对象")
    for k in ("key", "title", "segment", "want"):
        if not isinstance(p.get(k), str) or not p[k].strip():
            raise Fail(f"problems[{i}] 缺 {k}")
    strs = lambda v: isinstance(v, list) and all(isinstance(x, str) and x.strip() for x in v)
    if not strs(p.get("walls")) or not p["walls"]:
        raise Fail(f"problems[{i}]（{p['key']}）walls 须是非空的字符串列表")
    if not strs(p.get("evidence")):
        raise Fail(f"problems[{i}]（{p['key']}）evidence 须是字符串列表")
    m = p.get("metric")
    if not isinstance(m, dict) or not isinstance(m.get("name"), str) or not m["name"].strip():
        raise Fail(f"problems[{i}]（{p['key']}）metric 须是带 name 的对象")
    if not isinstance(p.get("absorbs", []), list):
        raise Fail(f"problems[{i}]（{p['key']}）absorbs 须是列表")
    imp = p.get("impact")
    if not isinstance(imp, dict) or imp.get("confidence") not in _CONF or not _unit(imp.get("size")) \
            or not _unit(imp.get("gap")):
        raise Fail(f"problems[{i}]（{p['key']}）impact 不合法：confidence 须是 {'/'.join(_CONF)}，"
                   f"size 和 gap 须是 0~1 的数")
    _check_product(i, p, need_product)


def _rejected_keys(env) -> set:
    path = env.home / "analyst" / "rejected.jsonl"
    keys = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                keys.add(json.loads(line)["key"])
            except (ValueError, KeyError, TypeError):
                continue
    return keys


def propose_from(env, st, data) -> dict:
    """分析 agent 的提议写进 st（调用方持锁、负责存盘）。整份拒收的三条（校准门 / 泄漏 / impact）都在写 st 之前跑完。
    maint 不在这里建票（T7 接 maint_add），原样放进 maint_pending。"""
    mode = data.get("mode")
    if mode not in ("weekly", "migrate"):
        raise Fail("mode 须是 weekly 或 migrate")
    if mode == "weekly" and (st.get("analyst") or {}).get("calibrated") is not True:
        raise Fail("校准未过：analyst 校准通过前，weekly 的提议一律不收（migrate 不受限）")
    problems, walls, close = data.get("problems", []), data.get("walls", []), data.get("close", [])
    maint = data.get("maint", [])
    if data.get("attach"):
        raise Fail("attach（老票并进已开的业务票）不经 propose：用户确认整份迁移清单后用 migrate-apply 落")
    for i, p in enumerate(problems):
        _check_problem(i, p, need_product=mode == "weekly")      # 每周的新问题必须先有产品诊断（v2.1 第 3 条）
    for i, m in enumerate(maint):
        _check_maint(env, st, i, m)
    for w in walls:
        if w.get("ticket") not in st["tickets"] or store.ticket_kind(st["tickets"][w["ticket"]]) != "business":
            raise Fail(f"walls 里的票 {w.get('ticket')} 不是已有的业务票")
    for c in close:
        if c.get("ticket") not in st["tickets"] or store.ticket_kind(st["tickets"][c["ticket"]]) != "business":
            raise Fail(f"close 里的票 {c.get('ticket')} 不是已有的业务票")
    olds = []
    for p in problems:                                # 和 migrate_check 一样严：只收存在、没并过的老票，一张只并一处
        for oid in p.get("absorbs", []):
            o = st["tickets"].get(oid) if isinstance(oid, str) else None
            if o is None:
                raise Fail(f"problems（{p['key']}）absorbs 里的票 {oid} 不存在")
            if store.ticket_kind(o) != "legacy" or o.get("parent"):
                why = f"已并进 {o['parent']}" if o.get("parent") else f"kind={store.ticket_kind(o)}"
                raise Fail(f"problems（{p['key']}）absorbs 里的 {oid} 不是没并过的老票（{why}）：不改挂")
            olds.append(oid)
    dup = sorted({o for o in olds if olds.count(o) > 1})
    if dup:
        raise Fail(f"同一张老票只能并进一个问题：{dup}")
    _refuse_leaks(env, (*problems, *maint, *walls, *close))
    rejected, at = _rejected_keys(env), env.now()
    pending = {(st.get("proposals", {}).get(q.get("proposal") or "") or {}).get("key")
               for q in st["queue"] if q["kind"] == "verify_problem"}
    out, skipped, small = [], [], []
    for p in problems:
        if p["key"] in pending or (p["key"] in rejected and not (p.get("new_evidence") or "").strip()):
            skipped.append(p["key"])
            continue
        if (p.get("impact") or {}).get("size", 1) < SMALL_SHARE:   # 人群 < 10%：不成问题（v2.1 补充 A），该写进 notes
            small.append(p["key"])
            continue
        st["seq"]["proposal"] = st["seq"].get("proposal", 0) + 1
        pid = f"P{st['seq']['proposal']}"
        st.setdefault("proposals", {})[pid] = {**p, "id": pid, "mode": mode, "at": at}
        prod = _product_line(p.get("product"))
        qid = store.push_queue(st, None, "verify_problem",
                               f"判真：{p['title']}（{p['segment']}）" + (f"｜产品诊断：{prod}" if prod else ""), at)
        st["queue"][-1]["proposal"] = pid
        out.append({"id": pid, "key": p["key"], "title": p["title"], "queue": qid})
    _flag_same_metric(st, [x["id"] for x in out])
    offered = []
    for w in walls:
        offered.append(_add_wall(env, st, w["ticket"], w["text"], "weekly")[1])
    for c in close:
        qid = store.push_queue(st, c["ticket"], "close", f"提议结束 {c['ticket']}：{c.get('reason', '')}", at)
        st["queue"][-1]["reason"] = c.get("reason", "")
    mids = []
    for m in maint:
        tid = maint_add(env, st, m)
        if tid not in mids:
            mids.append(tid)
    # 只建 / 追加 DRAFT，不在这里 start：start 会建 issue、起 worker、中途落盘，不能放进可回滚的收产出里
    to_start = [t for t in mids if st["tickets"][t]["status"] == store.DRAFT]
    return {"ok": True, "proposals": out, "skipped": skipped, "skipped_small": small, "maint": mids,
            "to_start": to_start, "next_items": [q for q in offered if q]}


SMALL_SHARE = 0.1          # v2.1 补充 A：人群 / 解释的流失不到 10% 的不成问题、不成墙


def _metric_key(m) -> str:
    return re.sub(r"\s+", "", str((m or {}).get("name", "") if isinstance(m, dict) else m or "")).lower()


def _flag_same_metric(st, pids) -> None:
    """新进决定单的判真提议：和别的待判提议、或在途的业务票同一指标 ⇒ 记 same_metric、判真项摘要里提一句
    （v2.1 第 2 条：同一指标只出一条，router 判真前先按根归并）。只提示，不拦。"""
    props = st.get("proposals") or {}
    waiting = {q.get("proposal") for q in st["queue"] if q["kind"] == "verify_problem"}
    live = {tid: _metric_key(t.get("metric")) for tid, t in st["tickets"].items()
            if store.ticket_kind(t) == "business" and t["status"] not in (store.DONE, store.SKIP)}
    for pid in pids:
        key = _metric_key(props[pid].get("metric"))
        if not key:
            continue
        same = [o for o in sorted(waiting) if o and o != pid and _metric_key((props.get(o) or {}).get("metric")) == key]
        same += [tid for tid, k in live.items() if k == key]
        if not same:
            continue
        props[pid]["same_metric"] = same
        q = next(q for q in st["queue"] if q.get("proposal") == pid)
        q["summary"] += f"（和 {'、'.join(same)} 同指标「{props[pid]['metric'].get('name', '')}」：先看是不是同一目标，是就并成一条）"


def _refuse_leaks(env, items) -> None:
    """要进 GitHub 的提议文本过防泄漏闸：闸失效或命中 ⇒ 整份拒收。"""
    try:
        guard = github.load_guard(env.cfg.get("leak_guard_globs", []))
    except github.GuardEmpty as e:
        raise Fail(f"防泄漏闸失效，提议一条都没收：{e}")
    texts = [json.dumps(x, ensure_ascii=False) for x in items]
    hits = sorted({h for tx in texts for h in github.leak_hits(tx, guard)})
    if hits:
        raise Fail("提议里有 uid / 用户原话，整份拒收", hits=hits)


def migrate_check(env, st, data) -> None:
    """迁移清单（migrate 产出）整份校验，一条不合规就整份拒：problems 照判真提议的校验；absorbs / attach 里的
    都是存在、没并进别处的老票，一张老票只出现一次；attach / walls 的目标是已开的业务票；文本过防泄漏闸。不改 st。"""
    problems, attach, walls = data.get("problems", []), data.get("attach", []), data.get("walls", [])
    for name, v in (("problems", problems), ("attach", attach), ("walls", walls)):
        if not isinstance(v, list):
            raise Fail(f"{name} 须是列表")
    for i, p in enumerate(problems):
        _check_problem(i, p)
    for i, x in enumerate(attach):
        if not isinstance(x, dict):
            raise Fail(f"attach[{i}] 不是对象")
        _check_absorb(st, x.get("ticket"), x.get("absorbs"))
    for p in problems:
        for oid in p.get("absorbs", []):
            o = st["tickets"].get(oid)
            if o is None or store.ticket_kind(o) != "legacy" or o.get("parent"):
                raise Fail(f"problems（{p['key']}）absorbs 里的 {oid} 不是没并过的老票")
    olds = [oid for p in problems for oid in p.get("absorbs", [])] + [oid for x in attach for oid in x["absorbs"]]
    dup = sorted({o for o in olds if olds.count(o) > 1})
    if dup:
        raise Fail(f"同一张老票只能并进一个问题：{dup}")
    for w in walls:
        if not isinstance(w, dict) or w.get("ticket") not in st["tickets"] \
                or store.ticket_kind(st["tickets"][w["ticket"]]) != "business" or not str(w.get("text", "")).strip():
            raise Fail(f"walls 里的票 {w.get('ticket') if isinstance(w, dict) else w} 不是已有的业务票，或墙文本为空")
    _refuse_leaks(env, (*problems, *attach, *walls))


def _check_maint(env, st, i, m) -> None:
    if not isinstance(m, dict):
        raise Fail(f"maint[{i}] 不是对象")
    for k in ("title", "problem"):
        if not isinstance(m.get(k), str) or not m[k].strip():
            raise Fail(f"maint[{i}] 缺 {k}")
    surf = m.get("surfaces")
    if not isinstance(surf, list) or not all(isinstance(x, str) for x in surf):
        raise Fail(f"maint[{i}] surfaces 须是代码面名字的列表")
    _check_surfaces(env, surf)
    unb = m.get("unblocks")
    if unb and (not isinstance(unb, str) or unb not in st["tickets"]):
        raise Fail(f"maint[{i}] unblocks 里的票 {unb} 不存在")


def _iso_week(env) -> str:
    y, w, _ = env.ctx.now().isocalendar()
    return f"{y}-W{w:02d}"


_MAINT_CLOSED = set(store.SETTLED) | {store.SKIP}


def maint_add(env, st, item) -> str:
    """维护条目 → 票号。调用方持锁、已载入 st；只建 / 追加（DRAFT），永不 start（start 由调用方在存盘之后另做）。
    不带 unblocks：追加到本周维护票（没有就建）；带 unblocks：单独建一张，不受维护名额限制。
    幂等：同 title+problem 的条目不重复追加；同 unblocks+同 title 的未结束票复用。"""
    at = env.now()
    surf = list(item.get("surfaces", []))
    task = {"text": f"{item['title']}：{item['problem']}", "done": False, "at": at}
    unb = item.get("unblocks")
    live = lambda t: store.ticket_kind(t) == "maint" and t["status"] not in _MAINT_CLOSED
    if unb:
        title = f"维护（解锁 {unb}）：{item['title']}"
        tid = next((x for x, t in st["tickets"].items() if live(t) and t.get("unblocks") == unb
                    and t["title"] == title), None)
        if tid is None:
            tid = store.new_ticket(
                st, title=title, origin="analyst", kind="maint", unblocks=unb, tasks=[task], surfaces=surf,
                weight=surfaces.ticket_weight("maint", surf, env.table), problem=task["text"])
            st["tickets"][tid]["history"].append({"at": at, "status": store.DRAFT})
        return tid
    title = f"维护 {_iso_week(env)}"
    tid = next((x for x, t in st["tickets"].items() if live(t) and not t.get("unblocks") and t["title"] == title), None)
    if tid is None:
        tid = store.new_ticket(st, title=title, origin="analyst", kind="maint", tasks=[task], surfaces=surf,
                               weight=surfaces.ticket_weight("maint", surf, env.table), problem=task["text"])
        st["tickets"][tid]["history"].append({"at": at, "status": store.DRAFT})
        return tid
    t = st["tickets"][tid]
    if any(x["text"] == task["text"] for x in t["tasks"]):
        return tid
    t["tasks"].append(task)
    t["surfaces"] = sorted(set(t["surfaces"]) | set(surf))
    t["weight"] = surfaces.ticket_weight("maint", t["surfaces"], env.table)
    t["problem"] = "；".join(x["text"] for x in t["tasks"])
    if t["status"] in store.RUNNING:
        # worker 活着就是即时通知；已死则这条留在 inbox，续跑时 _resume 读 inbox 一并带给它
        events.deliver(env.home, tid, {"ts": at, "ticket": "router", "type": "tell",
                                       "summary": f"本周维护票新增一条：{task['text']}"})
    return tid


def start_maint(env, ids) -> dict:
    """存盘之后再 start 维护票（调用方持锁）：失败时票仍是 DRAFT，下一轮巡检重试。"""
    with_st = store.load_state(env.state_path)
    ids = [t for t in ids if with_st["tickets"].get(t, {}).get("status") == store.DRAFT]
    if not ids:
        return {"started": []}
    return _start_locked(env, with_st, ids)


def cmd_maint(a, env):
    item = {"title": a.title, "problem": a.problem, "surfaces": _csv(a.surfaces), "unblocks": a.unblocks}
    with env.state() as st:
        _check_maint(env, st, 0, item)
        tid = maint_add(env, st, item)
        pending = st["tickets"][tid]["status"] == store.DRAFT
    if pending:
        try:
            with env.locked():
                start_maint(env, [tid])
        except Exception as e:  # noqa: BLE001 — Fail / GhError 都算 start 没成
            raise Fail(f"维护条目已存为 DRAFT（{tid}），start 没成：{e}；下一轮巡检会重试", ticket=tid,
                       **(e.extra if isinstance(e, Fail) else {}))
    return {"ok": True, "ticket": tid}


def cmd_impact(a, env):
    if not _unit(a.size) or not _unit(a.gap):
        raise Fail("size 和 gap 须是 0~1 的数")
    with env.state() as st:
        t = env.ticket(st, a.ticket)
        if store.ticket_kind(t) != "business":
            raise Fail(f"{a.ticket} 不是业务票，没有影响分")
        old = t.get("impact") or {}
        t["impact"] = {"size": a.size, "gap": a.gap, "confidence": a.confidence}
        did = store.append_decision(env.decisions, st, a.ticket, "调整影响分",
                                    f"router 改 impact：size {a.size} gap {a.gap} confidence {a.confidence}",
                                    f"原值 {json.dumps(old, ensure_ascii=False)}", env.now())
    return {"ok": True, "ticket": a.ticket, "impact": t["impact"], "decision": did}


def cmd_propose(a, env):
    data = json.loads(Path(a.source).read_text(encoding="utf-8"))
    with env.state() as st:
        res = propose_from(env, st, data)
    if res["to_start"]:                               # 提议已存盘；start 另做，失败不丢提议
        try:
            with env.locked():
                start_maint(env, res["to_start"])
        except Exception as e:  # noqa: BLE001 — Fail / GhError 都算 start 没成
            raise Fail(f"提议已存盘，维护票 {' '.join(res['to_start'])} 存为 DRAFT、start 没成：{e}；下一轮巡检会重试",
                       **{**(e.extra if isinstance(e, Fail) else {}), "proposals": res["proposals"],
                          "maint": res["maint"]})
    return res


def _answer_business(env, st, q, text) -> dict:
    t = (text or "").strip()
    if q["kind"] == "close":
        if _NOT_CLOSE.match(t):
            verb = "keep"
        elif _CLOSE.match(t):
            verb = "close"
        else:
            raise Fail(f"回答不认识。{CLOSE_HINT}")
    else:
        m = _NO.match(t)
        if m:
            verb, reason = "no", m.group(1).strip()
        elif _TRUE.match(t):
            verb = "hold" if "先放着" in t else "true"
        elif t.startswith("不是"):
            raise Fail(f"「不是」后面必须带理由。{VERIFY_HINT}")
        else:
            raise Fail(f"回答不认识。{VERIFY_HINT}")
    _append_local(env.home / "answers.md",
                  f"- {env.now()} | {q['id']} | {q['kind']} | {q['ticket'] or '-'} | {' '.join(t.splitlines())}")
    if q["kind"] == "close":
        res = {"ok": True, "queue": q["id"], "kind": "close", "ticket": q["ticket"], "closed": verb == "close"}
        if verb == "close":
            _close_business(env, st, q["ticket"], q.get("reason", ""), f"用户回答 {q['id']}：{t}")
        else:                                         # 不关：空闲的票要有下一个方向（终审 #3）
            nq = _offer_next(env, st, q["ticket"], "用户回「不关」")
            if nq:
                res["next_item"] = nq
        return res
    prop = st.get("proposals", {}).get(q.get("proposal") or "")
    if prop is None:
        raise Fail(f"{q['id']} 指向的提议 {q.get('proposal')} 不存在")
    if verb == "no":
        _append_local(env.home / "analyst" / "rejected.jsonl",
                      json.dumps({"key": prop["key"], "title": prop["title"], "reason": reason, "at": env.now()},
                                 ensure_ascii=False))
        return {"ok": True, "queue": q["id"], "kind": "verify_problem", "rejected": prop["key"]}
    tid, skipped = _ticket_from_problem(env, st, prop)
    res = {"ok": True, "queue": q["id"], "kind": "verify_problem", "ticket": tid, "held": verb == "hold"}
    if skipped:                                       # 提议之后被并进了别处：不改挂，报出来
        res["absorb_skipped"] = skipped
    if verb == "true":
        res["start"] = _start_locked(env, st, [tid])
    return res


def _ticket_from_problem(env, st, prop):
    """判真 / 确认迁移清单之后：提议 → 业务票（DRAFT）；absorbs 里的老票并进来（R1：验收过的记历史尝试）。
    → (票号, 没并的老票)。提议之后已并进别处 / 不是老票 / 不在了的不改挂（终审 #11），由调用方报出来。"""
    ok, skipped = [], []
    for oid in prop.get("absorbs", []):
        o = st["tickets"].get(oid) if isinstance(oid, str) else None
        good = o is not None and store.ticket_kind(o) == "legacy" and not o.get("parent") and oid not in ok
        (ok if good else skipped).append(oid)
    tid = store.new_business_ticket(
        st, title=prop["title"], segment=prop["segment"], want=prop["want"], walls=prop["walls"],
        metric=prop["metric"], evidence=prop.get("evidence", []), impact=prop["impact"], origin="analyst",
        absorbs=ok)
    nt = st["tickets"][tid]
    if isinstance(prop.get("product"), dict):        # 产品诊断留在本机（派单里给 worker 看），不进 GitHub
        nt["product"] = dict(prop["product"])
    _biz_issue_fields(nt)
    nt["history"].append({"at": env.now(), "status": store.DRAFT})
    for old_id in nt["absorbs"]:
        _link_legacy(st, tid, old_id)
    return tid, skipped


def cmd_migrate_apply(a, env):
    """用户确认整份迁移清单之后落它（controller 裁决：migrate 产出不自动落）。
    问题 → 业务票（确认 = 判真，默认 start；--hold 的只建票）；absorbs / attach 并老票；walls 挂到已开的业务票。
    --skip 去掉用户不要的条目（问题写 key，attach / walls 写业务票号）。整份校验过才落；一份 run 只落一次。"""
    said = (a.said or "").strip()
    if not said:
        raise Fail("--said 要写用户确认迁移清单时的原话")
    skip, hold = set(_csv(a.skip)), set(_csv(a.hold))
    with env.state() as st:
        run = ((st.get("analyst") or {}).get("runs") or {}).get(a.run)
        if run is None:
            raise Fail(f"没有分析 agent 的 run {a.run}")
        if run.get("mode") != "migrate":
            raise Fail(f"{a.run} 是 {run.get('mode')}：migrate-apply 只收 migrate 的产出")
        if run.get("failed"):
            raise Fail(f"{a.run} 失败了（{run.get('error', '')}）：先重跑 migrate")
        if run.get("applied"):
            raise Fail(f"{a.run} 已在 {run['applied']} 落过，不重复落")
        data, err = analyst.read_out(run["out"])
        if err:
            raise Fail(err)
        ignored = [k for k in ("maint", "close") if data.get(k)]   # 迁移不收维护项、不提议结束：报忽略了
        lists = {k: data.get(k, []) for k in ("problems", "attach", "walls")}
        bad = [k for k, v in lists.items() if not isinstance(v, list)]
        if bad:
            raise Fail(f"产出里 {'、'.join(bad)} 不是列表：整份不落，重跑 migrate")
        field = {"problems": "key", "attach": "ticket", "walls": "ticket"}
        named = {k: {x.get(field[k]) for x in v if isinstance(x, dict)} for k, v in lists.items()}
        unknown = sorted((skip - named["problems"] - named["attach"] - named["walls"]) | (hold - named["problems"]))
        if unknown:
            raise Fail(f"--skip / --hold 里有清单上没有的：{unknown}（问题写 key；--skip 还可写 attach / walls 的业务票号）")
        # 用户去掉的条目先拿掉，剩下的（坏条目原样留着，让校验拒）整份校验
        data = {k: [x for x in v if not (isinstance(x, dict) and x.get(field[k]) in skip)] for k, v in lists.items()}
        migrate_check(env, st, data)
        at = env.now()
        _append_local(env.home / "answers.md", f"- {at} | - | migrate-apply | {a.run} | {' '.join(said.splitlines())}")
        created = [(_ticket_from_problem(env, st, p)[0], p["key"]) for p in data["problems"]]
        attached = {}
        for x in data["attach"]:
            for oid in x["absorbs"]:
                _link_legacy(st, x["ticket"], oid)
            attached.setdefault(x["ticket"], []).extend(x["absorbs"])
        for w in data["walls"]:
            _add_wall(env, st, w["ticket"], w["text"], "migrate")
        did = store.append_decision(
            env.decisions, st, "-",
            f"迁移清单已落（{a.run}）：新业务票 {' '.join(t for t, _ in created) or '无'}"
            + "".join(f"；{olds} 并进 {t}" for t, olds in attached.items()),
            "用户确认了整份迁移清单（spec §4.7）" + (f"；去掉 {'、'.join(sorted(skip))}" if skip else ""),
            "用户原话见 answers.md（migrate-apply）", at)
        run["applied"] = at
        to_start = [t for t, k in created if k not in hold]
    res = {"ok": True, "run": a.run, "tickets": [t for t, _ in created], "held": [t for t, k in created if k in hold],
           "attached": attached, "decision": did, "ignored": ignored}
    if to_start:                                      # 先存再 start：start 建 issue、起 worker，失败不丢已落的清单
        try:
            with env.locked():
                res["start"] = start_maint(env, to_start)
        except Exception as e:  # noqa: BLE001
            raise Fail(f"迁移已落，{' '.join(to_start)} 存为草稿、start 没成：{e}；修好后 router.py start {' '.join(to_start)}",
                       **res)
    return res


def _inflight_change(env, t) -> tuple:
    """业务票还有在途的改动（关票前要先判完或撤回）→ (说明, 这次在途改动的撤回办法)；没有 → ("", "")。
    看改动在不在，不看票现在的状态（残留 2 #1：stop / takeover 之后照样拦）：
    合进了 staging 还没 released（store.staging_pending）⇒ 撤回 = revert 这次的 PR / 合并 sha；
    仓外改动还在真身上、还没核对上线（store.offrepo_pending）⇒ 撤回 = 从备份恢复；
    已上线的尝试还在等判定 ⇒ 撤回 = 这次尝试 released 时记的 --rollback。
    撤回办法都指向这次在途的改动，不是上一次已判完的尝试（残留 2 #4）。"""
    tid, found = t["id"], []
    if store.staging_pending(t):
        ref = f"PR #{t.get('pr') or '?'}、合并 sha {t.get('merge_sha')}"
        found.append((f"还有在途的改动（合进了 staging、还没上线判定：{ref}）",
                      f"撤回 staging 上这次的改动：revert {ref}"))
    if store.offrepo_pending(t):
        found.append(("还有在途的改动（在仓外真身上、还没核对上线）",
                      f"从 {env.home}/backups/{tid}/ 恢复仓外改动（改了什么见 {env.home}/cards/{tid}.diff）"))
    if store.attempt_inflight(t):
        cur = store.current_attempt(t) or {}
        found.append((f"还有在途尝试（尝试 {cur.get('n', '?')} 已上线、还在等 daily 判定）",
                      cur.get("rollback") or "（这次尝试没记撤回办法）"))
    return "；".join(w for w, _ in found), "；".join(r for _, r in found)


def _close_business(env, st, tid, why, basis, rolled_back=False) -> None:
    """关业务票（「Qn 关」和「关 Tn」共用）：走 v1 收尾（下一轮巡检关 issue、清 worktree / 分支），清掉它在决定单上的项。
    还有在途尝试 / 改动（终审残留 #1；按改动在不在判，stop / takeover 绕不过）⇒ 拒：关了之后有害判定就落不下、撤不回。
    等 daily 判完，或 router 先照这次在途改动的撤回办法撤回、再 close --rolled-back（记一条 decide + rolled_back_at）。"""
    ct = env.ticket(st, tid)
    if store.ticket_kind(ct) != "business":
        raise Fail(f"{tid} 不是业务票，不能这样关")
    if ct["status"] == store.DONE:
        raise Fail(f"{tid} 已经关了")
    inflight, rb = _inflight_change(env, ct)
    if inflight and not rolled_back:
        raise Fail(f"{tid} {inflight}：先等 daily 判完再关；要现在关，先照撤回办法撤回（{rb}），"
                   f"再 router.py close {tid} --rolled-back")
    _refuse_pending(ct, tid, env.ctx)                 # 派单待核实 / 认不出的 worker：可能正在起，不关
    pid = ct["worker"].get("pid")
    if pid and env.ctx.pid_alive(pid):
        raise Fail(f"{tid} 的 worker 还在跑（pid {pid}）：先 stop {tid}，再关")
    ct["closeout"] = {"downstream": _closeout_targets(st, tid),
                      "issue": "pending" if ct.get("issue") else "n/a",
                      "worktree": "pending", "branch": "pending"}
    cur, olds = ct["worker"].get("branch"), []
    for x in ct.get("attempts") or []:               # 旧尝试的本地分支：只删已合并的（尝试上记着 merge_sha）
        b = x.get("branch")
        if b and x.get("merge_sha") and not x.get("historical") and b != cur and b not in olds:
            olds.append(b)
    if olds:
        ct["closeout"]["old_branches"] = {b: "pending" for b in olds}
    st["queue"] = [q for q in st["queue"] if q.get("ticket") != tid]
    store.set_status(st, tid, store.DONE, env.now(), "收尾中（下一轮巡检：关 issue、清 worktree）")
    if inflight:                                      # --rolled-back：router 担保已撤回，记下来
        ct["rolled_back_at"] = env.now()              # 发版批次 / released 认它：关前已撤回，代码可能还在 staging 上
        store.append_decision(env.decisions, st, tid, f"关票前已撤回在途改动：{inflight}",
                              "close --rolled-back：router 已照撤回办法撤回", f"撤回办法：{rb}", env.now())
    store.append_decision(env.decisions, st, tid, "关闭业务票", why, basis, env.now())


def _legacy_attempt(st, tid, old) -> None:
    """老票 old（parent = 业务票 tid）有上线验收结论 ⇒ 在 tid 上记 / 更新一条历史尝试（R1 形状），结论取最新一条。
    老票上记着它对应的尝试号（parent_attempt），之后的结论只更新那一条，不重复记。没上线验收过的不记。"""
    pl = old.get("postlaunch") or {}
    results = pl.get("results") or []
    if old["status"] not in (store.POSTLAUNCH, store.DONE) or not results:
        return
    nt = st["tickets"][tid]
    rec = {"direction": old["title"], "shipped_at": pl.get("t0"), "verdict": "legacy",
           "legacy_verdict": results[-1].get("verdict"), "historical": True}
    n = old.get("parent_attempt")
    cur = next((x for x in nt["attempts"] if n and x.get("n") == n and x.get("historical")), None)
    if cur is not None:
        cur.update(rec)
        return
    n = max((x.get("n", 0) for x in nt["attempts"]), default=0) + 1
    live = next((i for i, x in enumerate(nt["attempts"]) if not x.get("historical")), len(nt["attempts"]))
    nt["attempts"].insert(live, {"n": n, **rec})     # 历史尝试排在所有 live 尝试之前：列表最后一条永远是当前尝试
    old["parent_attempt"] = n


def _link_legacy(st, tid, old_id) -> None:
    """老票并进业务票 tid（spec §4.7）：设 parent；已上线验收过的立即记一次历史尝试，还在做的等它出了上线验收结论再记
    （postlaunch-result 里的钩子）。幂等。"""
    old = st["tickets"][old_id]
    old["parent"] = tid
    absorbs = st["tickets"][tid].setdefault("absorbs", [])
    if old_id not in absorbs:
        absorbs.append(old_id)
    _legacy_attempt(st, tid, old)


def _check_absorb(st, tid, olds) -> None:
    """并老票的校验（absorb 命令和 migrate 的 attach 共用）：目标是业务票；并的都是存在的老票，没并进别的业务票。"""
    t = st["tickets"].get(tid)
    if t is None:
        raise Fail(f"没有 {tid}")
    if store.ticket_kind(t) != "business":
        raise Fail(f"{tid} 不是业务票：只能并进业务票")
    if not isinstance(olds, list) or not olds or not all(isinstance(x, str) for x in olds):
        raise Fail(f"并进 {tid} 的老票须是非空的票号列表")
    for oid in olds:
        o = st["tickets"].get(oid)
        if o is None:
            raise Fail(f"没有 {oid}")
        if oid == tid or store.ticket_kind(o) != "legacy":
            raise Fail(f"{oid} 不是老票（kind={store.ticket_kind(o)}）：只有老票能并进业务票")
        if o.get("parent") not in (None, tid):
            raise Fail(f"{oid} 已经并进了 {o['parent']}")


NEW_CARD_REFUSE = ("换方向归用户定（v2.1）：router 不能自己定方向。要换方向就出新方向卡交用户确认："
                   "router.py redirect {t} --new-card（新 worker 照没拆的墙和已试方向提方向，卡交用户）；"
                   "用户给了方向：router.py redirect {t} \"<原话>\" --as-user")


def _redirect_core(env, st, tid, text, who, record_answer=True) -> dict:
    """业务票换方向（redirect 和回答「要下一个方向吗」项共用）：话写进收件箱、票回 queued，调度器派新 worker
    （读收件箱和尝试记录，出新方向卡，卡交用户确认）。不在原 worker 上 resume。清掉这张票的 harm / return / requeue / next 项。
    只看有没有在途尝试（已派、没 released，或上线了还没判完），不看上一次判了什么（终审 #3）。
    who：user = 用户定的方向（原话）；new_card = router 只要一张新方向卡，不定方向（v2.1 第 1 条：方向一律用户定）。
    退回用户（return 项没回）时只收用户定的方向；没用的连续计数只在用户定方向时归零（终审 #4）。"""
    if who not in ("user", "new_card"):
        raise Fail(NEW_CARD_REFUSE.format(t=tid))
    t = env.ticket(st, tid)
    if store.ticket_kind(t) != "business":
        raise Fail(f"{tid} 不是业务票：redirect 只给业务票（老票用 tell / add 跟进票）")
    if t["status"] in (store.DONE, store.SKIP):
        raise Fail(f"{tid} 已关（{t['status']}）：关掉的票不再换方向")
    if t["status"] in (store.DRAFT, store.HOLD):
        raise Fail(f"{tid} 还没 start（{t['status']}）：先 start，派出去的 worker 照墙出方向卡")
    if t["status"] != store.POSTLAUNCH:
        raise Fail(f"{tid} 状态是 {t['status']}：有在途尝试（已派、还没 released）。redirect 只收已上线（postlaunch）、"
                   "当前尝试已判完的业务票")
    last = store.current_attempt(t)
    verdict = (last or {}).get("verdict")
    waiting = next((a for a in t.get("attempts") or [] if not a.get("historical")
                    and a.get("verdict") in (None, "pending_sample")), None)     # 并票后可能不止一次在等判定
    if last is None or waiting is not None:
        w = waiting or last or {}
        raise Fail(f"{tid} 有在途尝试：尝试 {w.get('n', '?')} 还在等 daily 判定（{w.get('verdict') or '待判'}），判完再换方向")
    pending_return = [q["id"] for q in st["queue"] if q.get("ticket") == tid and q["kind"] == "return"]
    if pending_return and who != "user":
        raise Fail(f"{tid} 已退回用户（{'、'.join(pending_return)} 还没回）：下一步由用户定（换方向 / 重新定义问题 / 关），"
                   "他给了方向用 --as-user 记他的原话（router 不能出新卡绕过）")
    returned = t.get("waiting_for") == RETURN_WAIT or bool(pending_return)
    if who == "user":
        msg = (f"换方向（尝试 {last['n']} 判定 {verdict}）。{USER_LABEL}：{text}。"
               "先读尝试记录和没拆的墙，再从第 1 段照这个方向写新方向卡（卡交用户确认）。")
    else:
        msg = (f"出新方向卡（尝试 {last['n']} 判定 {verdict}）。router 没定方向：方向由你照没拆的墙和已试方向提，"
               "方向卡交用户确认。先读尝试记录和没拆的墙，对准第一道没拆的墙从第 1 段写新方向卡，别在已试的原路上再试。")
    _requeue_business(env, st, tid, msg, REDIRECT_WAIT)
    cleared = [q["id"] for q in st["queue"] if q.get("ticket") == tid
               and (q["kind"] in ("harm", "return") or q.get("topic") in ("requeue", "next"))]
    st["queue"] = [q for q in st["queue"] if q["id"] not in cleared]
    if returned and who == "user":                    # 退回用户后由他定了新方向：没用的连续计数重新算
        t["no_use_streak"] = 0
    if record_answer and who == "user":
        _append_local(env.home / "answers.md",
                      f"- {env.now()} | - | redirect | {tid} | {' '.join(text.splitlines())}")
    did = store.append_decision(
        env.decisions, st, tid, f"换方向：{text}" if who == "user" else "出新方向卡交用户（router 不定方向）",
        "用户定的方向（原话见 answers.md）" if who == "user" else "v2.1：方向一律用户定，新方向卡交用户确认",
        f"尝试 {last['n']} 判定 {verdict}" + ("；连续没用已退回用户" if returned else ""), env.now())
    return {"ok": True, "ticket": tid, "status": store.QUEUED, "cleared": cleared, "decision": did}


def cmd_redirect(a, env):
    """业务票换方向（9b + 终审 #3/#4 + v2.1 第 1 条）：见 _redirect_core。
    --as-user：用户定的方向（原话必填）；--new-card：router 只要一张新方向卡（不带方向）；--as-router 一律拒。"""
    text = (a.text or "").strip()
    if a.as_router:
        raise Fail(NEW_CARD_REFUSE.format(t=a.ticket))
    if a.new_card and text:
        raise Fail("--new-card 不带方向：方向由新 worker 提、用户在方向卡上确认（router 不定方向）")
    if a.as_user and not text:
        raise Fail("--as-user 要写用户定的方向（原话，逐字）")
    with env.state() as st:
        return _redirect_core(env, st, a.ticket, text, "user" if a.as_user else "new_card")


def _offer_next(env, st, tid, why):
    """空闲的业务票（已上线、当前尝试判了有用 / 墙拆了人没留住、没有别的待办项）有了新墙、或用户回「不关」：
    推一条 topic=next 的方向项问「要下一个方向吗」（v2.1：方向归用户；router 只能 redirect --new-card 出新卡交用户确认）。
    已经在问 / 有害 / 退回 / 卡住的票不推。→ 项号 | None。"""
    t = st["tickets"].get(tid)
    if not t or not store.router_owned(t) or t["status"] != store.POSTLAUNCH:
        return None
    cur = store.current_attempt(t)
    if not cur or cur.get("verdict") not in ("useful", "wall_broken_not_retained"):
        return None
    if any(q.get("ticket") == tid and (q["kind"] in ("harm", "return") or q.get("topic") in ("requeue", "next"))
           for q in st["queue"]):
        return None
    qid = store.push_queue(st, tid, "direction", f"{tid}：{why}；没有在途尝试——要下一个方向吗（方向归用户：你回方向，"
                           "或 router 出新方向卡交你确认）", env.now(), topic="next")
    t["waiting_for"] = f"{why}：等下一个方向（{qid}：你回方向，或 router 出新方向卡交你确认）"
    return qid


def _add_wall(env, st, tid, text, source):
    """挂墙（daily / weekly / migrate 共用）；挂到空闲的业务票上 ⇒ 顺手问下一个方向。→ (墙号, 方向项号 | None)。"""
    wid = store.attach_wall(st, tid, text, source, env.now())
    return wid, _offer_next(env, st, tid, f"新墙 {wid}「{' '.join(str(text).split())[:40]}」待拆")


def cmd_close(a, env):
    """用户说「关 Tn」：关业务票（同「Qn 关」的收尾）。worker 还活着 ⇒ 拒，先 stop。"""
    reason = (a.reason or "").strip()
    with env.state() as st:
        _close_business(env, st, a.ticket, reason or "用户要关",
                        f"用户：关 {a.ticket}" + (f"（{reason}）" if reason else ""), rolled_back=a.rolled_back)
        _append_local(env.home / "answers.md",
                      f"- {env.now()} | - | close | {a.ticket} | 关 {a.ticket}" + (f"：{reason}" if reason else ""))
    return {"ok": True, "ticket": a.ticket, "status": store.DONE}


def cmd_absorb(a, env):
    """把老票并进已有的业务票（spec §4.7）。全部校验过才动：目标是业务票；并的都是存在的老票，没并进别的业务票。"""
    with env.state() as st:
        _check_absorb(st, a.ticket, a.olds)
        t = st["tickets"][a.ticket]
        before = {x.get("n") for x in t["attempts"]}
        for oid in a.olds:
            _link_legacy(st, a.ticket, oid)
        added = [x["n"] for x in t["attempts"] if x.get("n") not in before]
        did = store.append_decision(env.decisions, st, a.ticket, f"并入老票 {' '.join(a.olds)}",
                                    "同一群人想要的同一件东西（spec §4.7）", "router.py absorb", env.now())
    return {"ok": True, "ticket": a.ticket, "absorbed": a.olds, "attempts_added": added, "decision": did}


# 并票（v2.1 第 2 条）：一个业务目标一张票，同一指标的两张业务票并成一张
MERGE_SOURCE_OK = {store.DRAFT, store.HOLD, store.QUEUED, store.POSTLAUNCH, store.PAUSED, store.STUCK}


def _merge_checks(env, st, tid, sid) -> None:
    if tid == sid:
        raise Fail("不能把票并进自己")
    for x in (tid, sid):
        t = env.ticket(st, x)
        if store.ticket_kind(t) != "business":
            raise Fail(f"{x} 不是业务票：merge 只并业务票（老票并进业务票用 absorb）")
        if t.get("merged_into"):
            raise Fail(f"{x} 已经并进了 {t['merged_into']}")
    t, s = st["tickets"][tid], st["tickets"][sid]
    if t["status"] in (store.DONE, store.SKIP):
        raise Fail(f"{tid} 已关（{t['status']}）：不能往关掉的票里并")
    if s["status"] not in MERGE_SOURCE_OK:
        raise Fail(f"{sid} 正在做一次尝试（{s['status']}）：等这次尝试上线（之后的判定会跟着并过去），"
                   f"或 stop 之后再并")
    _refuse_pending(s, sid, env.ctx)
    if launcher.worker_alive(env.ctx, s["worker"]):
        raise Fail(f"{sid} 的 worker 还在跑（pid {s['worker'].get('pid')}）：先 stop {sid} 再并")
    if store.staging_pending(s) or store.offrepo_pending(s):
        raise Fail(f"{sid} 还有改动在 staging / 仓外真身上、没上线判定：等它上线（判定会跟着并过去）或撤回之后再并")
    if s.get("pr") and not s.get("merge_sha"):
        raise Fail(f"{sid} 的 PR #{s['pr']} 还开着没合：先关掉或等它合进 staging、上线之后再并")


def merge_business(env, st, tid, sid, said, walls_first=False) -> dict:
    """把业务票 sid 并进业务票 tid（调用方持锁、负责存盘）：墙 / 证据 / 尝试（含已上线待判的）/ 并进来的老票挪到 tid，
    sid 标成已并（done + merged_into，下一轮巡检关 issue、清本地），不再派发、不再收判定；记一条 decide。
    墙重新编号（记 from = 原票/原编号）；尝试重新编号（记 merged_from / orig_n），对准的墙一并换号——判定拆的是它原来对准的那道。
    sid 决定单上的有害待撤回项挪到 tid（撤回照样要做），其余项作废。"""
    _merge_checks(env, st, tid, sid)
    t, s, at = st["tickets"][tid], st["tickets"][sid], env.now()
    for x in (t, s):                                   # v2.1 之前的在途尝试没记对准哪道墙：按并之前的第一道没拆的墙补上
        first = store.first_open_wall(x)
        for a in x.get("attempts") or []:
            if not a.get("historical") and a.get("verdict") in (None, "pending_sample") and "wall" not in a:
                a["wall"] = first
    nums = [int(w["id"][1:]) for w in t["walls"] if str(w.get("id", "")).startswith("W") and w["id"][1:].isdigit()]
    nxt, wmap, moved_walls = max(nums, default=0), {}, []
    for w in s.get("walls") or []:
        nxt += 1
        wmap[w.get("id")] = f"W{nxt}"
        moved_walls.append({**w, "id": f"W{nxt}", "from": f"{sid}/{w.get('id')}"})
    t["walls"] = moved_walls + t["walls"] if walls_first else t["walls"] + moved_walls
    t["evidence"] = list(t.get("evidence") or []) + [e for e in s.get("evidence") or [] if e not in (t.get("evidence") or [])]
    n, amap = max((a.get("n", 0) for a in t["attempts"]), default=0), {}
    hist, done, live = [], [], []
    for a in s.get("attempts") or []:
        n += 1
        amap[a.get("n")] = n
        b = {**a, "n": n, "merged_from": sid, "orig_n": a.get("n")}
        if b.get("wall") in wmap:
            b["wall"] = wmap[b["wall"]]
        (hist if a.get("historical") else live if a.get("verdict") in (None, "pending_sample") else done).append(b)
    first_live = next((i for i, a in enumerate(t["attempts"]) if not a.get("historical")), len(t["attempts"]))
    t["attempts"][first_live:first_live] = hist + done     # 历史 / 判完的排在目标票自己的尝试之前；待判的排最后
    t["attempts"] += live
    olds = []
    for oid in s.get("absorbs") or []:                 # 并进被并票的老票：改挂到目标票，历史尝试号跟着换
        o = st["tickets"].get(oid)
        if o is None:
            continue
        o["parent"] = tid
        if o.get("parent_attempt") in amap:
            o["parent_attempt"] = amap[o["parent_attempt"]]
        if oid not in t.setdefault("absorbs", []):
            t["absorbs"].append(oid)
        olds.append(oid)
    moved_q, dropped_q = [], []
    for q in st["queue"]:
        if q.get("ticket") != sid:
            continue
        if q["kind"] == "harm":                        # 有害待撤回：撤回照样要做，挪到目标票
            q["ticket"], q["summary"] = tid, f"（并自 {sid}）{q['summary']}"
            moved_q.append(q["id"])
        else:
            dropped_q.append(q["id"])
    st["queue"] = [q for q in st["queue"] if q["id"] not in dropped_q]
    for items in (st.get("wake") or {}).values():      # 还没交给 router 的唤醒项：票号、尝试号跟着换
        for it in items:
            if isinstance(it, dict) and it.get("ticket") == sid:
                it["ticket"] = tid
                if it.get("attempt") in amap:
                    it["attempt"] = amap[it["attempt"]]
    s["walls"], s["attempts"], s["absorbs"], s["evidence"] = [], [], [], []
    s["merged_into"], s["merged_at"] = tid, at
    s["closeout"] = {"downstream": {}, "issue": "pending" if s.get("issue") else "n/a",
                     "worktree": "pending", "branch": "pending"}
    store.set_status(st, sid, store.DONE, at, f"已并入 {tid}（收尾中：下一轮巡检关 issue、清 worktree）")
    pending = [a for a in t["attempts"] if not a.get("historical") and a.get("verdict") in (None, "pending_sample")]
    offered = None
    if t["status"] == store.POSTLAUNCH and live:
        t["waiting_for"] = "等 daily 判定" + "、".join(f"尝试 {a['n']}" for a in pending) + f"（含并自 {sid} 的）"
    elif t["status"] == store.POSTLAUNCH and not pending:
        offered = _offer_next(env, st, tid, f"并入 {sid}，墙多了")
    _append_local(env.home / "answers.md", f"- {at} | - | merge | {tid}←{sid} | {' '.join(said.splitlines())}")
    did = store.append_decision(
        env.decisions, st, tid, f"并入 {sid}：墙 {'、'.join(f'{k}→{v}' for k, v in wmap.items()) or '无'}；"
        f"尝试 {'、'.join(f'{k}→{v}' for k, v in amap.items()) or '无'}" + (f"；老票 {'、'.join(olds)}" if olds else ""),
        "用户确认：同一业务目标（同一指标）只留一张票（v2.1 第 2 条）",
        f"用户原话见 answers.md（merge）；{sid} 原指标「{(s.get('metric') or {}).get('name', '')}」", at)
    return {"ok": True, "target": tid, "source": sid, "walls": wmap, "attempts": {str(k): v for k, v in amap.items()},
            "absorbs": olds, "moved_items": moved_q, "dropped_items": dropped_q, "decision": did,
            **({"next_item": offered} if offered else {})}


def cmd_merge(a, env):
    """用户说「把 T5 并进 T7」（两张都是业务票）：merge T7 T5 --said "<原话>"。见 merge_business。"""
    said = (a.said or "").strip()
    if not said:
        raise Fail("--said 要写用户确认并票时的原话")
    with env.state() as st:
        return merge_business(env, st, a.target, a.source, said, walls_first=a.walls_first)


def cmd_submit_card(a, env):
    card = Path(a.card)
    if not card.exists():
        raise Fail(f"方向卡不存在：{card}")
    with env.state() as st:
        t = env.ticket(st, a.ticket)
        if t["status"] != store.KICKOFF:
            raise Fail(f"{a.ticket} 状态是 {t['status']}，只有出方向卡阶段（kickoff）的票能交卡")
        _refuse_pending(t, a.ticket, env.ctx)         # 认不出可能在跑的那个 worker：停不了它
        st["queue"] = [q for q in st["queue"] if not (q["kind"] == "card" and q["ticket"] == a.ticket)]   # 旧版本作废
        version = t.get("card", {}).get("version", 0) + 1
        # 方向审没谈拢的挑战（v2.1 第 5 条）：卡末尾「## 未解决的挑战」一段，连同卡一起交用户确认
        unresolved = bool(re.search(r"^##\s*未解决的挑战", card.read_text(encoding="utf-8"), re.M))
        if a.unresolved and not unresolved:
            raise Fail(f"--unresolved 要求卡末尾有「## 未解决的挑战」一段（逐条：挑战、worker 的回应、卡在哪）：{card}")
        t["card"] = {"path": str(card), "rounds": a.rounds, "codex": a.codex, "overtime": a.overtime,
                     "version": version, "submitted_at": env.now(), "unresolved": unresolved}
        t["solution"] = {"kind": "card", "text": _card_direction(card.read_text(encoding="utf-8")),
                         "source": f"方向卡 v{version}（待你确认）"}
        launcher.stop_worker(env.ctx, t["worker"])      # 超时交卡：worker 还在写，先停，确认后续跑
        t["worker"]["pid"] = None
        t["worker"]["kickoff_rounds"] = a.rounds
        # v2.1 第 1 条：业务票的方向卡也交用户确认（不再按票级授权自动确认）
        store.set_status(st, a.ticket, store.AWAIT_CARD, env.now(), "等你确认方向卡")
        qid = store.push_queue(st, a.ticket, "card",
                               f"{a.ticket} 方向卡 v{version}{'（超时，见卡上未完成项）' if a.overtime else ''}"
                               f"{'（含未解决的挑战，见卡末尾「未解决的挑战」）' if unresolved else ''}：{t['title']}",
                               env.now())
    return {"ok": True, "queue": qid, "version": version}


def cmd_decide(a, env):
    with env.state() as st:
        did = store.append_decision(env.decisions, st, a.ticket, a.decision, a.why, a.basis, env.now())
    return {"ok": True, "decision": did}


def cmd_revoke(a, env):
    with env.state() as st:
        try:
            store.revoke_decision(env.decisions, a.did, a.reason, env.now())
        except KeyError:
            raise Fail(f"没有决定 {a.did}")
        d = next(x for x in store.recent_decisions(env.decisions, 10 ** 6) if x["id"] == a.did)
        affected = d["ticket"] if d["ticket"] in st["tickets"] else None
        if affected:
            events.mark_review(st, [affected])
            events.deliver(env.home, affected, {"ts": env.now(), "ticket": "router", "type": "tell",
                                                "summary": f"{a.did} 已撤回：{a.reason}（原决定：{d['decision']}）"})
    return {"ok": True, "revoked": a.did, "affected": affected}


def cmd_tell(a, env):
    """--as-router：router 自己的话（方向审的挑战、转述），标「router 转述 / 决定」，不冒充用户原话；
    用户说「告诉 A：…」照旧不加（原话）。"""
    relay = bool(getattr(a, "as_router", False))
    with env.state() as st:
        t = env.ticket(st, a.ticket)
        if t["status"] == store.TAKEOVER and a.resume:
            raise Fail(f"{a.ticket} 在你接管中：先「还给 router」（handback）再续跑")
        if a.resume:
            _refuse_pending(t, a.ticket, env.ctx)     # 先拒再投递：不然重试会投两份
        alive = launcher.worker_alive(env.ctx, t["worker"])
        if not (a.resume and alive):                  # 活着时由 _resume 投递，别投两份
            said = f"{ROUTER_RELAY}（不是用户原话）：{a.text}" if relay else a.text
            events.deliver(env.home, a.ticket, {"ts": env.now(), "ticket": "router", "type": "tell", "summary": said})
        resumed = None
        if a.resume and _resume(env, st, a.ticket, "direction", a.text,
                                t["status"] if t["status"] in store.RUNNING else store.KICKOFF, relay=relay) == "resumed":
            resumed = a.ticket
        elif not a.resume:
            pass
    return {"ok": True, "resumed": resumed}


def cmd_pause(a, env):
    with env.state() as st:
        t = env.ticket(st, a.ticket)
        events.deliver(env.home, a.ticket, {"ts": env.now(), "ticket": "router", "type": "pause",
                                            "summary": "先停：做完当前这一步就停"})
        t["pause_requested"] = True
    return {"ok": True}


def cmd_stop(a, env):
    with env.state() as st:
        t = env.ticket(st, a.ticket)
        _refuse_pending(t, a.ticket, env.ctx)         # 可能在跑的那个 worker 认不出：停不了它
        launcher.stop_worker(env.ctx, t["worker"])
        t["worker"]["pid"] = None
        store.set_status(st, a.ticket, store.PAUSED, env.now(), "你让它立刻停")
    return {"ok": True}


def cmd_takeover(a, env):
    with env.state() as st:
        t = env.ticket(st, a.ticket)
        w = t["worker"]
        if not w.get("session_id"):
            raise Fail(f"{a.ticket} 还没有 worker session")
        _refuse_pending(t, a.ticket, env.ctx)         # 后台可能已有 worker 在写同一个 session：不交出 resume 命令
        launcher.stop_worker(env.ctx, w)                # 同一个 session 不能两边同时写
        w["pid"] = None
        if t["status"] != store.TAKEOVER:
            t["takeover_from"] = t["status"]
        store.set_status(st, a.ticket, store.TAKEOVER, env.now(), "你接管中")
        command = f"cd {w['worktree']} && claude --resume {w['session_id']}"
    return {"ok": True, "command": command}


def cmd_handback(a, env):
    with env.state() as st:
        t = env.ticket(st, a.ticket)
        if t["status"] != store.TAKEOVER:
            raise Fail(f"{a.ticket} 不在接管中")
        _refuse_pending(t, a.ticket)
        prev = t.pop("takeover_from", store.PAUSED)
        store.set_status(st, a.ticket, prev, env.now())       # 先解除接管，_resume 才肯续跑
        if prev in store.RUNNING:
            _resume(env, st, a.ticket, "direction",
                    "用户接管结束，交还给 router。先看 git log / git status 了解用户做了什么，再按当前阶段继续。", prev,
                    waiting=RESUME_WAIT["handback"])
        else:
            store.set_status(st, a.ticket, prev, env.now())
    return {"ok": True, "status": prev}


def cmd_surfaces(a, env):
    """方向卡出来后按实际更新代码面，或越界时给票分配锁（spec §4.4、§8）。同时记一条决定。"""
    names = _csv(a.set)
    _check_surfaces(env, names)
    with env.state() as st:
        t = env.ticket(st, a.ticket)
        held = {s: h for s, h in surfaces.conflicts(st, a.ticket, set(names)).items()}
        if held and not a.force:
            raise Fail(f"这些代码面被别人锁着：{held}；等它们放锁，或确认后加 --force", held=held)
        old = t["surfaces"]
        t["surfaces"], t["weight"] = names, surfaces.ticket_weight(store.ticket_kind(t), names, env.table)
        did = store.append_decision(env.decisions, st, a.ticket, f"代码面 {old} → {names}", a.why,
                                    "router.py surfaces", env.now())
    return {"ok": True, "surfaces": names, "weight": t["weight"], "decision": did}


def cmd_ack_slow(a, env):
    with env.state() as st:
        n = len(st.get("slow_path", []))
        st["slow_path"] = []
    return {"ok": True, "cleared": n}


def cmd_prioritize(a, env):
    with env.state() as st:
        env.ticket(st, a.ticket)["priority"] = a.n
    return {"ok": True}


def _worker_ticket(a):
    tid = a.ticket or os.environ.get("ROUTER_TICKET")
    if not tid:
        raise Fail("没有票号：加 --ticket 或设 ROUTER_TICKET", code=2)
    return tid


def cmd_emit(a, env):
    try:
        ev = events.emit(env.home, _worker_ticket(a), a.type, a.summary, a.evidence,
                         _csv(a.surfaces), _csv(a.tags), _csv(a.tickets), at=env.now())
    except ValueError as e:
        raise Fail(str(e), code=2)
    return {"ok": True, "event": ev}


def cmd_progress(a, env):
    try:
        ev = events.emit_progress(env.home, _worker_ticket(a), a.phase, env.now())
    except ValueError as e:
        raise Fail(str(e), code=2)
    return {"ok": True, "event": ev}


def cmd_inbox(a, env):
    tid = _worker_ticket(a)
    with env.locked():
        st = store.load_state(env.state_path)
        msgs = events.read_inbox(env.home, st, tid)
        store.save_state(env.state_path, st)
    return {"ok": True, "messages": msgs}


def _closeout_targets(st, tid) -> dict:
    """核对完那一刻的下游快照：没 settle、没 skip、有 issue，且依赖本票或共享代码面的票。
    迁移来的旧票同样现算，不预设「已通知」：tick 留言前先查 marker / 旧格式签名，已留过的不重复。"""
    t = st["tickets"][tid]
    mine = set(t.get("surfaces", []))
    return {o["id"]: {"issue": o["issue"], "state": "pending"} for o in st["tickets"].values()
            if o["id"] != tid and o.get("issue") and o["status"] not in store.SETTLED | {store.SKIP}
            and (tid in o.get("blocked_by", []) or mine & set(o.get("surfaces", [])))}


def _checked(env, st, t) -> dict:
    """用户说「T 走查过了 / T 核对过了」（walkthrough-done，或回答 handson 项）：
    仓内已合 staging → 等上 prod；仓外待核对 → 完成，收尾（下游留言、关 issue、清 worktree / 分支）交给下一轮 tick。
    只改本地状态，不碰网络。重复说一次是幂等的。"""
    tid, status = t["id"], t["status"]
    drop_handson = lambda: [q for q in st["queue"] if not (q["kind"] == "handson" and q["ticket"] == tid)]
    if status == store.MERGED:
        store.set_status(st, tid, store.AWAIT_PROD, env.now(), "等上 prod")
        st["queue"] = drop_handson()
        return {"status": store.AWAIT_PROD}
    if status == store.AWAIT_CHECK and store.ticket_kind(t) == "business":
        res = _offrepo_attempt(env, st, t)
        st["queue"] = drop_handson()
        return res
    if status == store.AWAIT_CHECK:
        t["closeout"] = {"downstream": _closeout_targets(st, tid), "issue": "pending" if t.get("issue") else "n/a",
                         "worktree": "pending", "branch": "pending"}
        store.set_status(st, tid, store.DONE, env.now(), "收尾中（下一轮巡检：下游留言、关 issue、清 worktree）")
        st["queue"] = drop_handson()
        return {"status": store.DONE, "closeout": t["closeout"], "hint": "收尾在下一轮巡检完成"}
    if status == store.DONE and store.closeout_pending(t.get("closeout")):
        return {"status": store.DONE, "closeout": t["closeout"], "hint": "收尾还没做完，下一轮巡检继续"}
    if status in (store.DONE, store.AWAIT_PROD, store.POSTLAUNCH):
        st["queue"] = drop_handson()
        return {"status": status, "already": True}
    raise Fail(f"{tid} 还没合进 staging，也不在等你核对（{status}）")


def _offrepo_attempt(env, st, t) -> dict:
    """仓外改动的业务票，用户核对过（终审 #5）：仓外没有 staging 隔离层，改完就是真身 ⇒ 这次尝试算上线，记一次尝试、
    进 postlaunch 等 daily 判定（撤回办法 = 从备份恢复）。不关票：关业务票只走 close / 判真流程里的「关」。"""
    tid = t["id"]
    if store.attempt_inflight(t):
        raise Fail(f"{tid} 上一次尝试还没判（判定为空或待样本）：先等 daily 判完")
    sol = t.get("solution") or {}
    what = " ".join(sol["text"].split())[:300] if sol.get("kind") == "card" and sol.get("text") else t["title"]
    rollback = f"从 {env.home}/backups/{tid}/ 恢复仓外改动（改了什么见 {env.home}/cards/{tid}.diff）"
    n = store.add_attempt(st, tid, f"仓外改动：{what}", env.now(), rollback)
    next(x for x in t["attempts"] if x.get("n") == n).update(branch=t["worker"].get("branch"), offrepo=True)
    store.set_status(st, tid, store.POSTLAUNCH, env.now(), f"等 daily 判定尝试 {n}（仓外改动，已核对）")
    return {"status": store.POSTLAUNCH, "attempt": n, "hint": "仓外改动已核对：记为一次尝试，等 daily 判定（不关票）"}


def cmd_walkthrough_done(a, env):
    with env.state() as st:
        res = _checked(env, st, env.ticket(st, a.ticket))
    return {"ok": True, **res}


# 发版批次（终审 #6 + 残留 #2/#3 + 残留 2）：上 prod = 合 main 发版，staging 上领先 main 的东西会一起上去
PROD_STATE = {"ready": "可上", "approved": "已放行", "walkthrough": "待走查", "release": "待放行",
              "unjudged": "上次尝试还没判", "unconfirmed": "方向卡没经你确认"}
_PROD_OK = ("ready", "approved", "ride_ok")          # 不挡 router 自己上
_PROD_SHIPS = ("ready", "approved")                   # 发完照常 released（进 router_ships）
# 「放行」读法（v2.1 第 8 条）：可带前缀「Q<数字>」、空格、全角标点和尾随说明（「Q332 放行。生图放行，用grok」= 整批放行）；
# 关键词后面只能是结尾 / 空白 / 标点（可带「了 / 吧」；「放行吗」「放行不了」不算）；整句里有否定（「先不放行」「不放行」「先别」）一律不算
_PROD_GO = re.compile(r"^(?:Q\d+[\s，,：:。.、]*)?(?:放行|上\s*prod)[了吧啦]?(?:$|[\s，,。.！!；;、：:])", re.I)
_PROD_NO = re.compile(r"不放行|先别|别上|先不|暂缓|不要上|不能上|不上\s*prod", re.I)


def _said_go(text):
    """放行类回答（整批 prod 项、花钱项）→ "go"（放行）/ "no"（明确不放行）/ None（看不出来）。只有 go 才算放行。"""
    t = (text or "").strip()
    if _PROD_NO.search(t):
        return "no"
    return "go" if _PROD_GO.match(t) else None
_KIND_ZH = {"business": "业务票", "maint": "维护票", "legacy": "老票"}
_PR_SUFFIX = re.compile(r"\(#(\d+)\)\s*$")             # squash 合并的默认标题：「<PR 标题> (#<PR 号>)」


def _ride_along(t):
    """已停 / 接管 / 已关的票：代码还在 staging 上、会跟着这次发版上去，但不是这次上线的票（残留 2 #3）→ 说明；否则 None。"""
    if t["status"] == store.PAUSED:
        return "已停"
    if t["status"] == store.TAKEOVER:
        return "接管中"
    if t["status"] == store.DONE:
        return "已关（关前已撤回）" if t.get("rolled_back_at") else "已关"
    return None


def _prod_state(t):
    """批次里一张票能不能由 router 带上 prod → (状态码, 给人看的说明)。状态码在 _PROD_OK 里 = 不挡；在 _PROD_SHIPS 里 = 进
    router_ships。已停 / 接管 / 已关的票只有用户放行过才不挡（ride_ok），也永远不进 router_ships：它们不是这次上线的票，
    released 对它们只记 shipped_at（已停 / 接管）或跳过（关前已撤回），不进上线验收。"""
    ride = _ride_along(t)
    if ride:
        ok = bool(t.get("prod_ok"))
        return ("ride_ok" if ok else "ride"), f"{ride}，代码还在 staging 上·{'已放行' if ok else '要放行'}"
    if t["status"] == store.AWAIT_PROD and store.router_owned(t):
        # v2.1 第 1 条：router 只上用户确认过的方案；v2 时代 router 代确认的卡（没有 card_confirmed_by=user）要用户放行
        if store.attempt_inflight(t):
            code = "unjudged"
        elif t.get("prod_ok"):
            code = "approved"
        else:
            code = "ready" if t.get("card_confirmed_by") == "user" else "unconfirmed"
        return code, PROD_STATE[code]
    if t.get("prod_ok"):
        return "approved", PROD_STATE["approved"]
    if t["status"] in (store.MERGED, store.AWAIT_PROD):
        code = "walkthrough" if t["status"] == store.MERGED else "release"
        return code, PROD_STATE[code]
    return "parked", f"代码还在 staging 上（票 {t['status']}）"


def _needs_batch(st) -> bool:
    """有票级授权的业务票等上 prod ⇒ 走整批规则；没有就照 v1（老票、维护票攒成一条交用户）。"""
    return any(t["status"] == store.AWAIT_PROD and store.router_owned(t) for t in st["tickets"].values())


def _staging_commits(env) -> dict:
    """fetch 后钉住 origin/<main>、origin/<base> 的 sha，列出 main..base 的提交：
    {"ok", "error", "staging_sha", "commits": [{sha, parents: [父提交 sha], subject, sync}]}。
    staging_sha = 这次对账用的 origin/<base> 提交：router 合 main 时钉在它上面，不在它之后另取 staging 最新（残留 2 #4）。
    sync = main 回合 base 的 merge 提交（任一非首父提交是 origin/<main> 的祖先，残留 2 #2）：不算对不上的提交。
    git 失败 ⇒ ok=False（调用方 fail-closed：router 不自己上）；祖先判断本身失败 ⇒ 不认作回合并（照样算对不上）。"""
    main, base = env.cfg.get("main_branch", "main"), env.cfg.get("base_branch", "staging")
    git = lambda *a, **k: env.ctx.run(["git", "-C", str(env.root), *a], **k)
    f = git("fetch", "-q", "origin", main, base, timeout=launcher.FETCH_S)
    if f.returncode != 0:
        return {"ok": False, "error": f"git fetch 失败：{launcher._err(f)[:160]}", "staging_sha": None, "commits": []}
    r = git("rev-parse", f"origin/{main}^{{commit}}", f"origin/{base}^{{commit}}")
    tips = (r.stdout or "").split()
    if r.returncode != 0 or len(tips) != 2:
        return {"ok": False, "error": f"git rev-parse 失败：{launcher._err(r)[:160]}", "staging_sha": None, "commits": []}
    main_sha, staging_sha = tips
    r = git("log", "--format=%H%x1f%P%x1f%s", f"{main_sha}..{staging_sha}")
    if r.returncode != 0:
        return {"ok": False, "error": f"git log 失败：{launcher._err(r)[:160]}", "staging_sha": None, "commits": []}
    commits = []
    for line in (r.stdout or "").splitlines():
        sha, parents, subject = (line.split("\x1f") + ["", ""])[:3]
        if sha.strip():
            ps = parents.split()
            sync = any(git("merge-base", "--is-ancestor", p, main_sha).returncode == 0 for p in ps[1:])
            commits.append({"sha": sha.strip(), "parents": ps, "subject": subject.strip(), "sync": sync})
    return {"ok": True, "error": "", "staging_sha": staging_sha, "commits": commits}


def _staging_commits_if_needed(env):
    """不持锁先看一眼：要走整批规则才 fetch（fetch 可能要几十秒，不能占着 router 的锁）；老票单独时不碰 git。"""
    try:
        peek = store.load_state(env.state_path)
    except store.StateCorrupt:
        return None
    return _staging_commits(env) if _needs_batch(peek) else None


def _attribute(st, members, commits) -> list:
    """对不上批次里任何一张票的提交。对上的规则：提交 sha = 这张票记的合并 sha（merge_sha，gh 给的合并 commit）；
    或者它是普通提交（单个父提交）、标题以「(#<这张票的 PR 号>)」结尾。merge 提交（多个父提交）只认 sha。
    main 回合 base 的 merge 提交（sync）不在这里算：调用方单列成 sync_merges。"""
    shas = {str(st["tickets"][tid].get("merge_sha")): tid for tid in members
            if st["tickets"][tid].get("merge_sha") not in (None, "", "?")}
    prs = {str(st["tickets"][tid].get("pr")): tid for tid in members if st["tickets"][tid].get("pr")}
    out = []
    for c in commits:
        if c.get("sync") or c["sha"] in shas:
            continue
        m = _PR_SUFFIX.search(c["subject"])
        if len(c["parents"]) == 1 and m and m.group(1) in prs:
            continue
        out.append(c)
    return out


def _prod_item(env, st, check=None):
    """→ (prod 项号 | None, tickets, router_ships, batch | None, 对账 | None)。check = _staging_commits 的结果（None = 没核对）。
    没有业务票等上 prod：照 v1（老票 / 维护票攒成一条「上 prod：…」交用户），batch=None、不碰 git。
    有：整批 = staging 上合进来、还没 released 的每一张票（store.staging_pending，不看现在的状态）+ 所有 await_prod 的票；
    再拿 origin/<main>..origin/<base> 的提交对账（_attribute；main 回合 base 的 merge 提交单列 sync_merges，不算对不上）。
    每张票都不挡（可上的票级授权业务票、用户已放行，或用户放行过的已停 / 接管 / 已关的票）、每个提交都对得上（或用户放行过）、
    而且核对成功 ⇒ router 自己上，router_ships = 整批里发完要照常 released 的票（已停 / 接管 / 已关的不在里面，残留 2 #3）；
    否则 router_ships 为空，决定单上一条整批 prod 项（每张票的状态 + 对不上的提交）。批次不变（同一组票、同一组对不上的提交、
    同样核对成没成）就原地更新这一项、保留 Q 号。对账输出带 staging_sha：router 合 main 钉在它上面。"""
    tk, num = st["tickets"], (lambda x: int(x[1:]))
    waiting = sorted((tid for tid, t in tk.items() if t["status"] == store.AWAIT_PROD), key=num)
    ready = [tid for tid in waiting if not store.router_owned(tk[tid])]
    if not _needs_batch(st):
        st["queue"] = [q for q in st["queue"] if q["kind"] != "prod"]
        if ready:
            return store.push_queue(st, None, "prod", f"上 prod：{' '.join(ready)}", env.now()), ready, [], None, None
        return None, [], [], None, None
    members = [tid for tid, t in tk.items() if store.staging_pending(t)]
    tids = sorted(set(members) | set(waiting), key=num)
    rows = []
    for tid in tids:
        code, label = _prod_state(tk[tid])
        rows.append({"ticket": tid, "kind": store.ticket_kind(tk[tid]), "status": tk[tid]["status"], "state": label,
                     "ok": code in _PROD_OK, "ships": code in _PROD_SHIPS})
    check = check or {"ok": False, "error": "没核对 staging 上的提交（再跑一次 prod-batch）", "staging_sha": None,
                      "commits": []}
    approved = st.get("prod_ok_commits") or {}
    if check["ok"] and approved:                      # 已经上了 main 的提交不用再记放行
        live = {c["sha"] for c in check["commits"]}
        st["prod_ok_commits"] = approved = {k: v for k, v in approved.items() if k in live}
    loose = [{"sha": c["sha"], "subject": c["subject"], "state": "已放行" if c["sha"] in approved else "对不上票"}
             for c in _attribute(st, members, check["commits"])]
    batch = [{k: v for k, v in r.items() if k not in ("ok", "ships")} for r in rows]
    sync = [{"sha": c["sha"], "subject": c["subject"]} for c in check["commits"] if c.get("sync")]
    recon = {"verified": check["ok"], "staging_sha": check.get("staging_sha"), "commits": loose, "sync_merges": sync,
             **({"verify_error": check["error"]} if not check["ok"] else {})}
    old = [q for q in st["queue"] if q["kind"] == "prod"]
    if check["ok"] and all(r["ok"] for r in rows) and all(c["state"] == "已放行" for c in loose):
        st["queue"] = [q for q in st["queue"] if q["kind"] != "prod"]
        return None, [], [r["ticket"] for r in rows if r["ships"]], batch, recon
    key = "|".join([",".join(tids), ",".join(sorted(c["sha"] for c in loose)), "ok" if check["ok"] else "unverified"])
    parts = [f"{r['ticket']} {_KIND_ZH.get(r['kind'], r['kind'])}·{r['state']}" for r in rows]
    parts += [f"提交 {c['sha'][:12]}「{c['subject'][:40]}」·{c['state']}" for c in loose]
    if not check["ok"]:
        parts.append(f"核对不了 staging 领先 main 的提交（{check['error']}）")
    summary = ("上 prod（整批：合 main 会把 staging 上这些一起带上去，回「放行」= 整批一起上）：" + "；".join(parts))
    keep = next((q for q in old if q.get("batch_key") == key), None)
    st["queue"] = [q for q in st["queue"] if q["kind"] != "prod" or q is keep]
    if keep is None:
        store.push_queue(st, None, "prod", summary, env.now())
        keep = st["queue"][-1]
    keep.update(summary=summary, batch=tids, commits=[c["sha"] for c in loose], batch_key=key,
                staging_sha=check.get("staging_sha"))
    return keep["id"], ready, [], batch, recon


def _prod_answered(env, st, q, text):
    """用户回整批 prod 项：「放行」/「上 prod」⇒ 整批每张票、每个列出的提交都记已放行，router 照项目发版流程整批上；
    别的话只记原话（answers.md 已写），不放行。"""
    if _said_go(text) != "go":
        return "原话已记，没放行：整批照旧等你（再跑 prod-batch 会重推这一项）", {"approved": []}
    approved = []
    for tid in q.get("batch") or []:
        t = st["tickets"].get(tid)
        if t is not None:
            t["prod_ok"] = env.now()
            approved.append(tid)
    for sha in q.get("commits") or []:
        st.setdefault("prod_ok_commits", {})[sha] = env.now()
    return ("用户放行整批：照项目发版流程上 prod（整批一起），发完逐张 released（业务票带 --rollback）",
            {"approved": approved, "approved_commits": list(q.get("commits") or [])})


def cmd_prod_batch(a, env):
    check = _staging_commits_if_needed(env)
    with env.state() as st:
        qid, ready, ships, batch, recon = _prod_item(env, st, check)
    out = {"ok": True, "queue": qid, "tickets": ready, "router_ships": ships}
    if batch is not None:                             # 有业务票等上 prod：整批、每张的状态、对账（老票单独时不出这几栏）
        out["batch"] = batch
        out.update(recon)
    return out


def _parse_t0(s) -> str:
    """--t0：带时区的 ISO（2026-11-12T03:00:00Z / 2026-11-12T11:00:00+08:00）→ UTC 的 ISO。
    不带时区的拒绝：router 一律按 UTC 记，猜错时区 = 检查点整体错开几个小时。"""
    try:
        dt = datetime.fromisoformat(s.strip())
    except ValueError:
        raise Fail(f"--t0 不是 ISO 时间：{s!r}（例：2026-11-12T03:00:00Z）")
    if dt.tzinfo is None:
        raise Fail(f"--t0 要带时区（Z 或 +08:00）：{s!r}")
    return dt.astimezone(timezone.utc).strftime(store.ISO)


_BIZ_RELEASABLE = {store.AWAIT_PROD, store.POSTLAUNCH, store.MERGED}


def _release_mode(t) -> str:
    """released 怎么处理一张票（残留 2 #3）：
    ride = 已停 / 接管：改动随这次发版上去了，但票不归 router 推进 ⇒ 只在 history 记 shipped_at，不改状态、不进上线验收；
    skip = 带 --rolled-back 关掉的业务票：关前已撤回，不是这次上线的票 ⇒ 跳过（不记尝试、不报错），history 记 shipped_at
           让它出发版批次；
    release = 照常（业务票记尝试进 postlaunch，其余进 T+1/T+3/T+7 上线验收）。"""
    if t["status"] in (store.PAUSED, store.TAKEOVER):
        return "ride"
    if store.ticket_kind(t) == "business" and t["status"] == store.DONE and t.get("rolled_back_at"):
        return "skip"
    return "release"


def cmd_released(a, env):
    """发完 prod：写周报 releases.json（照旧），票进上线验收（postlaunch），不进 done。
    票的终点是上线验收：t0 + 1 / 3 / 7 天各查一次（tick 到期交 postlaunch_due）。
    已经验收过又重新上线（改了配置再发）：旧的验收记录挪进 postlaunch_prev，从新的 t0 重新查。
    已停 / 接管的票只记 shipped_at、不改状态；关前已撤回的业务票跳过（见 _release_mode），输出里分别列 parked / skipped。"""
    runs = env.cfg.get("weekly_runs_dir")
    if not runs:
        raise Fail("config 缺 weekly_runs_dir")
    t0 = _parse_t0(a.t0) if a.t0 else env.now()
    rel = Path(os.path.expanduser(runs)).parent / "state" / "releases.json"
    check = _staging_commits_if_needed(env)            # 发完之后重算整批项要用（老票单独时不碰 git）
    with env.state() as st:
        for tid in a.tickets:
            env.ticket(st, tid)
        mode = {tid: _release_mode(st["tickets"][tid]) for tid in a.tickets}
        biz = [tid for tid in a.tickets
               if mode[tid] == "release" and store.ticket_kind(st["tickets"][tid]) == "business"]
        for tid in biz:                                                  # 业务票：先验完再写 releases.json（拒收不留痕）
            status = st["tickets"][tid]["status"]
            if status not in _BIZ_RELEASABLE:                            # 终审 #16：草稿 / 已关 / 还在做的不收
                raise Fail(f"{tid} 是业务票、状态是 {status}：released 只收在途的票（等上 prod，或已上线、补了入口重上），"
                           "草稿、已关、还在做的一律拒")
            if not (a.rollback or "").strip():
                raise Fail(f"{tid} 是业务票：released 必须带 --rollback \"<撤回办法>\"")
            if any(x.get("verdict") in (None, "pending_sample") for x in st["tickets"][tid]["attempts"]
                   if not x.get("historical")):
                raise Fail(f"{tid} 上一次尝试还没判（判定为空或待样本）：先 verdict，再 released")
        entry = None
        if any(m != "skip" for m in mode.values()):                     # 只剩关前已撤回的票：没有东西算这次上线
            lines = json.loads(rel.read_text(encoding="utf-8")) if rel.exists() else []
            # at = 生效时刻：周报按它切窗；缺 at 会回落到 date 当天 00:00，窗口和批边界整体算歪。date 跟 at 走（本机时区）
            local = datetime.strptime(t0, store.ISO).replace(tzinfo=timezone.utc).astimezone()
            entry = {"date": local.strftime("%Y-%m-%d"), "at": t0, "what": a.what, "metrics": _csv(a.metrics)}
            if a.config_keys:
                entry["config_keys"] = _csv(a.config_keys)               # 周报 registered() 按结构化键名认登记，不靠 what 里的短名
            store._atomic_write(rel, json.dumps(lines + [entry], ensure_ascii=False, indent=2) + "\n")
        plans, parked, skipped = {}, [], []
        for tid in a.tickets:
            t = st["tickets"][tid]
            t.pop("prod_ok", None)                                       # 放行只管这一次发版
            if mode[tid] == "skip":
                store.record_shipped(st, tid, env.now(), t0)
                skipped.append({"ticket": tid, "why": "已关（关前已撤回）：不是这次上线的票，跳过；只记 shipped_at，出发版批次"})
                continue
            if mode[tid] == "ride":
                store.record_shipped(st, tid, env.now(), t0)
                who = "已停" if t["status"] == store.PAUSED else "在你接管中"
                parked.append({"ticket": tid, "status": t["status"],
                               "hint": f"{tid} {who}：改动随这次发版上去了，只记 shipped_at、状态不变、不进上线验收；"
                                       "要验收，恢复 / 交还（handback）之后再 released"})
                continue
            if tid in biz:                                               # 验收交给 daily 的判定，不排 T+1/T+3/T+7
                n = store.add_attempt(st, tid, a.what, t0, a.rollback.strip())
                att = next(x for x in t["attempts"] if x.get("n") == n)   # 这次上线的 PR / 合并 sha / 分支记在尝试上：
                att.update(pr=t.get("pr"), merge_sha=t.get("merge_sha"),  # 下次重排队会清掉票上的这几项
                           branch=t["worker"].get("branch"))
                store.set_status(st, tid, store.POSTLAUNCH, env.now(), f"等 daily 判定尝试 {n}")
                continue
            if t.get("postlaunch"):
                t.setdefault("postlaunch_prev", []).append(t["postlaunch"])
            t["postlaunch"] = plans[tid] = store.postlaunch_plan(t0)
            store.set_status(st, tid, store.POSTLAUNCH, env.now(), store.postlaunch_wait(st, t, env.now()))
        _prod_item(env, st, check)
    out = {"ok": True, "release": entry, "path": str(rel), "postlaunch": plans}
    if parked:
        out["parked"] = parked
    if skipped:
        out["skipped"] = skipped
    return out


# ---- 业务票：判定 / 尝试循环 ----------------------------------------------------

def _guard(env):
    try:
        return github.load_guard(env.cfg.get("leak_guard_globs", []))
    except github.GuardEmpty as e:
        raise Fail(f"防泄漏闸失效，一条都没收：{e}")


def _leaks(guard, *texts) -> list:
    return sorted({h for tx in texts if tx for h in github.leak_hits(str(tx), guard)})


def _wake(st, key, item) -> None:
    """唤醒键：存进 state["wake"]，下一轮 tick 弹出、放进 tick 结果交给 router。"""
    st.setdefault("wake", {}).setdefault(key, []).append(item)


# 判定落下时票正在做别的事（只有并票之后会出现）：不重新排队
_VERDICT_BUSY = {store.DRAFT, store.HOLD, store.QUEUED, store.KICKOFF, store.AWAIT_CARD, store.EXECUTING,
                 store.VERIFYING, store.MERGED, store.AWAIT_CHECK, store.AWAIT_PROD}


def _verdict_core(env, st, tid, n, verdict, *, touched, hit_wall, continued, note, new_wall=None) -> list:
    t = env.ticket(st, tid)
    if store.ticket_kind(t) != "business":
        raise Fail(f"{tid} 不是业务票，没有尝试判定")
    if t["status"] in (store.DONE, store.SKIP):       # 终审 #2：关掉的票不复活（不记判定、不重新排队）
        raise Fail(f"{tid} 已关（{t['status']}）：不再记判定")
    old = next((x for x in t["attempts"] if x.get("n") == n), None)
    if old is not None and old.get("verdict") not in (None, "pending_sample"):
        raise Fail(f"{tid} 尝试 {n} 已有最终判定（{old['verdict']}），不许重判")
    try:
        actions = store.record_verdict(st, tid, n, verdict, env.now(), touched=touched, hit_wall=hit_wall,
                                       continued=continued, note=note, new_wall=new_wall)["actions"]
    except ValueError as e:
        raise Fail(str(e))
    att = next(x for x in t["attempts"] if x.get("n") == n)
    if "return_to_user" in actions:                                      # 连续没用：退回用户，不再叫醒 worker
        actions = [x for x in actions if x != "change_direction"]
    # 并票（v2.1）之后：目标票正在做别的尝试，或还有别的尝试在等判定 ⇒ 判定照记、墙照拆，但不重新排队 / 不退回
    # （会打断正在做的那次）；判定转进它的收件箱。撤回、查入口照常（改动已经在 prod 上）。
    busy = t["status"] in _VERDICT_BUSY
    deferred = [x for x in actions if x in ("next_wall", "change_direction", "return_to_user")] \
        if busy or store.attempt_inflight(t) else []
    if deferred:
        actions = [x for x in actions if x not in deferred]
        events.deliver(env.home, tid, {"ts": env.now(), "ticket": "router", "type": "tell",
                                       "summary": f"尝试 {n}「{att.get('direction', '')}」判定 {verdict}（{note}）。"
                                                  "这张票还有别的尝试在做 / 在等判定：先不重新排队，墙的状态见尝试记录"})
    for act in actions:
        if act in ("next_wall", "change_direction"):
            opens = [w for w in t["walls"] if w["status"] == "open"]
            if act == "next_wall" and not opens:                         # 墙拆完了：不叫醒 worker，等分析 agent 提议结束
                t["waiting_for"] = NO_WALL_WAIT
                continue
            text = (f"尝试 {n} 判定 {verdict}（{note}）。"
                    + (f"下一堵墙：{opens[0]['text']}。" if act == "next_wall" else "换方向：别在原路上再试一遍。")
                    + "请重新写方向卡再做。")
            _follow_up(env, st, tid, t, text)
        elif act == "return_to_user":
            t["waiting_for"] = RETURN_WAIT
            store.push_queue(st, tid, "return", f"{tid} 连续 {t['no_use_streak']} 次没用，退回用户定夺", env.now())
            _wake(st, "return_to_user", {"ticket": tid, "attempt": n})
        elif act == "rollback":
            if not busy:
                t["waiting_for"] = "有害：等 router 按回滚办法撤回"
            store.push_queue(st, tid, "harm", f"{tid} 尝试 {n} 有害，需撤回：{att.get('rollback', '')}", env.now())
            _wake(st, "rollback_needed", {"ticket": tid, "rollback": att.get("rollback", "")})
        elif act == "check_entry":
            if not busy:
                t["waiting_for"] = "没有入口：等 router 查入口"
            _wake(st, "no_entry", {"ticket": tid, "attempt": n})
    return actions + (["deferred"] if deferred else [])


def _requeue_business(env, st, tid, text, wait) -> None:
    """业务票的下一次尝试：话写进收件箱 + 回 queued，调度器派新 worker（新 worker 读收件箱和尝试记录，从第 1 段出新卡）。
    不在原 worker 上 resume，也不看 worktree 在不在（9b：一条路径）。认不出可能在跑的 worker ⇒ Fail（_refuse_pending）。"""
    t = st["tickets"][tid]
    if t["status"] == store.TAKEOVER:
        raise Fail(f"{tid} 在你接管中：不重新排队，还给 router（handback）后再说")
    _refuse_pending(t, tid, env.ctx)
    w = t["worker"]
    if launcher.worker_alive(env.ctx, w):
        raise Fail(f"{tid} 的 worker 还在跑（pid {w.get('pid')}）：先 stop {tid} 再换方向")
    if w.get("worktree"):                             # 上一次的 checkout 还在：干净才删（不 --force）；有意保留 / 有改动 / 删不掉
        why = t.get("checkout_kept") or launcher.cleanup_ticket_checkout(env.ctx, env.root, w["worktree"], None)["worktree"]
        if why != "done":                             # 原样留着，记进 stale_checkouts 交人处理，不挡排队
            t.setdefault("stale_checkouts", []).append({"worktree": w["worktree"], "branch": w.get("branch"),
                                                        "why": str(why)[:200], "at": env.now()})
    # 派单「router 线索」换成没拆的墙 + 已试方向（终审 #8）：上一次方向卡判过了，不再当线索
    t["solution"] = {"kind": "clue", "text": _requeue_clue(t), "source": "router 重新排队：没拆的墙 + 已试方向（非方案）"}
    # 新一次尝试 = 新会话 + 新分支（router/<T>-a<k>，从 base 切；k = 这张票自己的第几次尝试，不算并进来的历史尝试）：
    # 旧 session id 会被 claude 拒（already in use）；旧分支已 squash 合进 base，接着用会把合过的提交又带进新 PR
    k = sum(1 for x in t.get("attempts") or [] if not x.get("historical")) + 1
    w.update(session_id=None, pid=None, pid_start=None, log=None, started_at=None, worktree=None,
             branch=f"router/{tid}-a{k}")
    w.pop("exit", None)
    t["pr"] = None                                    # 上一次的 PR 已合：留着会让新 worker 一报 pr_opened 就被当成已合
    t["fail_streak"] = {"step": "", "count": 0}
    for k in ("merge_sha", "verifying_at", "checkout_kept", "unconfirmed_reported", "no_pr_reported", "no_ci_reported",
              "leak_reported", "wrong_base_reported", "retarget_fail", "prod_ok"):
        t.pop(k, None)
    events.deliver(env.home, tid, {"ts": env.now(), "ticket": "router", "type": "tell", "summary": text})
    t.pop("card_ready_at", None)                    # 上一轮的交卡时间：不清会让新一轮的超时检查 / 重浮卡都认错
    t["card_confirmed"] = False                     # 新一轮尝试：必须出新方向卡（交用户确认）
    t.pop("card_confirmed_by", None)
    store.set_status(st, tid, store.QUEUED, env.now(), wait)


def _requeue_clue(t) -> str:
    """重新排队后派单的「router 线索」：没拆的墙（下一次对准第一道）+ 已试过的方向（标判定，不是线索）。"""
    opens = [f"{w.get('id')} {w.get('text', '')}" for w in t.get("walls", []) if w.get("status") == "open"]
    lines = ["没拆的墙（下一次尝试对准第一道）：" + ("；".join(opens) if opens else "无（下一个方向见收件箱）")]
    prev, cur = t.get("solution") or {}, store.current_attempt(t) or {}
    if prev.get("kind") == "card" and prev.get("text"):
        lines.append(f"上一次方向卡（已试：{VERDICT_ZH.get(cur.get('verdict'), '待判')}；不是线索，别在原路上再试）："
                     f"{' '.join(prev['text'].split())[:300]}")
    tried = [f"尝试 {x.get('n')}「{x.get('direction', '')}」{VERDICT_ZH.get(x.get('verdict'), x.get('verdict') or '待判')}"
             for x in t.get("attempts") or [] if not x.get("historical")]
    if tried:
        lines.append("已试（不是线索）：" + "；".join(tried))
    return "\n".join(lines)


def _follow_up(env, st, tid, t, text) -> None:
    """判定已落（调用方不依赖续跑成败）：一律重新排队（9b，concern 4 裁决）。排不了（派单待核实 / worker 认不出或还活着）：
    判定保留，推 stuck 项带原因。"""
    try:
        _requeue_business(env, st, tid, text, REQUEUE_WAIT)
    except Fail as e:                                 # 回答这一项 = 再排一次（不续跑旧 worker）；redirect 也会清掉它
        store.push_queue(st, tid, "stuck", f"{tid} 判定已记，但没能重新排队：{e}", env.now(), topic="requeue")
        st["queue"][-1]["next"] = text


def cmd_verdict(a, env):
    guard = _guard(env)
    if _leaks(guard, a.note, a.new_wall):
        raise Fail("note / new-wall 里有 uid / 用户原话，拒收", hits=_leaks(guard, a.note, a.new_wall))
    with env.state() as st:
        actions = _verdict_core(env, st, a.ticket, a.attempt, a.verdict, touched=a.touched, hit_wall=a.hit_wall,
                                continued=a.continued, note=a.note, new_wall=a.new_wall)
    return {"ok": True, "actions": actions}


def apply_daily(env, st, data) -> dict:
    """daily 的判定 / 新墙逐条落（调用方持锁、负责存盘）。每条独立：非法 / 泄漏 / 找不到票 → errors，不拖垮其余。"""
    guard = _guard(env)
    applied, errors, offered = [], [], []

    def one(kind, i, item, fn):
        snap = copy.deepcopy(st)
        try:
            fn()
            applied.append({"kind": kind, "index": i, "ticket": item.get("ticket")})
        except Exception as e:  # noqa: BLE001 — 单条失败：回滚这条的改动，记错，继续
            st.clear(); st.update(snap)
            errors.append({"kind": kind, "index": i, "ticket": item.get("ticket") if isinstance(item, dict) else None,
                           "error": str(e)})

    def do_verdict(v):
        for k in ("attempt", "touched", "hit_wall", "continued"):
            if type(v.get(k)) is not int:
                raise Fail(f"{k} 必须是整数：{v.get(k)!r}")
        hits = _leaks(guard, v.get("note"), v.get("new_wall"))
        if hits:
            raise Fail(f"泄漏闸命中：{hits}")
        _verdict_core(env, st, v["ticket"], v["attempt"], v["verdict"], touched=v["touched"], hit_wall=v["hit_wall"],
                      continued=v["continued"], note=v.get("note", ""), new_wall=v.get("new_wall"))

    def do_wall(w):
        hits = _leaks(guard, w.get("text"))
        if hits:
            raise Fail(f"泄漏闸命中：{hits}")
        t = env.ticket(st, w["ticket"])
        if store.ticket_kind(t) != "business" or not str(w.get("text", "")).strip():
            raise Fail(f"{w['ticket']} 不是业务票或墙文本为空")
        offered.append(_add_wall(env, st, w["ticket"], w["text"], "daily")[1])

    for i, v in enumerate(data.get("verdicts", [])):
        one("verdict", i, v, lambda v=v: do_verdict(v))
    for i, w in enumerate(data.get("walls", [])):
        one("wall", i, w, lambda w=w: do_wall(w))
    return {"ok": True, "applied": applied, "errors": errors, "notes": data.get("notes", ""),
            "next_items": [q for q in offered if q and store.in_queue(st, q)]}


def cmd_apply_daily(a, env):
    try:
        data = json.loads(Path(a.source).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise Fail(f"读不了 {a.source}：{e}")
    if not isinstance(data, dict):
        raise Fail("apply-daily 的输入顶层必须是对象")
    with env.state() as st:
        return apply_daily(env, st, data)


def _day(s, name):
    if s is None:
        return None
    try:
        datetime.strptime(s, "%Y-%m-%d")
    except ValueError:
        raise Fail(f"{name} 要 YYYY-MM-DD：{s!r}")
    return s


def cmd_analyst(a, env):
    """analyst run：手动起一次分析 agent（同模式已有在跑的就不起第二个）；产出由下一轮巡检收。
    analyst calibrated：router 对照项目 brief 的已知问题判校准过没过（weekly 的提议在此之前一律不收）。"""
    if a.analyst_cmd == "calibrated":
        with env.state() as st:
            an = st.setdefault("analyst", {})
            an.update(calibrated=bool(a.passed), calibrated_note=a.note, calibrated_at=env.now())
        return {"ok": True, "calibrated": bool(a.passed), "note": a.note}
    since, until = _day(a.since, "--since"), _day(a.until, "--until")
    with env.state() as st:
        before = analyst.active_run(env.ctx, st, a.mode)
        try:
            rid = analyst.start(env, st, a.mode, since, until)
        except analyst.AnalystError as e:
            raise Fail(str(e))
        except OSError as e:                     # 起进程失败（找不到 claude_bin……）：什么都没起
            raise Fail(f"分析 agent 起不来：{e}")
        r = st["analyst"]["runs"][rid]
    return {"ok": True, "run": rid, "mode": a.mode, "reused": rid == before, "since": r.get("since"),
            "until": r.get("until"), "out": r["out"], "log": r["log"]}


_BEHAVIOR = re.compile(r"^(?P<metric>[^=]+?)\s*=\s*(?P<before>.+?)\s*(?:→|->)\s*(?P<after>.+?)\s*[,，]\s*(?P<v>[a-z_]+)\s*$")


def _behavior(items) -> list:
    """--behavior "<指标=前→后, verdict>"（可多次）：格式不对就拒；verdict 用周报 progress 的判定词。"""
    out = []
    for b in items or []:
        m = _BEHAVIOR.match(b.strip())
        if not m or m.group("v") not in store.BEHAVIOR_VERDICTS:
            raise Fail(f"--behavior 格式是「<指标>=<前>→<后>, <verdict>」，verdict 取 {'/'.join(store.BEHAVIOR_VERDICTS)}："
                       f"{b!r}")
        out.append(b.strip())
    return out


def _postlaunch_say(tid, r, qid) -> str:
    """postlaunch 决定单项：结论 + 摘要 + 动作 + 要你回什么（看板「等你」一栏原样显示）。"""
    beh = f"；行为：{'；'.join(r['behavior'])}" if r.get("behavior") else ""
    return (f"{tid} 上线验收 {r['check']}（{r['type']}）{r['verdict']}：{r['summary']}{beh}；当天动作：{r['action']}；"
            f"回「{qid} 知道了」，或「{qid}：<你的意见>」")


def cmd_postlaunch_result(a, env):
    """记一次上线验收结论（router 跑完检查后调）。
    生效 ⇒ done；看不出来 ⇒ 等下一个检查点（T+7 也看不出来 ⇒ done，交周报）；
    没入口 / 没用 / 有害 ⇒ 留在 postlaunch、进决定单（kind=postlaunch），必须带当天动作。
    增长型判生效必须拿出行为收益（--behavior 至少一条 better、没有 worse）；机制生效但行为 flat 算没用。
    风险型判生效：行为不 worse 即可（给了 --behavior 且有 worse ⇒ 拒，算有害）。
    同一检查点重复提交 = 覆盖（照样追加进 results，标 override）；同票没回的旧 postlaunch 项被新结论取代。"""
    summary, action = a.summary.strip(), (a.action or "").strip()
    if not summary:
        raise Fail("--summary 不能空：一句话，带关键数字")
    if a.verdict in store.BAD_VERDICTS and not action:
        raise Fail(f"结论是「{a.verdict}」：必须带 --action（当天就做的动作）")
    behavior = _behavior(a.behavior)
    verdicts = [_BEHAVIOR.match(b).group("v") for b in behavior]
    if a.type == "风险型" and a.verdict == "生效" and "worse" in verdicts:
        raise Fail("风险型的行为指标变差了（worse）：不能判「生效」，行为 worse 算「有害」")
    if a.type == "增长型" and a.verdict == "生效":
        if not behavior:
            raise Fail("增长型判「生效」必须拿出行为收益：加 --behavior \"<指标>=<前>→<后>, better\"")
        if "better" not in verdicts or "worse" in verdicts:
            raise Fail(f"增长型的行为指标没有确认过的正收益（{'、'.join(verdicts)}）：不能判「生效」；"
                       "机制生效但行为 flat 算「没用」，看不出来就记「看不出来」")
    with env.state() as st:
        t = env.ticket(st, a.ticket)
        pl = t.get("postlaunch")
        if not pl:
            raise Fail(f"{a.ticket} 没有上线验收记录：先 released")
        check = next((c for c in pl["checks"] if c["label"] == a.check), None)
        if check is None:
            raise Fail(f"{a.ticket} 没有检查点 {a.check}")
        prev = store.postlaunch_effective(pl).get(a.check)
        if t["status"] != store.POSTLAUNCH and not (t["status"] == store.DONE and prev):
            raise Fail(f"{a.ticket} 状态是 {t['status']}，不在上线验收中")
        if store.parse_iso(env.now()) < store.parse_iso(check["at"]):
            raise Fail(f"{a.ticket} 的 {a.check} 还没到（{check['at']}）：样本窗口不够，到点再查")
        r = {"check": a.check, "type": a.type, "verdict": a.verdict, "summary": summary, "action": action,
             "behavior": behavior, "at": env.now()}
        if prev:
            r.update(override=True, replaces={"verdict": prev["verdict"], "at": prev["at"]})
        superseded = [q["id"] for q in st["queue"] if q["kind"] == "postlaunch" and q["ticket"] == a.ticket]
        st["queue"] = [q for q in st["queue"] if q["id"] not in superseded]
        qid = None
        last = a.check == pl["checks"][-1]["label"]
        if a.verdict == "生效":
            pl["results"].append(r)
            store.set_status(st, a.ticket, store.DONE, env.now(), "")
        elif a.verdict == "看不出来":
            pl["results"].append(r)
            if last or store.postlaunch_next(t) is None:
                r["note"] = "交周报"
                store.set_status(st, a.ticket, store.DONE, env.now(), f"上线验收到 {a.check} 仍看不出来：交周报")
            else:
                store.set_status(st, a.ticket, store.POSTLAUNCH, env.now(), store.postlaunch_wait(st, t, env.now()))
        else:
            qid = store.push_queue(st, a.ticket, "postlaunch", "", env.now())
            st["queue"][-1]["summary"] = _postlaunch_say(a.ticket, r, qid)
            r["queue"] = qid
            pl["results"].append(r)
            store.set_status(st, a.ticket, store.POSTLAUNCH, env.now(), store.postlaunch_wait(st, t, env.now()))
        parent = st["tickets"].get(t.get("parent") or "")
        if parent is not None and store.ticket_kind(parent) == "business":   # 并进业务票的老票：结论记成它的历史尝试
            _legacy_attempt(st, parent["id"], t)
        status = t["status"]
    return {"ok": True, "ticket": a.ticket, "status": status, "result": r, "queue": qid, "superseded": superseded}


def _postlaunch_answered(env, st, t, q):
    """用户回 postlaunch 项：只记原话（answers.md 已写），不唤醒任何 worker。
    检查点都走完了 ⇒ 这张票的上线验收结束（done）；还有检查点 ⇒ 照常等下一个。"""
    if not t or t["status"] != store.POSTLAUNCH:
        return "已记原话（不唤醒 worker）", {}
    if store.postlaunch_next(t) is None:
        last = store.postlaunch_view(t, env.now())["last"] or {}
        store.set_status(st, t["id"], store.DONE, env.now(),
                         f"上线验收结束（{last.get('check', '')} {last.get('verdict', '')}），你已回 {q['id']}")
        return "已记原话（不唤醒 worker）；检查点都走完了，上线验收结束", {"status": store.DONE}
    t["waiting_for"] = store.postlaunch_wait(st, t, env.now())
    return "已记原话（不唤醒 worker）；照原话调整，下一个检查点照常查", {"status": t["status"]}


def _summary(st):
    counts = {}
    for t in st["tickets"].values():
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    used = {"heavy": 0, "light": 0}
    for t in st["tickets"].values():
        if store.occupies_slot(t):
            used[t["weight"]] += 1
    used["heavy"] += sum(1 for e in st["external"].values() if e.get("active") and e.get("weight") == "heavy")
    return counts, used, store.queue_view(st)       # 「几张 / 挡几张」读时现算（R3）


def _closeout_line(co) -> str:
    """仓外票收尾进度（status 文字版）：没做完的、有意保留的、最近一次出错。"""
    o = store.closeout_open(co)
    names = {"issue": "关 issue", "worktree": "清 worktree", "branch": "删分支"}
    parts = []
    todo = [f"下游 {x}" for x in o["downstream"]] + [names[c] for c in o["cols"]] + \
        [f"旧分支 {b}" for b in o["old_branches"]]
    if todo:
        parts.append("收尾未完成：" + "、".join(todo))
    kept = [f"{c}（{co[c][len('kept: '):]}）" for c in ("worktree", "branch")
            if isinstance(co, dict) and str(co.get(c, "")).startswith("kept")]
    olds = (co.get("old_branches") or {}) if isinstance(co, dict) else {}
    kept += [f"旧分支 {b}（{str(v)[len('kept: '):]}）" for b, v in olds.items() if str(v).startswith("kept")]
    if kept:
        parts.append("保留：" + "、".join(kept))
    err = (co or {}).get("error") if isinstance(co, dict) else None
    if todo and err:
        parts.append(f"出错：{str(err.get('msg', ''))[:120]}")
    return "".join(f" · {x}" for x in parts)


def _stale_line(t) -> str:
    """业务票重新排队时留下的旧 checkout（有未提交改动 / 有意保留）：条数和路径，要人处理。"""
    sc = t.get("stale_checkouts") or []
    if not sc:
        return ""
    return f" · 旧 checkout {len(sc)} 条待你处理：" + "、".join(str(x.get("worktree")) for x in sc)


def _postlaunch_line(t, now) -> str:
    """上线验收（status 文字版）：下一次检查的时间、最近一次结论。"""
    if not t.get("postlaunch"):
        return ""
    v, parts = store.postlaunch_view(t, now), []
    if v["next"]:
        parts.append(f"下次检查 {v['next']['label']} {store.hhmm(v['next']['at'])} UTC"
                     f"{'（已到期）' if v['next']['due'] else ''}")
    if v["last"]:
        r = v["last"]
        parts.append(f"最近结论 {r['check']} {r['verdict']}{'（覆盖）' if r.get('override') else ''}："
                     f"{str(r.get('summary', ''))[:120]}")
    elif not v["next"]:
        parts.append("上线验收没有结论")
    return "".join(f" · {x}" for x in parts)


def _status_tickets(st) -> dict:
    """status --json 的票视图：浅拷贝 + kind；业务票加 walls_open / last_verdict；维护票加 tasks_open。不改 state。"""
    out = {}
    for tid, t in st["tickets"].items():
        v = dict(t)
        v["kind"] = store.ticket_kind(t)
        if v["kind"] == "business":
            v["walls_open"] = sum(1 for w in t.get("walls", []) if w.get("status") == "open")
            last = store.shown_attempt(t)              # 当前尝试（最后一条非历史的）；只有历史尝试时报 legacy_verdict
            v["last_verdict"] = (last.get("legacy_verdict") if last.get("historical") else last.get("verdict")) if last else None
        elif v["kind"] == "maint":
            v["tasks_open"] = sum(1 for x in t.get("tasks", []) if not x.get("done"))
        out[tid] = v
    return out


def cmd_status(a, env):
    with env.locked():
        st = store.load_state(env.state_path)
    counts, used, queue = _summary(st)
    slots = env.cfg.get("slots", {"heavy": 3, "light": 2})
    if a.json:
        return {"ok": True, "counts": counts, "slots": slots, "used": used, "queue": queue,
                "tickets": _status_tickets(st), "external": st["external"], "batch": st["batch"]}
    lines = [f"批次 {st['batch']['id']}（{'已 start' if st['batch']['started'] else '未 start'}）"
             f" · 重票 {used['heavy']}/{slots.get('heavy')} · 轻票 {used['light']}/{slots.get('light')}",
             "决定单：" + ("" if queue else "（空）")]
    lines += [f"  {q['id']} [{q['kind']}] {q['summary']}（挡 {q['blocks']} 张）" for q in queue]
    lines.append("票：")
    for tid in sorted(st["tickets"], key=lambda x: int(x[1:])):
        t = st["tickets"][tid]
        issue = f" #{t['issue']}" if t["issue"] else ""
        wait = f" · 等：{t['waiting_for']}" if t["waiting_for"] else ""
        merged = f" · 已并入 {t['merged_into']}" if t.get("merged_into") else ""
        lines.append(f"  {tid}{issue} [{t['status']}] {t['title']}{merged}{wait}{_closeout_line(t.get('closeout'))}"
                     f"{_postlaunch_line(t, env.now())}{_stale_line(t)}")
    print("\n".join(lines))
    return None


def _review_call(fn, *args):
    import review
    try:
        return {"ok": True, **fn(review, *args)}
    except review.Refused as e:
        raise Fail(str(e), reason=e.reason, **e.extra)


def cmd_review_pack(a, env):
    """方向审的 codex 材料 + 读边界 + 三道预检 → <home>/review/<T>/codex.sh。不过 ⇒ ok:false 带 reason，不给命令。"""
    return _review_call(lambda r: r.pack(env, a.ticket, a.round))


def cmd_review_canary(a, env):
    """真 codex exec 金丝雀（一次 codex 订阅的常规审查用量）：只在 codex 升级、或从没跑过时需要。"""
    return _review_call(lambda r: r.canary(env))


def cmd_discover(a, env):
    import tick
    with env.state() as st:
        run = tick.Run(env.ctx, env.root, env.cfg, st)
        run.step1_external()                          # 与巡检第 1 步同一套逻辑（含冲突只报一次）
    return {"ok": True, "external": st["external"], "new_queue": run.res["new_queue"]}


# ---- 入口 -------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(prog="router.py", description="router：一个 session 管全部待办")
    p.add_argument("--root", default=os.getcwd(), help="项目根（默认当前目录）")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("draft", help="分诊结果 → 派发单草稿（不碰 gh、不起进程）")
    s.add_argument("--from", dest="source", required=True)
    s.add_argument("--replace", action="store_true")
    s = sub.add_parser("add", help="用户临时提的票")
    s.add_argument("--title", required=True)
    for k in ("metric", "users", "evidence", "problem", "anchor", "clue", "surfaces", "tags", "depends"):
        s.add_argument(f"--{k}")
    s.add_argument("--weight", choices=["heavy", "light"])
    s.add_argument("--priority", type=int, default=50)
    s.add_argument("--business", action="store_true", help="业务票：用户直接提 = 判过真（需 --segment --want --wall --metric）")
    s.add_argument("--segment"); s.add_argument("--want")
    s.add_argument("--wall", action="append", default=[], help="墙，可多次")
    s = sub.add_parser("propose", help="分析 agent 的提议 → 决定单（verify_problem / close）；墙直接挂票")
    s.add_argument("--from", dest="source", required=True)
    s = sub.add_parser("maint", help="维护票：add 追加到本周维护票（--unblocks 则单独建）")
    msub = s.add_subparsers(dest="maint_cmd", required=True)
    m = msub.add_parser("add")
    m.add_argument("--title", required=True); m.add_argument("--problem", required=True)
    m.add_argument("--surfaces", required=True); m.add_argument("--unblocks")
    s = sub.add_parser("impact", help="改业务票的影响分（记一条决定）")
    s.add_argument("ticket"); s.add_argument("--size", type=float, required=True)
    s.add_argument("--gap", type=float, required=True)
    s.add_argument("--confidence", required=True, choices=_CONF)
    s = sub.add_parser("start", help="用户说 start：建 issue、进队列、派")
    s.add_argument("--all", action="store_true")
    s.add_argument("tickets", nargs="*")
    s = sub.add_parser("answer", help="回答决定单上的一项，原话转给 worker")
    s.add_argument("qid"); s.add_argument("text")
    s.add_argument("--reject", action="store_true", help="驳回方向卡：worker 回第 1 段重写")
    s.add_argument("--as-router", action="store_true",
                   help="router 按票级授权代答（只收业务票的技术细节类方向项 / 卡住 / 冲突项；方向卡、下一个方向、"
                        "核心假设被推翻归用户）：标为 router 决定，不冒充用户原话")
    s = sub.add_parser("redirect", help="业务票换方向：用户定的方向（--as-user）或出新方向卡交用户（--new-card）；重新排队派新 worker")
    s.add_argument("ticket"); s.add_argument("text", nargs="?", default="")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--as-user", action="store_true", help="用户定的方向（原话逐字，必填；进 answers.md）")
    g.add_argument("--new-card", action="store_true", help="router 只要一张新方向卡（不带方向）：新 worker 提方向，卡交用户确认")
    g.add_argument("--as-router", action="store_true", help="v2.1 起一律拒：router 不定方向（用 --new-card）")
    s = sub.add_parser("close", help="用户说「关 Tn」：关业务票（收尾：关 issue、清 worktree）")
    s.add_argument("ticket"); s.add_argument("--reason")
    s.add_argument("--rolled-back", action="store_true",
                   help="还有在途尝试 / 改动时：router 已照撤回办法撤回，确认要关（记一条 decide）")
    s = sub.add_parser("absorb", help="把老票并进已有的业务票（设 parent；验收过的记一次历史尝试）")
    s.add_argument("ticket"); s.add_argument("olds", nargs="+")
    s = sub.add_parser("merge", help="把业务票并进另一张业务票（同一业务目标 / 同一指标）：墙、证据、尝试挪过去，被并票标已并")
    s.add_argument("target", help="留下的业务票"); s.add_argument("source", help="被并的业务票")
    s.add_argument("--said", required=True, help="用户确认并票时的原话（进 answers.md）")
    s.add_argument("--walls-first", action="store_true", help="被并票的墙排在目标票的墙前面（它的卡点在用户路径上更早）")
    s = sub.add_parser("migrate-apply", help="用户确认整份迁移清单后落 migrate 的产出（建业务票、并老票）")
    s.add_argument("run", help="migrate 的 run 编号（analyst_done 里的 run，如 migrate-1）")
    s.add_argument("--said", required=True, help="用户确认时的原话（进 answers.md）")
    s.add_argument("--skip", help="用户不要的条目，逗号分隔：问题写 key，attach / walls 写业务票号")
    s.add_argument("--hold", help="用户说先放着的问题 key（只建票不 start），逗号分隔")
    s = sub.add_parser("submit-card", help="方向卡进决定单（全部票都交用户确认）")
    s.add_argument("ticket"); s.add_argument("--card", required=True)
    s.add_argument("--rounds", type=int, default=0); s.add_argument("--codex")
    s.add_argument("--overtime", action="store_true")
    s.add_argument("--unresolved", action="store_true",
                   help="方向审没谈拢：卡末尾有「## 未解决的挑战」，连同卡一起交用户（router 不裁决）")
    s = sub.add_parser("decide", help="router 替用户做的决定，记进 decisions.md")
    for k in ("ticket", "decision", "why", "basis"):
        s.add_argument(f"--{k}", required=True)
    s = sub.add_parser("revoke", help="推翻 Dn")
    s.add_argument("did"); s.add_argument("--reason", required=True)
    s = sub.add_parser("tell", help="写进 worker 收件箱")
    s.add_argument("ticket"); s.add_argument("text"); s.add_argument("--resume", action="store_true")
    s.add_argument("--as-router", action="store_true",
                   help="router 自己的话（方向审挑战、转述）：标「router 转述 / 决定」，不冒充用户原话")
    for name, h in (("pause", "先停：做完当前这一步"), ("stop", "立刻停"), ("takeover", "我接管"),
                    ("handback", "还给 router"), ("walkthrough-done", "staging 走查过了 / 仓外改动核对过了")):
        sub.add_parser(name, help=h).add_argument("ticket")
    s = sub.add_parser("surfaces", help="更新票的代码面 / 越界时分配锁（记一条决定）")
    s.add_argument("ticket"); s.add_argument("--set", required=True); s.add_argument("--why", required=True)
    s.add_argument("--force", action="store_true")
    sub.add_parser("ack-slow", help="慢路径事件处理完了：清掉存档")
    s = sub.add_parser("prioritize", help="调整优先级（越小越先）")
    s.add_argument("ticket"); s.add_argument("n", type=int)
    s = sub.add_parser("emit", help="worker 往上抛事件")
    s.add_argument("type"); s.add_argument("summary"); s.add_argument("--evidence", required=True)
    for k in ("surfaces", "tags", "tickets", "ticket"):
        s.add_argument(f"--{k}")
    s = sub.add_parser("progress", help="worker 报进度（只更新 UI）")
    s.add_argument("phase"); s.add_argument("--ticket")
    s = sub.add_parser("inbox", help="worker 读收件箱")
    s.add_argument("--ticket")
    sub.add_parser("prod-batch", help="把等上 prod 的票攒成一条决定")
    s = sub.add_parser("released", help="发完 prod：写周报 releases.json，票进上线验收（不是完成）")
    s.add_argument("--tickets", nargs="+", required=True); s.add_argument("--what", required=True)
    s.add_argument("--metrics", required=True)
    s.add_argument("--t0", help="生效时刻（带时区的 ISO）；配置类上线的生效时刻和执行 released 的时刻不同时用，缺省 = 现在")
    s.add_argument("--rollback", help="业务票必填：这次上线的撤回办法")
    s.add_argument("--config-keys", help="配置类上线改了哪些键（逗号分隔，如 llm_config.timeouts）；周报据此认登记，缺了会把同窗判定封顶")
    s = sub.add_parser("verdict", help="业务票：记一次尝试的判定，按结果叫醒 worker / 推决定单")
    s.add_argument("ticket"); s.add_argument("--attempt", type=int, required=True)
    s.add_argument("--verdict", required=True, choices=list(store.BIZ_VERDICTS))
    s.add_argument("--touched", type=int, required=True); s.add_argument("--hit-wall", type=int, required=True)
    s.add_argument("--continued", type=int, required=True); s.add_argument("--note", required=True)
    s.add_argument("--new-wall")
    s = sub.add_parser("apply-daily", help="daily 的判定 / 新墙逐条落（每条独立，坏的进 errors）")
    s.add_argument("--from", dest="source", required=True)
    s = sub.add_parser("analyst", help="分析 agent（Fable）：手动起一次 / 记校准结果")
    asub = s.add_subparsers(dest="analyst_cmd", required=True)
    r = asub.add_parser("run", help="起一次分析 agent；产出由下一轮巡检收")
    r.add_argument("--mode", required=True, choices=list(analyst.MODES))
    r.add_argument("--since", help="YYYY-MM-DD（UTC）；缺省按模式往前推")
    r.add_argument("--until", help="YYYY-MM-DD（UTC）；缺省今天")
    c = asub.add_parser("calibrated", help="router 判校准过没过")
    g = c.add_mutually_exclusive_group(required=True)
    g.add_argument("--pass", dest="passed", action="store_true")
    g.add_argument("--fail", dest="failed", action="store_true")
    c.add_argument("--note", required=True, help="判定依据：找到了哪几个已知问题")
    s = sub.add_parser("postlaunch-result", help="记一次上线验收结论（T+1 / T+3 / T+7）")
    s.add_argument("ticket")
    s.add_argument("--check", required=True, choices=[c for c, _ in store.POSTLAUNCH_CHECKS])
    s.add_argument("--verdict", required=True, choices=list(store.VERDICTS))
    s.add_argument("--type", required=True, choices=list(store.TICKET_TYPES))
    s.add_argument("--summary", required=True, help="一句话，带关键数字")
    s.add_argument("--action", help="当天动作；没入口 / 没用 / 有害必填")
    s.add_argument("--behavior", action="append", default=[],
                   help="行为指标「<指标>=<前>→<后>, <verdict>」，可多次；增长型判生效必填且要有 better")
    s = sub.add_parser("status", help="看板文字版")
    s.add_argument("--json", action="store_true")
    sub.add_parser("discover", help="扫外部 worktree，登记它们锁着的代码面")
    s = sub.add_parser("review-pack", help="方向审：拼 codex 材料、设读边界、预检，生成 codex.sh")
    s.add_argument("ticket"); s.add_argument("--round", type=int, default=1)
    sub.add_parser("review-canary", help="用真 codex exec 验读边界（按 codex 版本记 canary.json）")
    extend_parser(sub)
    return p


def extend_parser(sub):
    """tick / watch 由 tick.py 提供（未接入时不注册）。"""
    try:
        import tick
    except ImportError:
        return
    tick.add_subcommands(sub)


def main(argv=None):
    args = build_parser().parse_args(argv)
    fn = globals().get("cmd_" + args.cmd.replace("-", "_"))
    if fn is None:
        import tick
        fn = getattr(tick, "cmd_" + args.cmd)
    try:
        if os.environ.get("ROUTER_TICKET") and args.cmd in ROUTER_ONLY:
            raise Fail(f"{args.cmd} 只能由用户 / router session 跑，worker 不许调用", code=2)
        env = Env(Path(args.root), Ctx())
        res = fn(args, env)
    except Fail as e:
        print(json.dumps({"ok": False, "error": str(e), **e.extra}, ensure_ascii=False))
        return e.code
    except store.StateCorrupt as e:
        print(json.dumps({"ok": False, "error": str(e)}, ensure_ascii=False))
        return 1
    if res is not None:
        print(json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
