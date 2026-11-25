"""relay.py 命令行：子进程跑；reply 用 LARK_RELAY_FAKE_HTTP 换假 http。夹具全是合成的。"""
import json
import os
import subprocess
import sys
import time

import pytest
from conftest import SCRIPTS

import store

CLI = SCRIPTS / "relay.py"


def run(*args, env=None):
    p = subprocess.run([sys.executable, str(CLI), *args], capture_output=True, text=True, timeout=30,
                       env={**os.environ, **(env or {})})
    lines = [json.loads(l) for l in p.stdout.splitlines() if l.strip()]
    return p.returncode, lines


def msg(mid, parent=None):
    return {"message_id": mid, "chat_id": "oc_p", "chat_type": "p2p", "sender_open_id": "ou_owner",
            "sender_type": "user", "create_time": "1700000000000", "root_id": parent, "parent_id": parent,
            "kind": "text", "text": "staging 上 placement 一直 401"}


@pytest.fixture
def fake_http(relay_home, tmp_path):
    store.home()
    (relay_home / "credentials.env").write_text("LARK_APP_ID=cli_x\nLARK_APP_SECRET=sec\n")
    d = tmp_path / "fake"
    d.mkdir()

    def setup(responses):
        (d / "responses.json").write_text(json.dumps(responses))
        return {"LARK_RELAY_FAKE_HTTP": str(d / "responses.json")}

    def calls():
        p = d / "calls.jsonl"
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []
    setup.calls = calls
    return setup


OK_REPLY = {"code": 0, "data": {"message_id": "om_bot1"}}


def reply_args(rid="R1", kind="result", title="小王：staging placement 401", lines=("结论：缺请求头",)):
    out = ["reply", rid, "--kind", kind, "--title", title]
    for l in lines:
        out += ["--line", l]
    return out


def test_reply_rejects_long_title(fake_http):
    store.append_message(msg("om_a"))
    env = fake_http([OK_REPLY])
    rc, out = run(*reply_args(title="一" * 21), env=env)
    assert rc == 2 and out[0]["ok"] is False
    rc, _ = run(*reply_args(title="a" * 41), env=env)
    assert rc == 2
    assert fake_http.calls() == []


def test_reply_rejects_internal_id(fake_http):
    store.append_message(msg("om_a"))
    env = fake_http([OK_REPLY, OK_REPLY])
    for bad in ("见 R12", "给R12的结果", "R3。"):
        rc, out = run(*reply_args(lines=(bad,)), env=env)
        assert rc == 2, bad
    rc, _ = run(*reply_args(title="R1 的结果"), env=env)
    assert rc == 2
    assert fake_http.calls() == []
    rc, out = run(*reply_args(lines=("DR12x 型号没货",)), env=env)
    assert rc == 0 and out[0]["ok"] is True


def test_reply_rejects_too_many_lines(fake_http):
    store.append_message(msg("om_a"))
    rc, _ = run(*reply_args(lines=[f"第{i}行" for i in range(6)]), env=fake_http([OK_REPLY]))
    assert rc == 2
    rc, _ = run(*reply_args(lines=()), env=fake_http([OK_REPLY]))
    assert rc == 2


def test_reply_rejects_unknown_kind_and_id(fake_http):
    store.append_message(msg("om_a"))
    env = fake_http([OK_REPLY])
    rc, _ = run(*reply_args(kind="done"), env=env)
    assert rc == 2
    rc, _ = run(*reply_args(rid="R9"), env=env)
    assert rc == 2


