"""listener.handle / backfill：假 api 对象。夹具全是合成的。"""
import pytest

import lark_api
import listener
import store

OFFLINE = "已存下，总管不在线，回来后处理"


class FakeApi:
    def __init__(self, merged=None, names=None, merged_error=False, listed=None, list_error=False):
        self.replies = []
        self.merged = merged or []
        self.names = names or {}
        self.merged_error = merged_error
        self.listed = listed or []
        self.list_error = list_error
        self.list_calls = []
        self.merged_calls = 0

    def reply_text(self, message_id, text):
        self.replies.append((message_id, text))
        return f"om_bot{len(self.replies)}"

    def merged_items(self, message_id):
        self.merged_calls += 1
        if self.merged_error == "boom":
            raise RuntimeError("unexpected")
        if self.merged_error:
            raise lark_api.LarkError(99991672, "no permission")
        return self.merged

    def user_names(self, ids):
        return {i: n for i, n in self.names.items() if i in ids}

    def list_chat_messages(self, chat_id, start_ms):
        self.list_calls.append((chat_id, start_ms))
        if self.list_error:
            raise lark_api.LarkError(99991672, "no permission")
        return self.listed


def m(mid, sender="ou_owner", chat_type="p2p", kind="text", text="帮看下 staging 401", sender_type="user",
      create_time="1700000000000"):
    return {"message_id": mid, "chat_id": "oc_p", "chat_type": chat_type, "sender_open_id": sender,
            "sender_type": sender_type, "create_time": create_time, "root_id": None, "parent_id": None,
            "kind": kind, "text": text}


NOW_MS = 1700000000000 + 3600_000


@pytest.fixture
def owner():
    store.write_json(store.OWNER, {"open_id": "ou_owner", "chat_id": "oc_p", "bound_at": 1700000000000 - 60_000})


@pytest.fixture
def online():
    store.heartbeat(store.CONSUMER)


def test_group_message_dropped_silently(owner, online):
    api = FakeApi()
    assert listener.handle(m("om_g", chat_type="group"), api) == "dropped_not_p2p"
    assert api.replies == [] and store.read_inbox() == []


def test_bot_or_app_sender_dropped(owner, online):
    api = FakeApi()
    assert listener.handle(m("om_x", sender_type="app"), api) == "dropped_not_p2p"
    assert store.read_inbox() == []


def test_stranger_dropped_silently(owner, online):
    api = FakeApi()
    assert listener.handle(m("om_s", sender="ou_stranger"), api) == "dropped_not_owner"
    assert api.replies == [] and store.read_inbox() == []


def test_unbound_drops_everything(online):
    api = FakeApi()
    assert listener.handle(m("om_s"), api) == "dropped_not_owner"
    assert api.replies == [] and store.read_inbox() == []


def test_bind_records_pending_confirm():
    store.write_json(store.OWNER, {"pending": True})
    api = FakeApi()
    assert listener.handle(m("om_b", sender="ou_me"), api) == "bind_pending_confirm"
    assert store.read_json(store.OWNER, {}) == {"pending_confirm": "ou_me", "chat_id": "oc_p"}
    assert api.replies == [("om_b", "收到绑定请求，请在终端运行 relay.py bind --confirm")]
    assert store.read_inbox() == []


def test_owner_message_stored(owner, online):
    api = FakeApi()
    assert listener.handle(m("om_1"), api) == "stored"
    [e] = store.read_inbox()
    assert (e["id"], e["text"]) == ("R1", "帮看下 staging 401")


def test_duplicate_event(owner, online):
    api = FakeApi()
    assert listener.handle(m("om_1"), api) == "stored"
    assert listener.handle(m("om_1"), api) == "duplicate"
    assert len(store.read_inbox()) == 1


def test_offline_notice_when_consumer_stale(owner):
    api = FakeApi()
    assert listener.handle(m("om_1"), api) == "stored_offline_notified"
    assert api.replies == [("om_1", OFFLINE)]
    store.heartbeat(store.CONSUMER)
    assert listener.handle(m("om_2"), api) == "stored"
    assert len(api.replies) == 1


