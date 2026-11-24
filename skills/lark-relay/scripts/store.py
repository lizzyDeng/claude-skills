"""运行时状态：全在 home（~/.config/lark-relay，LARK_RELAY_HOME 可覆盖）下的文件里，不靠 session 记忆。

  inbox.jsonl     机主转来的消息，一行一条（只追加）；id = R<n>，只在内部用，永远不出现在发给用户的消息里
  acks.jsonl      总管处理完的 id（只追加；幂等）
  tasks.json      R → {title, target, status, origin_msg, bot_msgs, updated_at}
  owner.json      机主：{"open_id", "chat_id"}；绑定中 {"pending": true} / {"pending_confirm", "chat_id"}
  listener.json   listener 心跳 {"heartbeat", "last_create_time"}
  consumer.json   feed --follow 的消费心跳（listener 据此判断总管在不在线）
  dispatcher.json 总管 session 在 ListAgents 里的名字

目录 700，文件 600。
"""
import fcntl
import json
import os
import re
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

INBOX = "inbox.jsonl"
ACKS = "acks.jsonl"
TASKS = "tasks.json"
OWNER = "owner.json"
LISTENER = "listener"
CONSUMER = "consumer"
DISPATCHER = "dispatcher.json"
LINKS = "links.json"      # 总管认定的「这条是那件事的回答」（用户没点回复、直接发新消息时）
CREDENTIALS = "credentials.env"
_LOCK = ".inbox.lock"
_STATE_LOCK = ".state.lock"   # tasks.json / <name>.json 的 read-modify-write
_RID = re.compile(r"^R(\d+)$")


def home() -> Path:
    h = Path(os.environ.get("LARK_RELAY_HOME") or Path.home() / ".config" / "lark-relay")
    h.mkdir(parents=True, exist_ok=True)
    os.chmod(h, 0o700)
    return h


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _private(path: Path) -> None:
    os.chmod(path, 0o600)


@contextmanager
def _locked(lock: str = _LOCK):
    """fcntl 文件锁：跨进程（listener / relay CLI）也跨线程（每次新开 fd）。不要嵌套拿同一把。"""
    with open(home() / lock, "a") as f:
        _private(Path(f.name))
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def load_credentials() -> tuple:
    p = home() / CREDENTIALS
    env = {}
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    aid, sec = env.get("LARK_APP_ID", ""), env.get("LARK_APP_SECRET", "")
    if not (aid and sec):
        raise ValueError("credentials missing")
    return aid, sec


def read_json(name: str, default):
    p = home() / name
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return default


def write_json(name: str, obj) -> None:
    p = home() / name
    fd, tmp = tempfile.mkstemp(dir=p.parent, prefix=f".{p.name}.", suffix=".tmp")   # 每次独立的临时文件：并发写不撞
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False, indent=1))
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _read_lines(path: Path) -> list:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue                                    # 半截行 / 坏行：跳过，不挡后面的
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _append_line(path: Path, obj: dict) -> None:
    new = not path.exists()
    with path.open("a+", encoding="utf-8") as f:
        f.seek(0, os.SEEK_END)
        if f.tell() > 0:
            f.seek(f.tell() - 1)
            if f.read(1) != "\n":
                f.write("\n")                           # 上次写到一半：先断开，别和新行粘在一起
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")
        f.flush()
    if new:
        _private(path)


def read_inbox() -> list:
    return [e for e in _read_lines(home() / INBOX) if _RID.match(str(e.get("id", "")))]


def _acked() -> set:
    return {a.get("id") for a in _read_lines(home() / ACKS)}


def pending() -> list:
    done = _acked()
    return [e for e in read_inbox() if e["id"] not in done]


def ack(ids: list) -> None:
    done = _acked()
    for i in ids:
        if i not in done:
            _append_line(home() / ACKS, {"id": i, "at": _now_iso()})
            done.add(i)


def list_tasks() -> dict:
    t = read_json(TASKS, {})
    return t if isinstance(t, dict) else {}


def get_task(rid: str):
    return list_tasks().get(rid)


def set_task(rid: str, **fields) -> dict:
    with _locked(_STATE_LOCK):
        return _set_task(rid, **fields)


def _set_task(rid: str, **fields) -> dict:
    tasks = list_tasks()
    t = tasks.get(rid)
    if t is None:
        origin = next((e.get("message_id") for e in read_inbox() if e["id"] == rid), None)
        t = {"origin_msg": origin, "bot_msgs": []}
    t.update({k: v for k, v in fields.items() if v is not None})
    t["updated_at"] = _now_iso()
    tasks[rid] = t
    write_json(TASKS, tasks)
    return t


def add_bot_msg(rid: str, message_id: str) -> None:
    with _locked(_STATE_LOCK):
        t = get_task(rid) or _set_task(rid)
        msgs = list(t.get("bot_msgs") or [])
        if message_id not in msgs:
            msgs.append(message_id)
        _set_task(rid, bot_msgs=msgs)


def has_message(message_id: str) -> bool:
    return any(e.get("message_id") == message_id for e in read_inbox())


def root_of(rid: str):
    """追问 → 它追问的那件事（根）；根就是自己；不认识的 id → None。"""
    e = next((e for e in read_inbox() if e["id"] == rid), None)
    if e is None:
        return None
    return read_json(LINKS, {}).get(rid) or e.get("followup_of") or e["id"]


def link(rid: str, root: str) -> str:
    """把 rid 认作 root 那件事的追问。返回真正的根（root 本身是追问时落到它的根）。"""
    if root_of(rid) is None:
        raise ValueError(f"unknown id {rid}")
    r = root_of(root)
    if r is None:
        raise ValueError(f"unknown id {root}")
    if r == rid:
        raise ValueError("cannot link an item to itself")
    with _locked(_STATE_LOCK):
        links = read_json(LINKS, {})
        links[rid] = r
        write_json(LINKS, links)
    return r


def find_followup(msg: dict):
    inbox = read_inbox()
    by_mid = {e.get("message_id"): e for e in inbox}
    tasks = list_tasks()
    for ref in (msg.get("parent_id"), msg.get("root_id")):
        if not ref:
            continue
        e = by_mid.get(ref)
        if e is not None:
            return root_of(e["id"])
        for rid, t in tasks.items():
            if ref in (t.get("bot_msgs") or []):
                return rid
    return None


def append_message(msg: dict):
    with _locked():
        inbox = read_inbox()
        if any(e.get("message_id") == msg.get("message_id") for e in inbox):
            return None
        n = max((int(_RID.match(e["id"]).group(1)) for e in inbox), default=0) + 1
        entry = dict(msg)
        entry["id"] = f"R{n}"
        entry["at"] = _now_iso()
        entry["followup_of"] = find_followup(msg)
        _append_line(home() / INBOX, entry)
        return entry


def heartbeat(name: str, **extra) -> None:
    with _locked(_STATE_LOCK):
        cur = read_json(f"{name}.json", {})
        cur = cur if isinstance(cur, dict) else {}
        cur.update(extra)
        cur["heartbeat"] = time.time()
        write_json(f"{name}.json", cur)


def age(name: str):
    hb = read_json(f"{name}.json", {})
    ts = hb.get("heartbeat") if isinstance(hb, dict) else None
    return None if ts is None else max(0.0, time.time() - float(ts))
