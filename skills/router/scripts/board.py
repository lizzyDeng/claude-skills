"""本地看板 .claude/router/board.html：只读，30 秒自动刷新，router 每次状态变化重写。

版式照抄 docs/superpowers/specs/2026-11-02-router-board-mock.html（CSS 原样复制）。
「解法」一栏在方向卡出来之前显示 router 线索并标「非方案」（spec §5.4）。全部字段 html.escape。
"""
import html
from pathlib import Path

import store

CSS = ':root{\n  --bg:#f6f5f1; --card:#fff; --ink:#1f2328; --soft:#5d6670; --faint:#8b939c; --line:#dcd8cf;\n  --you:#6639ba; --you-bg:#f3eefc; --run:#0969da; --run-bg:#e8f1fd; --q:#9a6700; --q-bg:#fdf5dc;\n  --ok:#1a7f37; --ok-bg:#e8f5eb; --off:#6e7781; --off-bg:#eef0f2; --accent:#c2410c;\n  --mono:"SF Mono",Menlo,Consolas,monospace;\n}\n@media (prefers-color-scheme: dark){\n  :root:not([data-theme="light"]){\n    --bg:#15171b; --card:#1d2026; --ink:#e6e6e6; --soft:#a3abb4; --faint:#7d858e; --line:#323840;\n    --you:#b392f0; --you-bg:#241b36; --run:#58a6ff; --run-bg:#14243a; --q:#d4a72c; --q-bg:#2c2510;\n    --ok:#4ac26b; --ok-bg:#142619; --off:#8b949e; --off-bg:#22262c; --accent:#fb923c;\n  }\n}\n:root[data-theme="dark"]{\n  --bg:#15171b; --card:#1d2026; --ink:#e6e6e6; --soft:#a3abb4; --faint:#7d858e; --line:#323840;\n  --you:#b392f0; --you-bg:#241b36; --run:#58a6ff; --run-bg:#14243a; --q:#d4a72c; --q-bg:#2c2510;\n  --ok:#4ac26b; --ok-bg:#142619; --off:#8b949e; --off-bg:#22262c; --accent:#fb923c;\n}\n*{box-sizing:border-box}\nbody{margin:0;background:var(--bg);color:var(--ink);\n  font:15px/1.65 -apple-system,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif}\nmain{max-width:1080px;margin:0 auto;padding:28px 16px 64px}\n.mock{background:var(--q-bg);color:var(--q);border:1px dashed var(--q);border-radius:6px;\n  padding:6px 12px;font-size:13px;margin-bottom:16px}\nheader{display:flex;flex-wrap:wrap;align-items:baseline;gap:8px 18px;margin-bottom:6px}\nh1{font-size:24px;margin:0}\n.meta{color:var(--faint);font-size:13px}\n.stats{display:flex;flex-wrap:wrap;gap:8px;margin:14px 0 6px}\n.stat{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:8px 14px;min-width:96px}\n.stat b{display:block;font-size:22px;line-height:1.2;font-variant-numeric:tabular-nums}\n.stat span{font-size:12.5px;color:var(--soft)}\n.stat.you b{color:var(--you)} .stat.run b{color:var(--run)} .stat.q b{color:var(--q)} .stat.ok b{color:var(--ok)}\n.slots{font-size:13px;color:var(--soft);margin:4px 0 18px}\n.slots .bar{display:inline-flex;gap:3px;vertical-align:middle;margin:0 6px 0 4px}\n.slots .bar i{width:14px;height:10px;border-radius:2px;background:var(--off-bg);border:1px solid var(--line)}\n.slots .bar i.on{background:var(--run);border-color:var(--run)}\nh2{font-size:15px;color:var(--soft);font-weight:600;margin:26px 0 10px;letter-spacing:.02em}\n.decide{background:var(--you-bg);border:1px solid color-mix(in srgb,var(--you) 45%,transparent);\n  border-radius:10px;padding:14px 18px}\n.decide .row{display:flex;gap:12px;align-items:flex-start;padding:8px 0}\n.decide .row+.row{border-top:1px solid color-mix(in srgb,var(--you) 25%,transparent)}\n.decide .what{flex:1}\n.decide .what b{display:block}\n.decide .what small{color:var(--soft)}\n.say{font-family:var(--mono);font-size:12.5px;background:var(--card);border:1px solid var(--line);\n  border-radius:6px;padding:3px 8px;white-space:nowrap;color:var(--you)}\n.tabs{display:flex;flex-wrap:wrap;gap:6px;margin:6px 0 12px}\n.tabs button{font:inherit;font-size:13px;background:var(--card);color:var(--soft);border:1px solid var(--line);\n  border-radius:16px;padding:3px 12px;cursor:pointer}\n.tabs button[aria-pressed="true"]{background:var(--ink);color:var(--bg);border-color:var(--ink)}\n.issue{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--line);\n  border-radius:10px;padding:14px 18px;margin:10px 0}\n.issue[data-g="you"]{border-left-color:var(--you)}\n.issue[data-g="run"]{border-left-color:var(--run)}\n.issue[data-g="q"]{border-left-color:var(--q)}\n.issue[data-g="ok"]{border-left-color:var(--ok)}\n.issue[data-g="off"]{border-left-color:var(--off)}\n.ih{display:flex;flex-wrap:wrap;align-items:center;gap:6px 10px}\n.ih .no{font-family:var(--mono);font-size:13px;color:var(--faint)}\n.ih .t{font-weight:700;font-size:16px}\n.pill{font-size:12px;font-weight:600;padding:1px 9px;border-radius:10px;white-space:nowrap}\n.p-you{background:var(--you-bg);color:var(--you)} .p-run{background:var(--run-bg);color:var(--run)}\n.p-q{background:var(--q-bg);color:var(--q)} .p-ok{background:var(--ok-bg);color:var(--ok)}\n.p-off{background:var(--off-bg);color:var(--off)}\n.tagw{font-size:12px;color:var(--faint);border:1px solid var(--line);border-radius:10px;padding:0 8px}\n.wait{font-size:13px;color:var(--soft);margin:4px 0 10px}\n.wait b{color:var(--ink);font-weight:600}\n.three{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}\n@media (max-width:820px){.three{grid-template-columns:1fr}}\n.cell{background:color-mix(in srgb,var(--line) 22%,transparent);border-radius:8px;padding:10px 12px}\n.cell h4{margin:0 0 4px;font-size:12.5px;color:var(--faint);font-weight:600;letter-spacing:.04em}\n.cell p{margin:0;font-size:14px}\n.cell .src{display:block;margin-top:6px;font-size:12px;color:var(--faint)}\n.cell.sol.pending{border:1px dashed var(--line);background:transparent}\n.foot{display:flex;flex-wrap:wrap;gap:4px 16px;margin-top:10px;font-size:12.5px;color:var(--faint)}\n.foot code,code{font-family:var(--mono);font-size:12px;background:color-mix(in srgb,var(--line) 40%,transparent);\n  padding:0 5px;border-radius:4px;word-break:break-all}\n.log{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:6px 16px}\n.log .row{display:grid;grid-template-columns:44px 1fr auto;gap:10px;padding:8px 0;font-size:14px;align-items:baseline}\n.log .row+.row{border-top:1px solid var(--line)}\n.log .id{font-family:var(--mono);color:var(--faint);font-size:12.5px}\n.log small{color:var(--soft)}\n.hidden{display:none}\n.tl{margin:8px 0 0;padding:0;list-style:none;font-size:12.5px;color:var(--soft)}\n.tl li{padding:1px 0}\n.tl .ts{font-family:var(--mono);color:var(--faint);margin-right:6px}\n.p-review{background:var(--q-bg);color:var(--accent)}\n.revoked{text-decoration:line-through;color:var(--faint)}\n'
JS = "(function(){\n  var btns=document.querySelectorAll('.tabs button');\n  var items=document.querySelectorAll('#list .issue');\n  btns.forEach(function(b){\n    b.addEventListener('click',function(){\n      var f=b.getAttribute('data-f');\n      btns.forEach(function(x){x.setAttribute('aria-pressed',x===b?'true':'false')});\n      items.forEach(function(it){\n        var g=it.getAttribute('data-g');\n        it.classList.toggle('hidden', !(f==='all'||f===g));\n      });\n    });\n  });\n})();\n"