def test_reply_sends_prefixed_title_and_records_bot_msg(fake_http):
    store.append_message(msg("om_a"))
    rc, out = run(*reply_args(lines=("结论：缺请求头", "可直接转给小王：「加上就好」", "要你定：无")),
                  env=fake_http([OK_REPLY]))
    assert rc == 0 and out == [{"ok": True, "sent": "om_bot1"}]
    c = [x for x in fake_http.calls() if "reply" in x["url"]][0]
    assert c["url"].endswith("/open-apis/im/v1/messages/om_a/reply")
    post = json.loads(c["body"]["content"])["zh_cn"]
    assert post["title"] == "✅ 小王：staging placement 401"
    assert [p[0]["text"] for p in post["content"]] == ["结论：缺请求头", "可直接转给小王：「加上就好」", "要你定：无"]
    assert store.find_followup({"parent_id": "om_bot1"}) == "R1"


def test_reply_on_followup_records_on_root_task(fake_http):
    store.append_message(msg("om_a"))
    f = store.append_message(msg("om_f", parent="om_a"))
    assert f["id"] == "R2" and f["followup_of"] == "R1"
    rc, _ = run(*reply_args(rid="R2", kind="dispatch", lines=("交给 my-app-2e 处理",)), env=fake_http([OK_REPLY]))
    assert rc == 0
    c = [x for x in fake_http.calls() if "reply" in x["url"]][0]
    assert "/messages/om_f/reply" in c["url"]                    # 引用用户最近那条
    assert store.find_followup({"parent_id": "om_bot1"}) == "R1"   # 记在根任务上
    assert json.loads(c["body"]["content"])["zh_cn"]["title"].startswith("📨 ")


def test_reply_lark_error_exit2_no_record(fake_http):
    store.append_message(msg("om_a"))
    rc, out = run(*reply_args(), env=fake_http([{"code": 230002, "msg": "bot not in chat"}]))
    assert rc == 2 and out[0] == {"ok": False, "error": "lark 230002: bot not in chat"}
    assert (store.get_task("R1") or {}).get("bot_msgs", []) == []


def test_reply_with_images_uploads_then_posts(fake_http, tmp_path):
    store.append_message(msg("om_a"))
    a, b = tmp_path / "a.png", tmp_path / "b.jpg"
    a.write_bytes(b"\x89PNG a")
    b.write_bytes(b"\xff\xd8 b")
    env = fake_http([{"code": 0, "data": {"image_key": "img_a"}}, {"code": 0, "data": {"image_key": "img_b"}}, OK_REPLY])
    rc, out = run(*reply_args(), "--image", str(a), "--image", str(b), env=env)
    assert rc == 0 and out == [{"ok": True, "sent": "om_bot1"}]
    calls = fake_http.calls()
    assert [c["url"].split("/open-apis")[1] for c in calls] == ["/im/v1/images", "/im/v1/images",
                                                                 "/im/v1/messages/om_a/reply"]
    post = json.loads(calls[2]["body"]["content"])["zh_cn"]
    assert post["content"][-2:] == [[{"tag": "img", "image_key": "img_a"}], [{"tag": "img", "image_key": "img_b"}]]


def test_reply_forward_file_sends_full_text_after_post(fake_http, tmp_path):
    store.append_message(msg("om_a"))
    note = tmp_path / "note.txt"
    note.write_text("Hi team,\n" + "x" * 900 + "\nThanks!", encoding="utf-8")   # 对外长文：不受每行 50 字限制
    env = fake_http([OK_REPLY, {"code": 0, "data": {"message_id": "om_bot2"}}])
    rc, out = run(*reply_args(), "--forward-file", str(note), env=env)
    assert rc == 0 and out == [{"ok": True, "sent": "om_bot1", "forward_sent": "om_bot2"}]
    calls = fake_http.calls()
    assert [c["body"]["msg_type"] for c in calls] == ["post", "text"]
    assert json.loads(calls[1]["body"]["content"])["text"] == note.read_text(encoding="utf-8")


