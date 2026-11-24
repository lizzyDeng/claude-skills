#!/usr/bin/env python3
"""常驻监听（launchd 拉起，KeepAlive）：长连接收机器人私聊，只认机主，落进 inbox.jsonl。

- 非私聊、非真人（机器人自己、应用）→ 丢；没绑机主或不是机主 → 丢。都不回复，日志只记 message_id。
- 绑定中（owner.json pending）：第一个发消息的人记成 pending_confirm，回一句让机主去终端确认。
- 合并转发：拉子消息 + 发言人名字（通讯录权限没开就写「某人」）。
- 总管不在线（feed --follow 的消费心跳过期）：引用原消息回一句「已存下」。
- 补齐：启动时、长连接重连后、之后每 5 分钟，拉私聊里「绑定时刻 / 24 小时前」较晚者之后的消息，按 message_id 去重。
  launchd 的进程在 Mac 睡眠时是挂起不是重启，SDK 自己重连不会补，所以不能只在启动时补。
- 看门狗：心跳只在长连接真的连着时写；断开超过 5 分钟就退出，让 launchd 重启（重启也会补齐）。

日志只写 message_id / kind / 结果，不写正文、token、secret。lark-oapi 只在 run_ws 里 import。
"""
import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import lark_api  # noqa: E402
import parse  # noqa: E402
import store  # noqa: E402

STALE_S = 180
HEARTBEAT_S = 10
BACKFILL_EVERY_S = 300
BACKFILL_WINDOW_MS = 24 * 3600 * 1000
DISCONNECT_EXIT_S = 300
OFFLINE_TEXT = "已存下，总管不在线，回来后处理"
BIND_TEXT = "收到绑定请求，请在终端运行 relay.py bind --confirm"