GROUP = {store.AWAIT_CARD: "you", store.STUCK: "you", store.TAKEOVER: "you",
         store.KICKOFF: "run", store.EXECUTING: "run", store.VERIFYING: "run",
         store.DRAFT: "q", store.QUEUED: "q",
         store.MERGED: "ok", store.AWAIT_PROD: "ok", store.DONE: "ok",
         store.PAUSED: "off", store.HOLD: "off", store.SKIP: "off"}
GROUP_ORDER = {"you": 0, "run": 1, "q": 2, "ok": 3, "off": 4}
PILL = {store.DRAFT: "待 start", store.QUEUED: "排队", store.KICKOFF: "出方向卡", store.AWAIT_CARD: "等你确认方向卡",
        store.EXECUTING: "执行", store.VERIFYING: "验收", store.MERGED: "已合 staging", store.AWAIT_PROD: "等上 prod",
        store.DONE: "完成", store.PAUSED: "暂停", store.TAKEOVER: "你接管", store.STUCK: "卡住",
        store.HOLD: "暂缓", store.SKIP: "不派"}
KIND = {"start": "派发单", "card": "方向卡", "spend": "花钱放行", "prod": "上 prod", "handson": "待你动手",
        "stuck": "卡住", "conflict": "代码面冲突", "direction": "方向问题"}


