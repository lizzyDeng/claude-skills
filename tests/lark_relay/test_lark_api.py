"""lark_api：注入假 http，断言请求形状和重试；网络错误走真 urllib。夹具全是合成的。"""
import json
from urllib.parse import parse_qs, urlparse

import pytest

import lark_api

TOKEN_PATH = "/open-apis/auth/v3/tenant_access_token/internal"


class FakeHttp:
    """responses：按调用顺序消费的 (path 前缀, 响应 dict)。token 请求单独应答，不占队列。"""

    def __init__(self, responses, tokens=("t1", "t2", "t3")):
        self.responses = list(responses)
        self.tokens = list(tokens)
        self.calls = []

    def __call__(self, method, url, headers, body):
        u = urlparse(url)
        self.calls.append({"method": method, "path": u.path, "query": parse_qs(u.query), "headers": headers, "body": body})
        if u.path == TOKEN_PATH:
            return {"code": 0, "msg": "ok", "tenant_access_token": self.tokens.pop(0), "expire": 7200}
        prefix, resp = self.responses.pop(0)
        assert u.path.startswith(prefix), (u.path, prefix)
        return resp

    def api_calls(self):
        return [c for c in self.calls if c["path"] != TOKEN_PATH]


def make(responses, **kw):
    http = FakeHttp(responses, **kw)
    return lark_api.Lark("cli_x", "sec", http=http), http


def test_token_cached():
    api, http = make([("/open-apis/x", {"code": 0, "data": {}}), ("/open-apis/x", {"code": 0, "data": {}})])
    api.call("GET", "/open-apis/x")
    api.call("GET", "/open-apis/x")
    assert sum(c["path"] == TOKEN_PATH for c in http.calls) == 1
    assert http.api_calls()[1]["headers"]["Authorization"] == "Bearer t1"


def test_expired_token_retried_once():
    api, http = make([("/open-apis/x", {"code": 99991663, "msg": "token expired"}),
                      ("/open-apis/x", {"code": 0, "data": {"ok": 1}})])
    assert api.call("GET", "/open-apis/x") == {"ok": 1}
    assert [c["headers"]["Authorization"] for c in http.api_calls()] == ["Bearer t1", "Bearer t2"]

    api, http = make([("/open-apis/x", {"code": 99991661, "msg": "bad"}),
                      ("/open-apis/x", {"code": 99991668, "msg": "bad"})])
    with pytest.raises(lark_api.LarkError) as e:
        api.call("GET", "/open-apis/x")
    assert e.value.code == 99991668
    assert len(http.api_calls()) == 2


def test_nonzero_code_raises():
    api, _ = make([("/open-apis/x", {"code": 230002, "msg": "bot not in chat"})])
    with pytest.raises(lark_api.LarkError) as e:
        api.call("GET", "/open-apis/x")
    assert (e.value.code, e.value.msg) == (230002, "bot not in chat")


def test_token_failure_raises():
    http = lambda m, u, h, b: {"code": 10014, "msg": "app secret invalid"}  # noqa: E731
    with pytest.raises(lark_api.LarkError) as e:
        lark_api.Lark("cli_x", "bad", http=http).token()
    assert e.value.code == 10014


def test_reply_post_shape():
    api, http = make([("/open-apis/im/v1/messages/om_1/reply", {"code": 0, "data": {"message_id": "om_bot"}})])
    mid = api.reply_post("om_1", "✅ 小王：staging placement 401", ["结论：a", "可直接转给小王：「b」", "要你定：无"])
    assert mid == "om_bot"
    c = http.api_calls()[0]
    assert (c["method"], c["path"]) == ("POST", "/open-apis/im/v1/messages/om_1/reply")
    assert c["body"]["msg_type"] == "post"
    post = json.loads(c["body"]["content"])["zh_cn"]
    assert post["title"] == "✅ 小王：staging placement 401"
    assert post["content"] == [[{"tag": "text", "text": "结论：a"}], [{"tag": "text", "text": "可直接转给小王：「b」"}],
                               [{"tag": "text", "text": "要你定：无"}]]


def test_upload_image_multipart(tmp_path):
    img = tmp_path / "shot.png"
    img.write_bytes(b"\x89PNG fake bytes")
    api, http = make([("/open-apis/im/v1/images", {"code": 0, "data": {"image_key": "img_v3_1"}})])
    assert api.upload_image(str(img)) == "img_v3_1"
    c = http.api_calls()[0]
    assert (c["method"], c["path"]) == ("POST", "/open-apis/im/v1/images")
    ctype = c["headers"]["Content-Type"]
    assert ctype.startswith("multipart/form-data; boundary=")
    boundary = ctype.split("boundary=")[1].encode()
    body = c["body"]
    assert isinstance(body, bytes) and boundary in body
    assert b'name="image_type"\r\n\r\nmessage\r\n' in body
    assert b'name="image"; filename="shot.png"' in body and b"\x89PNG fake bytes" in body


