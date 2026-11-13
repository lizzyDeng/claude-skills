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
               "ack-slow", "review-pack", "review-canary", "propose", "verdict", "apply-daily"}
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


def _resume_prompt(kind, text, inbox):
    parts = ["【router】你之前停下等的事有了回答。", "", "用户原话（逐字）：", text, ""]
    if inbox:
        parts += ["你停下期间收件箱收到的：",
                  *[f"- [{m.get('type')}] {m.get('summary', '')}（来自 {m.get('ticket', 'router')}）" for m in inbox], ""]
    parts += [KIND_HINT.get(kind, KIND_HINT["direction"]), "先按 worker.md 读一次 inbox，再继续。"]
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


def _resume(env, st, tid, kind, text, status, force_restart=False, waiting=None):
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
            events.deliver(env.home, tid, {"ts": env.now(), "ticket": "router", "type": "tell", "summary": text})
            return "delivered"
        launcher.stop_worker(env.ctx, t["worker"])
    inbox = events.read_inbox(env.home, st, tid)
    wait = waiting if waiting is not None else _resume_wait(kind, status)
    try:
        launcher.resume_with_intent(env.ctx, env.home, env.cfg, st, tid, _resume_prompt(kind, text, inbox), status,
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
    with env.state() as st:
        tid = store.new_business_ticket(
            st, title=a.title, segment=a.segment, want=a.want, walls=list(a.wall), metric={"name": a.metric},
            evidence=[a.evidence] if a.evidence else [], impact={}, origin="user")   # 用户直接提 = 判过真
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


def cmd_answer(a, env):
    with env.state() as st:
        try:
            q = store.pop_queue(st, a.qid)
        except KeyError:
            raise Fail(f"决定单里没有 {a.qid}")
        if q["kind"] in ("verify_problem", "close"):      # 格式不对 ⇒ Fail，不存盘：项留在决定单里
            return _answer_business(env, st, q, a.text)
        _append_local(env.home / "answers.md",
                      f"- {env.now()} | {q['id']} | {q['kind']} | {q['ticket'] or '-'} | {' '.join(a.text.splitlines())}")
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
        elif q["kind"] == "postlaunch":
            hint, extra = _postlaunch_answered(env, st, t, q)
        elif t and t["status"] == store.TAKEOVER:
            hint = f"{t['id']} 在你接管中，回答已存档；还给 router 后再续跑"
        elif t and q["kind"] in RESUME_KINDS and t["worker"].get("session_id"):
            kind = q["kind"]
            if kind == "card" and a.reject:
                kind = "reject"
                t["card_confirmed"] = False
                t["solution"]["source"] = re.sub(r"（.*?）$", "（被驳回）", t["solution"]["source"])
            elif kind == "card":
                t["card_confirmed"] = True
                t["solution"]["source"] = t["solution"]["source"].replace("待你确认", "已确认")
            status = store.EXECUTING if t.get("card_confirmed") else store.KICKOFF
            how = _resume(env, st, t["id"], kind, a.text, status, force_restart=(kind == "stuck"))
            resumed, delivered = (t["id"], None) if how == "resumed" else (None, t["id"])
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


def _check_problem(i, p) -> None:
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
    for i, p in enumerate(problems):
        _check_problem(i, p)
    for w in walls:
        if w.get("ticket") not in st["tickets"] or store.ticket_kind(st["tickets"][w["ticket"]]) != "business":
            raise Fail(f"walls 里的票 {w.get('ticket')} 不是已有的业务票")
    for c in close:
        if c.get("ticket") not in st["tickets"] or store.ticket_kind(st["tickets"][c["ticket"]]) != "business":
            raise Fail(f"close 里的票 {c.get('ticket')} 不是已有的业务票")
    for p in problems:
        for oid in p.get("absorbs", []):
            if oid not in st["tickets"]:
                raise Fail(f"problems（{p['key']}）absorbs 里的票 {oid} 不存在")
    try:
        guard = github.load_guard(env.cfg.get("leak_guard_globs", []))
    except github.GuardEmpty as e:
        raise Fail(f"防泄漏闸失效，提议一条都没收：{e}")
    texts = [json.dumps(x, ensure_ascii=False) for x in (*problems, *maint, *walls, *close)]
    hits = sorted({h for tx in texts for h in github.leak_hits(tx, guard)})
    if hits:
        raise Fail("提议里有 uid / 用户原话，整份拒收", hits=hits)
    rejected, at = _rejected_keys(env), env.now()
    pending = {(st.get("proposals", {}).get(q.get("proposal") or "") or {}).get("key")
               for q in st["queue"] if q["kind"] == "verify_problem"}
    out, skipped = [], []
    for p in problems:
        if p["key"] in pending or (p["key"] in rejected and not (p.get("new_evidence") or "").strip()):
            skipped.append(p["key"])
            continue
        st["seq"]["proposal"] = st["seq"].get("proposal", 0) + 1
        pid = f"P{st['seq']['proposal']}"
        st.setdefault("proposals", {})[pid] = {**p, "id": pid, "mode": mode, "at": at}
        qid = store.push_queue(st, None, "verify_problem", f"判真：{p['title']}（{p['segment']}）", at)
        st["queue"][-1]["proposal"] = pid
        out.append({"id": pid, "key": p["key"], "title": p["title"], "queue": qid})
    for w in walls:
        store.attach_wall(st, w["ticket"], w["text"], "weekly", at)
    for c in close:
        qid = store.push_queue(st, c["ticket"], "close", f"提议结束 {c['ticket']}：{c.get('reason', '')}", at)
        st["queue"][-1]["reason"] = c.get("reason", "")
    return {"ok": True, "proposals": out, "skipped": skipped, "maint_pending": maint}


def cmd_propose(a, env):
    data = json.loads(Path(a.source).read_text(encoding="utf-8"))
    with env.state() as st:
        return propose_from(env, st, data)


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
        if verb == "close":
            ct = st["tickets"][q["ticket"]]
            if store.ticket_kind(ct) != "business":
                raise Fail(f"{q['ticket']} 不是业务票，不能这样关")
            pid = ct["worker"].get("pid")
            if pid and env.ctx.pid_alive(pid):
                raise Fail(f"{q['ticket']} 的 worker 还在跑（pid {pid}）：先 stop {q['ticket']}，再回「关」")
            ct["closeout"] = {"downstream": _closeout_targets(st, q["ticket"]),
                              "issue": "pending" if ct.get("issue") else "n/a",
                              "worktree": "pending", "branch": "pending"}
            store.set_status(st, q["ticket"], store.DONE, env.now(), "收尾中（下一轮巡检：关 issue、清 worktree）")
            store.append_decision(env.decisions, st, q["ticket"], "关闭业务票", q.get("reason", ""),
                                  f"用户回答 {q['id']}：{t}", env.now())
        return {"ok": True, "queue": q["id"], "kind": "close", "ticket": q["ticket"], "closed": verb == "close"}
    prop = st.get("proposals", {}).get(q.get("proposal") or "")
    if prop is None:
        raise Fail(f"{q['id']} 指向的提议 {q.get('proposal')} 不存在")
    if verb == "no":
        _append_local(env.home / "analyst" / "rejected.jsonl",
                      json.dumps({"key": prop["key"], "title": prop["title"], "reason": reason, "at": env.now()},
                                 ensure_ascii=False))
        return {"ok": True, "queue": q["id"], "kind": "verify_problem", "rejected": prop["key"]}
    tid = store.new_business_ticket(
        st, title=prop["title"], segment=prop["segment"], want=prop["want"], walls=prop["walls"],
        metric=prop["metric"], evidence=prop.get("evidence", []), impact=prop["impact"], origin="analyst",
        absorbs=prop.get("absorbs", []))
    nt = st["tickets"][tid]
    _biz_issue_fields(nt)
    nt["history"].append({"at": env.now(), "status": store.DRAFT})
    for old_id in nt["absorbs"]:                      # R1：被吸收的老票计为一次已结束的历史尝试
        old = st["tickets"].get(old_id)
        if old is None:
            continue
        old["parent"] = tid
        pl = old.get("postlaunch") or {}
        results = pl.get("results") or []
        if old["status"] in (store.POSTLAUNCH, store.DONE) and results:   # 只有真上线并验收过的老票才算一次历史尝试
            nt["attempts"].append({"n": len(nt["attempts"]) + 1, "direction": old["title"],
                                   "shipped_at": pl.get("t0"), "verdict": "legacy",
                                   "legacy_verdict": results[-1].get("verdict"), "historical": True})
    res = {"ok": True, "queue": q["id"], "kind": "verify_problem", "ticket": tid, "held": verb == "hold"}
    if verb == "true":
        res["start"] = _start_locked(env, st, [tid])
    return res


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
        t["card"] = {"path": str(card), "rounds": a.rounds, "codex": a.codex, "overtime": a.overtime,
                     "version": version, "submitted_at": env.now()}
        t["solution"] = {"kind": "card", "text": _card_direction(card.read_text(encoding="utf-8")),
                         "source": f"方向卡 v{version}（待你确认）"}
        launcher.stop_worker(env.ctx, t["worker"])      # 超时交卡：worker 还在写，先停，确认后续跑
        t["worker"]["pid"] = None
        t["worker"]["kickoff_rounds"] = a.rounds
        store.set_status(st, a.ticket, store.AWAIT_CARD, env.now(), "等你确认方向卡")
        qid = store.push_queue(st, a.ticket, "card",
                               f"{a.ticket} 方向卡 v{version}{'（超时，见卡上未完成项）' if a.overtime else ''}：{t['title']}",
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
    with env.state() as st:
        t = env.ticket(st, a.ticket)
        if t["status"] == store.TAKEOVER and a.resume:
            raise Fail(f"{a.ticket} 在你接管中：先「还给 router」（handback）再续跑")
        if a.resume:
            _refuse_pending(t, a.ticket, env.ctx)     # 先拒再投递：不然重试会投两份
        alive = launcher.worker_alive(env.ctx, t["worker"])
        if not (a.resume and alive):                  # 活着时由 _resume 投递，别投两份
            events.deliver(env.home, a.ticket, {"ts": env.now(), "ticket": "router", "type": "tell", "summary": a.text})
        resumed = None
        if a.resume and _resume(env, st, a.ticket, "direction", a.text,
                                t["status"] if t["status"] in store.RUNNING else store.KICKOFF) == "resumed":
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
        t["surfaces"], t["weight"] = names, surfaces.weight_for(set(names), env.table)
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


def cmd_walkthrough_done(a, env):
    with env.state() as st:
        res = _checked(env, st, env.ticket(st, a.ticket))
    return {"ok": True, **res}


def _prod_item(env, st):
    st["queue"] = [q for q in st["queue"] if q["kind"] != "prod"]
    ready = sorted((tid for tid, t in st["tickets"].items() if t["status"] == store.AWAIT_PROD),
                   key=lambda x: int(x[1:]))
    if ready:
        return store.push_queue(st, None, "prod", f"上 prod：{' '.join(ready)}", env.now()), ready
    return None, []


def cmd_prod_batch(a, env):
    with env.state() as st:
        qid, ready = _prod_item(env, st)
    return {"ok": True, "queue": qid, "tickets": ready}


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


def cmd_released(a, env):
    """发完 prod：写周报 releases.json（照旧），票进上线验收（postlaunch），不进 done。
    票的终点是上线验收：t0 + 1 / 3 / 7 天各查一次（tick 到期交 postlaunch_due）。
    已经验收过又重新上线（改了配置再发）：旧的验收记录挪进 postlaunch_prev，从新的 t0 重新查。"""
    runs = env.cfg.get("weekly_runs_dir")
    if not runs:
        raise Fail("config 缺 weekly_runs_dir")
    t0 = _parse_t0(a.t0) if a.t0 else env.now()
    rel = Path(os.path.expanduser(runs)).parent / "state" / "releases.json"
    with env.state() as st:
        for tid in a.tickets:
            env.ticket(st, tid)
        biz = [tid for tid in a.tickets if store.ticket_kind(st["tickets"][tid]) == "business"]
        for tid in biz:                                                  # 业务票：先验完再写 releases.json（拒收不留痕）
            if not (a.rollback or "").strip():
                raise Fail(f"{tid} 是业务票：released 必须带 --rollback \"<撤回办法>\"")
            if any(x.get("verdict") in (None, "pending_sample") for x in st["tickets"][tid]["attempts"]
                   if not x.get("historical")):
                raise Fail(f"{tid} 上一次尝试还没判（判定为空或待样本）：先 verdict，再 released")
        lines = json.loads(rel.read_text(encoding="utf-8")) if rel.exists() else []
        # at = 生效时刻：周报按它切窗；缺 at 会回落到 date 当天 00:00，窗口和批边界整体算歪。date 跟 at 走（本机时区）
        local = datetime.strptime(t0, store.ISO).replace(tzinfo=timezone.utc).astimezone()
        entry = {"date": local.strftime("%Y-%m-%d"), "at": t0, "what": a.what, "metrics": _csv(a.metrics)}
        if a.config_keys:
            entry["config_keys"] = _csv(a.config_keys)                   # 周报 registered() 按结构化键名认登记，不靠 what 里的短名
        store._atomic_write(rel, json.dumps(lines + [entry], ensure_ascii=False, indent=2) + "\n")
        plans = {}
        for tid in a.tickets:
            t = st["tickets"][tid]
            if tid in biz:                                               # 验收交给 daily 的判定，不排 T+1/T+3/T+7
                n = store.add_attempt(st, tid, a.what, t0, a.rollback.strip())
                store.set_status(st, tid, store.POSTLAUNCH, env.now(), f"等 daily 判定尝试 {n}")
                continue
            if t.get("postlaunch"):
                t.setdefault("postlaunch_prev", []).append(t["postlaunch"])
            t["postlaunch"] = plans[tid] = store.postlaunch_plan(t0)
            store.set_status(st, tid, store.POSTLAUNCH, env.now(), store.postlaunch_wait(st, t, env.now()))
        _prod_item(env, st)
    return {"ok": True, "release": entry, "path": str(rel), "postlaunch": plans}


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


def _verdict_core(env, st, tid, n, verdict, *, touched, hit_wall, continued, note, new_wall=None) -> list:
    t = env.ticket(st, tid)
    if store.ticket_kind(t) != "business":
        raise Fail(f"{tid} 不是业务票，没有尝试判定")
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
            t["waiting_for"] = "有害：等 router 按回滚办法撤回"
            store.push_queue(st, tid, "harm", f"{tid} 尝试 {n} 有害，需撤回：{att.get('rollback', '')}", env.now())
            _wake(st, "rollback_needed", {"ticket": tid, "rollback": att.get("rollback", "")})
        elif act == "check_entry":
            t["waiting_for"] = "没有入口：等 router 查入口"
            _wake(st, "no_entry", {"ticket": tid, "attempt": n})
    return actions


def _follow_up(env, st, tid, t, text) -> None:
    """判定已落（调用方不依赖续跑成败）。没 worktree（合并后已清）：话写进收件箱 + 回 queued，调度器派新 worker；
    其余 Fail：判定保留，推 stuck 项带原因。"""
    w = t["worker"]
    if not (w.get("worktree") and w.get("session_id")):
        events.deliver(env.home, tid, {"ts": env.now(), "ticket": "router", "type": "tell", "summary": text})
        store.set_status(st, tid, store.QUEUED, env.now(), "判定已记，等调度器派新 worker 接着做（读收件箱和尝试记录）")
        return
    try:
        _resume(env, st, tid, "direction", text, store.EXECUTING)
    except Fail as e:
        store.push_queue(st, tid, "stuck", f"{tid} 判定已记，但没能叫醒 worker：{e}", env.now())


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
    applied, errors = [], []

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
        store.attach_wall(st, w["ticket"], w["text"], "daily", env.now())

    for i, v in enumerate(data.get("verdicts", [])):
        one("verdict", i, v, lambda v=v: do_verdict(v))
    for i, w in enumerate(data.get("walls", [])):
        one("wall", i, w, lambda w=w: do_wall(w))
    return {"ok": True, "applied": applied, "errors": errors, "notes": data.get("notes", "")}


def cmd_apply_daily(a, env):
    try:
        data = json.loads(Path(a.source).read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise Fail(f"读不了 {a.source}：{e}")
    if not isinstance(data, dict):
        raise Fail("apply-daily 的输入顶层必须是对象")
    with env.state() as st:
        return apply_daily(env, st, data)


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
    todo = [f"下游 {x}" for x in o["downstream"]] + [names[c] for c in o["cols"]]
    if todo:
        parts.append("收尾未完成：" + "、".join(todo))
    kept = [f"{c}（{co[c][len('kept: '):]}）" for c in ("worktree", "branch")
            if isinstance(co, dict) and str(co.get(c, "")).startswith("kept")]
    if kept:
        parts.append("保留：" + "、".join(kept))
    err = (co or {}).get("error") if isinstance(co, dict) else None
    if todo and err:
        parts.append(f"出错：{str(err.get('msg', ''))[:120]}")
    return "".join(f" · {x}" for x in parts)


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


def cmd_status(a, env):
    with env.locked():
        st = store.load_state(env.state_path)
    counts, used, queue = _summary(st)
    slots = env.cfg.get("slots", {"heavy": 3, "light": 2})
    if a.json:
        return {"ok": True, "counts": counts, "slots": slots, "used": used, "queue": queue,
                "tickets": st["tickets"], "external": st["external"], "batch": st["batch"]}
    lines = [f"批次 {st['batch']['id']}（{'已 start' if st['batch']['started'] else '未 start'}）"
             f" · 重票 {used['heavy']}/{slots.get('heavy')} · 轻票 {used['light']}/{slots.get('light')}",
             "决定单：" + ("" if queue else "（空）")]
    lines += [f"  {q['id']} [{q['kind']}] {q['summary']}（挡 {q['blocks']} 张）" for q in queue]
    lines.append("票：")
    for tid in sorted(st["tickets"], key=lambda x: int(x[1:])):
        t = st["tickets"][tid]
        issue = f" #{t['issue']}" if t["issue"] else ""
        wait = f" · 等：{t['waiting_for']}" if t["waiting_for"] else ""
        lines.append(f"  {tid}{issue} [{t['status']}] {t['title']}{wait}{_closeout_line(t.get('closeout'))}"
                     f"{_postlaunch_line(t, env.now())}")
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
    s = sub.add_parser("start", help="用户说 start：建 issue、进队列、派")
    s.add_argument("--all", action="store_true")
    s.add_argument("tickets", nargs="*")
    s = sub.add_parser("answer", help="回答决定单上的一项，原话转给 worker")
    s.add_argument("qid"); s.add_argument("text")
    s.add_argument("--reject", action="store_true", help="驳回方向卡：worker 回第 1 段重写")
    s = sub.add_parser("submit-card", help="方向卡进决定单")
    s.add_argument("ticket"); s.add_argument("--card", required=True)
    s.add_argument("--rounds", type=int, default=0); s.add_argument("--codex")
    s.add_argument("--overtime", action="store_true")
    s = sub.add_parser("decide", help="router 替用户做的决定，记进 decisions.md")
    for k in ("ticket", "decision", "why", "basis"):
        s.add_argument(f"--{k}", required=True)
    s = sub.add_parser("revoke", help="推翻 Dn")
    s.add_argument("did"); s.add_argument("--reason", required=True)
    s = sub.add_parser("tell", help="写进 worker 收件箱")
    s.add_argument("ticket"); s.add_argument("text"); s.add_argument("--resume", action="store_true")
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
