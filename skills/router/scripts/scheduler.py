"""选出这一轮该派的票。纯函数：只读 state，不改。"""
import store
import surfaces


def _num(tid: str) -> int:
    return int(tid[1:])


def pick_dispatch(state: dict, slots: dict, heavy_blocked: bool = False) -> list:
    tickets = state["tickets"]
    free = dict(slots)
    for t in tickets.values():                       # 在跑的票占名额；等用户回答的不占
        if t["status"] in store.RUNNING:
            free[t["weight"]] = free.get(t["weight"], 0) - 1
    for ext in state["external"].values():           # 用户自己开着、正在动的重 session 也算
        if ext.get("active") and ext.get("weight") == "heavy":
            free["heavy"] = free.get("heavy", 0) - 1

    occupied = set(surfaces.held_locks(state))
    candidates = [t for t in tickets.values()
                  if t["status"] == store.QUEUED and t["start_ok"]
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
