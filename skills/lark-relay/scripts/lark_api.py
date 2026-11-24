"""Lark（国际版）开放平台 HTTP 客户端：只用标准库。http 可注入（测试用）。

token、secret 不打印、不写日志。
"""
import json
import os
import time
import uuid
import urllib.error
import urllib.request
from urllib.parse import urlencode

import parse

BASE = "https://open.larksuite.com"
_TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"
_EXPIRED = {99991663, 99991661, 99991668}
_TIMEOUT = 15


class LarkError(Exception):
    def __init__(self, code: int, msg: str):
        super().__init__(f"lark {code}: {msg}")
        self.code = code
        self.msg = msg


def _urllib_http(method, url, headers, body):
    """body 是 dict ⇒ JSON；是 bytes ⇒ 原样发（multipart 之类，Content-Type 由 headers 给）。"""
    data = None if body is None else body if isinstance(body, bytes) else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json; charset=utf-8", **headers})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:              # Lark 的 4xx 也带 JSON（code/msg），照常解析
        raw = e.read()
        if not raw:
            raise LarkError(-1, f"http {e.code}") from None
    except (urllib.error.URLError, OSError) as e:
        raise LarkError(-1, str(e)) from None
    try:
        return json.loads(raw)
    except ValueError:
        raise LarkError(-1, "invalid json response") from None


class Lark:
    def __init__(self, app_id: str, app_secret: str, base: str = BASE, http=None):
        self._id = app_id
        self._secret = app_secret
        self._base = base.rstrip("/")
        self._http = http or _urllib_http
        self._tok = None
        self._tok_until = 0.0

    def token(self) -> str:
        if self._tok and time.time() < self._tok_until:
            return self._tok
        r = self._http("POST", self._base + _TOKEN_PATH, {}, {"app_id": self._id, "app_secret": self._secret})
        if r.get("code") != 0 or not r.get("tenant_access_token"):
            raise LarkError(int(r.get("code", -1)), str(r.get("msg", "token failed")))
        self._tok = r["tenant_access_token"]
        self._tok_until = time.time() + int(r.get("expire", 7200)) - 60
        return self._tok

    def call(self, method: str, path: str, body=None, params=None, headers=None) -> dict:
        url = self._base + path + ("?" + urlencode(params, doseq=True) if params else "")
        for attempt in (0, 1):
            r = self._http(method, url, {"Authorization": "Bearer " + self.token(), **(headers or {})}, body)
            code = r.get("code")
            if code == 0:
                return r.get("data") or {}
            if code in _EXPIRED and attempt == 0:
                self._tok = None                     # token 过期 / 失效：清缓存重拿一次
                continue
            raise LarkError(int(code if code is not None else -1), str(r.get("msg", "")))
        raise AssertionError("unreachable")

    def _reply(self, message_id: str, msg_type: str, content: dict) -> str:
        d = self.call("POST", f"/open-apis/im/v1/messages/{message_id}/reply",
                      {"msg_type": msg_type, "content": json.dumps(content, ensure_ascii=False)})
        return d.get("message_id", "")

    def reply_post(self, message_id: str, title: str, lines: list, image_keys=()) -> str:
        content = [[{"tag": "text", "text": l}] for l in lines] + [[{"tag": "img", "image_key": k}] for k in image_keys]
        return self._reply(message_id, "post", {"zh_cn": {"title": title, "content": content}})

    def upload_image(self, path: str) -> str:
        """上传一张图（image_type=message），返回 image_key。要应用开了 im:resource 权限。"""
        boundary = "relay" + uuid.uuid4().hex
        with open(path, "rb") as f:
            data = f.read()
        name = os.path.basename(path).replace('"', "")
        body = (f'--{boundary}\r\nContent-Disposition: form-data; name="image_type"\r\n\r\nmessage\r\n'
                f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{name}"\r\n'
                f"Content-Type: application/octet-stream\r\n\r\n").encode() + data + f"\r\n--{boundary}--\r\n".encode()
        d = self.call("POST", "/open-apis/im/v1/images", body,
                      headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        if not d.get("image_key"):
            raise LarkError(-1, "upload returned no image_key")
        return d["image_key"]

    def reply_text(self, message_id: str, text: str) -> str:
        return self._reply(message_id, "text", {"text": text})

    def merged_items(self, message_id: str) -> list:
        d = self.call("GET", f"/open-apis/im/v1/messages/{message_id}")
        out = []
        for it in d.get("items") or []:
            if it.get("message_id") == message_id:
                continue
            out.append({"sender_open_id": (it.get("sender") or {}).get("id"),
                        "text": parse.content_text(it.get("msg_type", ""), (it.get("body") or {}).get("content") or "{}",
                                                   it.get("mentions"))})
        return out

    def user_names(self, open_ids: list) -> dict:
        if not open_ids:
            return {}
        try:
            d = self.call("GET", "/open-apis/contact/v3/users/batch",
                          params={"user_ids": list(open_ids), "user_id_type": "open_id"})
        except LarkError:
            return {}                                 # 通讯录权限可能没开：名字拿不到就算了
        return {u["open_id"]: u["name"] for u in d.get("items") or [] if u.get("open_id") and u.get("name")}

    def list_chat_messages(self, chat_id: str, start_ms: int) -> list:
        params = {"container_id_type": "chat", "container_id": chat_id, "start_time": str(int(start_ms) // 1000),
                  "sort_type": "ByCreateTimeAsc", "page_size": "50"}
        out = []
        while True:
            d = self.call("GET", "/open-apis/im/v1/messages", params=params)
            for it in d.get("items") or []:
                m = parse.normalize_listed(it)
                if not m.get("chat_id"):
                    m["chat_id"] = chat_id
                out.append(m)
            if not d.get("has_more") or not d.get("page_token"):
                return out
            params = {**params, "page_token": d["page_token"]}