def _e(x) -> str:
    return html.escape(str(x if x is not None else ""))


def _say(q) -> str:
    t, qid = q.get("ticket") or "", q["id"]
    return {"start": "start / start T1 T3",
            "card": f"{t} 同意 / {t} 改：…",
            "spend": f"放行 {qid} / 不放行 {qid}",
            "prod": "上 prod",
            "handson": f"{t} 走查过了",
            "stuck": f"{t} 续跑 / 我接管 {t}",
            "conflict": f"{qid}：按谁来",
            "direction": f"{qid}：…"}.get(q["kind"], qid)


def _hhmm(ts: str) -> str:
    return ts[5:16].replace("T", " ") if ts and len(ts) >= 16 else (ts or "")


def _ticket_card(t, evs) -> str:
    g = GROUP.get(t["status"], "off")
    why, sol = t.get("why", {}), t.get("solution", {})
    tags = [f'<span class="tagw">{"重" if t["weight"] == "heavy" else "轻"}</span>']
    if t.get("origin") == "user":
        tags.append('<span class="tagw">你提的</span>')
    if t.get("review_flag"):
        tags.append('<span class="pill p-review">需复核</span>')
    no = f"#{t['issue']}" if t.get("issue") else t["id"]
    wait = t.get("waiting_for") or ""
    if sol.get("kind") == "card":
        sol_cell = (f'<div class="cell sol"><h4>解法 · {_e(sol.get("source"))}</h4><p>{_e(sol.get("text"))}</p>'
                    f'<span class="src">{_e(t.get("card", {}).get("path", ""))}</span></div>')
    else:
        sol_cell = (f'<div class="cell sol pending"><h4>解法 · 方案待出</h4><p>{_e(sol.get("text") or "（无线索）")}</p>'
                    f'<span class="src">router 线索 · 非方案</span></div>')
    foot = [f'<span>占用：{_e(" · ".join(t["surfaces"]) or "无")}</span>',
            f'<span>依赖：{_e(" ".join(t["blocked_by"]) or "无")}</span>']
    subs = [f"代码面 {s}" for s in t["surfaces"]] + [f"标签 {x}" for x in t.get("tags", [])] + \
           [f"依赖 {b}" for b in t["blocked_by"]]
    foot.append(f'<span>订阅：{_e(" · ".join(subs) or "无")}</span>')
    w = t.get("worker", {})
    if w.get("session_id"):
        foot.append(f'<span>接管：<code>{_e("cd " + str(w.get("worktree")) + " && claude --resume " + w["session_id"])}</code></span>')
    if t.get("pr"):
        foot.append(f'<span>PR #{_e(t["pr"])}</span>')
    tl = "".join(f'<li><span class="ts">{_e(_hhmm(e.get("ts", "")))}</span>[{_e(e.get("phase") or e.get("type"))}] '
                 f'{_e(e.get("summary", ""))}</li>' for e in evs[-5:])
    return f"""<div class="issue" data-g="{g}">
  <div class="ih"><span class="no">{_e(no)} · {_e(t["id"])}</span><span class="t">{_e(t["title"])}</span>
    <span class="pill p-{g}">{_e(PILL.get(t["status"], t["status"]))}</span>{"".join(tags)}</div>
  <div class="wait">{"在等：<b>" + _e(wait) + "</b>" if wait else "&nbsp;"}</div>
  <div class="three">
    <div class="cell"><h4>为什么派</h4><p>{_e(why.get("metric"))} · {_e(why.get("users"))}</p><span class="src">{_e(why.get("evidence"))}</span></div>
    <div class="cell"><h4>解决什么问题</h4><p>{_e(t.get("problem"))}</p><span class="src"><code>{_e(t.get("problem_anchor"))}</code></span></div>
    {sol_cell}
  </div>
  <div class="foot">{"".join(foot)}</div>
  {"<ul class='tl'>" + tl + "</ul>" if tl else ""}
</div>"""


def _external_card(path, e) -> str:
    g = "run" if e.get("active") else "off"
    state = "活跃" if e.get("active") else ("闲置，仍持锁" if e.get("locking") else "闲置")
    return f"""<div class="issue" data-g="{g}">
  <div class="ih"><span class="no">{_e(e.get("branch"))}</span><span class="t">{_e(Path(path).name)}</span>
    <span class="pill p-{g}">{state}</span><span class="tagw">{"重" if e.get("weight") == "heavy" else "轻"}</span>
    <span class="tagw">你的 session</span></div>
  <div class="wait">ledger 更新：{_e(_hhmm(e.get("ledger_mtime") or e.get("last_activity") or ""))}</div>
  <div class="foot"><span>占用：{_e(" · ".join(e.get("surfaces", [])) or "无")}</span><span><code>{_e(path)}</code></span></div>
</div>"""


