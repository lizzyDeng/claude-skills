#!/usr/bin/env python3
"""router CLI：router session 和 worker 对状态的唯一入口。

  python3 router.py [--root DIR] <cmd> …

配置：<root>/.claude/router/config.json；状态目录：<root>/.claude/router/
（worker 进程里由环境变量 ROUTER_HOME 指定）。每个子命令往 stdout 打一个 JSON：{"ok": bool, …}。
改状态的子命令都在 <状态目录>/.lock 上加排他锁：后台 watch 的 tick 和前台命令不会互相覆盖。
"""
import argparse
import json
import os
import re
import sys
from contextlib import contextmanager
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
               "handback", "prioritize", "walkthrough-done", "prod-batch", "released", "surfaces", "ack-slow"}
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
        """load → 改 → save → 重画看板，全程持锁。出异常就不存。"""
        with self.locked():
            st = store.load_state(self.state_path)
            yield st
            store.save_state(self.state_path, st)
            render_board(self, st)

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


def _resume(env, st, tid, kind, text, status, force_restart=False, waiting=None):
    """续跑 worker → "resumed"；worker 还活着 → 只投递收件箱、不起第二个进程 → "delivered"。
    force_restart：用户回答的是「卡住」，旧进程可能挂死，先停再续。接管中的票一律不碰。
    waiting：续跑后的 waiting_for；不传就按 kind 给（RESUME_WAIT），不会被清空。"""
    t = st["tickets"][tid]
    if t["status"] == store.TAKEOVER:
        raise Fail(f"{tid} 在你接管中：先「还给 router」（handback）再续跑")
    if not (t["worker"].get("session_id") and t["worker"].get("worktree")):
        raise Fail(f"{tid} 没有可续跑的 worker session")
    if launcher.worker_alive(env.ctx, t["worker"]):
        if not force_restart:
            events.deliver(env.home, tid, {"ts": env.now(), "ticket": "router", "type": "tell", "summary": text})
            return "delivered"
        launcher.stop_worker(env.ctx, t["worker"])
    inbox = events.read_inbox(env.home, st, tid)
    launcher.run_worker(env.ctx, env.home, env.cfg, st, tid, _resume_prompt(kind, text, inbox), True, env.now())
    store.set_status(st, tid, status, env.now(), waiting if waiting is not None else _resume_wait(kind, status))
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


def cmd_add(a, env):
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
    import tick
    repo = env.cfg["repo"]
    blocked, errors, warnings, started = [], [], [], []
    with env.locked():
        st = store.load_state(env.state_path)
        if a.all:
            sel = [tid for tid, t in st["tickets"].items()
                   if t["batch"] == st["batch"]["id"] and t["origin"] == "weekly" and t["status"] == store.DRAFT]
        else:
            sel = a.tickets
        if not sel:
            raise Fail("没有可 start 的票")
        for tid in sel:
            if env.ticket(st, tid)["status"] not in (store.DRAFT, store.HOLD):
                raise Fail(f"{tid} 状态是 {st['tickets'][tid]['status']}，不能 start")
        # 在任何 GitHub 写入（map / issue）之前先查远端 base：base 不对，一张 issue 都不建
        run = tick.Run(env.ctx, env.root, env.cfg, st)
        base = run.ensure_base()
        if not base["ok"]:
            if not base["transient"]:                    # 进了决定单：项先落盘，再 Fail
                store.save_state(env.state_path, st)
                render_board(env, st)
            raise Fail(f"远端 {env.cfg.get('base_branch', 'staging')} 不可用，没有建任何 issue：{base['error']}",
                       base_wait=base["wait"])
        uids = github.load_guard_uids(env.cfg.get("leak_guard_globs", []))
        if not st.get("map"):
            st["map"] = github.ensure_map(env.ctx, repo, env.cfg["map_title"])
            store.save_state(env.state_path, st)
        for tid in sorted(sel, key=lambda x: (st["tickets"][x]["priority"], int(x[1:]))):
            t = st["tickets"][tid]
            if t["issue"] is None:
                try:
                    t["issue"] = github.create_ticket_issue(env.ctx, repo, st["map"], t, uids)
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


def cmd_submit_card(a, env):
    card = Path(a.card)
    if not card.exists():
        raise Fail(f"方向卡不存在：{card}")
    with env.state() as st:
        t = env.ticket(st, a.ticket)
        if t["status"] != store.KICKOFF:
            raise Fail(f"{a.ticket} 状态是 {t['status']}，只有出方向卡阶段（kickoff）的票能交卡")
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
    if status in (store.DONE, store.AWAIT_PROD):
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


def cmd_released(a, env):
    runs = env.cfg.get("weekly_runs_dir")
    if not runs:
        raise Fail("config 缺 weekly_runs_dir")
    rel = Path(os.path.expanduser(runs)).parent / "state" / "releases.json"
    with env.state() as st:
        for tid in a.tickets:
            env.ticket(st, tid)
        lines = json.loads(rel.read_text(encoding="utf-8")) if rel.exists() else []
        entry = {"date": env.ctx.now().strftime("%Y-%m-%d"), "what": a.what, "metrics": _csv(a.metrics)}
        store._atomic_write(rel, json.dumps(lines + [entry], ensure_ascii=False, indent=2) + "\n")
        for tid in a.tickets:
            store.set_status(st, tid, store.DONE, env.now())
        _prod_item(env, st)
    return {"ok": True, "release": entry, "path": str(rel)}


def _summary(st):
    counts = {}
    for t in st["tickets"].values():
        counts[t["status"]] = counts.get(t["status"], 0) + 1
    used = {"heavy": 0, "light": 0}
    for t in st["tickets"].values():
        if t["status"] in store.RUNNING:
            used[t["weight"]] += 1
    used["heavy"] += sum(1 for e in st["external"].values() if e.get("active") and e.get("weight") == "heavy")
    return counts, used, store.queue_view(st)       # 「几张 / 挡几张」读时现算（R3）


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
        lines.append(f"  {tid}{issue} [{t['status']}] {t['title']}{wait}")
    print("\n".join(lines))
    return None


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
    s = sub.add_parser("released", help="发完 prod：票改完成、写周报 releases.json")
    s.add_argument("--tickets", nargs="+", required=True); s.add_argument("--what", required=True)
    s.add_argument("--metrics", required=True)
    s = sub.add_parser("status", help="看板文字版")
    s.add_argument("--json", action="store_true")
    sub.add_parser("discover", help="扫外部 worktree，登记它们锁着的代码面")
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