def test_reply_post_with_images():
    api, http = make([("/open-apis/im/v1/messages/om_1/reply", {"code": 0, "data": {"message_id": "om_bot"}})])
    api.reply_post("om_1", "✅ t", ["结论：a"], image_keys=["img_1", "img_2"])
    post = json.loads(http.api_calls()[0]["body"]["content"])["zh_cn"]
    assert post["content"] == [[{"tag": "text", "text": "结论：a"}],
                               [{"tag": "img", "image_key": "img_1"}], [{"tag": "img", "image_key": "img_2"}]]


def test_reply_text_shape():
    api, http = make([("/open-apis/im/v1/messages/om_1/reply", {"code": 0, "data": {"message_id": "om_b"}})])
    assert api.reply_text("om_1", "已存下") == "om_b"
    body = http.api_calls()[0]["body"]
    assert body["msg_type"] == "text" and json.loads(body["content"]) == {"text": "已存下"}


def test_merged_items_skips_parent():
    items = [
        {"message_id": "om_m", "msg_type": "merge_forward", "body": {"content": "{}"}, "sender": {"id": "ou_owner"}},
        {"message_id": "om_c1", "upper_message_id": "om_m", "msg_type": "text",
         "body": {"content": json.dumps({"text": "401 了"})}, "sender": {"id": "ou_w", "id_type": "open_id"}},
        {"message_id": "om_c2", "upper_message_id": "om_m", "msg_type": "image",
         "body": {"content": json.dumps({"image_key": "k"})}, "sender": {"id": "ou_l", "id_type": "open_id"}},
    ]
    api, http = make([("/open-apis/im/v1/messages/om_m", {"code": 0, "data": {"items": items}})])
    assert api.merged_items("om_m") == [{"sender_open_id": "ou_w", "text": "401 了"},
                                        {"sender_open_id": "ou_l", "text": "[图片]"}]
    assert http.api_calls()[0]["method"] == "GET"


def test_user_names():
    api, http = make([("/open-apis/contact/v3/users/batch",
                       {"code": 0, "data": {"items": [{"open_id": "ou_w", "name": "小王"}]}})])
    assert api.user_names(["ou_w", "ou_l"]) == {"ou_w": "小王"}
    q = http.api_calls()[0]["query"]
    assert q["user_ids"] == ["ou_w", "ou_l"] and q["user_id_type"] == ["open_id"]
    assert api.user_names([]) == {}


def test_user_names_permission_error_returns_empty():
    api, _ = make([("/open-apis/contact/v3/users/batch", {"code": 99991672, "msg": "no permission"})])
    assert api.user_names(["ou_w"]) == {}


def test_list_chat_messages_pages():
    def item(mid):
        return {"message_id": mid, "msg_type": "text", "create_time": "1700000001000", "chat_id": "oc_p",
                "sender": {"id": "ou_owner", "id_type": "open_id", "sender_type": "user"},
                "body": {"content": json.dumps({"text": mid})}}
    api, http = make([
        ("/open-apis/im/v1/messages", {"code": 0, "data": {"items": [item("om_1")], "has_more": True, "page_token": "p2"}}),
        ("/open-apis/im/v1/messages", {"code": 0, "data": {"items": [item("om_2")], "has_more": False}}),
    ])
    got = api.list_chat_messages("oc_p", 1700000000500)
    assert [m["message_id"] for m in got] == ["om_1", "om_2"]
    assert got[0]["text"] == "om_1" and got[0]["chat_type"] == "p2p"
    q1, q2 = (c["query"] for c in http.api_calls())
    assert q1["container_id_type"] == ["chat"] and q1["container_id"] == ["oc_p"]
    assert q1["start_time"] == ["1700000000"] and q1["sort_type"] == ["ByCreateTimeAsc"] and q1["page_size"] == ["50"]
    assert "page_token" not in q1 and q2["page_token"] == ["p2"]


def test_network_error_is_lark_error():
    api = lark_api.Lark("cli_x", "sec", base="http://127.0.0.1:1")
    with pytest.raises(lark_api.LarkError) as e:
        api.token()
    assert e.value.code == -1
