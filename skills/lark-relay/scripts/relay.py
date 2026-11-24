#!/usr/bin/env python3
"""lark-relay 命令行：给总管 session 用。每个子命令打一行 JSON（feed 每条一行）。失败 {"ok": false} + 退出码 2。

  bind [--confirm | --show]       绑定机主
  status                          listener 在不在线、机主绑没绑、待处理条数、任务数
  feed [--follow]                 没 ack 的消息；--follow 挂在 Monitor 上持续输出
  ack <R>...                      处理完
  link <R> <根>                   用户没点回复、直接发新消息回答时，把这条认作那件事的追问
  reply <R> --kind K --title T --line L...   引用那条消息回复机主
  task set|get|list               R → 目标 session / 状态
  dispatcher set|get              总管 session 的名字
  listener install|uninstall|logs launchd 常驻
"""
import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import lark_api  # noqa: E402
import store  # noqa: E402

STALE_S = 180
PREFIX = {"dispatch": "📨", "result": "✅", "stuck": "⚠️", "ask": "❓"}
STATUSES = ("queued", "sent", "done", "stuck")
TITLE_MAX = 20
LINES_MAX = 5
LINE_MAX = 50              # 用户 11-23「你要不要精简一些」：每行一句话
FORWARD_LINE_MAX = 100     # 「可直接转给…」那行要能直接转发，放宽一些
IMAGES_MAX = 4
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
IMAGE_BYTES_MAX = 10 * 1024 * 1024     # Lark 上传图片上限
# 内部编号不许出现在发给用户的文字里。不用 \b：中文字符也算 \w，「给R12」会漏掉
INTERNAL_ID = re.compile(r"(?<![A-Za-z0-9])R\d+(?![A-Za-z0-9])")
LABEL = "com.lark-relay.listener"


class Fail(Exception):
    pass


def out(obj) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def width(s: str) -> float:
    """字数：ASCII 算半个字，其余（中文、全角标点）算一个。"""
    return sum(0.5 if ord(ch) < 128 else 1 for ch in s)


class _FakeHttp:
    """测试用：按顺序返回 responses 文件里的应答（token 请求自动应答），请求追加到同目录 calls.jsonl。"""

    def __init__(self, path: str):
        self.path = Path(path)
        self.calls = self.path.with_name("calls.jsonl")

    def __call__(self, method, url, headers, body):
        if url.split("?")[0].endswith("/tenant_access_token/internal"):
            return {"code": 0, "tenant_access_token": "fake", "expire": 7200}
        rec = {"bytes": len(body)} if isinstance(body, bytes) else body
        with self.calls.open("a") as f:
            f.write(json.dumps({"method": method, "url": url, "body": rec}, ensure_ascii=False) + "\n")
        n = len(self.calls.read_text().splitlines()) - 1
        return json.loads(self.path.read_text())[n]


def make_api() -> lark_api.Lark:
    try:
        app_id, secret = store.load_credentials()
    except ValueError as e:
        raise Fail(str(e))
    fake = os.environ.get("LARK_RELAY_FAKE_HTTP")
    return lark_api.Lark(app_id, secret, http=_FakeHttp(fake) if fake else None)


def _entry(rid: str) -> dict:
    e = next((e for e in store.read_inbox() if e["id"] == rid), None)
    if e is None:
        raise Fail(f"unknown id {rid}")
    return e


# ---- 子命令 ----

def cmd_bind(a) -> None:
    owner = store.read_json(store.OWNER, {})
    if a.show:
        return out({"ok": True, "owner": owner})
    if a.confirm:
        if not owner.get("pending_confirm"):
            raise Fail("no pending bind: run `relay.py bind`, then send the bot a message")
        store.write_json(store.OWNER, {"open_id": owner["pending_confirm"], "chat_id": owner.get("chat_id"),
                                       "bound_at": int(time.time() * 1000)})   # 补齐不往绑定之前看
        return out({"ok": True, "owner": store.read_json(store.OWNER, {})})
    store.write_json(store.OWNER, {"pending": True})
    out({"ok": True, "next": "在 Lark 上给机器人私聊发一句话，然后运行 relay.py bind --show 和 bind --confirm"})


def cmd_status(a) -> None:
    age = store.age(store.LISTENER)
    counts = {s: 0 for s in STATUSES}
    for t in store.list_tasks().values():
        if t.get("status") in counts:
            counts[t["status"]] += 1
    out({"ok": True,
         "listener": {"alive": age is not None and age <= STALE_S, "age_s": None if age is None else round(age, 1)},
         "owner_bound": bool(store.read_json(store.OWNER, {}).get("open_id")),
         "pending": len(store.pending()),
         "tasks": counts})


