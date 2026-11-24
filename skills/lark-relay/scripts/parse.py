"""把 Lark 的两种消息形状统一成一个 dict：

- 长连接事件 im.message.receive_v1 的 event 部分（message.message_type / message.content / sender.sender_id.open_id）
- 「获取会话历史消息」接口的一项（msg_type / body.content / sender.id），用于补齐

统一后的键：message_id, chat_id, chat_type, sender_open_id, sender_type, create_time, root_id, parent_id, kind, text
"""
import json

_LOCALES = ("zh_cn", "en_us", "ja_jp")


def _mention_names(mentions) -> dict:
    return {m.get("key"): m.get("name") for m in (mentions or []) if m.get("key") and m.get("name")}


def _post_lines(obj: dict, names: dict) -> list:
    for loc in _LOCALES:                                 # 有的形状外面包一层语言
        if isinstance(obj.get(loc), dict) and "content" in obj[loc]:
            obj = obj[loc]
            break
    lines = []
    if obj.get("title"):
        lines.append(obj["title"])
    for para in obj.get("content") or []:
        parts = []
        for el in para or []:
            tag = el.get("tag")
            if tag == "text":
                parts.append(el.get("text", ""))
            elif tag == "a":
                parts.append(f"{el.get('text', '')}({el.get('href', '')})")
            elif tag == "at":
                parts.append("@" + (el.get("user_name") or names.get(el.get("user_id")) or el.get("user_id", "")))
            elif tag == "img":
                parts.append("[图片]")
            elif tag == "emotion":
                parts.append("[表情]")
            elif tag == "media":
                parts.append("[视频]")
            elif "text" in el:
                parts.append(str(el["text"]))
        lines.append("".join(parts))
    return lines


def content_text(msg_type: str, content: str, mentions=None) -> str:
    try:
        obj = json.loads(content)
    except (TypeError, ValueError):
        return "[无法解析]"
    if not isinstance(obj, dict):
        return "[无法解析]"
    names = _mention_names(mentions)
    if msg_type == "text":
        text = obj.get("text", "")
        for key, name in names.items():
            text = text.replace(key, "@" + name)
        return text
    if msg_type == "post":
        return "\n".join(_post_lines(obj, names))
    if msg_type == "image":
        return "[图片]"
    if msg_type == "file":
        return f"[文件 {obj.get('file_name', '')}]".replace(" ]", "]")
    if msg_type == "merge_forward":
        return "[合并转发]"
    return f"[{msg_type}]"


def normalize(event: dict) -> dict:
    m = event.get("message") or {}
    s = event.get("sender") or {}
    kind = m.get("message_type") or ""
    return {
        "message_id": m.get("message_id"),
        "chat_id": m.get("chat_id"),
        "chat_type": m.get("chat_type"),
        "sender_open_id": (s.get("sender_id") or {}).get("open_id"),
        "sender_type": s.get("sender_type"),
        "create_time": m.get("create_time"),
        "root_id": m.get("root_id") or None,
        "parent_id": m.get("parent_id") or None,
        "kind": kind,
        "text": content_text(kind, m.get("content") or "{}", m.get("mentions")),
    }


def normalize_listed(item: dict) -> dict:
    s = item.get("sender") or {}
    kind = item.get("msg_type") or ""
    return {
        "message_id": item.get("message_id"),
        "chat_id": item.get("chat_id"),
        "chat_type": "p2p",
        "sender_open_id": s.get("id") if s.get("id_type", "open_id") == "open_id" else None,
        "sender_type": s.get("sender_type"),
        "create_time": item.get("create_time"),
        "root_id": item.get("root_id") or None,
        "parent_id": item.get("parent_id") or None,
        "kind": kind,
        "text": content_text(kind, (item.get("body") or {}).get("content") or "{}", item.get("mentions")),
    }
