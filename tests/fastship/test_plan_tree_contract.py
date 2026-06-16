"""Unit tests for plan_tree.py — the 计划树 decomposition primitives.

Pure functions (extract_contract_block / check_plan_node_graph / split_plan_tree)
plus the side-effecting materialize_plan_tree (idempotent + stale-cleanup) tested
against tmp_path. Mirrors test_plan_mapping_contract.py's check()-style shape.

跑法: python3 -m pytest tests/fastship/test_plan_tree_contract.py -q
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'skills', 'fastship'))

import plan_tree as pt


# ── factories ────────────────────────────────────────────────────────────────
def node(nid, *, deps=None, inputs=None, outputs=None, files=None, title=None, **extra):
    n = {
        "id": nid,
        "title": title or f"node {nid}",
        "deps": deps or [],
        "inputs": inputs or ["root:base"],
        "outputs": outputs or [f"sym:{nid}_out"],
        "files": files or [f"src/{nid}.rs"],
    }
    n.update(extra)
    return n


def block(nodes, ac_mapping=None, forks=None):
    return {
        "nodes": nodes,
        "ac_mapping": ac_mapping if ac_mapping is not None else [
            {"ac_id": "ac-1", "tasks": [n["id"] for n in nodes], "e2e": ["E2E-x"]}
        ],
        "exclusive_forks": forks or [],
    }


def good_block():
    return block([
        node("task-1", outputs=["sym:a"], files=["src/a.rs"]),
        node("task-2", deps=["task-1"], inputs=["root:base", "sym:a"],
             outputs=["sym:b"], files=["src/b.rs"]),
    ])


# ── check_plan_node_graph: green ─────────────────────────────────────────────
def test_good_dag_passes():
    ok, msg = pt.check_plan_node_graph(good_block())
    assert ok, msg


def test_diamond_dag_passes():
    b = block([
        node("a", deps=[], outputs=["sym:a"], files=["src/a.rs"]),
        node("b", deps=["a"], inputs=["sym:a"], outputs=["sym:b"], files=["src/b.rs"]),
        node("c", deps=["a"], inputs=["sym:a"], outputs=["sym:c"], files=["src/c.rs"]),
        node("d", deps=["b", "c"], inputs=["sym:b", "sym:c"], outputs=["sym:d"], files=["src/d.rs"]),
    ])
    ok, msg = pt.check_plan_node_graph(b)
    assert ok, msg


# ── check_plan_node_graph: FAIL branches ─────────────────────────────────────
def test_nodes_not_list_fails():
    ok, msg = pt.check_plan_node_graph({"nodes": {}})
    assert not ok and "nodes" in msg


def test_nodes_empty_fails():
    ok, msg = pt.check_plan_node_graph({"nodes": []})
    assert not ok and "非空" in msg


def test_bad_id_regex_fails():
    for bad in ["Task-1", "a/b", "a..b", "a b", "_x", "-x", ""]:
        b = block([node(bad)])
        ok, msg = pt.check_plan_node_graph(b)
        assert not ok and "id" in msg, f"{bad!r} should fail"


def test_duplicate_id_fails():
    b = block([node("x", outputs=["sym:1"]), node("x", outputs=["sym:2"])])
    ok, msg = pt.check_plan_node_graph(b)
    assert not ok and "重复" in msg


def test_missing_title_fails():
    n = node("x"); n["title"] = "  "
    ok, msg = pt.check_plan_node_graph(block([n]))
    assert not ok and "title" in msg


def test_empty_files_fails():
    n = node("x"); n["files"] = []
    ok, msg = pt.check_plan_node_graph(block([n]))
    assert not ok and "files" in msg


def test_glob_file_fails():
    n = node("x", files=["src/*.rs"])
    ok, msg = pt.check_plan_node_graph(block([n]))
    assert not ok and "files" in msg


def test_abs_file_fails():
    n = node("x", files=["/etc/passwd"])
    ok, msg = pt.check_plan_node_graph(block([n]))
    assert not ok and "files" in msg


def test_dotdot_file_fails():
    n = node("x", files=["../escape.rs"])
    ok, msg = pt.check_plan_node_graph(block([n]))
    assert not ok and "files" in msg


def test_outputs_global_dup_fails():
    b = block([
        node("a", outputs=["sym:dup"], files=["src/a.rs"]),
        node("b", outputs=["sym:dup"], files=["src/b.rs"]),
    ])
    ok, msg = pt.check_plan_node_graph(b)
    assert not ok and "全局重复" in msg


def test_dangling_dep_fails():
    b = block([node("a", deps=["ghost"], files=["src/a.rs"])])
    ok, msg = pt.check_plan_node_graph(b)
    assert not ok and "不存在" in msg


def test_self_dep_fails():
    b = block([node("a", deps=["a"], files=["src/a.rs"])])
    ok, msg = pt.check_plan_node_graph(b)
    assert not ok and ("自身" in msg or "环" in msg)


def test_cycle_fails():
    b = block([
        node("a", deps=["b"], inputs=["sym:b"], outputs=["sym:a"], files=["src/a.rs"]),
        node("b", deps=["a"], inputs=["sym:a"], outputs=["sym:b"], files=["src/b.rs"]),
    ])
    ok, msg = pt.check_plan_node_graph(b)
    assert not ok and "环" in msg


def test_dangling_input_fails():
    b = block([node("a", inputs=["sym:nowhere"], files=["src/a.rs"])])
    ok, msg = pt.check_plan_node_graph(b)
    assert not ok and "悬空" in msg


def test_input_from_non_ancestor_fails():
    # b's input is a's output but b does NOT depend on a
    b = block([
        node("a", deps=[], outputs=["sym:a"], files=["src/a.rs"]),
        node("b", deps=[], inputs=["sym:a"], outputs=["sym:b"], files=["src/b.rs"]),
    ])
    ok, msg = pt.check_plan_node_graph(b)
    assert not ok and ("deps" in msg or "不一致" in msg)


def test_tasks_reference_unknown_node_fails():
    b = block([node("a", files=["src/a.rs"])],
              ac_mapping=[{"ac_id": "ac-1", "tasks": ["ghost"], "e2e": ["E2E"]}])
    ok, msg = pt.check_plan_node_graph(b)
    assert not ok and "不存在" in msg


def test_orphan_node_fails():
    b = block([
        node("a", outputs=["sym:a"], files=["src/a.rs"]),
        node("b", outputs=["sym:b"], files=["src/b.rs"]),
    ], ac_mapping=[{"ac_id": "ac-1", "tasks": ["a"], "e2e": ["E2E"]}])
    ok, msg = pt.check_plan_node_graph(b)
    assert not ok and "游离" in msg


def test_orphan_rescued_by_supporting_for():
    b = block([
        node("a", outputs=["sym:a"], files=["src/a.rs"]),
        node("b", outputs=["sym:b"], files=["src/b.rs"], supporting_for=["ac-1"]),
    ], ac_mapping=[{"ac_id": "ac-1", "tasks": ["a"], "e2e": ["E2E"]}])
    ok, msg = pt.check_plan_node_graph(b)
    assert ok, msg


def test_file_overlap_without_edge_fails():
    b = block([
        node("a", outputs=["sym:a"], files=["src/shared.rs"]),
        node("b", outputs=["sym:b"], files=["src/shared.rs"]),
    ])
    ok, msg = pt.check_plan_node_graph(b)
    assert not ok and "共享文件" in msg


def test_file_overlap_with_edge_ok():
    b = block([
        node("a", outputs=["sym:a"], files=["src/shared.rs"]),
        node("b", deps=["a"], inputs=["sym:a"], outputs=["sym:b"], files=["src/shared.rs", "src/b.rs"]),
    ])
    ok, msg = pt.check_plan_node_graph(b)
    assert ok, msg


def test_file_overlap_canonicalization():
    # "./src/x.rs" and "src/x.rs" are the same file -> overlap without edge FAILs
    b = block([
        node("a", outputs=["sym:a"], files=["./src/x.rs"]),
        node("b", outputs=["sym:b"], files=["src/x.rs"]),
    ])
    ok, msg = pt.check_plan_node_graph(b)
    assert not ok and "共享文件" in msg


# ── canon_path ───────────────────────────────────────────────────────────────
def test_canon_path():
    assert pt.canon_path("./a/b.rs") == "a/b.rs"
    assert pt.canon_path("a/b.rs") == "a/b.rs"
    assert pt.canon_path("/abs") is None
    assert pt.canon_path("a/../b") is None
    assert pt.canon_path("a/*.rs") is None
    assert pt.canon_path("dir/") is None
    assert pt.canon_path("") is None


# ── extract_contract_block ───────────────────────────────────────────────────
CONTRACT_JSON = json.dumps(good_block(), ensure_ascii=False)


def plan_with_contract(extra_example=False):
    parts = ["# Plan", "", "<!-- fastship:root -->", "## 设计", "shared", "<!-- fastship:/root -->", ""]
    if extra_example:
        parts += ["这里是一个**示例** contract（不应被当真）：", "```json",
                  json.dumps({"nodes": [{"id": "demo"}], "ac_mapping": []}), "```", ""]
    parts += ["<!-- fastship:node task-1 -->", "### Task 1", "do a", ""]
    parts += ["<!-- fastship:node task-2 -->", "### Task 2", "do b", ""]
    parts += ["<!-- fastship:contract -->", "```json", CONTRACT_JSON, "```", ""]
    return "\n".join(parts)


def test_extract_single_contract():
    blk, err = pt.extract_contract_block(plan_with_contract())
    assert err is None and blk is not None
    assert [n["id"] for n in blk["nodes"]] == ["task-1", "task-2"]


def test_extract_ignores_example_block():
    # an echoed example ```json (no contract marker) must NOT be picked
    blk, err = pt.extract_contract_block(plan_with_contract(extra_example=True))
    assert err is None and blk is not None
    assert [n["id"] for n in blk["nodes"]] == ["task-1", "task-2"]


def test_extract_none_when_absent():
    blk, err = pt.extract_contract_block("# Plan\n\nno contract here\n")
    assert blk is None and err is None


def test_extract_multiple_markers_fails():
    md = plan_with_contract() + "\n<!-- fastship:contract -->\n```json\n{}\n```\n"
    blk, err = pt.extract_contract_block(md)
    assert blk is None and "仅一个" in err


def test_extract_marker_inside_fence_ignored():
    md = ("# Plan\n\n```\n<!-- fastship:contract -->\n```\n\n"
          "<!-- fastship:contract -->\n```json\n" + CONTRACT_JSON + "\n```\n")
    blk, err = pt.extract_contract_block(md)
    assert err is None and blk is not None and blk["nodes"][0]["id"] == "task-1"


def test_extract_bad_json_fails():
    md = "<!-- fastship:contract -->\n```json\n{not json}\n```\n"
    blk, err = pt.extract_contract_block(md)
    assert blk is None and "JSON" in err


# ── split_plan_tree ──────────────────────────────────────────────────────────
def test_split_root_and_nodes():
    md = plan_with_contract()
    blk, _ = pt.extract_contract_block(md)
    root, bodies, err = pt.split_plan_tree(md, blk)
    assert err is None
    assert "## 设计" in root and "shared" in root
    # root must NOT contain node bodies
    assert "do a" not in root and "do b" not in root
    assert set(bodies) == {"task-1", "task-2"}
    assert "do a" in bodies["task-1"] and "do b" not in bodies["task-1"]
    assert "do b" in bodies["task-2"] and "do a" not in bodies["task-2"]
    # contract json must not bleed into the last node body
    assert "ac_mapping" not in bodies["task-2"]


def test_split_fence_aware_fake_heading_not_split():
    # a code fence inside task-1 contains "### Task 2" — must NOT start a new node
    md = "\n".join([
        "# Plan", "",
        "<!-- fastship:node task-1 -->", "### Task 1", "real body",
        "```", "### Task 2  (this is a code sample, not a heading)", "```",
        "still task-1 body", "",
        "<!-- fastship:node task-2 -->", "### Task 2", "real task 2", "",
        "<!-- fastship:contract -->", "```json",
        json.dumps(block([node("task-1", outputs=["sym:a"], files=["src/a.rs"]),
                          node("task-2", deps=["task-1"], inputs=["sym:a"], outputs=["sym:b"], files=["src/b.rs"])])),
        "```", "",
    ])
    blk, _ = pt.extract_contract_block(md)
    root, bodies, err = pt.split_plan_tree(md, blk)
    assert err is None
    assert "still task-1 body" in bodies["task-1"]
    assert "code sample" in bodies["task-1"]  # the fenced fake heading stayed in task-1
    assert "real task 2" in bodies["task-2"]


def test_split_anchor_mismatch_fails():
    md = "\n".join([
        "# Plan", "",
        "<!-- fastship:node task-1 -->", "### Task 1", "body", "",
        "<!-- fastship:contract -->", "```json",
        json.dumps(block([node("task-1", files=["src/a.rs"]), node("task-2", deps=["task-1"], inputs=["root:base"], outputs=["sym:b"], files=["src/b.rs"])])),
        "```",
    ])
    blk, _ = pt.extract_contract_block(md)
    root, bodies, err = pt.split_plan_tree(md, blk)
    assert err is not None and "task-2" in err


# ── materialize_plan_tree ────────────────────────────────────────────────────
def test_materialize_writes_tree(tmp_path):
    md = plan_with_contract()
    out = str(tmp_path / "p.plantree")
    ok, msg, prov = pt.materialize_plan_tree(md, out, "deadbeef")
    assert ok, msg
    assert os.path.exists(os.path.join(out, "root.md"))
    assert os.path.exists(os.path.join(out, "nodes", "task-1.md"))
    assert os.path.exists(os.path.join(out, "nodes", "task-2.md"))
    assert os.path.exists(os.path.join(out, "briefs", "task-1.md"))
    sk = json.load(open(os.path.join(out, "skeleton.json")))
    assert sk["source_plan_sha256"] == "deadbeef"
    assert sk["tree_hash"] == prov["tree_hash"]
    assert [n["id"] for n in sk["nodes"]] == ["task-1", "task-2"]
    assert all(n["status"] == "pending" for n in sk["nodes"])
    # brief = root + this node; must not contain sibling body
    brief1 = open(os.path.join(out, "briefs", "task-1.md")).read()
    assert "## 设计" in brief1 and "do a" in brief1 and "do b" not in brief1


def test_materialize_idempotent(tmp_path):
    md = plan_with_contract()
    out = str(tmp_path / "p.plantree")
    ok1, _, prov1 = pt.materialize_plan_tree(md, out, "sha1")
    ok2, _, prov2 = pt.materialize_plan_tree(md, out, "sha1")
    assert ok1 and ok2
    assert prov1["tree_hash"] == prov2["tree_hash"]


def test_materialize_stale_cleanup(tmp_path):
    out = str(tmp_path / "p.plantree")
    # first plan has task-1 + task-2
    ok, _, _ = pt.materialize_plan_tree(plan_with_contract(), out, "sha1")
    assert ok and os.path.exists(os.path.join(out, "nodes", "task-2.md"))
    # second plan drops task-2 -> its node/brief files must be gone
    one_node = "\n".join([
        "# Plan", "", "<!-- fastship:node task-1 -->", "### Task 1", "do a", "",
        "<!-- fastship:contract -->", "```json",
        json.dumps(block([node("task-1", outputs=["sym:a"], files=["src/a.rs"])],
                         ac_mapping=[{"ac_id": "ac-1", "tasks": ["task-1"], "e2e": ["E2E"]}])),
        "```",
    ])
    ok2, msg2, _ = pt.materialize_plan_tree(one_node, out, "sha2")
    assert ok2, msg2
    assert not os.path.exists(os.path.join(out, "nodes", "task-2.md"))
    assert not os.path.exists(os.path.join(out, "briefs", "task-2.md"))


def test_materialize_fails_on_bad_graph(tmp_path):
    bad = "\n".join([
        "# Plan", "", "<!-- fastship:node task-1 -->", "### Task 1", "x", "",
        "<!-- fastship:contract -->", "```json",
        json.dumps(block([node("task-1", deps=["ghost"], files=["src/a.rs"])])),
        "```",
    ])
    ok, msg, prov = pt.materialize_plan_tree(bad, str(tmp_path / "o"), "sha")
    assert not ok and prov is None and "node graph" in msg


def test_materialize_fails_when_no_contract(tmp_path):
    ok, msg, prov = pt.materialize_plan_tree("# Plan\nno contract\n", str(tmp_path / "o"), "sha")
    assert not ok and prov is None


# ── files_changed_within ─────────────────────────────────────────────────────
def test_files_changed_within():
    ok, off = pt.files_changed_within(["src/a.rs", "./src/b.rs"], ["src/a.rs"])
    assert ok and off == []
    ok, off = pt.files_changed_within(["src/a.rs"], ["src/a.rs", "src/evil.rs"])
    assert not ok and off == ["src/evil.rs"]


# ── hardening fixes (codex + auditors) ───────────────────────────────────────
def test_bracket_filename_allowed():
    # Next.js/Remix dynamic routes are literal filenames, NOT globs.
    assert pt.canon_path("app/routes/[id].tsx") == "app/routes/[id].tsx"
    b = block([node("a", files=["app/routes/[id].tsx"])])
    ok, msg = pt.check_plan_node_graph(b)
    assert ok, msg


def test_star_still_rejected():
    assert pt.canon_path("src/*.rs") is None


def test_output_root_prefix_fails():
    b = block([node("a", outputs=["root:sneaky"], files=["src/a.rs"])])
    ok, msg = pt.check_plan_node_graph(b)
    assert not ok and "root:" in msg


def test_supporting_for_unknown_ac_fails():
    b = block([
        node("a", outputs=["sym:a"], files=["src/a.rs"]),
        node("b", outputs=["sym:b"], files=["src/b.rs"], supporting_for=["NOPE"]),
    ], ac_mapping=[{"ac_id": "ac-1", "tasks": ["a"], "e2e": ["E2E"]}])
    ok, msg = pt.check_plan_node_graph(b)
    assert not ok and "supporting_for" in msg and "NOPE" in msg


def test_ac_mapping_non_list_fails_clearly():
    b = {"nodes": [node("a")], "ac_mapping": {"not": "a list"}, "exclusive_forks": []}
    ok, msg = pt.check_plan_node_graph(b)
    assert not ok and "ac_mapping" in msg and "数组" in msg


def test_id_with_trailing_newline_fails():
    b = block([node("task-1\n", files=["src/a.rs"])])
    ok, msg = pt.check_plan_node_graph(b)
    assert not ok and "id" in msg


def test_case_insensitive_file_overlap_fails():
    b = block([
        node("a", outputs=["sym:a"], files=["src/Foo.rs"]),
        node("b", outputs=["sym:b"], files=["src/foo.rs"]),
    ])
    ok, msg = pt.check_plan_node_graph(b)
    assert not ok and "共享文件" in msg


def test_contract_before_last_anchor_fails():
    md = "\n".join([
        "# Plan", "",
        "<!-- fastship:node task-1 -->", "### Task 1", "a", "",
        "<!-- fastship:contract -->", "```json",
        json.dumps(block([node("task-1", outputs=["sym:a"], files=["src/a.rs"]),
                          node("task-2", deps=["task-1"], inputs=["sym:a"], outputs=["sym:b"], files=["src/b.rs"])],
                         ac_mapping=[{"ac_id":"ac-1","tasks":["task-1","task-2"],"e2e":["E"]}])),
        "```", "",
        "<!-- fastship:node task-2 -->", "### Task 2", "b", "",
    ])
    blk, _ = pt.extract_contract_block(md)
    root, bodies, err = pt.split_plan_tree(md, blk)
    assert err is not None and "之后" in err


def test_indented_fence_hides_anchor():
    # a marker inside a ≤3-space-indented code fence must NOT be a real anchor
    md = "\n".join([
        "# Plan", "",
        "<!-- fastship:node task-1 -->", "### Task 1", "body",
        "   ```", "   <!-- fastship:node fake -->", "   ```",
        "more task-1", "",
        "<!-- fastship:contract -->", "```json",
        json.dumps(block([node("task-1", outputs=["sym:a"], files=["src/a.rs"])],
                         ac_mapping=[{"ac_id":"ac-1","tasks":["task-1"],"e2e":["E"]}])),
        "```", "",
    ])
    blk, err = pt.extract_contract_block(md)
    assert err is None
    root, bodies, serr = pt.split_plan_tree(md, blk)
    assert serr is None and set(bodies) == {"task-1"} and "more task-1" in bodies["task-1"]


def test_verify_tree_integrity(tmp_path):
    out = str(tmp_path / "p.plantree")
    ok, _, prov = pt.materialize_plan_tree(plan_with_contract(), out, "sha")
    assert ok
    vok, vmsg = pt.verify_tree_integrity(out, prov["tree_hash"])
    assert vok, vmsg
    # tamper a node body → integrity FAILs
    p = os.path.join(out, "nodes", "task-1.md")
    open(p, "w").write("HIJACKED")
    vok, vmsg = pt.verify_tree_integrity(out, prov["tree_hash"])
    assert not vok and "篡改" in vmsg


def test_verify_tree_integrity_ignores_status(tmp_path):
    out = str(tmp_path / "p.plantree")
    ok, _, prov = pt.materialize_plan_tree(plan_with_contract(), out, "sha")
    assert ok
    # flipping status / manifest must NOT break integrity (mutable progress)
    sok, _ = pt.update_node_status(os.path.join(out, "skeleton.json"), "task-1",
                                   status="done", manifest={"files_changed": ["src/a.rs"]})
    assert sok
    vok, vmsg = pt.verify_tree_integrity(out, prov["tree_hash"])
    assert vok, vmsg
    sk = json.load(open(os.path.join(out, "skeleton.json")))
    n1 = next(n for n in sk["nodes"] if n["id"] == "task-1")
    assert n1["status"] == "done" and n1["manifest"]["files_changed"] == ["src/a.rs"]


def test_update_node_status_unknown_node(tmp_path):
    out = str(tmp_path / "p.plantree")
    ok, _, prov = pt.materialize_plan_tree(plan_with_contract(), out, "sha")
    assert ok
    sok, smsg = pt.update_node_status(prov["skeleton_path"], "ghost", status="done")
    assert not sok and "无此 node" in smsg


def test_update_node_status_bad_status(tmp_path):
    out = str(tmp_path / "p.plantree")
    ok, _, prov = pt.materialize_plan_tree(plan_with_contract(), out, "sha")
    assert ok
    sok, smsg = pt.update_node_status(prov["skeleton_path"], "task-1", status="bogus")
    assert not sok and "status" in smsg


def test_materialize_preserves_progress_on_unchanged_hash(tmp_path):
    out = str(tmp_path / "p.plantree")
    ok, _, prov = pt.materialize_plan_tree(plan_with_contract(), out, "sha")
    assert ok
    pt.update_node_status(prov["skeleton_path"], "task-1", status="done")
    # re-materialize the SAME plan → progress must be preserved (no wipe)
    ok2, msg2, prov2 = pt.materialize_plan_tree(plan_with_contract(), out, "sha")
    assert ok2 and prov2["tree_hash"] == prov["tree_hash"]
    sk = json.load(open(prov["skeleton_path"]))
    n1 = next(n for n in sk["nodes"] if n["id"] == "task-1")
    assert n1["status"] == "done", "progress wiped on unchanged-hash re-materialize"


def test_prov_carries_node_ids(tmp_path):
    ok, _, prov = pt.materialize_plan_tree(plan_with_contract(), str(tmp_path / "p.plantree"), "sha")
    assert ok and prov["node_ids"] == ["task-1", "task-2"]
