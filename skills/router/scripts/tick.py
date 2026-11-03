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
# router 自己做、只记一条 decide 的两件事（重建 base、改回被自动改指的 PR）共用的授权说明
AUTO_FIX_WHY = "用户 11-02 长期授权：base 被删时从 main 重建、改回被 GitHub 自动改指的 router PR、记一条 decide"


def home_for(root: Path) -> Path:
    return Path(os.environ.get("ROUTER_HOME") or Path(root) / ".claude" / "router")


def _iso(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _ts(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp() if s else 0


class Run:
    def __init__(self, ctx, root, cfg, state):
        self.ctx, self.root, self.cfg, self.state = ctx, Path(root), cfg, state
        self.home = home_for(root)
        sp = self.home / "surfaces.json"
        self.table = surfaces.load_surfaces(sp) if sp.exists() else {}
        self.now = _iso(ctx.now())
        self.res = {"triage_needed": None, "dispatched": [], "dispatch_failed": [], "new_queue": [],
                    "slow_path_events": [], "external_deliveries": [], "cards_for_review": [], "notified": False,
                    "errors": []}
        self._base = None              # ensure_base 的结果：每个 Run 只查一次
        self.fail_errors = []          # _fail 写进 errors 的那几条（start 已按结构化 dispatch_failed 报，不再重复）

    def queue(self, tid, kind, summary, topic=None):
        """summary 里的「{qid}」换成这一项自己的编号（文案要写「回『Qn 好了』」）。"""
        qid = store.push_queue(self.state, tid, kind, summary, self.now, topic=topic)
        if "{qid}" in summary:
            self.state["queue"][-1]["summary"] = summary.replace("{qid}", qid)
        self.res["new_queue"].append(qid)
        return qid

    def resume(self, tid, prompt, status):
        launcher.run_worker(self.ctx, self.home, self.cfg, self.state, tid, prompt, True, self.now)
        store.set_status(self.state, tid, status, self.now)

    # ---- base（R1）：远端 base 在不在、是不是从我们认得的 tip 来的 ------------------
    def ensure_base(self) -> dict:
        """→ {"ok", "transient", "error", "wait"}。每个 Run 只查一次（结果缓存）。
        ok=False ⇒ 这一轮一张都不派。transient（查不到远端 / 核实不了）⇒ 不重建、不进决定单，只记一条 errors。
        状态：base_seen（见过的远端 tip）、base_incident（重建进行中，带幂等 key）、
        base_problem（进了决定单的 base 问题；expected_sha = 重建时 main 的 sha，远端恢复后必须是它或它的后代）。"""
        if self._base is None:
            try:
                self._base = self._check_base()
            except Exception as e:  # noqa: BLE001 — 一律当「查不了」：不重建、不派
                self._base = self._base_res(False, True, f"base 检查异常：{e!r}",
                                            f"查不到 origin/{self._base_names()[0]}（下一轮再查）")
            if not self._base["ok"] and self._base["transient"]:
                self.res["errors"].append(f"base 检查：{self._base['error']}")
        return self._base

    def _base_names(self):
        return self.cfg.get("base_branch", "staging"), self.cfg.get("main_branch", "main")

    @staticmethod
    def _base_res(ok, transient=False, error="", wait=""):
        return {"ok": ok, "transient": transient, "error": error, "wait": wait}

    def _check_base(self):
        base, main = self._base_names()
        kind, val = launcher.remote_branch(self.ctx, self.root, base)
        if kind == "unknown":                   # 弱网 / 超时：永远不触发重建
            return self._base_res(False, True, f"查不到 origin/{base}：{val}", f"查不到 origin/{base}（下一轮再查）")
        if kind == "present":
            return self._base_present(base, main, val)
        return self._base_missing(base, main)

    def _base_present(self, base, main, sha):
        st = self.state
        seen = st.get("base_seen") or {}
        if seen.get("branch") != base or seen.get("sha") != sha:
            st["base_seen"] = {"branch": base, "sha": sha, "at": self.now}
        inc, bp = st.get("base_incident"), st.get("base_problem")
        expected = (inc or {}).get("main_sha") or (bp or {}).get("expected_sha")
        good = True
        if expected and sha != expected:        # H1：远端恢复了也要验——必须是重建时的 main sha，或它的后代
            good = launcher.is_ancestor(self.ctx, self.root, base, expected, sha)
            if good is None:
                return self._base_res(False, True, f"核实不了 origin/{base} {sha[:12]} 是否基于 {expected[:12]}",
                                      f"核实 origin/{base} 中（下一轮再查）")
        if inc:                                  # 上一轮推了、没来得及记账就崩：按 key 补记（记过就跳过）
            st.pop("base_incident")
            if not good:
                return self._foreign_tip(base, main, sha, inc["main_sha"])
            self._log_rebuild(inc, base, main, sha, late=True)
        if bp:
            if bp.get("kind") == "foreign_tip" and store.in_queue(st, bp.get("qid")):
                # 认不出的 tip 不自愈，只认你的回答（回答后下一轮照样重验）
                return self._base_res(False, error=f"远端 {base} 指向认不出的 tip（{bp['qid']}）",
                                      wait=f"远端 {base} 是认不出的 tip，等你回答 {bp['qid']}")
            if not good:
                return self._foreign_tip(base, main, sha, expected)
            self._withdraw_base_problem()        # 自愈：撤掉它的决定单项
        return self._base_res(True)

    def _base_missing(self, base, main):
        st = self.state
        bp = st.get("base_problem")
        if bp and store.in_queue(st, bp.get("qid")):     # 已经在等你回答：退避，不重推
            return self._base_res(False, error=f"远端 {base} 有问题（{bp['qid']}）",
                                  wait=f"远端 {base} 有问题，等你回答 {bp['qid']}")
        tracked = launcher.tracking_sha(self.ctx, self.root, base)
        if not ((st.get("base_seen") or {}).get("branch") == base or tracked):
            return self._base_problem("never_seen", f"远端没有 {base}，router 也从没见过它：检查 config 的 base_branch；"
                                                    f"确认无误后回「{{qid}} 好了」")
        kind, val = launcher.remote_branch(self.ctx, self.root, main)
        if kind == "unknown":
            return self._base_res(False, True, f"远端没有 {base}，查不到 origin/{main}：{val}",
                                  f"查不到 origin/{main}（下一轮再查）")
        if kind == "missing":
            return self._base_problem("main_missing", f"远端没有 {base}，origin 也没有 {main}：不重建；"
                                                      f"修好远端后回「{{qid}} 好了」")
        m = val
        inc = {"key": f"base:{base}:{self.now}:{m[:12]}", "main_sha": m,
               "old": tracked or (st.get("base_seen") or {}).get("sha"), "at": self.now}
        st["base_incident"] = inc
        try:                                     # 先记意图再推：推了、没来得及记账就崩，下一轮按 key 对账
            store.save_state(self.home / "state.json", st)
        except Exception as e:  # noqa: BLE001
            st.pop("base_incident", None)
            return self._base_res(False, True, f"重建 {base} 前存盘失败：{e!r}", f"远端 {base} 待重建（下一轮再试）")
        r = launcher.rebuild_branch(self.ctx, self.root, base, main, m)
        if r["ok"]:
            self._log_rebuild(inc, base, main, r["sha"], late=False, by_other=r.get("by_other"))
            st.pop("base_incident", None)
            st["base_seen"] = {"branch": base, "sha": r["sha"], "at": self.now}
            if r.get("warn"):
                self.res["errors"].append(f"base 重建：{r['warn']}")
            self._withdraw_base_problem()
            return self._base_res(True)
        st.pop("base_incident", None)
        if r.get("reason") == "foreign_tip":
            return self._foreign_tip(base, main, r["sha"], m)
        return self._base_problem("rebuild_failed",
                                  f"远端 {base} 不存在，从 origin/{main} {m[:12]} 重建失败：{str(r.get('error', ''))[:120]}；"
                                  f"修好后回「{{qid}} 好了」", expected=m)

    def _withdraw_base_problem(self):
        bp = self.state.pop("base_problem", None)
        if bp and store.in_queue(self.state, bp.get("qid")):
            store.pop_queue(self.state, bp["qid"])

    def _base_problem(self, kind, summary, expected=None):
        base = self._base_names()[0]
        self._withdraw_base_problem()
        qid = self.queue(None, "stuck", summary, topic="base")
        self.state["base_problem"] = {"qid": qid, "kind": kind, "expected_sha": expected, "at": self.now}
        return self._base_res(False, error=self.state["queue"][-1]["summary"],
                              wait=f"远端 {base} 有问题，等你回答 {qid}")

    def _foreign_tip(self, base, main, sha, expected):
        self._withdraw_base_problem()
        qid = self.queue(None, "stuck", f"远端 {base} 指向 {sha[:12]}，不是本次重建的 origin/{main} {expected[:12]}："
                                        f"不从未知 tip 派工；把远端 {base} 改成 {expected[:12]} 或它的后代后回「{{qid}} 好了」",
                         topic="base")
        self.state["base_problem"] = {"qid": qid, "kind": "foreign_tip", "expected_sha": expected, "sha": sha,
                                      "at": self.now}
        return self._base_res(False, error=f"远端 {base} 指向认不出的 tip {sha[:12]}",
                              wait=f"远端 {base} 是认不出的 tip，等你回答 {qid}")

    def _log_rebuild(self, inc, base, main, sha, late, by_other=False):
        m = inc["main_sha"]
        what = (f"远端 {base} 不存在：已由别处从 origin/{main} {m[:12]} 重建（router 的推送被 lease 拒）" if by_other
                else f"远端 {base} 不存在：从 origin/{main} {m[:12]} 重建")
        what += "（补记）" if late else ""
        what += f"；之后已前进到 {sha[:12]}" if sha and sha != m else ""
        basis = f"ls-remote 无 refs/heads/{base}；上次见到 {inc.get('old') or '未知'}；--no-verify：推远端已有 commit"
        store.append_decision(self.home / "decisions.md", self.state, "-", what, AUTO_FIX_WHY, basis, self.now,
                              key=inc["key"])

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
                            set(), self.cfg.get("stale_min", 45), self.cfg.get("external_lock_hours", 24),
                            self.cfg.get("repo"))
        self.state["external"] = ext
        # 同一个冲突只报一次：答完之后外部 session 仍持有那块代码，也不再每分钟叫用户（反目标 5）
        current = {f"{wt}|{surf}|{tid}": (wt, surf, tid) for wt, surf, tid in discover.conflicts_with_router(self.state, ext)}
        reported = set(self.state.get("conflicts_reported", []))
        for key in sorted(set(current) - reported):
            wt, surf, tid = current[key]
            self.queue(tid, "conflict", f"外部 session {Path(wt).name} 改了 {tid} 锁着的代码面 {surf}")
        self.state["conflicts_reported"] = sorted(current)    # 冲突消失后再出现，会再报

    # ---- 2 ----
    def step2_exits(self):
        for tid, t in list(self.state["tickets"].items()):
            w = t["worker"]
            if t["status"] not in store.RUNNING or not w.get("pid") or launcher.worker_alive(self.ctx, w):
                continue
            try:
                self._handle_exit(tid, t)
            except Exception as e:  # 一张票出错不挡其它票
                self.res["errors"].append(f"step2 {tid}：{e!r}")

    def _offrepo(self, t) -> bool:
        """票的代码面全在仓外（~/… 或仓外绝对路径）：没有 PR 可开，走「备份 → 改 → diff → 你核对」。"""
        globs = [g for s in t["surfaces"] for g in self.table.get(s, {}).get("globs", [])]
        root = str(self.root)
        return bool(globs) and all(g.startswith("~") or (g.startswith("/") and not g.startswith(root)) for g in globs)

    def _handle_exit(self, tid, t):
        w = t["worker"]
        mine = [e for e in events.read_all(self.home, tid)
                if e.get("ts", "") >= (w.get("started_at") or "") and (e.get("type") == "progress" or e.get("stop"))]
        result = launcher.read_result(w["log"]) if w.get("log") else None
        w["exit"] = {"at": self.now, "result": result is not None, "is_error": bool(result and result.get("is_error"))}
        w["pid"] = None
        pause = t.pop("pause_requested", False)
        last = mine[-1] if mine else None
        phase = last.get("phase") if last else None
        if last and last.get("stop"):
            t["stops"] += 1
            kind = "spend" if "spend" in last.get("affects", {}).get("tags", []) else STOP_KIND[last["type"]]
            prefix = {"assumption_broken": "核心假设被推翻：", "overreach": "要越界："}.get(last["type"], "")
            qid = self.queue(tid, kind, f"{tid}：{prefix}{last['summary']}")
            store.set_status(self.state, tid, store.PAUSED, self.now, f"等你回答 {qid}")
        elif phase == "card_ready" and t["status"] == store.KICKOFF:
            t["card_ready_at"] = self.now
            t["waiting_for"] = "router 方向审中"
            self.res["cards_for_review"].append(tid)
        elif phase == "pr_opened" and not t.get("card_confirmed"):
            # 🔴 方向卡没经用户确认就开了 PR：不进验收，更不会被合（方案确认归用户）
            qid = self.queue(tid, "direction", f"{tid}：方向卡还没确认就开了 PR，不会合；先确认或驳回方向卡")
            store.set_status(self.state, tid, store.PAUSED, self.now, f"等你回答 {qid}")
        elif phase == "pr_opened":
            store.set_status(self.state, tid, store.VERIFYING, self.now, "等 CI")
            t["verifying_at"] = self.now
        elif phase == "impl_done" and t.get("card_confirmed") and self._offrepo(t):
            self._settle(tid, t, sha=None)
        elif pause:
            store.set_status(self.state, tid, store.PAUSED, self.now, "你让它先停，已停在检查点")
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
                    self.state.setdefault("slow_path", []).append(ev)   # 存盘，router 处理完 ack-slow 才删
        self.state["slow_path"] = self.state.get("slow_path", [])[-50:]

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
        try:
            guard = github.load_guard(self.cfg.get("leak_guard_globs", []))
        except github.GuardEmpty as e:          # 闸失效就一张都不合（fail-closed）
            self.res["errors"].append(f"step5 防泄漏闸：{e}")
            return
        for tid, t in list(self.state["tickets"].items()):
            if t["status"] != store.VERIFYING:
                continue
            try:
                self._verify(tid, t, repo, base, guard)
            except Exception as e:  # 一个 PR 出错（被删、gh 抖动）不挡其它票
                self.res["errors"].append(f"step5 {tid}：{e!r}")

    def _once(self, t, key, value, tid, kind, summary):
        if t.get(key) != value:
            t[key] = value
            self.queue(tid, kind, summary)

    def _retry_or_stuck(self, tid, t, step, summary, prompt):
        fs = t["fail_streak"]
        if fs.get("step") == step and fs.get("count", 0) >= 1:
            fs["count"] += 1
            self._stuck(tid, f"{summary}（连续 {fs['count']} 次）")
        else:
            t["fail_streak"] = {"step": step, "count": 1}
            self.resume(tid, prompt, store.EXECUTING)

    def _verify(self, tid, t, repo, base, guard):
        if launcher.worker_alive(self.ctx, t["worker"]):
            return                                # worker 还在跑（修 CI / rebase / 用户刚还回来）：等它退出
        if not t.get("card_confirmed"):
            self._once(t, "unconfirmed_reported", t.get("pr") or True, tid, "direction",
                       f"{tid}：方向卡没经你确认，PR 不合")
            return
        pr = t.get("pr") or github.pr_for_branch(self.ctx, repo, t["worker"].get("branch") or f"router/{tid}")
        if not pr:
            if self.ctx.now().timestamp() - _ts(t.get("verifying_at") or self.now) > 30 * 60:
                self._once(t, "no_pr_reported", t.get("verifying_at"), tid, "stuck", f"{tid} 报了 pr_opened，但找不到它的 PR")
            return
        t["pr"] = pr
        st = github.pr_status(self.ctx, repo, pr)
        if st["base"] != base:
            return self._wrong_base(tid, t, repo, base, pr, st)
        if st["merged"]:
            return self._settle(tid, t, st["merge_sha"] or "?")
        if st["state"] == "CLOSED":
            t["pr"] = None
            return self._stuck(tid, f"{tid} PR #{pr} 被关闭了（没合）")
        if st["checks"] == "failure":
            return self._retry_or_stuck(tid, t, "ci", f"{tid} PR #{pr} CI 红",
                                        f"【router】PR #{pr} 的 CI 红了。先 `gh pr checks {pr}` 看是哪项；"
                                        "「Platform release contract」偶发失败可以先 `gh run rerun --failed`。修好推上去，"
                                        "再 `progress pr_opened` 后退出。")
        if st["mergeable"] == "CONFLICTING":
            return self._retry_or_stuck(tid, t, "conflict", f"{tid} PR #{pr} 和 {base} 有冲突",
                                        f"【router】PR #{pr} 和最新 {base} 冲突。rebase 到 origin/{base}、解决冲突、"
                                        "重跑验收，推上去（--force-with-lease 只推你自己的 router 分支），再 `progress pr_opened` 后退出。")
        if st["checks"] == "none":
            if self.ctx.now().timestamp() - _ts(t.get("verifying_at") or self.now) > 30 * 60:
                self._once(t, "no_ci_reported", pr, tid, "stuck", f"{tid} PR #{pr} 30 分钟了还没有任何 CI 检查")
            return
        if st["checks"] != "success":
            return
        hits = github.leak_hits(github.pr_text(self.ctx, repo, pr), guard)
        if hits:
            return self._once(t, "leak_reported", st["head"], tid, "stuck",
                              f"{tid} PR #{pr} 含用户 uid 或原话（{len(hits)} 处），不合；要改掉并重写这条分支的历史")
        if not github.merge_if_green(self.ctx, repo, pr, base):
            fs = t["fail_streak"]
            t["fail_streak"] = {"step": "merge", "count": fs.get("count", 0) + 1 if fs.get("step") == "merge" else 1}
            if t["fail_streak"]["count"] >= 2:
                self._stuck(tid, f"{tid} PR #{pr} 合并失败 {t['fail_streak']['count']} 次")
            return
        self._settle(tid, t, github.pr_status(self.ctx, repo, pr)["merge_sha"] or "?")

    def _wrong_base(self, tid, t, repo, base, pr, st):
        """🔴 合进 main = 上 prod，归用户。base 不对一律不合（合并闸 merge_if_green 再兜一道）。
        唯一例外：PR 时间线最近一次改 base 是 GitHub 因 base 被删而自动改走的 ⇒ base 好了之后改回来，记一条 decide。
        人手改过的不碰；时间线查不了 ⇒ 走原来的决定单。改回失败：第 1 次进 errors，第 2 次进决定单（topic=retarget），之后退避。"""
        rf = t.get("retarget_fail") or {}
        if store.in_queue(self.state, rf.get("qid")):
            return                                         # 已在决定单等你回答：不重试
        ev = github.last_base_change(self.ctx, repo, pr)
        if github.is_auto_retarget(ev, base):
            if not self.ensure_base()["ok"]:
                return                                     # base 自己还没好（已进决定单 / errors）：先不改回
            try:
                github.retarget_pr(self.ctx, repo, pr, base)
            except github.GhError as e:
                rf.update(count=rf.get("count", 0) + 1, error=str(e)[:200], at=self.now)
                t["retarget_fail"] = rf
                if rf["count"] == 1:
                    self.res["errors"].append(f"step5 {tid} PR #{pr} 改回 {base} 失败（第 1 次，下一轮重试）：{e}")
                else:
                    rf["qid"] = self.queue(tid, "stuck", f"{tid} PR #{pr} 改回 {base} 失败 {rf['count']} 次：{str(e)[:120]}"
                                                         f"（可手工 gh pr edit {pr} --base {base}）；改好或要重试回「{{qid}} 改好了」",
                                           topic="retarget")
                return
            store.append_decision(self.home / "decisions.md", self.state, tid,
                                  f"PR #{pr} 被 GitHub 自动改指到 {st['base']}（{base} 曾被删）→ 改回 {base}", AUTO_FIX_WHY,
                                  f"PR 时间线 AutomaticBaseChangeSucceededEvent {ev.get('oldBase')}→{ev.get('newBase')}"
                                  f" @ {ev.get('createdAt')}", self.now)
            t.pop("retarget_fail", None)
            t.pop("wrong_base_reported", None)
            return                                         # 下一轮重验 CI
        self._once(t, "wrong_base_reported", pr, tid, "stuck",
                   f"{tid} PR #{pr} 的 base 是 {st['base']}，不是 {base}：不合（gh pr edit {pr} --base {base}）")

    def _settle(self, tid, t, sha):
        """一张票完成：仓内 = 合进 base；仓外 = 改完待你核对。放锁、通知下游、删 worktree、进「待你动手」。"""
        base = self.cfg.get("base_branch", "staging")
        t["merge_sha"] = sha
        t["fail_streak"] = {"step": "", "count": 0}
        offrepo = sha is None
        store.set_status(self.state, tid, store.MERGED, self.now, "等你核对仓外改动" if offrepo else f"等你 {base} 走查")
        try:
            guard = github.load_guard(self.cfg.get("leak_guard_globs", []))
        except github.GuardEmpty as e:
            guard = None
            self.res["errors"].append(f"下游留言跳过（防泄漏闸失效）：{e}")
        if guard is not None:
            self._notify_downstream(tid, t, sha or "仓外改动，无 commit", base, guard)
        if t["worker"].get("worktree"):
            try:
                launcher.remove_worktree(self.ctx, self.root, t["worker"]["worktree"])
            except RuntimeError as e:
                self.res["errors"].append(f"删 worktree {tid}：{e}")
        if offrepo:
            self.queue(tid, "handson", f"核对 {tid} 的仓外改动：diff {self.home}/cards/{tid}.diff · "
                                       f"备份 {self.home}/backups/{tid}/（不满意说「{tid} 回滚」）")
        else:
            self.queue(tid, "handson", f"{base} 走查 {tid}：{t['title']}")

    def _notify_downstream(self, tid, t, sha, base, uids):
        for o in self.state["tickets"].values():
            if o["id"] == tid or not o.get("issue") or o["status"] in store.SETTLED | {store.SKIP}:
                continue
            dep = tid in o["blocked_by"]
            shared = set(o["surfaces"]) & set(t["surfaces"])
            if not (dep or shared):
                continue
            why = "依赖已解锁" if dep else f"共享代码面 {'、'.join(sorted(shared))} 已释放"
            body = (f"上游 {tid}{' #' + str(t['issue']) if t.get('issue') else ''}「{t['title']}」已完成（合进 {base}），SHA `{sha[:12]}`。\n"
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

    # ---- 派单事务（R2）：worktree → brief → 意图落盘 → 起进程 → 落盘 ------------------
    def _dispatch_one(self, tid) -> str:
        """→ "ok" | "failed" | "stop"（意图没落盘，本轮停派）| "ok_stop"（worker 已起但落盘失败，本轮停派）。
        不变量：一张票同一时刻最多一个活 worker。起进程之前先把意图（含启动回执路径）落盘；
        起了之后、落盘之前崩溃 ⇒ 下一轮 recover_dispatch_intents 按回执认领，不重起。"""
        t = self.state["tickets"][tid]
        w = t["worker"]
        try:
            wt, branch = launcher.create_worktree(self.ctx, self.root, self.cfg.get("base_branch", "staging"),
                                                  self.cfg.get("worktree_dir", ".claude/worktrees"), tid)
        except Exception as e:  # noqa: BLE001
            self._fail(tid, "worktree", e)
            return "failed"
        w.update(worktree=str(wt), branch=branch)
        try:
            prompt = _brief(self, tid)
        except Exception as e:  # noqa: BLE001
            self._fail(tid, "brief", e)
            return "failed"
        before = {k: w.get(k) for k in ("session_id", "runs")}
        log = launcher.prepare_run(w, self.home, tid)
        receipt = launcher.receipt_path(log)
        t["dispatch"] = {"phase": "spawning", "session_id": w["session_id"], "log": str(log), "receipt": str(receipt),
                         "at": self.now}
        try:
            store.save_state(self.home / "state.json", self.state)
        except Exception as e:  # noqa: BLE001 — 意图没落盘：一个进程都不起，撤回内存里的意图
            t.pop("dispatch", None)
            w.update(before)
            self._fail(tid, "intent", e)
            return "stop"
        try:
            launcher.spawn_run(self.ctx, self.home, self.cfg, self.state, tid, prompt, False, self.now,
                               log=log, receipt=receipt)
        except Exception as e:  # noqa: BLE001 — 抛异常 ⇔ 子进程没起（ctx.spawn_detached 的语义）
            t.pop("dispatch", None)
            self._fail(tid, "spawn", e)
            return "failed"
        t["kickoff_at"] = self.now
        store.set_status(self.state, tid, store.KICKOFF, self.now,
                         f"写方向卡（{self.cfg.get('kickoff_budget_min', 30)} 分钟内）")
        t.pop("dispatch", None)
        t.pop("dispatch_fail", None)
        try:
            store.save_state(self.home / "state.json", self.state)     # 起了进程就落盘：这轮后面崩了也不会丢 worker
        except Exception as e:  # noqa: BLE001 — worker 已经在跑：不算派发失败；盘上留着意图，下一轮按回执认领
            self.res["errors"].append(f"dispatch {tid}：已起 worker（pid {w.get('pid')}），但落盘失败：{e!r}；"
                                      "盘上留着启动意图，下一轮按启动回执认领，不会重起")
            return "ok_stop"
        return "ok"

    def _fail(self, tid, phase, e):
        """派发某阶段失败：第 1 次进 errors（带票号和阶段），第 2 次进决定单（topic=dispatch）后不再派；
        用户回答或派成功后清零。"""
        t = self.state["tickets"][tid]
        msg = (str(e) or type(e).__name__)[:300]
        df = t.get("dispatch_fail") or {}
        df.update(count=df.get("count", 0) + 1, phase=phase, error=msg, at=self.now)
        t["dispatch_fail"] = df
        self.res["dispatch_failed"].append({"ticket": tid, "phase": phase, "error": msg})
        if df["count"] == 1:
            err = f"dispatch {tid}（阶段 {phase}）：{msg}"
            self.res["errors"].append(err)
            self.fail_errors.append(err)
        elif not df.get("qid"):
            df["qid"] = self.queue(tid, "stuck", f"{tid} 派发失败 {df['count']} 次（阶段 {phase}）：{msg[:120]}；"
                                                 f"修好后回「{{qid}} 重派」", topic="dispatch")

    # ---- 派单对账（R2 + H4）：上次落了意图、没落 commit 的票 ---------------------------
    def recover_dispatch_intents(self):
        """逐张处理 dispatch.phase == "spawning" 的票，证据只认启动回执（不扫 ps、不靠 argv）：
        回执在：活（pid + 启动时间对得上）⇒ 认领；死 ⇒ 认领后走退出处理；判活失败 ⇒ 保留意图，不派不杀。
        回执读不了 ⇒ 保留意图，不派不杀。回执不在：有日志 / transcript ⇒ 起过，认领后走退出处理；
        什么痕迹都没有 ⇒ 从没起过，清掉意图，这张票重新可派（sid 没用过，复用）。"""
        for tid, t in list(self.state["tickets"].items()):
            it = t.get("dispatch")
            if not isinstance(it, dict) or it.get("phase") != "spawning":
                continue
            try:
                self._recover_one(tid, t, it)
            except Exception as e:  # noqa: BLE001 — 对账出错：保留意图，本票不派
                self._unverified(tid, it, f"对账出错：{e!r}")

    def _recover_one(self, tid, t, it):
        try:
            rec = launcher.read_receipt(it.get("receipt"))
        except ValueError as e:
            return self._unverified(tid, it, f"启动回执读不了：{e}")
        if rec is not None:
            if rec.get("session_id") != it.get("session_id"):
                return self._unverified(tid, it, "启动回执的 session 对不上")
            live = launcher.receipt_liveness(self.ctx, rec)
            if live == "unknown":
                return self._unverified(tid, it, f"pid {rec.get('pid')} 还在，但查不到它的启动时间，认不出是不是这个 worker")
            if live == "alive":
                return self._adopt(tid, t, it, rec["pid"], rec.get("pid_start"))
            self._adopt(tid, t, it, None, None)
            return self._handle_exit(tid, t)
        if self._has_trace(it):
            self._adopt(tid, t, it, None, None)
            return self._handle_exit(tid, t)
        t.pop("dispatch", None)                            # 进程从没起过：重新可派

    def _has_trace(self, it) -> bool:
        log = Path(it["log"]) if it.get("log") else None
        if log and log.exists() and log.stat().st_size > 0:
            return True
        pd = Path(os.path.expanduser(self.cfg.get("claude_projects_dir", "~/.claude/projects")))
        return bool(it.get("session_id")) and any(pd.glob(f"*/{it['session_id']}.jsonl"))

    def _adopt(self, tid, t, it, pid, pid_start):
        w = t["worker"]
        w.update(session_id=it.get("session_id") or w.get("session_id"), pid=pid, pid_start=pid_start,
                 log=it.get("log"), started_at=it.get("at"))
        w.pop("exit", None)
        t["kickoff_at"] = it.get("at")
        if t["status"] == store.QUEUED:
            store.set_status(self.state, tid, store.KICKOFF, self.now,
                             f"写方向卡（{self.cfg.get('kickoff_budget_min', 30)} 分钟内）")
        t.pop("dispatch", None)
        t.pop("dispatch_fail", None)

    def _unverified(self, tid, it, reason):
        it["unverified"] = reason
        if not it.get("recover_reported"):
            it["recover_reported"] = self.now
            self.res["errors"].append(f"dispatch {tid}（阶段 recover）：{reason}；保留启动意图，本票不派、不杀")

    # ---- 7 ----
    def step7_board_and_notify(self):
        board.write(self.home / "board.html",
                    board.render(self.state, store.recent_decisions(self.home / "decisions.md"),
                                 {tid: events.read_all(self.home, tid)[-5:] for tid in self.state["tickets"]},
                                 self.ctx.now(), self.cfg.get("slots")))
        if self.res["new_queue"]:
            q = store.queue_view(self.state)
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
        card_path=str(run.home / "cards" / f"{tid}.md"), kickoff_budget=run.cfg.get("kickoff_budget_min", 30),
        finish=(f"这张票只改仓外目录（{'、'.join(t['surfaces'])}），不推分支、不开 PR：开改前把要动的文件整份备份到 "
                f"{run.home}/backups/{tid}/（cp -a，保留目录结构）；改完把 `diff -ru <备份> <现状>` 写到 "
                f"{run.home}/cards/{tid}.diff，跑验收，然后 progress impl_done 退出。")
        if run._offrepo(t) else
        (f"改完照 worker.md 第 3 段：推 router/{tid}，`gh pr create --base {run.cfg.get('base_branch', 'staging')}`"
         "（🔴 必须写 --base，默认 base 是 main），然后 progress pr_opened 退出。"))


def _explain_waits(run, picked, done, disk):
    """排队的票在等什么（看板「在等」）。picked = 这一轮选中的，done = 真派出去的。判定顺序：
    依赖 → 代码面锁 → base 问题 → 核实 worker 是否已起 → 派发失败 → 磁盘 → 名额真满 → 下一轮派。"""
    st = run.state
    tickets = st["tickets"]
    base_name = run.cfg.get("base_branch", "staging")
    locks = surfaces.held_locks(st)
    free = scheduler.free_slots(st, run.cfg.get("slots", {"heavy": 3, "light": 2}))
    base = run._base if run._base is not None and not run._base["ok"] else None
    for tid, t in tickets.items():
        if t["status"] != store.QUEUED or not t["start_ok"] or tid in done:
            continue
        unsettled = [b for b in t["blocked_by"] if tickets.get(b, {}).get("status") not in store.SETTLED]
        held = sorted((s, h) for s, h in locks.items() if s in t["surfaces"] and h != tid)
        intent, df = t.get("dispatch") or {}, t.get("dispatch_fail") or {}
        if unsettled:
            checking = [b for b in unsettled if tickets.get(b, {}).get("status") == store.AWAIT_CHECK]
            t["waiting_for"] = (f"等你核对 {'、'.join(checking)} 的仓外改动" if checking
                                else f"等 {'、'.join(unsettled)} 合进 {base_name}")
        elif held:
            s, h = held[0]
            if h.startswith("ext:"):
                who = f"外部 session {Path(h[4:]).name} 持有"
            elif tickets.get(h, {}).get("status") == store.AWAIT_CHECK:
                who = f"{h} 待你核对"
            else:
                who = f"{h} 持有"
            t["waiting_for"] = f"等代码面 {s}（{who}）"
        elif base:
            t["waiting_for"] = base["wait"] or f"等 origin/{base_name}"
        elif intent.get("phase") == "spawning":
            t["waiting_for"] = f"核实 worker 是否已起（{intent.get('unverified') or '上次派发没落完盘'}）"
        elif df:
            t["waiting_for"] = (f"派发失败，等你回答 {df['qid']}" if df.get("qid") else
                                f"派发失败（第 {df.get('count', 1)} 次，阶段 {df.get('phase')}）：{str(df.get('error', ''))[:120]}")
        elif t["weight"] == "heavy" and disk:
            t["waiting_for"] = f"磁盘：{disk}，先清再派重票"
        elif free.get(t["weight"], 0) <= 0:
            t["waiting_for"] = "等重名额" if t["weight"] == "heavy" else "等轻名额"
        else:
            t["waiting_for"] = "下一轮派"


def dispatch(ctx, root, cfg, state, run=None) -> list:
    """对账上次没落完盘的派发 → 选票 → 查 base → 逐张派。start 子命令（已持锁）和第 6 步共用。
    派单是一个五阶段事务（Run._dispatch_one），每张票自己的故障边界；失败结构化进 run.res["dispatch_failed"]。"""
    run = run or Run(ctx, root, cfg, state)
    run.recover_dispatch_intents()
    heavy_cand = any(t["status"] == store.QUEUED and t["start_ok"] and t["weight"] == "heavy"
                     for t in state["tickets"].values())
    disk = run.disk_block() if heavy_cand else ""
    picked = scheduler.pick_dispatch(state, cfg.get("slots", {"heavy": 3, "light": 2}), bool(disk))
    done = []
    if picked and run.ensure_base()["ok"]:
        for tid in picked:
            outcome = run._dispatch_one(tid)
            if outcome in ("ok", "ok_stop"):
                done.append(tid)
            if outcome in ("stop", "ok_stop"):
                break                                    # 存盘出了问题：这一轮不再派后面的票
    _explain_waits(run, picked, done, disk)
    return done


def _resurface(r):
    """router 被压缩 / 重启后，上一次 watch 交出去、还没处理完的东西再交一次（只在 watch 第一轮和 tick 子命令做）。"""
    for tid, t in r.state["tickets"].items():
        if (t["status"] == store.KICKOFF and t.get("card_ready_at") and not launcher.worker_alive(r.ctx, t["worker"])
                and tid not in r.res["cards_for_review"]):
            r.res["cards_for_review"].append(tid)
    pending = [e for e in r.state.get("slow_path", []) if e not in r.res["slow_path_events"]]
    r.res["slow_path_events"] = pending + r.res["slow_path_events"]


def run(ctx, root, cfg, resurface=False) -> dict:
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
        if resurface:
            _resurface(r)
        store.save_state(home / "state.json", state)
    return r.res


# ---- 子命令（router.py 动态挂载）---------------------------------------------

def add_subcommands(sub):
    sub.add_parser("tick", help="跑一轮巡检")
    s = sub.add_parser("watch", help="循环巡检，有事才退出（router 用 Bash 后台跑）")
    s.add_argument("--interval", type=int, default=60)
    s.add_argument("--max-minutes", type=int, default=480, help="router session 没了也别永远跑下去")


def cmd_tick(a, env):
    return {"ok": True, **run(env.ctx, env.root, env.cfg, resurface=True)}


def cmd_watch(a, env):
    started, err_streak, first = time.time(), 0, True
    while True:
        res = run(env.ctx, env.root, env.cfg, resurface=first)
        first = False
        err_streak = err_streak + 1 if res["errors"] else 0
        if any(res.get(k) for k in WAKE_KEYS) or err_streak >= 3:
            return {"ok": True, "woke": [k for k in WAKE_KEYS if res.get(k)] + (["errors"] if err_streak >= 3 else []), **res}
        if time.time() - started > a.max_minutes * 60:
            return {"ok": True, "woke": ["max_minutes"], **res}
        time.sleep(a.interval)