def cmd_feed(a) -> None:
    shown = set()

    def emit_new():
        for e in store.pending():
            if e["id"] not in shown:
                shown.add(e["id"])
                out({**e, "msg_type": e.get("kind"), "kind": "message"})   # inbox 的 kind 是消息类型，改名 msg_type

    emit_new()
    if not a.follow:
        return
    deadline = time.time() + a.max_seconds
    down_reported = False
    while time.time() < deadline:
        store.heartbeat(store.CONSUMER)
        emit_new()
        age = store.age(store.LISTENER)
        down = age is None or age > STALE_S
        if down and not down_reported:
            out({"kind": "listener_down"})
        down_reported = down
        time.sleep(min(1.0, max(0.0, deadline - time.time())))
    out({"kind": "feed_end"})


def cmd_ack(a) -> None:
    known = {e["id"] for e in store.read_inbox()}
    unknown = [i for i in a.ids if i not in known]
    if unknown:
        raise Fail(f"unknown id {', '.join(unknown)}")
    store.ack(a.ids)
    out({"ok": True, "acked": a.ids})


def cmd_reply(a) -> None:
    if a.kind not in PREFIX:
        raise Fail(f"kind must be one of {', '.join(PREFIX)}")
    title = a.title.strip()
    lines = [l for l in (a.line or []) if l.strip()]
    if not title or width(title) > TITLE_MAX:
        raise Fail(f"title must be 1-{TITLE_MAX} 字 (ASCII 算半个), got {width(title)}")
    if not 1 <= len(lines) <= LINES_MAX:
        raise Fail(f"need 1-{LINES_MAX} lines, got {len(lines)}")
    for l in lines:
        cap = FORWARD_LINE_MAX if l.startswith("可直接转给") else LINE_MAX
        if width(l) > cap:
            raise Fail(f"line too long ({width(l)} > {cap} 字): 精简成一句话，细节等用户追问再给")
    for s in [title, *lines]:
        if INTERNAL_ID.search(s):
            raise Fail(f"internal id must not reach the user: {INTERNAL_ID.search(s).group(0)}")
    images = a.image or []
    if len(images) > IMAGES_MAX:
        raise Fail(f"at most {IMAGES_MAX} images, got {len(images)}")
    for p in images:
        f = Path(p).expanduser()
        if f.suffix.lower() not in IMAGE_EXT:
            raise Fail(f"image must be {'/'.join(sorted(IMAGE_EXT))}: {p}")
        if not f.is_file():
            raise Fail(f"image not found: {p}")
        if f.stat().st_size > IMAGE_BYTES_MAX:
            raise Fail(f"image over 10 MB (Lark limit): {p}")
    e = _entry(a.rid)
    api = make_api()
    try:
        keys = [api.upload_image(str(Path(p).expanduser())) for p in images]
        sent = api.reply_post(e["message_id"], f"{PREFIX[a.kind]} {title}", lines, image_keys=keys)
    except lark_api.LarkError as err:
        raise Fail(str(err))
    store.add_bot_msg(store.root_of(e["id"]), sent)     # 记在根任务上：回复它也算追问这件事
    out({"ok": True, "sent": sent})


def cmd_task(a) -> None:
    if a.op in ("set", "get"):
        root = store.root_of(a.rid)                       # 追问的 id 一律落到它追问的那件事上，不拆成两件
        if root is None:
            raise Fail(f"unknown id {a.rid}")
    if a.op == "set":
        if a.status and a.status not in STATUSES:
            raise Fail(f"status must be one of {', '.join(STATUSES)}")
        t = store.set_task(root, title=a.title, target=a.target, status=a.status)
        return out({"ok": True, "id": root, "task": t})
    if a.op == "get":
        t = store.get_task(root)
        if t is None:
            raise Fail(f"no task {root}")
        inbox = store.read_inbox()
        chain = [e for e in inbox if store.root_of(e["id"]) == root]
        return out({"ok": True, "id": root, "task": t, "entry": chain[0],
                    "latest": chain[-1]["id"]})           # entry = 原问题（重启后也拿得到）；latest = 回复挂在最近那条上
    tasks = store.list_tasks()
    if a.open:
        # 只算真正在办的：直接答完的那种只记了 bot_msgs、没有 status，不算
        tasks = {k: v for k, v in tasks.items() if v.get("status") in ("queued", "sent", "stuck")}
    out({"ok": True, "tasks": tasks})