def test_reply_forward_file_rejects_bad_files(fake_http, tmp_path):
    store.append_message(msg("om_a"))
    env = fake_http([OK_REPLY, OK_REPLY])
    empty = tmp_path / "empty.txt"
    empty.write_text("  \n", encoding="utf-8")
    leak = tmp_path / "leak.txt"
    leak.write_text("see R12 for details", encoding="utf-8")
    huge = tmp_path / "huge.txt"
    huge.write_text("y" * 8001, encoding="utf-8")
    for f in (tmp_path / "missing.txt", empty, leak, huge):
        rc, out = run(*reply_args(), "--forward-file", str(f), env=env)
        assert rc != 0 and out[0]["ok"] is False, f
    assert fake_http.calls() == []                       # 校验在发之前，什么都没发出去


def test_reply_rejects_bad_images(fake_http, tmp_path):
    store.append_message(msg("om_a"))
    env = fake_http([OK_REPLY])
    txt = tmp_path / "x.txt"
    txt.write_text("no")
    big = tmp_path / "big.png"
    big.write_bytes(b"0" * (10 * 1024 * 1024 + 1))
    imgs = []
    for i in range(5):
        p = tmp_path / f"s{i}.png"
        p.write_bytes(b"\x89PNG")
        imgs += ["--image", str(p)]
    for extra in (["--image", str(tmp_path / "missing.png")], ["--image", str(txt)], ["--image", str(big)], imgs):
        rc, out = run(*reply_args(), *extra, env=env)
        assert rc == 2 and out[0]["ok"] is False, extra[:2]
    assert fake_http.calls() == []


def test_feed_once_prints_pending_only():
    store.append_message(msg("om_a"))
    store.append_message(msg("om_b"))
    store.ack(["R1"])
    rc, out = run("feed")
    assert rc == 0
    assert [(o["kind"], o["id"]) for o in out] == [("message", "R2")]
    assert out[0]["text"] == "staging 上 placement 一直 401"


def test_feed_follow_ends_and_heartbeats():
    store.heartbeat(store.LISTENER)
    store.append_message(msg("om_a"))
    rc, out = run("feed", "--follow", "--max-seconds", "2")
    assert rc == 0
    assert out[0]["id"] == "R1"
    assert out[-1] == {"kind": "feed_end"}
    assert not any(o["kind"] == "listener_down" for o in out)
    assert store.age(store.CONSUMER) < 5


def test_feed_follow_picks_up_new_messages():
    store.heartbeat(store.LISTENER)
    p = subprocess.Popen([sys.executable, str(CLI), "feed", "--follow", "--max-seconds", "4"],
                         stdout=subprocess.PIPE, text=True, env=dict(os.environ))
    time.sleep(1.5)
    store.append_message(msg("om_late"))
    out = [json.loads(l) for l in p.communicate(timeout=15)[0].splitlines() if l.strip()]
    assert [o.get("id") for o in out if o["kind"] == "message"] == ["R1"]
    assert out[-1] == {"kind": "feed_end"}


def test_feed_reports_listener_down_once():
    rc, out = run("feed", "--follow", "--max-seconds", "2.5")
    assert rc == 0
    assert [o["kind"] for o in out].count("listener_down") == 1


def test_status_reports_feed_consumer():
    rc, out = run("status")
    assert out[0]["feed"] == {"alive": False, "age_s": None}      # 没人在消费 feed
    store.heartbeat(store.CONSUMER)
    rc, out = run("status")
    assert out[0]["feed"]["alive"] is True and out[0]["feed"]["age_s"] < 5


def test_status_counts():
    store.heartbeat(store.LISTENER)
    store.append_message(msg("om_a"))
    store.append_message(msg("om_b"))
    store.set_task("R1", status="sent")
    store.set_task("R2", status="done")
    store.ack(["R2"])
    rc, out = run("status")
    s = out[0]
    assert rc == 0 and s["ok"] is True
    assert s["listener"]["alive"] is True and s["listener"]["age_s"] < 5
    assert s["owner_bound"] is False
    assert s["pending"] == 1
    assert s["tasks"] == {"queued": 0, "sent": 1, "done": 1, "stuck": 0}