def render(state, decisions, events, now, slots=None) -> str:
    slots = slots or {"heavy": 3, "light": 2}
    tickets = state["tickets"]
    count = lambda sts: sum(1 for t in tickets.values() if t["status"] in sts)
    used = {"heavy": 0, "light": 0}
    for t in tickets.values():
        if t["status"] in store.RUNNING:
            used[t["weight"]] += 1
    active_ext = [e for e in state["external"].values() if e.get("active")]
    used["heavy"] += sum(1 for e in active_ext if e.get("weight") == "heavy")
    bar = lambda n, cap: "".join('<i class="on"></i>' if i < n else "<i></i>" for i in range(max(cap, n)))
    queue = sorted(state["queue"], key=lambda q: (-q["blocks"], int(q["id"][1:])))
    rows = "".join(f'<div class="row"><div class="what"><b>{_e(q["summary"])}</b>'
                   f'<small>{_e(q["id"])} · {_e(KIND.get(q["kind"], q["kind"]))} · 挡 {_e(q["blocks"])} 张 · {_e(_hhmm(q["created_at"]))}</small></div>'
                   f'<span class="say">{_e(_say(q))}</span></div>' for q in queue) or \
        '<div class="row"><div class="what"><small>没有要你做的事。</small></div></div>'
    order = sorted(tickets.values(), key=lambda t: (GROUP_ORDER[GROUP.get(t["status"], "off")], t["priority"], int(t["id"][1:])))
    cards = "".join(_ticket_card(t, events.get(t["id"], [])) for t in order)
    cards += "".join(_external_card(p, e) for p, e in sorted(state["external"].items()))
    groups = {}
    for t in tickets.values():
        groups[GROUP.get(t["status"], "off")] = groups.get(GROUP.get(t["status"], "off"), 0) + 1
    tab = lambda f, label: f'<button aria-pressed="{"true" if f == "all" else "false"}" data-f="{f}">{label}</button>'
    tabs = tab("all", f"全部 {len(tickets) + len(state['external'])}") + "".join(
        tab(f, f"{label} {groups.get(f, 0)}") for f, label in
        (("you", "等你"), ("run", "在跑"), ("q", "待 start / 排队"), ("ok", "已完成"), ("off", "暂缓 / 不派")))
    log = "".join(f'<div class="row"><span class="id">{_e(d["id"])}</span><span class="{"revoked" if d["revoked"] else ""}">'
                  f'{_e(d["ticket"])}：{_e(d["decision"])}<br><small>理由：{_e(d["why"])} · 依据：{_e(d["basis"])}</small></span>'
                  f'<span class="say">{"已撤回" if d["revoked"] else "推翻 " + _e(d["id"])}</span></div>' for d in decisions) or \
        '<div class="row"><span class="id">—</span><span><small>还没有替你做的决定。</small></span><span></span></div>'
    b = state.get("batch", {})
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="30">
<title>Router 看板</title>
<style>
{CSS}</style>
</head>
<body>
<main>
<header>
  <h1>Router 看板</h1>
  <span class="meta">批次：{_e(b.get("id") or "—")}（{"已 start" if b.get("started") else "未 start"}）· 更新于 {_e(now.strftime("%m-%d %H:%M"))} UTC · 只读，30 秒自动刷新</span>
</header>
<div class="stats">
  <div class="stat you"><b>{len(queue)}</b><span>等你</span></div>
  <div class="stat run"><b>{count(store.RUNNING) + len(active_ext)}</b><span>在跑</span></div>
  <div class="stat q"><b>{count({store.DRAFT, store.QUEUED})}</b><span>待 start / 排队</span></div>
  <div class="stat ok"><b>{count(store.SETTLED)}</b><span>已合 staging</span></div>
  <div class="stat"><b>{count({store.HOLD, store.SKIP, store.PAUSED})}</b><span>暂缓</span></div>
</div>
<div class="slots">
  重名额（编译 Rust）<span class="bar">{bar(used["heavy"], slots.get("heavy", 3))}</span>{used["heavy"]}/{slots.get("heavy", 3)}
  &nbsp;·&nbsp; 轻名额（数据 / 脚本 / 文案）<span class="bar">{bar(used["light"], slots.get("light", 2))}</span>{used["light"]}/{slots.get("light", 2)}
</div>
<h2>等你</h2>
<div class="decide">{rows}</div>
<h2>全部 issue</h2>
<div class="tabs" role="group" aria-label="筛选">{tabs}</div>
<div id="list">
{cards}
</div>
<h2>router 最近替你定的</h2>
<div class="log">{log}</div>
</main>
<script>
{JS}</script>
</body>
</html>
"""


def write(path, html_text) -> None:
    store._atomic_write(Path(path), html_text)
