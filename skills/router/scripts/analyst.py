"""分析 agent（Fable）运行器：拼 prompt、起后台 `claude -p`、读产出文件。替代 v1 的分诊。

和 worker 的区别：
- 不在 worktree 里跑：cwd = router home；日志 <home>/analyst/logs/<run>.log；产出 <home>/analyst/out/<run>.json
  （路径写在 prompt 的 `OUT=` 行里，它把 JSON 写到那里）；
- 不开票、不派活：产出只经 tick 收尾交给 apply_daily（daily）/ propose_from（weekly、migrate），calibrate 只叫醒 router；
  play（玩法复盘，每周一次）校验后只记复盘结论（尝试上的 refuted），整份交 router 给用户挑方向，不自动落别的；
- 产出里的 `learned`（关于用户的新事实，一句一条）由 tick 追加进 <home>/analyst/learned.jsonl，play / weekly 先读它；
- 环境里 ROUTER_TICKET=analyst：router.py 的 router-only 子命令（add / propose / verdict / analyst …）对它一律拒绝；
- calibrate 要盲跑：cwd 是项目外新建的空临时目录，环境里去掉 ROUTER_HOME（OUT / 日志仍是 router home 下的绝对路径）。

没配 config `analyst.brief`（项目取数入口）= 没启用：tick 不自动起，`analyst run` 也拒绝。
"""
import json
import os
import re
import string
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import launcher
import store

SKILL_DIR = Path(__file__).resolve().parent.parent
MODES = ("daily", "weekly", "calibrate", "migrate", "play")
DEFAULT_MODEL = "claude-fable-5-1"
DEFAULT_DAILY_AT = "01:30"
SPAN_DAYS = {"daily": 1, "weekly": 7, "calibrate": 14, "migrate": 14, "play": 14}   # 没给 since 时往前看几天（until 缺省 = 今天）
STALE_S = 2 * 3600       # 进程这么久还没退出（或认不出是不是它）：判失败；身份对得上就停掉
ANALYST_ENV = "analyst"  # 起进程时的 ROUTER_TICKET

# play（玩法复盘）
NEGATIVE = ("useless", "no_entry", "harmful", "wall_broken_not_retained")   # 否定的判定：play 逐条复盘
REFUTING = ("useless", "harmful", "wall_broken_not_retained")   # 算连败的判定：no_entry = 没人碰到，对玩法没有信息，连败里跳过
KILLED = ("mechanism", "play")           # 这次否定否掉的是做法（机制）还是玩法本身
STREAK_AT = 3                            # 同一指标最近连续几次判定都是否定 ⇒ 必须 escalate（重审前提）
PRIOR_PLAYS = 4                          # 传给 play 的上几次 play 产出（最多几份）
MAX_HYPOTHESES = 3
HYPO_KEYS = ("key", "title", "segment", "play_change", "why_more_engagement", "metric", "expected_lift_points",
             "traffic_per_day", "readable_in_days", "cheapest_test", "needs_code", "ticket", "prior_refuted")
REF = re.compile(r"^\s*(T\d+)\s*a(\d+)\s*$")   # 尝试的引用写法：「T<n> a<k>」
LEARNED_TEXT_MAX = 300


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
    src = f"（并自 {a['merged_from']} 的尝试 {a.get('orig_n', '?')}）" if a.get("merged_from") else ""
    return f"尝试 {a.get('n')}「{a.get('direction', '')}」{src}上线 {when}，判定 {a.get('verdict') or '待判'}"


def _metric_name(t) -> str:
    m = t.get("metric") or {}
    return str((m.get("name") if isinstance(m, dict) else m) or "").strip()


def attempt_ref(tid, a) -> str:
    return f"{tid} a{a.get('n')}"


def parse_ref(ref):
    """「T3 a2」→ ("T3", 2)；认不出 → None。"""
    m = REF.match(str(ref or ""))
    return (m.group(1), int(m.group(2))) if m else None


def find_attempt(st, ref):
    """引用 → (票号, 票, 尝试)；票不在、不是业务票、没有这次尝试、或是并进来的历史尝试 → None。"""
    p = parse_ref(ref)
    t = st["tickets"].get(p[0]) if p else None
    if t is None or store.ticket_kind(t) != "business":
        return None
    a = next((x for x in t.get("attempts") or [] if x.get("n") == p[1] and not x.get("historical")), None)
    return (p[0], t, a) if a else None


def _play_tickets(st):
    """play 看的业务票：在途的和已关的都看（已关的尝试也是教训）；并进别处的跳过（尝试已挪到目标票上）。"""
    return [(tid, t) for tid, t in st["tickets"].items()
            if store.ticket_kind(t) == "business" and not t.get("merged_into")]


