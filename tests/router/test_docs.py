import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import FIX, SCRIPTS

SKILL = SCRIPTS.parent
CLI = SCRIPTS / "router.py"
CMD = re.compile(r"(?:router\.py|\$ROUTER_CLI)(?:\s+--root\s+\S+)?\s+([a-z][a-z-]+)")


def help_commands():
    out = subprocess.run([sys.executable, str(CLI), "--help"], capture_output=True, text=True).stdout
    return set(re.search(r"\{([a-z,-]+)\}", out).group(1).split(","))


def test_docs_reference_only_existing_subcommands():
    known = help_commands()
    used = set()
    for doc in ("SKILL.md", "worker.md", "templates/brief.md", "templates/triage.md"):
        used |= set(CMD.findall((SKILL / doc).read_text(encoding="utf-8")))
    assert used, "文档里一个 router.py 子命令都没引用"
    assert used - known == set()
    # 协议依赖的关键子命令都要在文档里出现过
    assert {"discover", "tick", "watch", "draft", "start", "answer", "submit-card", "decide", "tell",
            "emit", "progress", "inbox", "status"} <= used


def test_worker_doc_lists_exactly_three_stop_conditions():
    text = (SKILL / "worker.md").read_text(encoding="utf-8")
    sec = re.search(r"^##[^\n]*三种情况[^\n]*\n(.*?)(?=^## )", text, re.S | re.M)
    assert sec, "worker.md 缺「只在三种情况停」一节"
    items = re.findall(r"^\d+\.\s", sec.group(1), re.M)
    assert len(items) == 3


def test_worker_doc_has_eight_item_card_template():
    text = (SKILL / "worker.md").read_text(encoding="utf-8")
    sec = re.search(r"^##[^\n]*方向卡模板[^\n]*\n(.*?)(?=^## )", text, re.S | re.M).group(1)
    assert [int(n) for n in re.findall(r"^(\d+)\.\s", sec, re.M)][:8] == list(range(1, 9))
    assert "$ROUTER_HOME/cards/" in text and "progress card_ready" in text


def test_skill_frontmatter_and_triggers():
    text = (SKILL / "SKILL.md").read_text(encoding="utf-8")
    fm = re.match(r"^---\n(.*?)\n---\n", text, re.S).group(1)
    assert re.search(r"^name: router$", fm, re.M)
    for trig in ("router", "派发周报", "看板", "start"):
        assert trig in fm


def test_triage_schema_matches_draft_loader(tmp_path):
    text = (SKILL / "templates" / "triage.md").read_text(encoding="utf-8")
    example = json.loads(re.search(r"```json\n(.*?)\n```", text, re.S).group(1))
    (tmp_path / "t.json").write_text(json.dumps(example, ensure_ascii=False))
    home = tmp_path / ".claude" / "router"; home.mkdir(parents=True)
    (home / "config.json").write_text(json.dumps({"repo": "o/r", "lark_config": str(tmp_path / "none.json")}))
    (home / "surfaces.json").write_text((FIX / "surfaces.json").read_text())
    r = subprocess.run([sys.executable, str(CLI), "--root", str(tmp_path), "draft", "--from", str(tmp_path / "t.json")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stdout + r.stderr
    keys = {"title", "weight", "priority", "why", "problem", "problem_anchor", "clue", "surfaces", "tags",
            "depends_on", "covered_by", "decision", "reason"}
    assert all(set(t) == keys for t in example["tickets"])


def test_direction_review_output_schema():
    text = (SKILL / "templates" / "direction_review.md").read_text(encoding="utf-8")
    assert '"pass"' in text and '"challenges"' in text
    for q in ("派单时的那个问题", "修症状", "更简单"):
        assert q in text
