"""Lark 提醒。签名同 ~/.her-persona-daily/weekly.py:post：
key = f"{timestamp}\\n{secret}"，HMAC-SHA256 签空消息体，再 base64。

🔴 只写件数和编号，不写为什么派 / 问题描述 / 用户原话（spec §5.1）。调用方负责只传这些。
"""
import base64
import hashlib
import hmac
import json
from pathlib import Path


def send(lark_cfg: Path, text: str, ctx) -> str:
    """→ "sent" | "DRY RUN: …"（没有配置）| "failed: …"。"""
    p = Path(lark_cfg).expanduser() if lark_cfg else None
    if not p or not p.is_file():
        return f"DRY RUN: {text}"
    cfg = json.loads(p.read_text(encoding="utf-8"))
    payload = {"msg_type": "text", "content": {"text": text}}
    if cfg.get("sign_secret"):
        ts = str(int(ctx.now().timestamp()))
        key = f"{ts}\n{cfg['sign_secret']}".encode()
        payload.update(timestamp=ts, sign=base64.b64encode(hmac.new(key, b"", hashlib.sha256).digest()).decode())
    try:
        body = ctx.post_json(cfg["webhook_url"], payload)
    except Exception as e:  # 网络 / webhook 失效：提醒失败不能拖垮巡检
        return f"failed: {e}"
    code = body.get("code", body.get("StatusCode", 0))
    return "sent" if code == 0 else f"failed: {body}"