def test_bind_flow():
    rc, _ = run("bind")
    assert rc == 0 and store.read_json(store.OWNER, {}) == {"pending": True}
    rc, _ = run("bind", "--confirm")
    assert rc == 2                                          # 还没人发消息过来
    store.write_json(store.OWNER, {"pending_confirm": "ou_owner", "chat_id": "oc_p"})
    rc, out = run("bind", "--show")
    assert out[0]["owner"] == {"pending_confirm": "ou_owner", "chat_id": "oc_p"}
    rc, _ = run("bind", "--confirm")
    o = store.read_json(store.OWNER, {})
    assert rc == 0 and (o["open_id"], o["chat_id"]) == ("ou_owner", "oc_p")
    assert run("status")[1][0]["owner_bound"] is True


def test_ack_and_task_commands():
    store.append_message(msg("om_a"))
    assert run("ack", "R1")[0] == 0
    assert run("ack", "R7")[0] == 2
    assert run("task", "set", "R1", "--title", "小王：401", "--target", "my-app-2e", "--status", "sent")[0] == 0
    assert run("task", "set", "R1", "--status", "weird")[0] == 2
    assert run("task", "get", "R1")[1][0]["task"]["target"] == "my-app-2e"
    assert run("task", "get", "R9")[0] == 2
    assert list(run("task", "list", "--open")[1][0]["tasks"]) == ["R1"]
    run("task", "set", "R1", "--status", "done")
    assert run("task", "list", "--open")[1][0]["tasks"] == {}


def test_task_get_reports_latest_entry_in_chain():
    store.append_message(msg("om_a"))
    store.append_message(msg("om_other"))
    store.append_message(msg("om_f", parent="om_a"))
    store.set_task("R1", title="小王：401")
    assert run("task", "get", "R1")[1][0]["latest"] == "R3"      # 结果回在用户最近那条追问上
    store.set_task("R2", title="别的事")
    assert run("task", "get", "R2")[1][0]["latest"] == "R2"


def test_dispatcher_name():
    assert run("dispatcher", "set", "my-app-3e")[0] == 0
    assert run("dispatcher", "get")[1][0]["name"] == "my-app-3e"


def test_task_set_on_followup_redirects_to_root():
    store.append_message(msg("om_a"))
    store.set_task("R1", title="小王：401", status="queued")
    store.append_message(msg("om_ans", parent="om_a"))           # 用户回答「交给谁」
    rc, out = run("task", "set", "R2", "--target", "my-app-2e", "--status", "sent")
    assert rc == 0 and out[0]["id"] == "R1"
    assert list(store.list_tasks()) == ["R1"]                    # 不拆成两件事
    assert store.get_task("R1")["status"] == "sent"


def test_task_get_includes_root_entry_and_redirects():
    store.append_message(msg("om_a"))
    store.set_task("R1", title="小王：401")
    store.append_message(msg("om_ans", parent="om_a"))
    rc, out = run("task", "get", "R2")
    assert rc == 0 and out[0]["id"] == "R1"
    assert out[0]["entry"]["text"] == "staging 上 placement 一直 401"    # 重启后也拿得到原问题
    assert out[0]["latest"] == "R2"


def test_bind_confirm_records_bound_at():
    store.write_json(store.OWNER, {"pending_confirm": "ou_owner", "chat_id": "oc_p"})
    before = int(time.time() * 1000)
    assert run("bind", "--confirm")[0] == 0
    assert store.read_json(store.OWNER, {})["bound_at"] >= before


FAKE_LAUNCHCTL = """#!/bin/sh
# 假 launchctl：bootout 之后服务还要「拆」两次 print 才消失；拆完之前 bootstrap 报 5
S="$FAKE_LAUNCHCTL_STATE"
echo "$1" >> "$S.calls"
n=$(cat "$S" 2>/dev/null || echo 0)
case "$1" in
  bootout) echo 2 > "$S"; exit 0 ;;
  print) if [ "$n" -gt 0 ]; then echo $((n-1)) > "$S"; exit 0; else exit 113; fi ;;
  bootstrap) if [ "$n" -gt 0 ]; then echo "Bootstrap failed: 5: Input/output error" >&2; exit 5; fi; exit 0 ;;
esac
"""


