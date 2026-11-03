"""选出这一轮该派的票。纯函数：只读 state，不改。"""
import store
import surfaces


def _num(tid: str) -> int:
    return int(tid[1:])


def free_slots(state: dict, slots: dict) -> dict:
    """各档剩余名额（可能为负）：在跑的票占名额，等用户回答的不占；带未核实意图的票按「可能在跑」占；
    用户自己开着、正在动的重 session 也算。"""
    free = dict(slots)
    for t in state["tickets"].values():
        if store.occupies_slot(t):
            free[t["weight"]] = free.get(t["weight"], 0) - 1
    for ext in state["external"].values():
        if ext.get("active") and ext.get("weight") == "heavy":
            free["heavy"] = free.get("heavy", 0) - 1
    return free


def _dispatchable(t) -> bool:
    """派发失败已进决定单（等你回答）、或上次派到一半待对账（意图还在）的票，这一轮不派。"""
    return not (t.get("dispatch_fail") or {}).get("qid") and (t.get("dispatch") or {}).get("phase") != "spawning"


def pick_dispatch(state: dict, slots: dict, heavy_blocked: bool = False) -> list:
    tickets = state["tickets"]
    free = free_slots(state, slots)
    occupied = set(surfaces.held_locks(state))
    candidates = [t for t in tickets.values()
                  if t["status"] == store.QUEUED and t["start_ok"] and _dispatchable(t)
                  and all(tickets.get(b, {}).get("status") in store.SETTLED for b in t["blocked_by"])]
    candidates.sort(key=lambda t: (t["priority"], _num(t["id"])))

    picked = []
    for t in candidates:
        w = t["weight"]
        if (w == "heavy" and heavy_blocked) or free.get(w, 0) <= 0 or occupied & set(t["surfaces"]):
            continue
        picked.append(t["id"])
        free[w] -= 1
        occupied |= set(t["surfaces"])
    return picked
