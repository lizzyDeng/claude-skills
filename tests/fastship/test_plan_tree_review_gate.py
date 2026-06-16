"""Step 2.5 计划树覆盖 — validate_code_review must, WHEN a plan tree exists, bind the
review to the current tree hash, require every required node done in the skeleton, and
cover reviewed_node_ids. Bugfix / no-tree runs keep the looser basename behaviour
(covered by test_orchestrator.py); here we exercise the tree branch end-to-end.

跑法: python3 -m pytest tests/fastship/test_plan_tree_review_gate.py -q
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'skills', 'fastship'))

import plan_tree as pt


PLAN = "\n".join([
    "# Plan", "",
    "<!-- fastship:node task-1 -->", "### Task 1", "实现 a。", "",
    "<!-- fastship:node task-2 -->", "### Task 2", "实现 b。", "",
    "<!-- fastship:contract -->", "```json",
    json.dumps({
        "nodes": [
            {"id": "task-1", "title": "A", "deps": [], "inputs": ["root:base"],
             "outputs": ["sym:a"], "files": ["src/a.rs"]},
            {"id": "task-2", "title": "B", "deps": ["task-1"], "inputs": ["sym:a"],
             "outputs": ["sym:b"], "files": ["src/b.rs"]},
        ],
        "ac_mapping": [{"ac_id": "ac-1", "tasks": ["task-1", "task-2"], "e2e": ["E2E-x"]}],
        "exclusive_forks": [],
    }),
    "```", "",
])


def _gate(tree_hash, **over):
    g = {
        "gate": "PASS",
        "reviewed_against": "design.html",
        "reviewed_files": ["src/a.rs", "src/b.rs"],
        "design_fidelity_reviewed": True,
        "spec_compliance_reviewed": True,
        "quality_reviewed": True,
        "design_deviations": [], "spec_gaps": [], "quality_issues": [], "unverified_claims": [],
        "reviewed_plan_tree_sha256": tree_hash,
        "reviewed_node_ids": ["task-1", "task-2"],
        "reviewed_manifests": [{"node_id": "task-1", "files_changed": ["src/a.rs"]},
                               {"node_id": "task-2", "files_changed": ["src/b.rs"]}],
    }
    g.update(over)
    return g


def _review_md(gate):
    return ("## Code Review\n### Per-node verdicts\n- task-1 OK\n- task-2 OK\n"
            "### Design Fidelity\n- matches\n### Contract Gate\n```json\n"
            + json.dumps(gate, ensure_ascii=False, indent=2) + "\n```\n### GATE: PASS\n")


def _setup(tmp_path, monkeypatch, gate_over=None, node_status="done"):
    import orchestrator as o
    monkeypatch.setattr(o, "_repo_root", lambda: str(tmp_path))
    (tmp_path / "design.html").write_text("<html>d</html>")
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "src" / "a.rs").write_text("// a")
    (tmp_path / "src" / "b.rs").write_text("// b")

    tree_dir = str(tmp_path / "plan.plantree")
    ok, msg, prov = pt.materialize_plan_tree(PLAN, tree_dir, "srcsha")
    assert ok, msg
    # mark node statuses in the driver-owned skeleton
    sk = json.load(open(prov["skeleton_path"]))
    for n in sk["nodes"]:
        n["status"] = node_status
    json.dump(sk, open(prov["skeleton_path"], "w"))

    claude = tmp_path / ".claude"
    claude.mkdir(parents=True, exist_ok=True)
    review = claude / ".fastship-code-review.md"
    gate = _gate(prov["tree_hash"], **(gate_over or {}))
    review.write_text(_review_md(gate))

    orch = {"artifacts": {"code_review_path": str(review)}}
    o.record_step_artifact(orch, "2.5", str(review), source="test")
    o._record_plan_tree_artifact(orch, prov)
    return o, orch, prov


def test_tree_coverage_passes(tmp_path, monkeypatch):
    o, orch, _ = _setup(tmp_path, monkeypatch)
    ok, msg = o.validate_code_review(orch, {})
    assert ok, msg


def test_wrong_tree_hash_fails(tmp_path, monkeypatch):
    o, orch, _ = _setup(tmp_path, monkeypatch, gate_over={"reviewed_plan_tree_sha256": "stale"})
    ok, msg = o.validate_code_review(orch, {})
    assert not ok and "tree_hash" in msg


def test_undone_node_fails(tmp_path, monkeypatch):
    o, orch, _ = _setup(tmp_path, monkeypatch, node_status="pending")
    ok, msg = o.validate_code_review(orch, {})
    assert not ok and "node 未完成" in msg


def test_missing_reviewed_node_ids_fails(tmp_path, monkeypatch):
    o, orch, _ = _setup(tmp_path, monkeypatch, gate_over={"reviewed_node_ids": ["task-1"]})
    ok, msg = o.validate_code_review(orch, {})
    assert not ok and "reviewed_node_ids" in msg and "task-2" in msg


def test_missing_reviewed_manifests_fails(tmp_path, monkeypatch):
    o, orch, _ = _setup(tmp_path, monkeypatch, gate_over={"reviewed_manifests": "nope"})
    ok, msg = o.validate_code_review(orch, {})
    assert not ok and "reviewed_manifests" in msg


def test_unknown_manifest_node_fails(tmp_path, monkeypatch):
    o, orch, _ = _setup(tmp_path, monkeypatch,
                        gate_over={"reviewed_manifests": [{"node_id": "ghost"}]})
    ok, msg = o.validate_code_review(orch, {})
    assert not ok and "ghost" in msg


def test_empty_manifests_fails(tmp_path, monkeypatch):
    # empty manifest list must NOT skip the per-node boundary check (codex confirm High)
    o, orch, _ = _setup(tmp_path, monkeypatch, gate_over={"reviewed_manifests": []})
    ok, msg = o.validate_code_review(orch, {})
    assert not ok and "reviewed_manifests 未覆盖 node" in msg


def test_partial_manifest_coverage_fails(tmp_path, monkeypatch):
    # only task-1 has a manifest → task-2 uncovered → FAIL
    o, orch, _ = _setup(tmp_path, monkeypatch, gate_over={
        "reviewed_manifests": [{"node_id": "task-1", "files_changed": ["src/a.rs"]}]})
    ok, msg = o.validate_code_review(orch, {})
    assert not ok and "task-2" in msg


def test_manifest_missing_files_changed_fails(tmp_path, monkeypatch):
    o, orch, _ = _setup(tmp_path, monkeypatch, gate_over={
        "reviewed_manifests": [{"node_id": "task-1", "files_changed": ["src/a.rs"]},
                               {"node_id": "task-2"}]})  # no files_changed
    ok, msg = o.validate_code_review(orch, {})
    assert not ok and "files_changed" in msg


def test_manifest_out_of_bounds_fails(tmp_path, monkeypatch):
    # task-2 claims it changed a file outside its declared node.files
    o, orch, _ = _setup(tmp_path, monkeypatch, gate_over={
        "reviewed_manifests": [{"node_id": "task-1", "files_changed": ["src/a.rs"]},
                               {"node_id": "task-2", "files_changed": ["src/evil.rs"]}]})
    ok, msg = o.validate_code_review(orch, {})
    assert not ok and "越界" in msg


def test_empty_manifest_with_real_change_fails(tmp_path, monkeypatch):
    # task-1 reports files_changed:[] but a.rs (its territory) IS in the real diff →
    # unclaimed → FAIL (closes the empty-manifest bypass, codex confirm #1).
    o, orch, _ = _setup(tmp_path, monkeypatch, gate_over={
        "reviewed_files": ["src/a.rs"],
        "reviewed_manifests": [{"node_id": "task-1", "files_changed": []},
                               {"node_id": "task-2", "files_changed": []}]})
    monkeypatch.setattr(o, "_changed_files", lambda *a, **k: {"src/a.rs"})
    ok, msg = o.validate_code_review(orch, {})
    assert not ok and "未被任何 manifest 认领" in msg and "src/a.rs" in msg


def test_claimed_change_passes(tmp_path, monkeypatch):
    # same real change, but task-1's manifest claims a.rs → covered → PASS
    o, orch, _ = _setup(tmp_path, monkeypatch, gate_over={
        "reviewed_files": ["src/a.rs"],
        "reviewed_manifests": [{"node_id": "task-1", "files_changed": ["src/a.rs"]},
                               {"node_id": "task-2", "files_changed": []}]})
    monkeypatch.setattr(o, "_changed_files", lambda *a, **k: {"src/a.rs"})
    ok, msg = o.validate_code_review(orch, {})
    assert ok, msg


# ── [FASTSHIP_GOAL] status line — node-progress fields ──────────────────────
def _goal_line(status_text):
    for line in status_text.splitlines():
        if line.startswith("[FASTSHIP_GOAL]"):
            return line
    return ""


def test_status_line_has_node_fields_with_tree(tmp_path, monkeypatch):
    o, orch, _ = _setup(tmp_path, monkeypatch)          # both nodes done
    orch["requirement"] = "x"
    line = _goal_line(o.format_status(orch))
    assert "nodes_done=2/2" in line
    assert "nodes_failed=0" in line
    assert "current_node=-" in line                     # all done → no current
    assert "plan_tree_hash=" in line and "plan_tree_hash=-" not in line


def test_status_line_current_node_when_pending(tmp_path, monkeypatch):
    o, orch, _ = _setup(tmp_path, monkeypatch, node_status="pending")
    orch["requirement"] = "x"
    line = _goal_line(o.format_status(orch))
    assert "nodes_done=0/2" in line and "current_node=task-1" in line


def test_status_line_degrades_without_tree(tmp_path, monkeypatch):
    import orchestrator as o
    monkeypatch.setattr(o, "_repo_root", lambda: str(tmp_path))
    orch = {"requirement": "x", "artifacts": {}, "phase": 1, "current_step": "1.0"}
    line = _goal_line(o.format_status(orch))
    assert "nodes_done=0/0" in line and "current_node=-" in line and "plan_tree_hash=-" in line


def test_goal_condition_appends_skeleton_when_tree(tmp_path, monkeypatch):
    o, orch, prov = _setup(tmp_path, monkeypatch)
    orch["requirement"] = "x"
    cond = o.goal_condition(orch)
    assert "skeleton_path=" in cond and prov["skeleton_path"] in cond
    # bugfix / no-tree session stays byte-identical (no skeleton pointer)
    plain = o.goal_condition({"requirement": "x", "artifacts": {}})
    assert "skeleton_path=" not in plain
