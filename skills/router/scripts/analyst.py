"""分析 agent（Fable）运行器：拼 prompt、起后台 `claude -p`、读产出文件。替代 v1 的分诊。

和 worker 的区别：
- 不在 worktree 里跑：cwd = router home；日志 <home>/analyst/logs/<run>.log；产出 <home>/analyst/out/<run>.json
  （路径写在 prompt 的 `OUT=` 行里，它把 JSON 写到那里）；
- 不开票、不派活：产出只经 tick 收尾交给 apply_daily（daily）/ propose_from（weekly、migrate），calibrate 只叫醒 router；
- 环境里 ROUTER_TICKET=analyst：router.py 的 router-only 子命令（add / propose / verdict / analyst …）对它一律拒绝；
- calibrate 要盲跑：cwd 是项目外新建的空临时目录，环境里去掉 ROUTER_HOME（OUT / 日志仍是 router home 下的绝对路径）。

没配 config `analyst.brief`（项目取数入口）= 没启用：tick 不自动起，`analyst run` 也拒绝。
"""
import json
import os
import string
import tempfile
from datetime import timedelta
from pathlib import Path

import launcher
import store

SKILL_DIR = Path(__file__).resolve().parent.parent
MODES = ("daily", "weekly", "calibrate", "migrate")
DEFAULT_MODEL = "claude-fable-5-1"
DEFAULT_DAILY_AT = "01:30"
SPAN_DAYS = {"daily": 1, "weekly": 7, "calibrate": 14, "migrate": 14}   # 没给 since 时往前看几天（until 缺省 = 今天）
STALE_S = 2 * 3600       # 进程这么久还没退出（或认不出是不是它）：判失败；身份对得上就停掉
ANALYST_ENV = "analyst"  # 起进程时的 ROUTER_TICKET


class AnalystError(Exception):
    """起不了：没配 brief、模式不认识、config 缺字段。"""


def enabled(cfg) -> bool:
    return bool((cfg.get("analyst") or {}).get("brief"))


def daily_at(cfg):
    """config analyst.daily_at_utc（缺省 01:30）→ (时, 分)。"""
    raw = str((cfg.get("analyst") or {}).get("daily_at_utc") or DEFAULT_DAILY_AT)
    try:
        h, m = (int(x) for x in raw.split(":"))
        if 0 <= h < 24 and 0 <= m < 60:
            return h, m
    except ValueError:
        pass
    raise AnalystError(f"config analyst.daily_at_utc 看不懂：{raw!r}（要 HH:MM，UTC）")


def build_cmd(cfg, prompt: str) -> list:
    """claude_bin / permission_mode / allowed、disallowed tools 取 worker 的；模型取 analyst.model（缺省 Fable）。"""
    w = cfg.get("worker") or {}
    pm = w.get("permission_mode")
    if not pm:
        raise AnalystError("config.worker 缺 permission_mode")
    binary = w.get("claude_bin", "claude")
    model = (cfg.get("analyst") or {}).get("model") or DEFAULT_MODEL
    cmd = [*([binary] if isinstance(binary, str) else binary), "-p", prompt, "--model", model,
           "--output-format", "json", "--permission-mode", pm]
    if w.get("allowed_tools"):
        cmd += ["--allowedTools", ",".join(w["allowed_tools"])]
    if w.get("disallowed_tools"):
        cmd += ["--disallowedTools", *w["disallowed_tools"]]   # 变长参数，必须放最后
    return cmd


def _attempt_line(a) -> str:
    when = a.get("at") or a.get("shipped_at") or "?"
    return f"尝试 {a.get('n')}「{a.get('direction', '')}」上线 {when}，判定 {a.get('verdict') or '待判'}"


def state_summary(st, mode) -> str:
    """给 daily / weekly / migrate 的票况（紧凑文本）。calibrate 一律空串：校准要盲跑。"""
    if mode == "calibrate":
        return ""
    lines = ["业务票（编号｜标题｜人群｜状态｜没拆的墙｜最近一次尝试）："]
    biz = [(tid, t) for tid, t in st["tickets"].items() if store.ticket_kind(t) == "business"]
    for tid, t in biz:
        walls = "；".join(f"{w.get('id')} {w.get('text', '')}" for w in t.get("walls", [])
                         if w.get("status") == "open") or "无"
        cur = store.current_attempt(t)                # 当前尝试 = 最后一条非历史的（历史尝试排在哪都不能盖住它）
        hist = sum(1 for a in t.get("attempts") or [] if a.get("historical"))
        last = _attempt_line(cur) if cur else "还没有尝试"
        if hist:
            last += f"（另有 {hist} 条老票的历史尝试，不用判）"
        lines.append(f"- {tid}｜{t.get('title', '')}｜{t.get('segment', '')}｜{t.get('status')}｜墙：{walls}｜{last}")
    if not biz:
        lines.append("（无）")
    if mode == "migrate":
        legacy = [(tid, t) for tid, t in st["tickets"].items() if store.ticket_kind(t) == "legacy"]
        lines += ["", "老票（编号｜标题｜状态）："]
        lines += [f"- {tid}｜{t.get('title', '')}｜{t.get('status')}" for tid, t in legacy] or ["（无）"]
    return "\n".join(lines)


def daily_outs(st, since, until) -> list:
    """时间窗 [since, until] 里跑完、收过的 daily 产出路径（按跑的先后）。失败的、文件不在的不算。"""
    out = []
    for r in ((st.get("analyst") or {}).get("runs") or {}).values():
        if r.get("mode") != "daily" or r.get("failed") or not r.get("out") or not Path(r["out"]).exists():
            continue
        if (since and (r.get("since") or "") < since) or (until and (r.get("until") or "") > until):
            continue
        out.append((r.get("started_at") or r.get("until") or "", r["out"]))
    return [o for _, o in sorted(out)]


