#!/usr/bin/env python3
"""router CLI：router session 和 worker 对状态的唯一入口。

  python3 router.py [--root DIR] <cmd> …

配置：<root>/.claude/router/config.json；状态目录：<root>/.claude/router/
（worker 进程里由环境变量 ROUTER_HOME 指定）。每个子命令往 stdout 打一个 JSON：{"ok": bool, …}。
改状态的子命令都在 <状态目录>/.lock 上加排他锁：后台 watch 的 tick 和前台命令不会互相覆盖。
"""
import argparse
import fcntl
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
KIND_HINT = {
    "card": "方向卡已确认（用户原话里如有修改，以修改为准）。进入 worker.md 第 2 段「执行」。",
    "spend": "这是花钱放行的回答：照原话执行；没放行就换不花钱的做法，做不到就再 emit decision 停下。",
    "direction": "照原话继续当前阶段。",
    "stuck": "router 发现你上次没留下结果就退出了。先看 git status / git log 和你的笔记，确认做到哪，再照原话继续。",
    "conflict": "照原话处理代码面冲突后继续。",
}


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

    @contextmanager
    def locked(self):
        self.home.mkdir(parents=True, exist_ok=True)
        with open(self.home / ".lock", "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)

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


def notify(env, text) -> str:
    try:
        import notify as nt
    except ImportError:
        return f"DRY RUN（notify 未接入）：{text}"
    return nt.send(Path(os.path.expanduser(env.cfg.get("lark_config", ""))), text, env.ctx)


def dispatch(env, st) -> list:
    try:
        import tick
    except ImportError:
        return []
    return tick.dispatch(env.ctx, env.root, env.cfg, st)


def _csv(s):
    return [x.strip() for x in (s or "").split(",") if x.strip()]


def _blocks(st, tid) -> int:
    """这张票挡住了几张：依赖它的 + 排队等它代码面的。"""
    t = st["tickets"][tid]
    return sum(1 for o in st["tickets"].values() if o["id"] != tid and o["status"] not in store.SETTLED
               and (tid in o["blocked_by"]
                    or (o["status"] == store.QUEUED and set(o["surfaces"]) & set(t["surfaces"]))))


def _resume_prompt(kind, text, inbox):
    parts = ["【router】你之前停下等的事有了回答。", "", "用户原话（逐字）：", text, ""]
    if inbox:
        parts += ["你停下期间收件箱收到的：",
                  *[f"- [{m.get('type')}] {m.get('summary', '')}（来自 {m.get('ticket', 'router')}）" for m in inbox], ""]
    parts += [KIND_HINT.get(kind, KIND_HINT["direction"]), "先按 worker.md 读一次 inbox，再继续。"]
    return "\n".join(parts)


def _resume(env, st, tid, kind, text, status):
    t = st["tickets"][tid]
    if not (t["worker"].get("session_id") and t["worker"].get("worktree")):
        raise Fail(f"{tid} 没有可续跑的 worker session")
    inbox = events.read_inbox(env.home, st, tid)
    launcher.run_worker(env.ctx, env.home, env.cfg, st, tid, _resume_prompt(kind, text, inbox), True, env.now())
    store.set_status(st, tid, status, env.now())


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
        qid = store.push_queue(st, None, "start", f"派发单已备好（{n} 张）", n, env.now())
    return {"ok": True, "batch": batch, "tickets": created, "queue": qid, "unknown_depends": unknown,
            "notify": notify(env, f"router：派发单已备好（{n} 张，批次 {batch}）")}


def cmd_add(a, env):
    surf = _csv(a.surfaces)
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
        dispatched = dispatch(env, st)
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
        resumed, hint = None, ""
        t = st["tickets"].get(q["ticket"]) if q["ticket"] else None
        if t and q["kind"] in RESUME_KINDS and t["worker"].get("session_id"):
            if q["kind"] == "card":
                t["card_confirmed"] = True
                t["solution"]["source"] = t["solution"]["source"].replace("待你确认", "已确认")
            status = store.EXECUTING if t.get("card_confirmed") else store.KICKOFF
            _resume(env, st, t["id"], q["kind"], a.text, status)
            resumed = t["id"]
        elif q["kind"] == "start":
            hint = "派发单要用 start 子命令"
        elif q["kind"] == "handson":
            hint = "走查完用 walkthrough-done"
        elif q["kind"] == "prod":
            hint = "按 aifriends 发版流程上 prod，发完用 released"
    return {"ok": True, "queue": q["id"], "kind": q["kind"], "resumed": resumed, "hint": hint}


def cmd_submit_card(a, env):
    card = Path(a.card)
    if not card.exists():
        raise Fail(f"方向卡不存在：{card}")
    with env.state() as st:
        t = env.ticket(st, a.ticket)
        version = t.get("card", {}).get("version", 0) + 1
        t["card"] = {"path": str(card), "rounds": a.rounds, "codex": a.codex, "overtime": a.overtime,
                     "version": version, "submitted_at": env.now()}
        t["solution"] = {"kind": "card", "text": _card_direction(card.read_text(encoding="utf-8")),
                         "source": f"方向卡 v{version}（待你确认）"}
        t["worker"]["pid"] = None
        t["worker"]["kickoff_rounds"] = a.rounds
        store.set_status(st, a.ticket, store.AWAIT_CARD, env.now(), "等你确认方向卡")
        qid = store.push_queue(st, a.ticket, "card",
                               f"{a.ticket} 方向卡 v{version}{'（超时，见卡上未完成项）' if a.overtime else ''}：{t['title']}",
                               _blocks(st, a.ticket), env.now())
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
        events.deliver(env.home, a.ticket, {"ts": env.now(), "ticket": "router", "type": "tell", "summary": a.text})
        resumed = None
        if a.resume and not env.ctx.pid_alive(t["worker"].get("pid")):
            _resume(env, st, a.ticket, "direction", a.text,
                    t["status"] if t["status"] in store.RUNNING else store.KICKOFF)
            resumed = a.ticket
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
        if t["worker"].get("pid"):
            launcher.stop_now(t["worker"]["pid"])
        t["worker"]["pid"] = None
        store.set_status(st, a.ticket, store.PAUSED, env.now(), "你让它立刻停")
    return {"ok": True}


def cmd_takeover(a, env):
    with env.state() as st:
        t = env.ticket(st, a.ticket)
        w = t["worker"]
        if not w.get("session_id"):
            raise Fail(f"{a.ticket} 还没有 worker session")
        if w.get("pid"):
            launcher.stop_now(w["pid"])                 # 同一个 session 不能两边同时写
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
        if prev in store.RUNNING:
            _resume(env, st, a.ticket, "direction",
                    "用户接管结束，交还给 router。先看 git log / git status 了解用户做了什么，再按当前阶段继续。", prev)
        else:
            store.set_status(st, a.ticket, prev, env.now())
    return {"ok": True, "status": prev}


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


def cmd_walkthrough_done(a, env):
    with env.state() as st:
        t = env.ticket(st, a.ticket)
        if t["status"] != store.MERGED:
            raise Fail(f"{a.ticket} 还没合进 staging（{t['status']}）")
        store.set_status(st, a.ticket, store.AWAIT_PROD, env.now(), "等上 prod")
        st["queue"] = [q for q in st["queue"] if not (q["kind"] == "handson" and q["ticket"] == a.ticket)]
    return {"ok": True}


def _prod_item(env, st):
    st["queue"] = [q for q in st["queue"] if q["kind"] != "prod"]
    ready = sorted((tid for tid, t in st["tickets"].items() if t["status"] == store.AWAIT_PROD),
                   key=lambda x: int(x[1:]))
    if ready:
        return store.push_queue(st, None, "prod", f"上 prod：{' '.join(ready)}", len(ready), env.now()), ready
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
    queue = sorted(st["queue"], key=lambda q: (-q["blocks"], int(q["id"][1:])))
    return counts, used, queue


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
    ctx = env.ctx
    with env.state() as st:
        ext = discover.scan(ctx, env.root, f"origin/{env.cfg.get('base_branch', 'staging')}", env.table,
                            set(), env.cfg.get("stale_min", 45), env.cfg.get("external_lock_hours", 24))
        st["external"] = ext
        new = []
        seen = {q["summary"] for q in st["queue"] if q["kind"] == "conflict"}
        for wt, surf, tid in discover.conflicts_with_router(st, ext):
            summary = f"外部 session {Path(wt).name} 改了 {tid} 锁着的代码面 {surf}"
            if summary not in seen:
                new.append(store.push_queue(st, tid, "conflict", summary, _blocks(st, tid), env.now()))
    return {"ok": True, "external": ext, "new_queue": new}


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
                    ("handback", "还给 router"), ("walkthrough-done", "staging 走查过了")):
        sub.add_parser(name, help=h).add_argument("ticket")
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