def test_merge_forward_expanded_with_names(owner, online):
    api = FakeApi(merged=[{"sender_open_id": "ou_w", "text": "401 了"}, {"sender_open_id": "ou_z", "text": "急"}],
                  names={"ou_w": "小王"})
    assert listener.handle(m("om_m", kind="merge_forward", text="[合并转发]"), api) == "stored"
    [e] = store.read_inbox()
    assert e["items"] == [{"sender": "小王", "text": "401 了"}, {"sender": "某人", "text": "急"}]
    assert e["text"] == "[合并转发]"


def test_merge_forward_fetch_failure_marked(owner, online):
    api = FakeApi(merged_error=True)
    assert listener.handle(m("om_m", kind="merge_forward", text="[合并转发]"), api) == "stored"
    [e] = store.read_inbox()
    assert e["items"] == [] and e["text"] == "[合并转发]（子消息拉取失败）"


def test_backfill_skips_already_stored(owner, online):
    listener.handle(m("om_1", create_time="1700000000000"), FakeApi())
    api = FakeApi(listed=[m("om_1", create_time="1700000000000"), m("om_2", create_time="1700000005000"),
                          m("om_bot", sender="ou_app", sender_type="app")])
    assert listener.backfill(api, now_ms=NOW_MS) == 1
    assert [e["message_id"] for e in store.read_inbox()] == ["om_1", "om_2"]


def test_backfill_window_starts_at_bind_or_24h(owner):
    api = FakeApi()
    listener.backfill(api, now_ms=NOW_MS)
    assert api.list_calls == [("oc_p", 1700000000000 - 60_000)]          # 绑定之前的不补（绑定那句不算）
    later = NOW_MS + 3 * 86400_000
    listener.backfill(api, now_ms=later)
    assert api.list_calls[-1] == ("oc_p", later - 86400_000)             # 最多往回看 24 小时


def test_backfill_needs_bound_owner():
    api = FakeApi()
    assert listener.backfill(api, now_ms=NOW_MS) == 0
    store.write_json(store.OWNER, {"pending": True})
    assert listener.backfill(api, now_ms=NOW_MS) == 0 and api.list_calls == []


def test_backfill_permission_error_returns_zero(owner, online):
    listener.handle(m("om_1"), FakeApi())
    assert listener.backfill(FakeApi(list_error=True), now_ms=NOW_MS) == 0


def test_backfill_survives_bad_message(owner, online):
    api = FakeApi(merged_error="boom",
                  listed=[m("om_bad", kind="merge_forward", text="[合并转发]"), m("om_ok")])
    assert listener.backfill(api, now_ms=NOW_MS) == 1                      # 一条坏的不挡后面的，也不让进程崩
    assert [e["message_id"] for e in store.read_inbox()] == ["om_ok"]


def test_duplicate_merge_forward_not_reexpanded(owner, online):
    api = FakeApi(merged=[{"sender_open_id": "ou_w", "text": "x"}])
    listener.handle(m("om_m", kind="merge_forward", text="[合并转发]"), api)
    assert listener.handle(m("om_m", kind="merge_forward", text="[合并转发]"), api) == "duplicate"
    assert api.merged_calls == 1                                         # 补齐每 5 分钟一次，不能每次重拉子消息


def test_watchdog_step():
    st = {}
    assert listener.watchdog_step(st, True, 1000.0) == "heartbeat"
    assert listener.watchdog_step(st, False, 1010.0) == "wait"
    assert listener.watchdog_step(st, False, 1010.0 + 299) == "wait"
    assert listener.watchdog_step(st, True, 1400.0) == "heartbeat"      # 连回来了：重新计时
    assert listener.watchdog_step(st, False, 1500.0) == "wait"
    assert listener.watchdog_step(st, False, 1800.0) == "exit"          # 断了 5 分钟：退出让 launchd 重启


def test_log_has_no_message_text(owner, online):
    listener.handle(m("om_1", text="机密正文xyz-42"), FakeApi())
    listener.handle(m("om_s", sender="ou_stranger", text="陌生人正文abc-7"), FakeApi())
    log = (store.home() / "logs" / "listener.log").read_text()
    assert "om_1" in log and "stored" in log
    assert "机密正文xyz-42" not in log and "陌生人正文abc-7" not in log
