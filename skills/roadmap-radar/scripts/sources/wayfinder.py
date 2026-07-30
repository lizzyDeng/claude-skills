"""wayfinder preset —— github-issues 加上 map 约定的那点知识。

约定来自 /wayfinder skill：map = label wayfinder:map，ticket = sub-issue，
map 正文有 Decisions so far / Not yet specified / Out of scope 三节。
"""

import re

import model
from . import github_issues

PRESET = {
    "group_from": "label:^wayfinder:map$",
    "goal_from": "label:^obj-\\d+$",
    "fetch_dependencies": True,
}

SECTION_KEYS = {
    "decisions so far": "decisions",
    "not yet specified": "fog",
    "out of scope": "out_of_scope",
}

_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$")
_BULLET_RE = re.compile(r"^[-*]\s+(.+?)\s*$")


def collect(config, ctx):
    merged = dict(PRESET)
    merged.update({k: v for k, v in config.items() if k != "type"})
    merged["type"] = "github-issues"
    nodes = github_issues.collect(merged, ctx)
    for node in nodes:
        if node["kind"] == model.KIND_GROUP:
            node["meta"].update(parse_sections(node["meta"].get("body", "")))
    return nodes


def parse_sections(body):
    """把 map 正文的三节抽成列表。缺的节返回空列表，不是缺 key。"""
    out = {key: [] for key in SECTION_KEYS.values()}
    current = None
    for line in (body or "").splitlines():
        heading = _HEADING_RE.match(line)
        if heading:
            current = SECTION_KEYS.get(heading.group(1).strip().lower())
            continue
        if current is None:
            continue
        bullet = _BULLET_RE.match(line)
        if bullet:
            out[current].append(bullet.group(1))
    return out
