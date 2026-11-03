"""Lark 提醒。签名同 ~/.her-persona-daily/weekly.py:post：
key = f"{timestamp}\\n{secret}"，HMAC-SHA256 签空消息体，再 base64。

🔴 只写件数和编号，不写为什么派 / 问题描述 / 用户原话（spec §5.1）。调用方负责只传这些。
"""
import base64
import hashlib
import hmac
import json
from pathlib import Path

import store


def send(lark_cfg: Path, text: str, ctx) -> str:
    """→ "sent" | "DRY RUN: …"（没有配置文件）| "failed: …"。
    「配置了路径但文件不在」算不算失败由调用方定（flush 按失败处理，见 H5）。"""
    p = Path(lark_cfg).expanduser() if lark_cfg else None
    if not p or not p.is_file():
        return f"DRY RUN: {text}"
    try:
        cfg = json.loads(p.read_text(encoding="utf-8"))
        url = cfg["webhook_url"]
    except (OSError, ValueError, KeyError, TypeError) as e:     # 配置坏了 ≠ 没配置：按失败处理，下一轮重试
        return f"failed: Lark 配置读不了（{p}）：{e!r}"
    payload = {"msg_type": "text", "content": {"text": text}}
    if cfg.get("sign_secret"):
        ts = str(int(ctx.now().timestamp()))
        key = f"{ts}\n{cfg['sign_secret']}".encode()
        payload.update(timestamp=ts, sign=base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode())
    try:
        body = ctx.post_json(url, payload)
    except Exception as e:  # 网络 / webhook 失效：提醒失败不能拖垮巡检
        return f"failed: {e}"
    code = body.get("code", body.get("StatusCode", 0))
    return "sent" if code == 0 else f"failed: {body}"


def flush(state: dict, lark_cfg, ctx, now: str):
    """决定单的通知 outbox：把 notified_at 为 None 的项一次推出去（只写件数和编号）→ send 的结果；没有待推的 → None。
    - 老 state 里没有 notified_at 键的项算已通知（升级后不补发）
    - 送达（sent）或未配置 Lark（lark_cfg 为空：开发态 DRY RUN）⇒ 给这一批写 notified_at，清 notify_error
    - 失败（网络 / webhook 报错 / 配置了路径但文件不在 / 配置坏了）⇒ 不写 notified_at，notify_error 计数，下一轮 tick 重发
    投递语义是「至少一次」：推出去了、存盘前崩溃，下一轮会再推一次。"""
    pending = [q for q in state.get("queue", []) if q.get("notified_at", "legacy") is None]
    if not pending:
        return None
    view = store.queue_view(state)
    text = f"router：等你 {len(view)} 件（{' '.join(q['id'] for q in view)}）"
    if lark_cfg and not Path(lark_cfg).expanduser().is_file():
        out = f"failed: 配置了 Lark（{lark_cfg}），但文件不存在"
    else:
        out = send(lark_cfg or None, text, ctx)
    if out == "sent" or out.startswith("DRY RUN"):
        for q in pending:
            q["notified_at"] = now
        state.pop("notify_error", None)
    else:
        prev = state.get("notify_error") or {}
        state["notify_error"] = {"at": now, "error": out[:200], "count": prev.get("count", 0) + 1}
    return out
