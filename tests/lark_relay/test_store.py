"""store：落盘、编号、去重、ack、任务、心跳。夹具全是合成的。"""
import json
import stat

import pytest

import store


def msg(mid, parent=None, root=None, text="hello"):
    return {"message_id": mid, "chat_id": "oc_test", "chat_type": "p2p", "sender_open_id": "ou_owner",
            "sender_type": "user", "create_time": "1700000000000", "root_id": root, "parent_id": parent,
            "kind": "text", "text": text}


def test_append_numbers_and_dedupes():
    a = store.append_message(msg("om_a"))
    b = store.append_message(msg("om_b"))
    assert (a["id"], b["id"]) == ("R1", "R2")
    assert a["at"].endswith("Z")
    assert store.append_message(msg("om_a")) is None
    assert [e["id"] for e in store.read_inbox()] == ["R1", "R2"]
    assert len((store.home() / store.INBOX).read_text().splitlines()) == 2


def test_files_are_private():
    store.append_message(msg("om_a"))
    store.set_task("R1", title="t")
    h = store.home()
    assert stat.S_IMODE(h.stat().st_mode) == 0o700
    assert stat.S_IMODE((h / store.INBOX).stat().st_mode) == 0o600
    assert stat.S_IMODE((h / store.TASKS).stat().st_mode) == 0o600


def test_half_line_skipped():
    store.append_message(msg("om_a"))
    with (store.home() / store.INBOX).open("a") as f:
        f.write('{"id": "R9"')                       # 进程写到一半崩了
    assert [e["id"] for e in store.read_inbox()] == ["R1"]
    b = store.append_message(msg("om_b"))
    assert b["id"] == "R2"
    assert [e["id"] for e in store.read_inbox()] == ["R1", "R2"]


def test_ack_idempotent_and_pending():
    store.append_message(msg("om_a"))
    store.append_message(msg("om_b"))
    store.ack(["R1"])
    store.ack(["R1"])
    assert [e["id"] for e in store.pending()] == ["R2"]
    store.ack(["R2", "R1"])
    assert store.pending() == []


def test_followup_by_inbox_and_bot_msg():
    store.append_message(msg("om_a"))
    f1 = store.append_message(msg("om_f1", parent="om_a", root="om_a"))
    assert f1["followup_of"] == "R1"
    store.add_bot_msg("R1", "om_bot")
    f2 = store.append_message(msg("om_f2", parent="om_bot", root="om_a"))
    assert f2["followup_of"] == "R1"
    f3 = store.append_message(msg("om_f3", parent="om_f2"))      # 追问的追问
    assert f3["followup_of"] == "R1"
    fresh = store.append_message(msg("om_new"))
    assert fresh["followup_of"] is None


def test_followup_by_root_when_parent_unknown():
    store.append_message(msg("om_a"))
    f = store.append_message(msg("om_x", parent="om_unknown", root="om_a"))
    assert f["followup_of"] == "R1"


def test_set_task_records_origin():
    store.append_message(msg("om_a"))
    t = store.set_task("R1", title="小王：staging 401", target="my-app-2e", status="sent")
    assert t["origin_msg"] == "om_a"
    assert store.get_task("R1")["status"] == "sent"
    store.set_task("R1", status="done")
    assert store.get_task("R1")["title"] == "小王：staging 401"
    assert list(store.list_tasks()) == ["R1"]
    assert store.get_task("R404") is None


def test_heartbeat_and_age():
    assert store.age(store.CONSUMER) is None
    store.heartbeat(store.LISTENER, last_create_time=5)
    assert 0 <= store.age(store.LISTENER) < 5
    assert store.read_json("listener.json", {})["last_create_time"] == 5


def test_load_credentials_missing(relay_home):
    with pytest.raises(ValueError, match="credentials missing"):
        store.load_credentials()
    store.home()
    (relay_home / "credentials.env").write_text("LARK_APP_ID=cli_x\nLARK_APP_SECRET=\n")
    with pytest.raises(ValueError, match="credentials missing"):
        store.load_credentials()
    (relay_home / "credentials.env").write_text("LARK_APP_ID=cli_x\nLARK_APP_SECRET='s3'\n")
    assert store.load_credentials() == ("cli_x", "s3")


def test_write_json_roundtrip():
    store.write_json("owner.json", {"pending": True})
    assert store.read_json("owner.json", {}) == {"pending": True}
    assert store.read_json("missing.json", {"d": 1}) == {"d": 1}
    assert json.loads((store.home() / "owner.json").read_text()) == {"pending": True}


def test_concurrent_writers_do_not_collide():
    import threading
    store.append_message(msg("om_a"))
    store.heartbeat(store.LISTENER, pid=1)
    store.set_task("R1", status="queued")
    errors = []

    def guard(fn):
        def run():
            try:
                fn()
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))
        return run

    def hb():
        for _ in range(150):
            store.heartbeat(store.LISTENER, pid=1)

    def tk():
        for i in range(150):
            store.set_task("R1", status="sent" if i % 2 else "queued")

    def bot():
        for i in range(50):
            store.add_bot_msg("R1", f"om_b{i}")

    def rd():
        for _ in range(300):
            for name in ("listener.json", store.TASKS):
                json.loads((store.home() / name).read_text())      # 不许读到半截

    ts = [threading.Thread(target=guard(f)) for f in (hb, hb, tk, tk, bot, rd)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert errors == []
    assert len(store.get_task("R1")["bot_msgs"]) == 50                 # read-modify-write 不丢更新
    assert store.read_json("listener.json", {})["pid"] == 1


def test_has_message():
    store.append_message(msg("om_a"))
    assert store.has_message("om_a") and not store.has_message("om_b")


def test_root_of():
    store.append_message(msg("om_a"))
    store.append_message(msg("om_f", parent="om_a"))
    assert store.root_of("R2") == "R1" and store.root_of("R1") == "R1"
    assert store.root_of("R9") is None
