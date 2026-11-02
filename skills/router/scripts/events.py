"""worker 往上抛的事件、进度、收件箱，以及按「代码面 + 标签 + 依赖」的订阅路由。

文件布局（dir = router 状态目录）：
  events/<T>.jsonl   worker 追加；事件和 progress 混写，一行一条
  inbox/<T>.jsonl    router 投递给 worker；worker 每个检查点读一次
游标按字节偏移存在 state["cursors"]，router 重启后不会重复投递。
"""
import json
from datetime import datetime, timezone
from pathlib import Path

import store

TYPES = {"decision", "assumption_broken", "overreach", "interface_changed", "finding"}
STOP_TYPES = {"decision", "assumption_broken", "overreach"}
PHASES = {"card_ready", "impl_done", "pr_opened"}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _append(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:     # 单行 O_APPEND，worker 与 router 并发也不交错
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def emit(dir: Path, ticket: str, type: str, summary: str, evidence: str,
         surfaces=(), tags=(), tickets=(), *, at: str = None) -> dict:
    if type not in TYPES:
        raise ValueError(f"未知事件类型 {type}；只有 {sorted(TYPES)}")
    if len(summary.strip().splitlines()) > 3:
        raise ValueError("摘要最多 3 行")
    ev = {"ts": at or _now(), "ticket": ticket, "type": type, "summary": summary.strip(),
          "evidence": evidence,
          "affects": {"surfaces": list(surfaces), "tags": list(tags), "tickets": list(tickets)},
          "stop": type in STOP_TYPES}
    _append(Path(dir) / "events" / f"{ticket}.jsonl", ev)
    return ev


def emit_progress(dir: Path, ticket: str, phase: str, at: str = None) -> dict:
    if phase not in PHASES:
        raise ValueError(f"未知进度 {phase}；只有 {sorted(PHASES)}")
    ev = {"ts": at or _now(), "ticket": ticket, "type": "progress", "phase": phase}
    _append(Path(dir) / "events" / f"{ticket}.jsonl", ev)
    return ev


def read_all(dir: Path, ticket: str) -> list:
    """不动游标的全量读（tick 判 worker 退出原因、看板时间线用）。"""
    items, _ = _read_from(Path(dir) / "events" / f"{ticket}.jsonl", 0)
    return items


def _read_from(path: Path, offset: int):
    if not path.exists():
        return [], offset
    data = path.read_bytes()
    end = data.rfind(b"\n") + 1                      # 最后一行没写完（没有换行）就留到下次
    if end <= offset:
        return [], offset
    items = []
    for line in data[offset:end].splitlines():
        if not line.strip():
            continue
        try:
            items.append(json.loads(line))
        except ValueError:
            items.append({"type": "malformed", "raw": line.decode("utf-8", "replace")})
    return items, end


def read_new(dir, state, ticket) -> list:
    cur = state["cursors"]["events"]
    items, cur[ticket] = _read_from(Path(dir) / "events" / f"{ticket}.jsonl", cur.get(ticket, 0))
    return items


def deliver(dir, to: str, event) -> None:
    _append(Path(dir) / "inbox" / f"{to}.jsonl", event)


def read_inbox(dir, state, ticket) -> list:
    cur = state["cursors"]["inbox"]
    items, cur[ticket] = _read_from(Path(dir) / "inbox" / f"{ticket}.jsonl", cur.get(ticket, 0))
    return items


def route(state, event):
    """→ (订阅方票号, 订阅方外部 worktree, 是否命中快路径)。"""
    sender = event.get("ticket")
    aff = event.get("affects", {})
    surf = set(aff.get("surfaces", []))
    if not surf and event.get("type") == "interface_changed" and sender in state["tickets"]:
        surf = set(state["tickets"][sender]["surfaces"])   # 改接口没写影响面 → 默认是自己的代码面
    tags, named = set(aff.get("tags", [])), set(aff.get("tickets", []))
    gone = store.SETTLED | {store.SKIP}
    hits = [tid for tid, t in state["tickets"].items()
            if tid != sender and t["status"] not in gone
            and (surf & set(t["surfaces"]) or tags & set(t["tags"])
                 or sender in t["blocked_by"] or tid in named)]
    hits.sort(key=lambda x: int(x[1:]))
    exts = sorted(p for p, e in state["external"].items()
                  if e.get("locking", True) and surf & set(e.get("surfaces", [])))
    return hits, exts, bool(hits or exts)


def mark_review(state, tids) -> None:
    for tid in tids:
        state["tickets"][tid]["review_flag"] = True