def _play_summary(st) -> str:
    """play 的票况：每张业务票的指标、全部墙，和全部尝试（含判定、note、上次 play 的复盘结论）。"""
    lines = ["业务票（编号｜标题｜人群｜指标｜状态）："]
    biz = _play_tickets(st)
    for tid, t in biz:
        closed = "（已关）" if t.get("status") in (store.DONE, store.SKIP) else ""
        lines.append(f"- {tid}｜{t.get('title', '')}｜{t.get('segment', '')}｜{_metric_name(t) or '—'}｜"
                     f"{t.get('status')}{closed}")
        walls = "；".join(f"{w.get('id')} {store.wall_line(w)}（{'已拆' if w.get('status') == 'broken' else '没拆'}）"
                         for w in t.get("walls") or []) or "无"
        lines.append(f"  墙：{walls}")
        atts = t.get("attempts") or []
        if not atts:
            lines.append("  尝试：还没有")
        for a in atts:
            if a.get("historical"):
                lines.append(f"  - 老票的历史尝试「{a.get('direction', '')}」：{a.get('legacy_verdict') or '—'}")
                continue
            when = str(a.get("at") or a.get("shipped_at") or "?")[:10]
            note = f"：{a['note']}" if a.get("note") else ""
            ref = a.get("refuted") or {}
            done = f"（已复盘：否定的是{'玩法' if ref.get('killed') == 'play' else '做法'}；{ref.get('lesson', '')}）" \
                if ref else ""
            lines.append(f"  - {attempt_ref(tid, a)}「{a.get('direction', '')}」上线 {when}，"
                         f"判定 {a.get('verdict') or '待判'}{note}{done}")
    if not biz:
        lines.append("（无）")
    return "\n".join(lines)


def state_summary(st, mode) -> str:
    """给 daily / weekly / migrate / play 的票况（紧凑文本）。calibrate 一律空串：校准要盲跑。"""
    if mode == "calibrate":
        return ""
    if mode == "play":
        return _play_summary(st)
    lines = ["业务票（编号｜标题｜人群｜状态｜没拆的墙｜最近一次尝试）："]
    # 只列在途的业务票：已关（done / skip）的不判、不挂墙、不提议结束（终审 #2：关掉的票不复活）
    biz = [(tid, t) for tid, t in st["tickets"].items()
           if store.ticket_kind(t) == "business" and t.get("status") not in (store.DONE, store.SKIP)]
    for tid, t in biz:
        walls = "；".join(f"{w.get('id')} {store.wall_line(w)}" for w in t.get("walls", [])
                         if w.get("status") == "open") or "无"
        cur = store.current_attempt(t)                # 当前尝试 = 最后一条非历史的（历史尝试排在哪都不能盖住它）
        hist = sum(1 for a in t.get("attempts") or [] if a.get("historical"))
        last = _attempt_line(cur) if cur else "还没有尝试"
        # 并票（v2.1）后一张票可能同时有几次尝试在等判定：都列出来，daily 都要判
        more = [a for a in t.get("attempts") or [] if a is not cur and not a.get("historical")
                and a.get("verdict") in (None, "pending_sample")]
        if more:
            last += "；另有待判：" + "；".join(_attempt_line(a) for a in more)
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


def prior_outs(st, mode, limit=PRIOR_PLAYS) -> list:
    """同模式跑成功、收过的产出路径（按跑的先后，最近 limit 份）。失败的、文件不在的不算。"""
    out = [(r.get("started_at") or "", r["out"]) for r in ((st.get("analyst") or {}).get("runs") or {}).values()
           if r.get("mode") == mode and r.get("ingested") and not r.get("failed") and r.get("out")
           and Path(r["out"]).exists()]
    return [o for _, o in sorted(out)][-limit:] if limit else []


def iso_week(ts) -> str:
    """「2026-11-16T…Z」→「2026-W47」。"""
    y, w, _ = datetime.strptime(str(ts)[:10], "%Y-%m-%d").isocalendar()
    return f"{y}-W{w:02d}"


