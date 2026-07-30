import importlib.util
import json
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parents[2] / "skills" / "roadmap-radar" / "scripts"
FIX = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(BASE))

import model  # noqa: E402
from sources import forge  # noqa: E402


class Ctx:
    def __init__(self, root):
        self.root = Path(root)

    def resolve(self, rel):
        return str(self.root / rel)

    def gh_json(self, args):  # forge source 不该用到
        raise AssertionError("forge source must not call gh")


def collect(fixture_name, **cfg):
    cfg.setdefault("path", fixture_name)
    return forge.collect(dict(cfg, type="forge"), Ctx(FIX))


def test_aifriends_probes_objective_id_and_north_star():
    nodes = {n["id"]: n for n in collect("aifriends_roadmap.json")}
    assert nodes["goal:obj-3"]["title"] == "变现能力"
    assert nodes["goal:obj-3"]["meta"]["target_metric"].startswith("免费→付费转化率")
    assert nodes["forge:weekend-date-foundation"]["parent"] == "goal:obj-3"
    banner = nodes["forge:north-star"]
    assert banner["kind"] == model.KIND_GOAL
    assert "30 分钟" in banner["title"]
    assert nodes["goal:obj-3"]["parent"] == "forge:north-star"


def test_supersdk_probes_objective_and_project_without_config():
    """字段名和 aifriends 不同，但零配置也要命中。"""
    nodes = {n["id"]: n for n in collect("supersdk_roadmap.json")}
    assert nodes["forge:login-bridge"]["parent"] == "goal:obj-1"
    assert nodes["forge:ad-bridge"]["parent"] == "goal:obj-2"
    assert "WebView" in nodes["forge:north-star"]["title"]


def test_status_maps_to_progress():
    nodes = {n["id"]: n for n in collect("aifriends_roadmap.json")}
    assert nodes["forge:gift-system"]["progress"] == 1.0        # concluded
    assert nodes["forge:core-tracking-events"]["progress"] == 0.0  # draft
    assert nodes["forge:her-world-gift-wall"]["progress"] == 0.85  # measuring


def test_unknown_status_is_none_and_badged_not_crash():
    nodes = {n["id"]: n for n in collect("aifriends_roadmap.json")}
    weird = nodes["forge:mystery-status"]
    assert weird["progress"] is None
    assert "unmapped-status" in weird["badges"]
    assert weird["state"] == "totally-new-word"


def test_status_progress_override_from_config():
    nodes = {n["id"]: n for n in collect("aifriends_roadmap.json",
                                        status_progress={"draft": 0.25})}
    assert nodes["forge:core-tracking-events"]["progress"] == 0.25
    assert nodes["forge:gift-system"]["progress"] == 1.0   # 未覆盖的词保持默认


def test_feature_pointing_at_absent_objective_keeps_parent_for_doctor():
    """obj-6 不在 objectives 列表里 —— 保留 parent，让 collect 标 orphan 报出来。"""
    nodes = {n["id"]: n for n in collect("aifriends_roadmap.json")}
    assert nodes["forge:her-world-gift-wall"]["parent"] == "goal:obj-6"
    assert "goal:obj-6" not in nodes


def test_orphan_feature_dirs_are_emitted(tmp_path):
    """features/ 里有目录但 roadmap.json 没登记 → 必须出现在图里。"""
    (tmp_path / "project-roadmap").mkdir()
    (tmp_path / "project-roadmap" / "roadmap.json").write_text(json.dumps({
        "north_star": "NS",
        "objectives": [{"id": "obj-1", "name": "G", "target_metric": "M"}],
        "features": [{"slug": "known", "name": "Known", "objective_id": "obj-1",
                      "status": "draft"}],
    }), encoding="utf-8")
    feats = tmp_path / "project-roadmap" / "features"
    (feats / "known").mkdir(parents=True)
    (feats / "ghost-one").mkdir()
    (feats / "ghost-two").mkdir()

    nodes = {n["id"]: n for n in forge.collect(
        {"type": "forge", "path": "project-roadmap/roadmap.json"}, Ctx(tmp_path))}
    assert "forge:ghost-one" in nodes
    assert "orphan" in nodes["forge:ghost-one"]["badges"]
    assert "no-count" in nodes["forge:ghost-one"]["badges"]
    assert "forge:ghost-two" in nodes


def test_registry_exposes_forge():
    from sources import REGISTRY
    assert "forge" in REGISTRY
