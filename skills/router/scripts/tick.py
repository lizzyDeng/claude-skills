"""一轮巡检。顺序写死（计划 Task 9），实现者不要改顺序：

0 新周报 → triage_needed      1 扫外部 session，查抢锁      2 处理退出的 worker
3 事件路由（快路径投递 / 慢路径交 router）                4 巡检：卡住 / 超时
5 验收中的票：CI 全绿 → 合 staging → 放锁 → 通知下游       6 派发      7 看板 + Lark

整轮持 <home>/.lock。每一步单独 try：一步出错不拖垮后面几步，错误进返回值。
"""
import os
import shutil
import string
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

import board
import discover
import events
import github
import launcher
import notify
import scheduler
import store
import surfaces

SKILL_DIR = Path(__file__).resolve().parent.parent
WAKE_KEYS = ("triage_needed", "new_queue", "slow_path_events", "external_deliveries", "cards_for_review")
STOP_KIND = {"decision": "direction", "assumption_broken": "direction", "overreach": "conflict"}


def home_for(root: Path) -> Path:
    return Path(os.environ.get("ROUTER_HOME") or Path(root) / ".claude" / "router")


def _iso(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp() if s else 0


def _blocks(state, tid):
    t = state["tickets"][tid]
    return sum(1 for o in state["tickets"].values() if o["id"] != tid and o["status"] not in store.SETTLED
               and (tid in o["blocked_by"] or (o["status"] == store.QUEUED and set(o["surfaces"]) & set(t["surfaces"]))))


class Run:
    def __init__(self, ctx, root, cfg, state):
        self.ctx, self.root, self.cfg, self.state = ctx, Path(root), cfg, state
        self.home = home_for(root)
        sp = self.home / "surfaces.json"
        self.table = surfaces.load_surfaces(sp) if sp.exists() else {}
        self.now = _iso(ctx.now())
        self.res = {"triage_needed": None, "dispatched": [], "new_queue": [], "slow_path_events": [],
                    "external_deliveries": [], "cards_for_review": [], "notified": False, "errors": []}

    def queue(self, tid, kind, summary):
        qid = store.push_queue(self.state, tid, kind, summary, _blocks(self.state, tid) if tid else 0, self.now)
        self.res["new_queue"].append(qid)
        return qid

    def resume(self, tid, prompt, status):
        launcher.run_worker(self.ctx, self.home, self.cfg, self.state, tid, prompt, True, self.now)
        store.set_status(self.state, tid, status, self.now)

    # ---- 0 ----
    def step0_weekly(self):
        runs = Path(os.path.expanduser(self.cfg.get("weekly_runs_dir", "")))
        if not runs.is_dir():
            return
        weeks = sorted(d.name for d in runs.iterdir() if (d / "sent").exists())
        if not weeks:
            return
        latest = weeks[-1]
        drafted = any(t["batch"] == latest for t in self.state["tickets"].values())
        if latest != self.state["batch"]["id"] and not drafted:
            self.res["triage_needed"] = latest

    # ---- 1 ----
    def step1_external(self):
        ext = discover.scan(self.ctx, self.root, f"origin/{self.cfg.get('base_branch', 'staging')}", self.table,
                            set(), self.cfg.get("stale_min", 45), self.cfg.get("external_lock_hours", 24))
        self.state["external"] = ext
        seen = {q["summary"] for q in self.state["queue"] if q["kind"] == "conflict"}
        for wt, surf, tid in discover.conflicts_with_router(self.state, ext):
            summary = f"外部 session {Path(wt).name} 改了 {tid} 锁着的代码面 {surf}"
            if summary not in seen:
                self.queue(tid, "conflict", summary)

    # ---- 2 ----
    def step2_exits(self):
        for tid, t in list(self.state["tickets"].items()):
            w = t["worker"]
            if t["status"] not in store.RUNNING or not w.get("pid") or self.ctx.pid_alive(w["pid"]):
                continue
            mine = [e for e in events.read_all(self.home, tid)
                    if e.get("ts", "") >= (w.get("started_at") or "") and (e.get("type") == "progress" or e.get("stop"))]
            result = launcher.read_result(w["log"]) if w.get("log") else None
            w["exit"] = {"at": self.now, "result": result is not None,
                         "is_error": bool(result and result.get("is_error"))}
            w["pid"] = None
            if t.pop("pause_requested", False):
                store.set_status(self.state, tid, store.PAUSED, self.now, "你让它先停，已停在检查点")
                continue
            last = mine[-1] if mine else None
            if last and last.get("stop"):
                t["stops"] += 1
                kind = "spend" if "spend" in last.get("affects", {}).get("tags", []) else STOP_KIND[last["type"]]
                prefix = {"assumption_broken": "核心假设被推翻：", "overreach": "要越界："}.get(last["type"], "")
                qid = self.queue(tid, kind, f"{tid}：{prefix}{last['summary']}")
                store.set_status(self.state, tid, store.PAUSED, self.now, f"等你回答 {qid}")
            elif last and last.get("phase") == "card_ready" and t["status"] == store.KICKOFF:
                t["card_ready_at"] = self.now
                t["waiting_for"] = "router 方向审中"
                self.res["cards_for_review"].append(tid)
            elif last and last.get("phase") == "pr_opened":
                store.set_status(self.state, tid, store.VERIFYING, self.now, "等 CI")
                t["verifying_at"] = self.now
            elif result and result.get("is_error"):
                fs = t["fail_streak"]
                if fs.get("step") == t["status"] and fs.get("count", 0) >= 1:
                    fs["count"] += 1
                    self._stuck(tid, f"{tid} 同一步连续失败 {fs['count']} 次：{str(result.get('result'))[:120]}")
                else:
                    t["fail_streak"] = {"step": t["status"], "count": 1}
                    self.resume(tid, "【router】你上一次运行报错退出了："
                                     f"{str(result.get('result'))[:300]}\n先读 inbox，确认做到哪，再从中断处继续。",
                                t["status"])
            elif result:
                self._stuck(tid, f"{tid} 退出了但没报进度（违反 worker.md）")
            else:
                self._stuck(tid, f"{tid} 没留下结果就退出了（被杀 / 睡眠 / OOM？）")

    def _stuck(self, tid, summary):
        qid = self.queue(tid, "stuck", summary)
        store.set_status(self.state, tid, store.STUCK, self.now, f"等你回答 {qid}")

    # ---- 3 ----
    def step3_events(self):
        edir = self.home / "events"
        if not edir.is_dir():
            return
        for f in sorted(edir.glob("*.jsonl")):
            tid = f.stem
            for ev in events.read_new(self.home, self.state, tid):
                if ev.get("type") not in events.TYPES or ev["type"] in ("decision", "overreach"):
                    continue                          # progress / 坏行不路由；decision / overreach 走第 2 步的决定单
                hits, exts, fast = events.route(self.state, ev)
                for h in hits:
                    events.deliver(self.home, h, ev)
                for x in exts:
                    self._external_inbox(x, ev)
                    self.res["external_deliveries"].append({"worktree": x, "event": ev})
                if ev["type"] in ("interface_changed", "assumption_broken"):
                    events.mark_review(self.state, hits)
                if not fast:
                    self.res["slow_path_events"].append(ev)

    def _external_inbox(self, wt, ev):
        p = Path(wt) / ".claude" / "router-inbox.md"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(f"- {ev.get('ts')} [{ev.get('type')}] 来自 router 票 {ev.get('ticket')}："
                    f"{ev.get('summary')}（证据：{ev.get('evidence')}）\n")

    # ---- 4 ----
    def _activity(self, tid, t):
        w = t["worker"]
        stamps = [_ts(w.get("started_at"))]
        for p in [w.get("log"), self.home / "events" / f"{tid}.jsonl"]:
            if p and Path(p).exists():
                stamps.append(Path(p).stat().st_mtime)
        if w.get("session_id"):                       # --output-format json 进程结束才写日志；transcript 每条消息都更新
            pd = Path(os.path.expanduser(self.cfg.get("claude_projects_dir", "~/.claude/projects")))
            stamps += [p.stat().st_mtime for p in pd.glob(f"*/{w['session_id']}.jsonl")]
        return max(stamps)

    def step4_patrol(self):
        now = self.ctx.now().timestamp()
        stale = self.cfg.get("stale_min", 45) * 60
        budget = self.cfg.get("kickoff_budget_min", 30) * 60
        for tid, t in self.state["tickets"].items():
            w = t["worker"]
            if t["status"] not in store.RUNNING or not w.get("pid"):
                continue
            if now - self._activity(tid, t) > stale and t.get("stale_reported_run") != w.get("runs"):
                t["stale_reported_run"] = w.get("runs")
                self.queue(tid, "stuck", f"{tid} 疑似卡住：{stale // 60} 分钟没有动静（进程还在）")
            if (t["status"] == store.KICKOFF and not t.get("card_ready_at") and t.get("kickoff_at")
                    and now - _ts(t["kickoff_at"]) > budget and t.get("overtime_flagged") != t["kickoff_at"]):
                t["overtime_flagged"] = t["kickoff_at"]
                t["waiting_for"] = f"方向卡超时（> {budget // 60} 分钟），router 照常交卡并注明缺什么"
                self.res["cards_for_review"].append(tid)

    # ---- 5 ----
    def step5_merge(self):
        repo, base = self.cfg["repo"], self.cfg.get("base_branch", "staging")
        uids = github.load_guard_uids(self.cfg.get("leak_guard_globs", []))
        for tid, t in list(self.state["tickets"].items()):
            if t["status"] != store.VERIFYING:
                continue
            pr = t.get("pr") or github.pr_for_branch(self.ctx, repo, t["worker"].get("branch") or f"router/{tid}")
            if not pr:
                continue
            t["pr"] = pr
            st = github.pr_status(self.ctx, repo, pr)
            if st["checks"] == "failure" and not st["merged"]:
                fs = t["fail_streak"]
                if fs.get("step") == "ci" and fs.get("count", 0) >= 1:
                    fs["count"] += 1
                    self._stuck(tid, f"{tid} PR #{pr} CI 连续红 {fs['count']} 次")
                else:
                    t["fail_streak"] = {"step": "ci", "count": 1}
                    self.resume(tid, f"【router】PR #{pr} 的 CI 红了。先 `gh pr checks {pr}` 看是哪项；"
                                     "「Platform release contract」偶发失败可以先 `gh run rerun --failed`。修好推上去，"
                                     "再 `progress pr_opened` 后退出。", store.EXECUTING)
                continue
            if st["checks"] == "none" and not st["merged"]:
                since = _ts(t.get("verifying_at") or self.now)
                if self.ctx.now().timestamp() - since > 30 * 60 and t.get("no_ci_reported") != pr:
                    t["no_ci_reported"] = pr
                    self.queue(tid, "stuck", f"{tid} PR #{pr} 30 分钟了还没有任何 CI 检查")
                continue
            if not github.merge_if_green(self.ctx, repo, pr):
                continue
            sha = github.pr_status(self.ctx, repo, pr)["merge_sha"] or "?"
            t["merge_sha"] = sha
            t["fail_streak"] = {"step": "", "count": 0}
            store.set_status(self.state, tid, store.MERGED, self.now, "等你 staging 走查")
            self._notify_downstream(tid, t, sha, base, uids)
            if t["worker"].get("worktree"):
                try:
                    launcher.remove_worktree(self.ctx, self.root, t["worker"]["worktree"])
                except RuntimeError as e:
                    self.res["errors"].append(f"step5 删 worktree：{e}")
            self.queue(tid, "handson", f"staging 走查 {tid}：{t['title']}")

    def _notify_downstream(self, tid, t, sha, base, uids):
        for o in self.state["tickets"].values():
            if o["id"] == tid or not o.get("issue") or o["status"] in store.SETTLED | {store.SKIP}:
                continue
            dep = tid in o["blocked_by"]
            shared = set(o["surfaces"]) & set(t["surfaces"])
            if not (dep or shared):
                continue
            why = "依赖已解锁" if dep else f"共享代码面 {'、'.join(sorted(shared))} 已释放"
            body = (f"上游 {tid}{' #' + str(t['issue']) if t.get('issue') else ''}「{t['title']}」已合进 {base}，SHA `{sha[:12]}`。\n"
                    f"- 合了什么：{(t.get('solution') or {}).get('text', '')[:300]}\n"
                    f"- 对本票的影响：{why}；开工 / 继续前先基于最新 {base}。\n\n由 /router 自动留言。")
            try:
                github.comment(self.ctx, self.cfg["repo"], o["issue"], body, uids)
            except (github.LeakBlocked, github.GhError) as e:
                self.res["errors"].append(f"step5 下游留言 {o['id']}：{e}")

    # ---- 6 ----
    def disk_block(self):
        """派重票前的磁盘闸 → 拦住的原因（空串 = 不拦）。
        两种口径：配置了共享 CARGO_TARGET_DIR 就看它的体积；再看项目所在卷的剩余空间
        （aifriends 每个 worktree 自带 target/，一张重票 8–32G，合并删 worktree 时一起清掉）。"""
        reasons = []
        d = self.cfg.get("cargo_target_dir") or self.ctx.env("CARGO_TARGET_DIR")
        if d:
            cache = self.state.get("disk") or {}
            if cache.get("dir") == d and self.ctx.now().timestamp() - _ts(cache.get("at")) < 30 * 60:
                gb = cache["gb"]
            else:
                out = self.ctx.run(["du", "-sk", d]).stdout.split()
                gb = int(out[0]) / 1024 / 1024 if out and out[0].isdigit() else None
                self.state["disk"] = {"dir": d, "gb": gb, "at": self.now}
            if gb is not None and gb > self.cfg.get("disk_limit_gb", 150):
                reasons.append(f"CARGO_TARGET_DIR {gb:.0f}G 超过 {self.cfg.get('disk_limit_gb', 150)}G")
        need = self.cfg.get("disk_min_free_gb")
        if need:
            free = shutil.disk_usage(self.root if self.root.exists() else self.home).free / 1024 ** 3
            if free < need:
                reasons.append(f"磁盘剩余 {free:.0f}G 低于 {need}G")
        return "；".join(reasons)

    def step6_dispatch(self):
        self.res["dispatched"] = dispatch(self.ctx, self.root, self.cfg, self.state, run=self)

    # ---- 7 ----
    def step7_board_and_notify(self):
        board.write(self.home / "board.html",
                    board.render(self.state, store.recent_decisions(self.home / "decisions.md"),
                                 {tid: events.read_all(self.home, tid)[-5:] for tid in self.state["tickets"]},
                                 self.ctx.now(), self.cfg.get("slots")))
        if self.res["new_queue"]:
            q = sorted(self.state["queue"], key=lambda x: (-x["blocks"], int(x["id"][1:])))
            text = f"router：等你 {len(q)} 件（{' '.join(x['id'] for x in q)}）"
            out = notify.send(Path(os.path.expanduser(self.cfg.get("lark_config", ""))), text, self.ctx)
            self.res["notified"] = out == "sent"
            self.res["notify"] = out


def _brief(run, tid):
    t = run.state["tickets"][tid]
    why = t.get("why", {})
    tpl = string.Template((SKILL_DIR / "templates" / "brief.md").read_text(encoding="utf-8"))
    return tpl.safe_substitute(
        ticket=tid, title=t["title"],
        issue_line=f"GitHub：{run.cfg['repo']}#{t['issue']}" if t.get("issue") else "（没有 issue）",
        worker_md=str(SKILL_DIR / "worker.md"),
        why_metric=why.get("metric", ""), why_users=why.get("users", ""), why_evidence=why.get("evidence", ""),
        problem=t.get("problem", ""), anchor=t.get("problem_anchor", "") or "（无）",
        clue=(t.get("solution") or {}).get("text", "") or "（无）",
        surfaces="、".join(t["surfaces"]) or "（未声明）", tags="、".join(t.get("tags", [])) or "（无）",
        blocked_by="、".join(t["blocked_by"]) or "无", base_branch=run.cfg.get("base_branch", "staging"),
        router_home=str(run.home), router_cli=str(Path(__file__).resolve().with_name("router.py")),
        card_path=str(run.home / "cards" / f"{tid}.md"), kickoff_budget=run.cfg.get("kickoff_budget_min", 30))


def _explain_waits(run, picked, disk):
    st = run.state
    locks = surfaces.held_locks(st)
    for tid in picked:
        for s in st["tickets"][tid]["surfaces"]:
            locks.setdefault(s, tid)
    for tid, t in st["tickets"].items():
        if t["status"] != store.QUEUED or not t["start_ok"] or tid in picked:
            continue
        unsettled = [b for b in t["blocked_by"] if st["tickets"].get(b, {}).get("status") not in store.SETTLED]
        held = sorted((s, h) for s, h in locks.items() if s in t["surfaces"] and h != tid)
        if unsettled:
            t["waiting_for"] = f"等 {'、'.join(unsettled)} 合进 {run.cfg.get('base_branch', 'staging')}"
        elif held:
            s, h = held[0]
            t["waiting_for"] = f"等代码面 {s}（{('外部 session ' + Path(h[4:]).name) if h.startswith('ext:') else h} 持有）"
        elif t["weight"] == "heavy" and disk:
            t["waiting_for"] = f"磁盘：{disk}，先清再派重票"
        else:
            t["waiting_for"] = "等重名额" if t["weight"] == "heavy" else "等轻名额"


def dispatch(ctx, root, cfg, state, run=None) -> list:
    """选票 → 开 worktree → 拼派单 → 起 worker → KICKOFF。start 子命令（已持锁）和第 6 步共用。"""
    run = run or Run(ctx, root, cfg, state)
    heavy_cand = any(t["status"] == store.QUEUED and t["start_ok"] and t["weight"] == "heavy"
                     for t in state["tickets"].values())
    disk = run.disk_block() if heavy_cand else ""
    heavy_blocked = bool(disk)
    picked, done = scheduler.pick_dispatch(state, cfg.get("slots", {"heavy": 3, "light": 2}), heavy_blocked), []
    for tid in picked:
        t = state["tickets"][tid]
        try:
            wt, branch = launcher.create_worktree(ctx, Path(root), cfg.get("base_branch", "staging"),
                                                  cfg.get("worktree_dir", ".claude/worktrees"), tid)
        except RuntimeError as e:
            run.res["errors"].append(f"dispatch {tid}：{e}")
            continue
        t["worker"].update(worktree=str(wt), branch=branch)
        launcher.run_worker(ctx, run.home, cfg, state, tid, _brief(run, tid), False, run.now)
        t["kickoff_at"] = run.now
        store.set_status(state, tid, store.KICKOFF, run.now, f"写方向卡（{cfg.get('kickoff_budget_min', 30)} 分钟内）")
        done.append(tid)
    _explain_waits(run, done, disk)
    return done


def run(ctx, root, cfg) -> dict:
    home = home_for(root)
    with store.locked(home):
        state = store.load_state(home / "state.json")
        r = Run(ctx, root, cfg, state)
        for step in (r.step0_weekly, r.step1_external, r.step2_exits, r.step3_events, r.step4_patrol,
                     r.step5_merge, r.step6_dispatch, r.step7_board_and_notify):
            try:
                step()
            except Exception as e:  # 一步出错不拖垮整轮；错误交给 router
                r.res["errors"].append(f"{step.__name__}：{e!r}\n{traceback.format_exc(limit=3)}")
        store.save_state(home / "state.json", state)
    return r.res


# ---- 子命令（router.py 动态挂载）---------------------------------------------

def add_subcommands(sub):
    sub.add_parser("tick", help="跑一轮巡检")
    s = sub.add_parser("watch", help="循环巡检，有事才退出（router 用 Bash 后台跑）")
    s.add_argument("--interval", type=int, default=60)
    s.add_argument("--max-minutes", type=int, default=480, help="router session 没了也别永远跑下去")


def cmd_tick(a, env):
    return {"ok": True, **run(env.ctx, env.root, env.cfg)}


def cmd_watch(a, env):
    started, err_streak = time.time(), 0
    while True:
        res = run(env.ctx, env.root, env.cfg)
        err_streak = err_streak + 1 if res["errors"] else 0
        if any(res.get(k) for k in WAKE_KEYS) or err_streak >= 3:
            return {"ok": True, "woke": [k for k in WAKE_KEYS if res.get(k)] + (["errors"] if err_streak >= 3 else []), **res}
        if time.time() - started > a.max_minutes * 60:
            return {"ok": True, "woke": ["max_minutes"], **res}
        time.sleep(a.interval)