def log(event: str, **fields) -> None:
    d = store.home() / "logs"
    d.mkdir(exist_ok=True)
    os.chmod(d, 0o700)
    p = d / "listener.log"
    new = not p.exists()
    line = {"at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "event": event, **fields}
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")
    if new:
        os.chmod(p, 0o600)


def _reply(api, message_id: str, text: str) -> None:
    try:
        api.reply_text(message_id, text)
    except lark_api.LarkError as e:
        log("reply_failed", message_id=message_id, code=e.code)


_backfill_lock = threading.Lock()


def _expand_merged(msg: dict, api) -> dict:
    try:
        items = api.merged_items(msg["message_id"])
    except lark_api.LarkError as e:
        log("merged_failed", message_id=msg["message_id"], code=e.code)
        return {**msg, "items": [], "text": msg["text"] + "（子消息拉取失败）"}
    names = api.user_names(sorted({i["sender_open_id"] for i in items if i.get("sender_open_id")}))
    return {**msg, "items": [{"sender": names.get(i.get("sender_open_id")) or "某人", "text": i["text"]} for i in items]}


def _outcome(msg: dict, result: str) -> str:
    log("handle", message_id=msg.get("message_id"), kind=msg.get("kind"), outcome=result)
    return result


def handle(msg: dict, api) -> str:
    if msg.get("chat_type") != "p2p" or msg.get("sender_type") != "user":
        return _outcome(msg, "dropped_not_p2p")
    owner = store.read_json(store.OWNER, {})
    if owner.get("pending"):
        store.write_json(store.OWNER, {"pending_confirm": msg["sender_open_id"], "chat_id": msg.get("chat_id")})
        _reply(api, msg["message_id"], BIND_TEXT)
        return _outcome(msg, "bind_pending_confirm")
    if not owner.get("open_id") or owner["open_id"] != msg.get("sender_open_id"):
        return _outcome(msg, "dropped_not_owner")
    if store.has_message(msg.get("message_id")):
        return _outcome(msg, "duplicate")                 # 先去重：补齐会反复看到已经存过的消息
    if msg.get("kind") == "merge_forward":
        msg = _expand_merged(msg, api)
    entry = store.append_message(msg)
    if entry is None:
        return _outcome(msg, "duplicate")
    age = store.age(store.CONSUMER)
    if age is None or age > STALE_S:
        _reply(api, msg["message_id"], OFFLINE_TEXT)
        return _outcome(msg, "stored_offline_notified")
    return _outcome(msg, "stored")


def backfill(api, now_ms=None) -> int:
    """补齐漏掉的消息。同一时刻只跑一份（重连回调和定时器可能撞上），撞上了就跳过。"""
    owner = store.read_json(store.OWNER, {})
    if not owner.get("open_id") or not owner.get("chat_id"):
        return 0
    if not _backfill_lock.acquire(blocking=False):
        return 0
    try:
        now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
        start = max(int(owner.get("bound_at") or 0), now_ms - BACKFILL_WINDOW_MS)
        try:
            listed = api.list_chat_messages(owner["chat_id"], start)
        except lark_api.LarkError as e:
            log("backfill_failed", code=e.code)
            return 0
        n = 0
        for m in listed:
            try:
                if handle(m, api) in ("stored", "stored_offline_notified"):
                    n += 1
            except Exception as e:  # noqa: BLE001  —— 一条坏的不挡后面的，也不让进程崩
                log("handle_error", message_id=m.get("message_id"), error=type(e).__name__)
        if n:
            log("backfill", stored=n)
        return n
    finally:
        _backfill_lock.release()


def watchdog_step(state: dict, connected: bool, now: float) -> str:
    """连着 → heartbeat；断开不到 5 分钟 → wait；断开满 5 分钟 → exit（交给 launchd 重启）。"""
    if connected:
        state.pop("down_since", None)
        return "heartbeat"
    since = state.setdefault("down_since", now)
    return "exit" if now - since >= DISCONNECT_EXIT_S else "wait"


def _supervise(api, is_connected) -> None:
    state = {}
    last_backfill = time.time()                          # main 启动时已经补过一次
    while True:
        try:
            action = watchdog_step(state, is_connected(), time.time())
            if action == "heartbeat":
                store.heartbeat(store.LISTENER, pid=os.getpid())
            elif action == "exit":
                log("watchdog_exit")
                os._exit(1)
            if time.time() - last_backfill >= BACKFILL_EVERY_S:
                last_backfill = time.time()
                backfill(api)
        except Exception as e:  # noqa: BLE001  —— 看门狗自己不能死
            log("supervise_error", error=type(e).__name__)
        time.sleep(HEARTBEAT_S)


def run_ws(app_id: str, app_secret: str, api) -> None:
    import lark_oapi as lark

    def on_message(data) -> None:
        try:
            d = json.loads(lark.JSON.marshal(data))
            handle(parse.normalize(d.get("event") or {}), api)
        except Exception as e:  # noqa: BLE001  —— 一条坏消息不能把长连接带挂
            log("handle_error", error=type(e).__name__)

    handler = lark.EventDispatcherHandler.builder("", "").register_p2_im_message_receive_v1(on_message).build()
    # WARNING：INFO 会打带鉴权参数的连接 URL，DEBUG 会打消息正文
    client = lark.ws.Client(app_id, app_secret, event_handler=handler, domain=lark.LARK_DOMAIN,
                            log_level=lark.LogLevel.WARNING)

    def on_reconnecting() -> None:
        log("ws_reconnecting")

    def on_reconnected() -> None:
        log("ws_reconnected")
        threading.Thread(target=backfill, args=(api,), daemon=True).start()

    client.on_reconnecting = on_reconnecting
    client.on_reconnected = on_reconnected
    # 连没连着：看 SDK 的 _conn（lark-oapi 1.8.0 断开时置 None）。升级 SDK 时要复核
    threading.Thread(target=_supervise, args=(api, lambda: client._conn is not None), daemon=True).start()
    log("ws_start")
    client.start()


def main() -> int:
    app_id, secret = store.load_credentials()
    api = lark_api.Lark(app_id, secret)
    backfill(api)
    run_ws(app_id, secret, api)
    return 0


if __name__ == "__main__":
    sys.exit(main())