def cmd_link(a) -> None:
    try:
        root = store.link(a.rid, a.root)
    except ValueError as e:
        raise Fail(str(e))
    out({"ok": True, "id": a.rid, "root": root})


def cmd_dispatcher(a) -> None:
    if a.op == "set":
        store.write_json(store.DISPATCHER, {"name": a.name})
    out({"ok": True, "name": store.read_json(store.DISPATCHER, {}).get("name")})


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _bootout_and_wait(uid: int, timeout_s: float = 15.0) -> None:
    """bootout 是异步的：旧服务没拆完就 bootstrap 会报 5（Input/output error）。等 print 找不到它再返回。"""
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"], capture_output=True)
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if subprocess.run(["launchctl", "print", f"gui/{uid}/{LABEL}"], capture_output=True).returncode != 0:
            return
        time.sleep(0.2)


def cmd_listener(a) -> None:
    h = store.home()
    uid = os.getuid()
    if a.op == "install":
        py = h / "venv" / "bin" / "python"
        if not py.exists():
            raise Fail(f"missing {py}: python3 -m venv {h / 'venv'} && {py} -m pip install lark-oapi==1.8.0")
        (h / "logs").mkdir(exist_ok=True)
        os.chmod(h / "logs", 0o700)
        script = Path(__file__).resolve().with_name("listener.py")
        plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LABEL}</string>
  <key>ProgramArguments</key><array><string>{py}</string><string>-I</string><string>{script}</string></array>
  <key>EnvironmentVariables</key><dict><key>LARK_RELAY_HOME</key><string>{h}</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>Umask</key><integer>63</integer>
  <key>StandardOutPath</key><string>{h / 'logs' / 'launchd.out.log'}</string>
  <key>StandardErrorPath</key><string>{h / 'logs' / 'launchd.err.log'}</string>
</dict></plist>
"""
        p = _plist_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        _bootout_and_wait(uid)
        p.write_text(plist)
        r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(p)], capture_output=True, text=True)
        if r.returncode != 0:
            raise Fail(f"launchctl bootstrap failed: {r.stderr.strip()}")
        return out({"ok": True, "plist": str(p)})
    if a.op == "uninstall":
        _bootout_and_wait(uid)
        _plist_path().unlink(missing_ok=True)
        return out({"ok": True})
    logs = []
    for name in ("listener.log", "launchd.err.log"):
        p = h / "logs" / name
        if p.exists():
            logs += [f"[{name}] {l}" for l in p.read_text(encoding="utf-8", errors="replace").splitlines()[-50:]]
    out({"ok": True, "logs": logs})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="relay.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("bind")
    b.add_argument("--confirm", action="store_true")
    b.add_argument("--show", action="store_true")
    sub.add_parser("status")
    f = sub.add_parser("feed")
    f.add_argument("--follow", action="store_true")
    f.add_argument("--max-seconds", type=float, default=1740)
    ln = sub.add_parser("link")
    ln.add_argument("rid")
    ln.add_argument("root")
    k = sub.add_parser("ack")
    k.add_argument("ids", nargs="+")
    r = sub.add_parser("reply")
    r.add_argument("rid")
    r.add_argument("--kind", required=True)
    r.add_argument("--title", required=True)
    r.add_argument("--line", action="append")
    r.add_argument("--image", action="append", help="png/jpg/gif/webp, ≤ 10 MB, 最多 4 张，排在文字后面")
    t = sub.add_parser("task")
    tsub = t.add_subparsers(dest="op", required=True)
    ts = tsub.add_parser("set")
    ts.add_argument("rid")
    ts.add_argument("--title")
    ts.add_argument("--target")
    ts.add_argument("--status")
    tg = tsub.add_parser("get")
    tg.add_argument("rid")
    tl = tsub.add_parser("list")
    tl.add_argument("--open", action="store_true")
    d = sub.add_parser("dispatcher")
    dsub = d.add_subparsers(dest="op", required=True)
    dsub.add_parser("set").add_argument("name")
    dsub.add_parser("get")
    li = sub.add_parser("listener")
    li.add_argument("op", choices=("install", "uninstall", "logs"))
    a = ap.parse_args(argv)
    handlers = {"bind": cmd_bind, "status": cmd_status, "feed": cmd_feed, "ack": cmd_ack, "reply": cmd_reply,
                "task": cmd_task, "dispatcher": cmd_dispatcher, "link": cmd_link, "listener": cmd_listener}
    try:
        handlers[a.cmd](a)
    except Fail as e:
        out({"ok": False, "error": str(e)})
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
