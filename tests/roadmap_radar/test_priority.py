import sys
from pathlib import Path

import pytest

BASE = Path(__file__).resolve().parents[2] / "skills" / "roadmap-radar" / "scripts"
sys.path.insert(0, str(BASE))

import priority  # noqa: E402

REPO = "hyoteam/aifriends"


class FakeCtx:
    """录制 gh 调用；issue view 返回预置的现有 label。"""

    def __init__(self, existing=None):
        self.existing = existing or {}
        self.runs = []

    def gh_json(self, args):
        self.runs.append(args)
        number = int(args[args.index("view") + 1])
        return {"labels": [{"name": name}
                           for name in self.existing.get(number, [])]}

    def gh_run(self, args):
        self.runs.append(args)


def test_parse_accepts_hash_and_case_variants():
    assert priority.parse_assignments("36=P0, #28=p1") == [(36, 0), (28, 1)]


def test_parse_rejects_garbage_before_writing_anything():
    with pytest.raises(ValueError):
        priority.parse_assignments("36=高")
    with pytest.raises(ValueError):
        priority.parse_assignments("")


def test_repo_from_config_takes_first_github_source():
    config = {"sources": [{"type": "forge", "path": "x"},
                          {"type": "wayfinder", "repo": REPO}]}
    assert priority.repo_from_config(config) == REPO


def test_set_priorities_adds_label_and_replaces_stale_one():
    ctx = FakeCtx(existing={36: ["wayfinder:map", "P2"]})
    summary = priority.set_priorities(ctx, REPO, [(36, 0)])
    assert summary == [(36, "P2", "P0")]
    edit = next(a for a in ctx.runs if a[0] == "issue" and a[1] == "edit")
    assert "--add-label" in edit and "P0" in edit
    assert "--remove-label" in edit and "P2" in edit


def test_set_priorities_creates_label_once_per_level():
    ctx = FakeCtx()
    priority.set_priorities(ctx, REPO, [(36, 0), (28, 0), (14, 2)])
    creates = [a for a in ctx.runs if a[0] == "label"]
    assert len(creates) == 2                      # P0 只建一次，P2 一次
    assert all("--force" in a for a in creates)   # 幂等，已存在不炸


def test_same_label_is_not_removed_when_reapplied():
    ctx = FakeCtx(existing={36: ["P0"]})
    summary = priority.set_priorities(ctx, REPO, [(36, 0)])
    assert summary == [(36, None, "P0")]
    edit = next(a for a in ctx.runs if a[0] == "issue" and a[1] == "edit")
    assert "--remove-label" not in edit
