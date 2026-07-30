import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2] / "skills" / "roadmap-radar" / "scripts"
FIX = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(BASE))

import model  # noqa: E402
from sources import wayfinder  # noqa: E402
from test_github_issues import FakeCtx  # noqa: E402

REPO = "hyoteam/aifriends"


def collect(ctx=None, **cfg):
    cfg.setdefault("repo", REPO)
    return wayfinder.collect(dict(cfg, type="wayfinder"), ctx or FakeCtx())


def test_preset_finds_maps_without_group_from_config():
    nodes = {n["id"]: n for n in collect()}
    assert nodes["gh:%s#12" % REPO]["kind"] == model.KIND_GROUP
    assert nodes["gh:%s#14" % REPO]["kind"] == model.KIND_GROUP


def test_preset_turns_dependencies_on():
    ctx = FakeCtx(deps={13: 1})
    nodes = {n["id"]: n for n in collect(ctx=ctx)}
    assert "blocked" in nodes["gh:%s#13" % REPO]["badges"]


def test_map_body_sections_land_in_meta():
    nodes = {n["id"]: n for n in collect()}
    meta = nodes["gh:%s#12" % REPO]["meta"]
    assert meta["decisions"] == ["拆两轨 5 ticket,零丢弃", "抗漂移字段手填为准"]
    assert meta["fog"] == ["预算感知分层的分档阈值"]
    assert meta["out_of_scope"] == ["换记忆算法(北极星弱于我们)"]


def test_map_without_those_sections_gets_empty_lists_not_missing_keys():
    nodes = {n["id"]: n for n in collect()}
    meta = nodes["gh:%s#14" % REPO]["meta"]
    assert meta["decisions"] == []
    assert meta["fog"] == []


def test_leaf_nodes_are_not_section_parsed():
    nodes = {n["id"]: n for n in collect()}
    assert "decisions" not in nodes["gh:%s#13" % REPO]["meta"]


def test_config_can_override_preset():
    ctx = FakeCtx()
    collect(ctx=ctx, fetch_dependencies=False)
    probed = [a for a in ctx.calls
              if "/issues/" in " ".join(a) and "/sub_issues" not in " ".join(a)]
    assert probed == []


def test_section_parser_handles_bullets_with_links_and_bold():
    body = ("## Decisions so far\n\n"
            "- **D1**：挂 obj-3 变现（[#419](http://x) 已关）\n"
            "- 北极星集中在 F3 回收\n\n"
            "## Not yet specified\n\n- 准入门三条\n")
    parsed = wayfinder.parse_sections(body)
    assert parsed["decisions"][0].startswith("**D1**：挂 obj-3 变现")
    assert parsed["decisions"][1] == "北极星集中在 F3 回收"
    assert parsed["fog"] == ["准入门三条"]


def test_registry_exposes_wayfinder():
    from sources import REGISTRY
    assert "wayfinder" in REGISTRY
