"""parse：事件 / 历史消息 → 统一的消息 dict。夹具全是合成的。"""
import json

import parse

KEYS = {"message_id", "chat_id", "chat_type", "sender_open_id", "sender_type", "create_time",
        "root_id", "parent_id", "kind", "text"}


def test_text_with_mention():
    content = json.dumps({"text": "@_user_1 帮看下 staging 401"})
    mentions = [{"key": "@_user_1", "name": "小王", "id": {"open_id": "ou_w"}}]
    assert parse.content_text("text", content, mentions) == "@小王 帮看下 staging 401"
    assert parse.content_text("text", content) == "@_user_1 帮看下 staging 401"


def test_post_paragraphs():
    content = json.dumps({"title": "上线问题", "content": [
        [{"tag": "text", "text": "第一段 "}, {"tag": "at", "user_id": "@_user_1", "user_name": "小李"}],
        [{"tag": "a", "text": "日志", "href": "https://example.com/log"}, {"tag": "emotion", "emoji_type": "OK"}],
        [{"tag": "img", "image_key": "img_x"}],
    ]})
    assert parse.content_text("post", content) == "上线问题\n第一段 @小李\n日志(https://example.com/log)[表情]\n[图片]"


def test_post_with_locale_wrapper():
    content = json.dumps({"zh_cn": {"title": "", "content": [[{"tag": "text", "text": "hi"}]]}})
    assert parse.content_text("post", content) == "hi"


def test_post_at_resolved_from_mentions():
    content = json.dumps({"content": [[{"tag": "at", "user_id": "@_user_2"}]]})
    mentions = [{"key": "@_user_2", "name": "老张"}]
    assert parse.content_text("post", content, mentions) == "@老张"


def test_image_file_merge_forward_placeholders():
    assert parse.content_text("image", json.dumps({"image_key": "img_x"})) == "[图片]"
    assert parse.content_text("file", json.dumps({"file_key": "f", "file_name": "日志.txt"})) == "[文件 日志.txt]"
    assert parse.content_text("merge_forward", json.dumps({"content": "Merged and Forwarded Message"})) == "[合并转发]"
    assert parse.content_text("sticker", json.dumps({"file_key": "s"})) == "[sticker]"


def test_bad_json():
    assert parse.content_text("text", "{not json") == "[无法解析]"
    assert parse.content_text("post", "") == "[无法解析]"


EVENT = {
    "sender": {"sender_id": {"open_id": "ou_owner", "union_id": "on_x"}, "sender_type": "user", "tenant_key": "t"},
    "message": {"message_id": "om_1", "root_id": "om_0", "parent_id": "om_0", "create_time": "1700000000000",
                "chat_id": "oc_p2p", "chat_type": "p2p", "message_type": "text",
                "content": json.dumps({"text": "@_user_1 你好"}),
                "mentions": [{"key": "@_user_1", "name": "小王", "id": {"open_id": "ou_w"}}]},
}

LISTED = {
    "message_id": "om_1", "root_id": "om_0", "parent_id": "om_0", "msg_type": "text",
    "create_time": "1700000000000", "chat_id": "oc_p2p",
    "sender": {"id": "ou_owner", "id_type": "open_id", "sender_type": "user"},
    "body": {"content": json.dumps({"text": "@_user_1 你好"})},
    "mentions": [{"key": "@_user_1", "id": "ou_w", "id_type": "open_id", "name": "小王"}],
}


def test_normalize_event_keys():
    m = parse.normalize(EVENT)
    assert set(m) == KEYS
    assert m["message_id"] == "om_1"
    assert m["sender_open_id"] == "ou_owner"
    assert m["chat_type"] == "p2p"
    assert m["kind"] == "text"
    assert m["text"] == "@小王 你好"


def test_normalize_event_missing_parts_is_safe():
    m = parse.normalize({"message": {"message_id": "om_2", "message_type": "text", "content": "{}"}})
    assert set(m) == KEYS
    assert m["sender_open_id"] is None
    assert m["text"] == ""


def test_normalize_listed_same_keys():
    a, b = parse.normalize(EVENT), parse.normalize_listed(LISTED)
    assert set(b) == KEYS
    assert b["chat_type"] == "p2p"
    assert (a["kind"], a["text"], a["sender_open_id"], a["parent_id"]) == \
        (b["kind"], b["text"], b["sender_open_id"], b["parent_id"])
