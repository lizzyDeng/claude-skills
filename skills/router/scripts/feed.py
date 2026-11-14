"""事件流：巡检交给 router 的唤醒项，逐条写进 <home>/feed.jsonl（只追加）；router 处理完 ack。

  feed.jsonl      一行一条 {"id": "E<n>", "key": <自然 id>, "kind": <唤醒键>, "at", "item"}
  feed.ack.jsonl  一行一条 {"id": "E<n>", "at"}（只追加；ack 幂等）

- 谁跑巡检都写（daemon / tick / watch 都走 tick.run）：只交一次的唤醒项不会因为某个调用方没处理就丢。
- 同一件事只写一次：key 用它们自然的 id（决定单项号、票号 + 尝试号、检查点、run 号……），已写过的 key 不再写。
- router 用 Claude Code 的 Monitor 挂 `router.py feed --follow`：每行 stdout = 一条通知（逐行 flush）；
  启动时先补发全部没 ack 的；超过 30 分钟没 ack 的再提醒一次。
"""
import fcntl
import hashlib
import json
import time
from contextlib import contextmanager
from pathlib import Path

FEED = "feed.jsonl"
ACKS = "feed.ack.jsonl"
LOCK = ".feed.lock"
REMIND_S = 30 * 60
SLOW = "slow_path_events"


@contextmanager
def _locked(home):
    """feed 自己的锁（不是 state 的锁）：ack 不必等一整轮巡检跑完。"""
    Path(home).mkdir(parents=True, exist_ok=True)
    with open(Path(home) / LOCK, "a") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def _read(path: Path) -> list:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue                                   # 半截行 / 坏行：跳过，不挡后面的
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _append_lines(path: Path, objs) -> None:
    if not objs:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write("".join(json.dumps(o, ensure_ascii=False) + "\n" for o in objs))
        f.flush()


def read_events(home) -> list:
    return _read(Path(home) / FEED)


def acked_ids(home) -> set:
    return {a.get("id") for a in _read(Path(home) / ACKS)}


def unacked(home) -> list:
    done = acked_ids(home)
    return [e for e in read_events(home) if e.get("id") not in done]


def append_many(home, entries, at) -> list:
    """entries = [(key, kind, item)]：key 没写过的才写，编号 E<n> 接着已有的排。→ 新写的编号。"""
    with _locked(home):
        evs = read_events(home)
        seen = {e.get("key") for e in evs}
        n = max([int(str(e.get("id", "E0"))[1:]) for e in evs if str(e.get("id", "")).startswith("E")
                 and str(e.get("id"))[1:].isdigit()] or [0])
        new = []
        for key, kind, item in entries:
            if key in seen:
                continue
            seen.add(key)
            n += 1
            new.append({"id": f"E{n}", "key": key, "kind": kind, "at": at, "item": item})
        _append_lines(Path(home) / FEED, new)
    return [e["id"] for e in new]


def append(home, key, kind, item, at):
    ids = append_many(home, [(key, kind, item)], at)
    return ids[0] if ids else None


def ack(home, ids, at) -> dict:
    """幂等：已 ack 的报 already；不认识的报 unknown（不写）。编号和 key 都认。"""
    with _locked(home):
        evs = read_events(home)
        by = {e.get("id"): e.get("id") for e in evs}
        by.update({e.get("key"): e.get("id") for e in evs})
        done = acked_ids(home)
        acked, already, unknown, new = [], [], [], []
        for x in ids:
            eid = by.get(x)
            if eid is None:
                unknown.append(x)
            elif eid in done or eid in acked:
                already.append(eid)
            else:
                acked.append(eid)
                new.append({"id": eid, "at": at})
        _append_lines(Path(home) / ACKS, new)
    return {"acked": acked, "already": already, "unknown": unknown}


def ack_where(home, pred, at) -> list:
    """把满足 pred(event) 的、还没 ack 的全部 ack（ack-slow / ack --all 用）。→ ack 掉的编号。"""
    ids = [e["id"] for e in unacked(home) if pred(e)]
    return ack(home, ids, at)["acked"] if ids else []


# ---- tick 结果 → 事件 -------------------------------------------------------------------------------