def play_context(st, since) -> dict:
    """play 起跑时定下的两件事（存进 run 记录，收产出时按它校验，期间新落的判定留给下一次）：
    - must_review：上次 play（跑成功的那次起跑时刻；没有就是这次的 since）以来判了否定、还没复盘过的尝试，逐条要写 refuted_review；
    - streaks：同一指标最近连续 STREAK_AT 次判定都是否定（no_entry 跳过：没人碰到不算否定玩法）、其中有这次要复盘的 ⇒ 这次必须填 escalate。"""
    done = [r.get("started_at") or "" for r in ((st.get("analyst") or {}).get("runs") or {}).values()
            if r.get("mode") == "play" and r.get("ingested") and not r.get("failed")]
    cutoff = max(done) if done else (since or "")
    must, by_metric = [], {}
    for tid, t in _play_tickets(st):
        for a in t.get("attempts") or []:
            if a.get("historical") or a.get("verdict") in (None, "pending_sample", "legacy"):
                continue
            ref = attempt_ref(tid, a)
            if a["verdict"] != "no_entry":                       # 没入口不算对玩法的否定，也不打断连败
                by_metric.setdefault(_metric_name(t) or tid, []).append((a.get("verdict_at") or "", ref, a["verdict"]))
            if a["verdict"] in NEGATIVE and not a.get("refuted") and (a.get("verdict_at") or "") >= cutoff:
                must.append(ref)
    streaks = []
    for metric, rows in by_metric.items():
        last = sorted(rows)[-STREAK_AT:]
        if len(last) == STREAK_AT and all(v in REFUTING for _, _, v in last) and any(r in must for _, r, _ in last):
            streaks.append({"metric": metric, "refs": [r for _, r, _ in last]})
    return {"cutoff": cutoff, "must_review": must, "streaks": streaks}


def learned_path(home) -> Path:
    return Path(home) / "analyst" / "learned.jsonl"


def read_learned(home) -> list:
    """learned.jsonl 全部行（坏行跳过）；文件不在 → []。"""
    p = learned_path(home)
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            x = json.loads(line)
        except ValueError:
            continue
        if isinstance(x, dict) and str(x.get("text") or "").strip():
            out.append(x)
    return out


def recent_learned(home, n=3) -> list:
    return read_learned(home)[-n:] if n else []


def append_learned(home, run, mode, items, at) -> int:
    """产出里的 learned（一句一条）逐条追加，每行带 run / at / mode → 追加了几条。不是字符串、空的跳过。"""
    rows = [{"run": run, "at": at, "mode": mode, "text": str(x).strip()[:LEARNED_TEXT_MAX]}
            for x in items or [] if isinstance(x, str) and x.strip()]
    if rows:
        p = learned_path(home)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)
    return len(rows)


def _cell(x) -> str:
    if not isinstance(x, dict):
        return "—"
    n, pct = x.get("n"), x.get("pct")
    return f"{n if n is not None else '—'}" + (f"（{pct}%）" if pct is not None else "")


def funnel_line(f) -> str:
    """state 的 analyst.funnel → 一行（status 文字版 / 汇报用）：各阶段本周 n（%），括号里是上周。"""
    stages = (f or {}).get("stages") if isinstance(f, dict) else None
    if not stages:
        return "还没有漏斗读数"
    parts = [f"{s.get('stage', '?')} {_cell(s.get('this_week'))}，上周 {_cell(s.get('last_week'))}"
             for s in stages if isinstance(s, dict)]
    return f"{f.get('window') or '—'}：" + " → ".join(parts)


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


def _play_extra(st, ctx) -> dict:
    """play 模板的 $prior_plays / $must_review / $streaks。"""
    outs = prior_outs(st, "play")
    must = []
    for ref in ctx["must_review"]:
        hit = find_attempt(st, ref)
        a = hit[2] if hit else {}
        must.append(f"  - {ref}「{a.get('direction', '')}」判定 {a.get('verdict')}"
                    + (f"：{a['note']}" if a.get("note") else ""))
    streaks = [f"  - 指标「{s['metric']}」：{'、'.join(s['refs'])} 连续 {STREAK_AT} 次否定 ⇒ 这次必须填 `escalate`"
               for s in ctx["streaks"]]
    return {"prior_plays": "\n".join(f"  - {o}" for o in outs) or "  - （还没有跑过 play）",
            "must_review": "\n".join(must) or "  - （上次 play 以来没有新的否定判定）",
            "streaks": "\n".join(streaks) or "  - （没有）"}


def render(env, mode, since, until, out_path, st=None, week=None, play=None) -> str:
    """templates/analyst_<mode>.md 代入 $brief $out $since $until $home $state_summary；
    weekly 另代入 $daily_outs（本周 daily 产出清单）和 $weekly_dir（本期周报目录，week 不给就取最新一期）；
    play 另代入 $prior_plays（上几次 play 的产出）、$must_review、$streaks（play 不给就按 st 现算 play_context）。
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
    elif mode == "play":
        extra = _play_extra(st, play or play_context(st, since))
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
    play = play_context(st, since) if mode == "play" else None
    cmd = build_cmd(env.cfg, render(env, mode, since, until, out, st=st, week=week, play=play))
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
                                      "cwd": str(cwd), **({"week": week} if mode == "weekly" and week else {}),
                                      **(play or {})}
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