def latest_week(cfg):
    """周报目录里最新一期已发出（有 sent）的期号；没有 → None。"""
    runs = Path(os.path.expanduser(cfg.get("weekly_runs_dir") or ""))
    if not cfg.get("weekly_runs_dir") or not runs.is_dir():
        return None
    weeks = sorted(d.name for d in runs.iterdir() if (d / "sent").exists())
    return weeks[-1] if weeks else None


def weekly_dir(cfg, week) -> str:
    if not cfg.get("weekly_runs_dir"):
        return "（config 没配 weekly_runs_dir）"
    week = week or latest_week(cfg)
    if not week:
        return "（没找到已发出的周报）"
    return str(Path(os.path.expanduser(cfg["weekly_runs_dir"])) / week)


def render(env, mode, since, until, out_path, st=None, week=None) -> str:
    """templates/analyst_<mode>.md 代入 $brief $out $since $until $home $state_summary；
    weekly 另代入 $daily_outs（本周 daily 产出清单）和 $weekly_dir（本期周报目录，week 不给就取最新一期）。
    st 不给就读 <home>/state.json（tick 里传内存中的 state）。calibrate 不读、不代入票况。"""
    if mode not in MODES:
        raise AnalystError(f"不认识的模式 {mode}")
    if mode != "calibrate" and st is None:
        st = store.load_state(Path(env.home) / "state.json")
    tpl = string.Template((SKILL_DIR / "templates" / f"analyst_{mode}.md").read_text(encoding="utf-8"))
    brief = (env.cfg.get("analyst") or {}).get("brief") or ""
    extra = {}
    if mode == "weekly":
        outs = daily_outs(st, since, until)
        extra = {"daily_outs": "\n".join(f"  - {o}" for o in outs) or "  - （这段时间没有 daily 产出）",
                 "weekly_dir": weekly_dir(env.cfg, week)}
    return tpl.safe_substitute(
        brief=os.path.expanduser(brief) if brief else "（config 没配 analyst.brief）",
        out=str(out_path), since=since or "", until=until or "", home=str(env.home),
        state_summary=state_summary(st, mode) if mode != "calibrate" else "", **extra)


def finished(run) -> bool:
    return bool(run.get("ingested") or run.get("failed"))


def active_run(ctx, st, mode):
    """同模式还没收尾、进程也没确认死掉的 run（认不出 = 当它还在）→ run_id | None。"""
    for rid, r in ((st.get("analyst") or {}).get("runs") or {}).items():
        if r.get("mode") == mode and not finished(r) and launcher.worker_liveness(ctx, r) != "dead":
            return rid
    return None


def start(env, st, mode, since=None, until=None, week=None) -> str:
    """起一次分析 agent，记进 st["analyst"]["runs"][run_id]（调用方持锁、负责存盘）→ run_id。
    同模式已有在跑的就不起第二个，返回它的 run_id。起进程失败原样抛（ctx.spawn_detached 的语义）。"""
    if mode not in MODES:
        raise AnalystError(f"不认识的模式 {mode}")
    if not enabled(env.cfg):
        raise AnalystError("config 没配 analyst.brief（项目取数入口）：分析 agent 不起")
    rid = active_run(env.ctx, st, mode)
    if rid:
        return rid
    now = env.ctx.now()
    until = until or now.strftime("%Y-%m-%d")
    since = since or (now - timedelta(days=SPAN_DAYS[mode])).strftime("%Y-%m-%d")
    an = st.setdefault("analyst", {})
    an["seq"] = an.get("seq", 0) + 1
    rid = f"{mode}-{an['seq']}"
    adir = Path(env.home) / "analyst"
    out, log = adir / "out" / f"{rid}.json", adir / "logs" / f"{rid}.log"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)                  # 编号复用（state 重建过）时，旧产出不能冒充这一次的
    if mode == "weekly":
        week = week or latest_week(env.cfg)
    cmd = build_cmd(env.cfg, render(env, mode, since, until, out, st=st, week=week))
    if mode == "calibrate":                      # 盲跑：不在 router 状态目录里起，也不告诉它在哪（None = 连继承的一起去掉）
        cwd, penv = Path(tempfile.mkdtemp(prefix="router-calibrate-")), {"ROUTER_HOME": None, "ROUTER_TICKET": ANALYST_ENV}
    else:
        cwd, penv = Path(env.home), {"ROUTER_HOME": str(env.home), "ROUTER_TICKET": ANALYST_ENV}
    pid = env.ctx.spawn_detached(cmd, cwd, log, env=penv)
    try:
        pid_start = env.ctx.proc_start(pid)
    except Exception:  # noqa: BLE001 — 进程已经起了：查不到启动时间只影响认身份
        pid_start = None
    an.setdefault("runs", {})[rid] = {"mode": mode, "pid": pid, "pid_start": pid_start,
                                      "started_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                      "out": str(out), "log": str(log), "since": since, "until": until,
                                      "cwd": str(cwd), **({"week": week} if mode == "weekly" and week else {})}
    return rid


def read_out(path):
    """→ (dict, None) | (None, 原因)。文件不在 / 不是 JSON / 顶层不是对象都算没产出。"""
    p = Path(path)
    if not p.exists():
        return None, f"没有产出文件（{p}）"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        return None, f"产出不是合法 JSON（{p}）：{str(e)[:120]}"
    if not isinstance(data, dict):
        return None, f"产出顶层不是对象（{p}）"
    return data, None