def _h(x) -> str:
    return hashlib.sha1(json.dumps(x, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()[:10]


def slow_key(ev) -> str:
    ev = ev if isinstance(ev, dict) else {}
    return f"{SLOW}:{ev.get('ticket')}:{ev.get('ts')}:{_h(ev.get('summary'))}"


def _last_status_at(t, status) -> str:
    return next((h.get("at") for h in reversed((t or {}).get("history") or []) if h.get("status") == status), "") or ""


def _key(kind, item, state) -> str:
    """唤醒项的自然 id：同一件事在不同轮次、不同调用方交出来，key 一样。"""
    tickets = state.get("tickets") or {}
    tid = item if isinstance(item, str) else (item.get("ticket") if isinstance(item, dict) else None)
    t = tickets.get(tid) if isinstance(tid, str) else None
    if kind == "new_queue":
        return f"new_queue:{item.get('qid') if isinstance(item, dict) else item}"
    if kind == SLOW:
        return slow_key(item)
    if kind == "external_deliveries" and isinstance(item, dict):
        return f"external_deliveries:{_h(item.get('worktree'))}:{slow_key(item.get('event'))}"
    if kind == "cards_for_review":
        return f"cards_for_review:{tid}:{(t or {}).get('card_ready_at') or (t or {}).get('kickoff_at') or ''}"
    if kind == "postlaunch_due" and isinstance(item, dict):
        return f"postlaunch_due:{tid}:{item.get('check')}:{((t or {}).get('postlaunch') or {}).get('t0', '')}"
    if kind in ("no_entry", "return_to_user") and isinstance(item, dict) and item.get("attempt") is not None:
        return f"{kind}:{tid}:a{item['attempt']}"
    if kind == "rollback_needed" and isinstance(item, dict):
        harmful = [a.get("n") for a in (t or {}).get("attempts") or [] if a.get("verdict") == "harmful"]
        return f"rollback_needed:{tid}:a{max(harmful)}" if harmful else f"rollback_needed:{tid}:{_h(item.get('rollback'))}"
    if kind == "analyst_done" and isinstance(item, dict):
        return f"analyst_done:{item['run']}" if item.get("run") else \
            f"analyst_done:{item.get('mode')}:{_h(item.get('error'))}"
    if kind == "prod_ready" and isinstance(item, dict):
        return f"prod_ready:{tid}:{_last_status_at(t, 'await_prod')}"
    return f"{kind}:{_h(item)}"


def tick_entries(state, res, wake_keys) -> list:
    """tick 结果里叫醒 router 的那些键 → [(key, kind, item)]，按 wake_keys 的顺序、每项一条。"""
    out = []
    queue = {q.get("id"): q for q in state.get("queue") or []}
    for kind in wake_keys:
        v = res.get(kind)
        if not v:
            continue
        for item in v if isinstance(v, list) else [v]:
            if kind == "new_queue" and isinstance(item, str):
                q = queue.get(item) or {}
                item = {"qid": item, **{k: q[k] for k in ("ticket", "kind", "topic", "summary") if q.get(k) is not None}}
            elif kind == "cards_for_review" and isinstance(item, str):
                item = {"ticket": item}
            out.append((_key(kind, item, state), kind, item))
    return out


def record_tick(home, state, res, wake_keys, at) -> list:
    return append_many(home, tick_entries(state, res, wake_keys), at)


def prune_slow_path(home, state) -> None:
    """state 里存着的慢路径事件，feed 上已经 ack 的清掉（ack 不拿 state 的锁，清理放在下一轮巡检里做）。"""
    sp = state.get("slow_path") or []
    if not sp:
        return
    done = acked_ids(home)
    acked = {e.get("key") for e in read_events(home) if e.get("kind") == SLOW and e.get("id") in done}
    if acked:
        state["slow_path"] = [e for e in sp if slow_key(e) not in acked]


# ---- feed：一行一个 JSON -------------------------------------------------------------------------

def line(ev, remind=False) -> str:
    out = {"id": ev.get("id"), "kind": ev.get("kind"), "at": ev.get("at"), "item": ev.get("item"), "key": ev.get("key")}
    if remind:
        out["remind"] = True
    return json.dumps(out, ensure_ascii=False)


def follow(home, write, *, clock=time.time, sleep=time.sleep, interval=2.0, max_s=REMIND_S, remind_s=REMIND_S,
           daemon_check=None, check_every=30.0) -> str:
    """先补发全部没 ack 的，然后每 interval 秒看一次：新事件立刻发；发出去 remind_s 秒还没 ack 的再发一次（只一次）。
    daemon_check() → None（在跑）| {"summary": …}（没在跑）：没在跑时发一条 daemon_down（同一段停机只发一次）。
    到 max_s 发一条 feed_end 退出（Monitor 到期前后都会走到这里，router 重挂即可）。write 每次一整行。"""
    shown, reminded = {}, set()
    start, last_check, down_sent = clock(), None, False
    while True:
        now = clock()
        for ev in unacked(home):
            eid = ev.get("id")
            if eid not in shown:
                write(line(ev))
                shown[eid] = now
            elif eid not in reminded and now - shown[eid] >= remind_s:
                write(line(ev, remind=True))
                reminded.add(eid)
        if daemon_check is not None and (last_check is None or now - last_check >= check_every):
            last_check = now
            down = daemon_check()
            if down and not down_sent:
                write(json.dumps({"id": None, "kind": "daemon_down", **down}, ensure_ascii=False))
            down_sent = bool(down)
        if now - start >= max_s:
            write(json.dumps({"id": None, "kind": "feed_end", "reason": "max_minutes",
                              "summary": "feed 到期退出：重挂 router.py feed --follow"}, ensure_ascii=False))
            return "max_minutes"
        sleep(interval)