def test_listener_install_waits_for_bootout(relay_home, tmp_path):
    store.home()
    (relay_home / "venv" / "bin").mkdir(parents=True)
    (relay_home / "venv" / "bin" / "python").write_text("")
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "launchctl"
    fake.write_text(FAKE_LAUNCHCTL)
    fake.chmod(0o755)
    state = tmp_path / "lc_state"
    env = {"PATH": f"{bindir}:{os.environ['PATH']}", "HOME": str(tmp_path / "fakehome"),
           "FAKE_LAUNCHCTL_STATE": str(state)}
    rc, out = run("listener", "install", env=env)
    assert rc == 0, out
    calls = (tmp_path / "lc_state.calls").read_text().split()
    assert calls[0] == "bootout" and calls[-1] == "bootstrap"
    assert calls.count("bootstrap") == 1                       # 等旧服务拆完才 bootstrap，不靠撞运气
    assert (tmp_path / "fakehome" / "Library" / "LaunchAgents" / "com.lark-relay.listener.plist").exists()


def test_link_bare_answer_to_task(fake_http):
    store.append_message(msg("om_q"))
    store.set_task("R1", title="v20 下一步", status="done")
    store.append_message(msg("om_ans"))                          # 用户没点回复，直接发了新消息回答
    rc, out = run("link", "R2", "R1")
    assert rc == 0 and out[0] == {"ok": True, "id": "R2", "root": "R1"}
    assert store.root_of("R2") == "R1"
    got = run("task", "get", "R2")[1][0]
    assert (got["id"], got["latest"]) == ("R1", "R2")
    rc, _ = run(*reply_args(rid="R2", kind="dispatch", title="v20 下一步", lines=("转给 b9",)), env=fake_http([OK_REPLY]))
    assert rc == 0
    assert store.find_followup({"parent_id": "om_bot1"}) == "R1"   # 回复这条回执也算同一件事
    assert store.find_followup({"parent_id": "om_ans"}) == "R1"
    assert list(store.list_tasks()) == ["R1"]


def test_link_rejects_unknown_ids():
    store.append_message(msg("om_q"))
    assert run("link", "R2", "R1")[0] == 2
    assert run("link", "R1", "R9")[0] == 2
    assert run("link", "R1", "R1")[0] == 2


def test_reply_caps_line_length(fake_http):
    """用户 11-23：「你要不要精简一些」。每行 ≤ 50 字；「可直接转给」那行 ≤ 100 字（ASCII 算半个）。"""
    store.append_message(msg("om_a"))
    env = fake_http([OK_REPLY, OK_REPLY])
    rc, out = run(*reply_args(lines=("结论：" + "长" * 48,)), env=env)
    assert rc == 2 and "50" in out[0]["error"]
    rc, _ = run(*reply_args(lines=("可直接转给小王：「" + "长" * 100 + "」",)), env=env)
    assert rc == 2
    assert fake_http.calls() == []
    rc, _ = run(*reply_args(lines=("结论：" + "短" * 40, "可直接转给小王：「" + "a" * 160 + "」")), env=env)
    assert rc == 0


def test_task_list_open_skips_reply_only_records(fake_http):
    store.append_message(msg("om_a"))
    store.append_message(msg("om_b"))
    store.append_message(msg("om_c"))
    run("task", "set", "R1", "--title", "t", "--status", "sent")
    run("task", "set", "R2", "--title", "t", "--status", "done")
    rc, _ = run(*reply_args(rid="R3"), env=fake_http([OK_REPLY]))   # 直接答完的：只记了 bot_msgs，没有 status
    assert rc == 0
    rc, out = run("task", "list", "--open")
    assert rc == 0 and list(out[0]["tasks"]) == ["R1"]
