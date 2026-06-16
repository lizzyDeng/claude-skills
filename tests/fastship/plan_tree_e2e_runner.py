#!/usr/bin/env python3
"""Pure-Python E2E for fastship 计划树 (plan tree) decomposition. Zero mock.

Drives the REAL plan_tree.materialize_plan_tree + the REAL
orchestrator.validate_code_review against an embedded, deps-carrying plan constant
(real <!-- fastship:node --> anchors + a single <!-- fastship:contract --> block),
plus a REAL git repo so validate_code_review's `git diff` recheck sees real changes.

The 6 scenarios are named to align with the embedded plan's ac_mapping[].e2e list:
  1 plan_tree_skeleton_structure      — skeleton nodes id/deps/inputs/outputs/files complete,
                                         acyclic, no dangling, tree_hash/source recorded.
  2 plan_tree_node_self_contained     — nodes/<id>.md carries no sibling BODY (prose 'Task N' ok).
  3 plan_tree_driver_brief_no_full_plan — briefs/<id>.md = root + this node + dep contract,
                                          under a size threshold, no other node's body.
  4 plan_tree_files_changed_subset    — files_changed_within FAILs an out-of-bounds diff, passes legal.
  5 plan_tree_review_coverage         — validate_code_review FAILs on missing reviewed_node_id /
                                        wrong tree_hash, PASSes on full coverage (real git diff).
  6 plan_tree_unique_contract_block   — an echoed EXAMPLE json block + the real contract marker →
                                        extractor takes the marked block, never the example.

Each scenario asserts via turn(), every turn carries a non-empty `response` so the e2e
gate reports zero empty replies. Result schema mirrors plan_html / sniff runners.
Exit 0 iff every turn passes. The runner isolates its own tmp + env (strips
FASTSHIP_SESSION / STATE_HOME / REPO_ROOT, sets FASTSHIP_PLAN_HTML_OPEN=never).
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
FASTSHIP = os.path.join(ROOT, "skills", "fastship")
sys.path.insert(0, FASTSHIP)

import plan_tree as pt  # noqa: E402  (pure module, import after sys.path tweak)

# Brief size ceiling (spec L116: per-leaf brief stays ~6–14KB regardless of feature
# size). The embedded fixture is tiny, so a generous 5KB ceiling still proves the
# brief never balloons into the full multi-node plan.
BRIEF_MAX_BYTES = 5120

# Node ids that look like contract references but live ONLY in the example json
# block — they must never surface in the extracted contract.
EXAMPLE_NODE_ID = "example-decoy"

# ── embedded plan: real anchors + ONE contract marker + an echoed EXAMPLE block ──
_CONTRACT = {
    "nodes": [
        {"id": "task-1", "title": "HerChatDeps gains agent_loop deps",
         "deps": [], "inputs": ["root:HerChatDeps"], "outputs": ["sym:agent_loop_handle"],
         "files": ["services/api-server/src/her/her_chat.rs"]},
        {"id": "task-2", "title": "gallery rotation consumes loop handle",
         "deps": ["task-1"], "inputs": ["sym:agent_loop_handle"], "outputs": ["sym:gallery_rot"],
         "files": ["services/api-server/src/her/gallery.rs"]},
        {"id": "task-3", "title": "guardrails wrap rotation",
         "deps": ["task-2"], "inputs": ["sym:gallery_rot"], "outputs": ["sym:guardrails"],
         "files": ["services/api-server/src/her/guardrails.rs"]},
    ],
    "ac_mapping": [
        {"ac_id": "HAC1", "tasks": ["task-1"],
         "e2e": ["plan_tree_skeleton_structure", "plan_tree_node_self_contained"]},
        {"ac_id": "HAC2", "tasks": ["task-2"],
         "e2e": ["plan_tree_driver_brief_no_full_plan", "plan_tree_files_changed_subset"]},
        {"ac_id": "HAC3", "tasks": ["task-3"],
         "e2e": ["plan_tree_review_coverage", "plan_tree_unique_contract_block"]},
    ],
    "exclusive_forks": [],
}

# An EXAMPLE json block embedded in the plan PROSE — it carries nodes/ac_mapping too,
# so a naive "last json block with ac_mapping" extractor would mis-pick it. It has NO
# contract marker, so the marker-driven extractor must ignore it entirely.
_EXAMPLE_BLOCK = {
    "nodes": [{"id": EXAMPLE_NODE_ID, "title": "decoy", "deps": [],
               "inputs": ["root:decoy"], "outputs": ["sym:decoy"], "files": ["DECOY.rs"]}],
    "ac_mapping": [{"ac_id": "DECOY", "tasks": [EXAMPLE_NODE_ID], "e2e": ["decoy"]}],
    "exclusive_forks": [],
}

PLAN = "\n".join([
    "# her-loops 计划树 E2E fixture",
    "",
    "## 设计决策（root 共享层）",
    "root:HerChatDeps 在此声明。所有 node 共读本层。",
    "",
    "### 契约格式示例（仅说明，禁止被 extractor 选中）",
    "下面是一个 EXAMPLE 块，它也含 nodes + ac_mapping，但没有 contract 锚点：",
    "```json",
    json.dumps(_EXAMPLE_BLOCK, ensure_ascii=False, indent=2),
    "```",
    "",
    "<!-- fastship:node task-1 -->",
    "### Task 1: HerChatDeps gains agent_loop deps",
    "实现 agent_loop 句柄。BODY_MARKER_ONE 唯一正文标记。",
    "下面代码围栏里有个假标题，必须留在 task-1，不被切到别处：",
    "```rust",
    "// ## Task 2: 这是代码块内的假标题，fence-aware 必须忽略它",
    "fn wire_agent_loop() {}",
    "```",
    "",
    "<!-- fastship:node task-2 -->",
    "### Task 2: gallery rotation consumes loop handle",
    "实现轮换。BODY_MARKER_TWO 唯一正文标记。如 Task 1 所述（prose 引用，允许）。",
    "",
    "<!-- fastship:node task-3 -->",
    "### Task 3: guardrails wrap rotation",
    "实现护栏。BODY_MARKER_THREE 唯一正文标记。",
    "",
    "<!-- fastship:contract -->",
    "```json",
    json.dumps(_CONTRACT, ensure_ascii=False, indent=2),
    "```",
    "",
])

# Per-node unique body markers — used to detect cross-node leakage.
BODY_MARKERS = {"task-1": "BODY_MARKER_ONE", "task-2": "BODY_MARKER_TWO",
                "task-3": "BODY_MARKER_THREE"}


# ── scenario / turn plumbing (schema mirrors plan_html / sniff) ───────────────
scenarios = []
_cur = None


def scenario(name, desc=""):
    global _cur
    _cur = {"name": name, "description": desc, "rounds": [{"turns": []}]}
    scenarios.append(_cur)


def turn(action, cond, detail="", response=""):
    """One assertion → one turn. `response` is always non-empty so the e2e gate
    sees zero empty replies (clean evidence)."""
    cond = bool(cond)
    _cur["rounds"][0]["turns"].append({
        "action": action,
        "status": "pass" if cond else "fail",
        "passed": cond,
        "response": (response or ("ok" if cond else "FAILED"))[:300],
        "detail": str(detail)[:300],
    })
    return cond


# ── code-review gate fixtures (real validate_code_review) ────────────────────
def _gate(tree_hash, **over):
    g = {
        "gate": "PASS",
        "reviewed_against": "design.html",
        "reviewed_files": ["services/api-server/src/her/her_chat.rs",
                           "services/api-server/src/her/gallery.rs",
                           "services/api-server/src/her/guardrails.rs"],
        "design_fidelity_reviewed": True,
        "spec_compliance_reviewed": True,
        "quality_reviewed": True,
        "design_deviations": [], "spec_gaps": [], "quality_issues": [], "unverified_claims": [],
        "reviewed_plan_tree_sha256": tree_hash,
        "reviewed_node_ids": ["task-1", "task-2", "task-3"],
        "reviewed_manifests": [
            {"node_id": "task-1", "files_changed": ["services/api-server/src/her/her_chat.rs"]},
            {"node_id": "task-2", "files_changed": ["services/api-server/src/her/gallery.rs"]},
            {"node_id": "task-3", "files_changed": ["services/api-server/src/her/guardrails.rs"]},
        ],
    }
    g.update(over)
    return g


def _review_md(gate):
    return ("## Code Review\n### Per-node verdicts\n- task-1 OK\n- task-2 OK\n- task-3 OK\n"
            "### Design Fidelity\n- matches design\n### Contract Gate\n```json\n"
            + json.dumps(gate, ensure_ascii=False, indent=2) + "\n```\n### GATE: PASS\n")


def _git(repo, *args):
    subprocess.run(["git", "-C", repo, *args], check=True,
                   capture_output=True, text=True)


def run(tmp):
    src_sha = hashlib.sha256(PLAN.encode("utf-8")).hexdigest()

    # ── materialize once into an isolated tree dir (real side effects) ────────
    tree_dir = os.path.join(tmp, "plan.plantree")
    ok, msg, prov = pt.materialize_plan_tree(PLAN, tree_dir, src_sha)

    block, blk_err = pt.extract_contract_block(PLAN)
    root_text, bodies, split_err = (None, {}, "no block") if block is None \
        else pt.split_plan_tree(PLAN, block)

    # ── Scenario 1: skeleton structure ───────────────────────────────────────
    scenario("plan_tree_skeleton_structure",
             "skeleton nodes id/deps/inputs/outputs/files complete, acyclic, no dangling, "
             "tree_hash/source recorded")
    turn("materialize succeeds", ok, msg, response=msg)
    sk = json.load(open(prov["skeleton_path"], encoding="utf-8")) if ok else {}
    nodes = sk.get("nodes", [])
    turn("skeleton has all 3 nodes", len(nodes) == 3, f"n={len(nodes)}",
         response=f"{len(nodes)} nodes")
    req_fields = ("id", "deps", "inputs", "outputs", "files")
    complete = all(all(f in n and (f == "deps" or n[f]) for f in req_fields) for n in nodes)
    turn("every node carries id/deps/inputs/outputs/files", complete,
         response="all fields present" if complete else "missing field")
    # graph validity is what the materialize hard-step already enforced; re-assert
    g_ok, g_msg = pt.check_plan_node_graph(block) if block else (False, "no block")
    turn("node graph acyclic + no dangling deps/inputs", g_ok, g_msg, response=g_msg)
    turn("tree_hash recorded in skeleton == provenance",
         ok and sk.get("tree_hash") == prov["tree_hash"],
         response=str(sk.get("tree_hash", ""))[:24])
    turn("source_plan_sha256 recorded in skeleton == plan sha",
         ok and sk.get("source_plan_sha256") == src_sha,
         response=str(sk.get("source_plan_sha256", ""))[:24])
    # negative: a cyclic block must FAIL the graph check (proves the gate has teeth)
    cyclic = json.loads(json.dumps(_CONTRACT))
    cyclic["nodes"][0]["deps"] = ["task-3"]            # task-1 -> task-3 -> task-2 -> task-1
    c_ok, c_msg = pt.check_plan_node_graph(cyclic)
    turn("NEGATIVE: cyclic deps rejected", (not c_ok) and "环" in c_msg, c_msg,
         response=c_msg)
    # negative: dangling dep
    dang = json.loads(json.dumps(_CONTRACT))
    dang["nodes"][1]["deps"] = ["ghost"]
    d_ok, d_msg = pt.check_plan_node_graph(dang)
    turn("NEGATIVE: dangling dep rejected", (not d_ok) and "不存在" in d_msg, d_msg,
         response=d_msg)

    # ── Scenario 2: node self-contained ──────────────────────────────────────
    scenario("plan_tree_node_self_contained",
             "nodes/<id>.md carries no sibling BODY marker (prose 'Task N' allowed)")
    turn("split succeeds", split_err is None, str(split_err),
         response=split_err or "split ok")
    for nid, marker in BODY_MARKERS.items():
        body = bodies.get(nid, "")
        own = marker in body
        siblings = [BODY_MARKERS[o] for o in BODY_MARKERS if o != nid]
        leak = [s for s in siblings if s in body]
        turn(f"{nid}.md keeps own body, no sibling body",
             own and not leak, f"own={own} leak={leak}",
             response=f"{nid}: own={own} leak={leak}")
    turn("task-1 retains its OWN fenced fake '## Task 2' heading (fence-aware)",
         "## Task 2: 这是代码块内的假标题" in bodies.get("task-1", ""),
         response="fenced heading kept in task-1")
    turn("prose cross-ref 'Task 1 所述' allowed to remain in task-2",
         "Task 1 所述" in bodies.get("task-2", ""),
         response="prose ref tolerated")
    # filesystem artifact must match in-memory body (materialize wrote it verbatim)
    if ok:
        disk_body = open(os.path.join(tree_dir, "nodes", "task-2.md"), encoding="utf-8").read()
        turn("on-disk nodes/task-2.md == split body, no task-1 body",
             "BODY_MARKER_TWO" in disk_body and "BODY_MARKER_ONE" not in disk_body,
             response="disk body self-contained")

    # ── Scenario 3: driver brief excludes full plan ──────────────────────────
    scenario("plan_tree_driver_brief_no_full_plan",
             "briefs/<id>.md = root + this node + dep contract; under size threshold; "
             "no other node's body")
    nbi = {n["id"]: n for n in (block["nodes"] if block else [])}
    for nid in BODY_MARKERS:
        brief = pt.build_brief(root_text, bodies[nid], nbi[nid], nbi) if root_text else ""
        size = len(brief.encode("utf-8"))
        own = BODY_MARKERS[nid] in brief
        siblings = [BODY_MARKERS[o] for o in BODY_MARKERS if o != nid]
        leak = [s for s in siblings if s in brief]
        has_root = "HerChatDeps 在此声明" in brief
        turn(f"brief[{nid}] has root + own body, no sibling body, < {BRIEF_MAX_BYTES}B",
             own and has_root and not leak and size < BRIEF_MAX_BYTES,
             f"size={size} own={own} root={has_root} leak={leak}",
             response=f"{nid}: {size}B root={has_root} leak={leak}")
    # dep-output contract IS expected in a brief (it's the wiring, not sibling body):
    brief2 = pt.build_brief(root_text, bodies["task-2"], nbi["task-2"], nbi)
    turn("brief[task-2] declares upstream dep output sym (wiring, not body)",
         "sym:agent_loop_handle" in brief2 and "BODY_MARKER_ONE" not in brief2,
         response="dep contract present, upstream body absent")
    # on-disk brief matches
    if ok:
        disk_brief = open(os.path.join(tree_dir, "briefs", "task-3.md"), encoding="utf-8").read()
        turn("on-disk briefs/task-3.md self-contained (no task-1/2 body)",
             "BODY_MARKER_THREE" in disk_brief
             and "BODY_MARKER_ONE" not in disk_brief
             and "BODY_MARKER_TWO" not in disk_brief,
             response="disk brief self-contained")

    # ── Scenario 4: files_changed_within subset enforcement ──────────────────
    scenario("plan_tree_files_changed_subset",
             "files_changed_within FAILs an out-of-bounds diff, passes a legal subset")
    nf = ["services/api-server/src/her/gallery.rs"]
    legal_ok, legal_off = pt.files_changed_within(nf, ["services/api-server/src/her/gallery.rs"])
    turn("legal in-bounds diff passes", legal_ok and not legal_off,
         response=f"ok={legal_ok} offending={legal_off}")
    over_ok, over_off = pt.files_changed_within(
        nf, ["services/api-server/src/her/gallery.rs", "services/api-server/src/her/SECRET.rs"])
    turn("out-of-bounds file FAILs with the offender named",
         (not over_ok) and over_off == ["services/api-server/src/her/SECRET.rs"],
         response=f"ok={over_ok} offending={over_off}")
    norm_ok, norm_off = pt.files_changed_within(nf, ["./services/api-server/src/her/gallery.rs"])
    turn("'./'-prefixed path canonicalizes to in-bounds (no false positive)",
         norm_ok and not norm_off, response=f"ok={norm_ok} offending={norm_off}")
    empty_ok, empty_off = pt.files_changed_within(nf, [])
    turn("empty diff is trivially within bounds", empty_ok and not empty_off,
         response="empty diff ok")

    # ── Scenario 5: 2.5 review coverage via REAL validate_code_review ─────────
    scenario("plan_tree_review_coverage",
             "validate_code_review FAILs on missing reviewed_node_id / wrong tree_hash, "
             "PASSes on full coverage (real git diff)")
    import orchestrator as o
    repo = os.path.join(tmp, "repo")
    os.makedirs(os.path.join(repo, "services", "api-server", "src", "her"))
    subprocess.run(["git", "init", "-q", repo], check=True)
    _git(repo, "config", "user.email", "e2e@test")
    _git(repo, "config", "user.name", "e2e")
    open(os.path.join(repo, "design.html"), "w").write("<html>design</html>")
    her = os.path.join(repo, "services", "api-server", "src", "her")
    for f in ("her_chat.rs", "gallery.rs", "guardrails.rs"):
        open(os.path.join(her, f), "w").write(f"// {f}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    # real changes so `git diff HEAD` reports all three node files (the changed set)
    for f in ("her_chat.rs", "gallery.rs", "guardrails.rs"):
        open(os.path.join(her, f), "w").write(f"// {f} v2 — implemented\n")

    repo_tree = os.path.join(repo, "plan.plantree")
    rok, rmsg, rprov = pt.materialize_plan_tree(PLAN, repo_tree, src_sha)
    rsk = json.load(open(rprov["skeleton_path"], encoding="utf-8"))
    for n in rsk["nodes"]:
        n["status"] = "done"
    json.dump(rsk, open(rprov["skeleton_path"], "w"), ensure_ascii=False)

    claude = os.path.join(repo, ".claude")
    os.makedirs(claude)
    review_path = os.path.join(claude, ".fastship-code-review.md")

    orig_root = o._repo_root
    o._repo_root = lambda: repo
    try:
        changed = sorted(o._changed_files(None))
        turn("real git diff reports the 3 node files as changed",
             changed == ["services/api-server/src/her/gallery.rs",
                         "services/api-server/src/her/guardrails.rs",
                         "services/api-server/src/her/her_chat.rs"],
             response=str(changed))

        def make_orch(gate):
            open(review_path, "w").write(_review_md(gate))
            orch = {"artifacts": {"code_review_path": review_path}}
            o.record_step_artifact(orch, "2.5", review_path, source="e2e")
            o._record_plan_tree_artifact(orch, rprov)
            return orch

        ok_full, msg_full = o.validate_code_review(make_orch(_gate(rprov["tree_hash"])), {})
        turn("full coverage + correct tree_hash → PASS", ok_full, msg_full,
             response=msg_full or "PASS")

        ok_miss, msg_miss = o.validate_code_review(
            make_orch(_gate(rprov["tree_hash"], reviewed_node_ids=["task-1", "task-2"])), {})
        turn("missing reviewed_node_id (task-3) → FAIL",
             (not ok_miss) and "task-3" in msg_miss and "reviewed_node_ids" in msg_miss,
             msg_miss, response=msg_miss)

        ok_hash, msg_hash = o.validate_code_review(
            make_orch(_gate("deadbeef-stale-hash")), {})
        turn("wrong tree_hash (stale review) → FAIL",
             (not ok_hash) and "tree_hash" in msg_hash, msg_hash, response=msg_hash)

        # negative: a node still pending in the skeleton must FAIL even with full review
        for n in rsk["nodes"]:
            n["status"] = "pending"
        json.dump(rsk, open(rprov["skeleton_path"], "w"), ensure_ascii=False)
        ok_undone, msg_undone = o.validate_code_review(make_orch(_gate(rprov["tree_hash"])), {})
        turn("required node not done in skeleton → FAIL",
             (not ok_undone) and "node 未完成" in msg_undone, msg_undone,
             response=msg_undone)
    finally:
        o._repo_root = orig_root

    # ── Scenario 6: unique contract block extractor ──────────────────────────
    scenario("plan_tree_unique_contract_block",
             "plan carries an echoed EXAMPLE json block AND the real contract marker → "
             "extractor takes the marked block, never the example")
    turn("extractor returns a block with no error", block is not None and blk_err is None,
         str(blk_err), response=blk_err or "block extracted")
    picked = [n["id"] for n in (block["nodes"] if block else [])]
    turn("picked the marked contract (task-1/2/3), not the EXAMPLE decoy",
         picked == ["task-1", "task-2", "task-3"] and EXAMPLE_NODE_ID not in picked,
         str(picked), response=str(picked))
    turn("decoy node id absent from skeleton too",
         all(n["id"] != EXAMPLE_NODE_ID for n in nodes),
         response="no decoy in skeleton")
    # NEGATIVE: two real contract markers → ambiguous → error (never silently pick one)
    dup_plan = PLAN + "\n<!-- fastship:contract -->\n```json\n" + json.dumps(_CONTRACT) + "\n```\n"
    dup_block, dup_err = pt.extract_contract_block(dup_plan)
    turn("NEGATIVE: two contract markers → ambiguous error, no silent pick",
         dup_block is None and dup_err is not None and "仅一个" in dup_err,
         dup_err, response=dup_err or "ambiguous rejected")
    # NEGATIVE: a contract marker inside a code fence must NOT count (fence-aware)
    fenced = "\n".join([
        "# Plan", "```text", "<!-- fastship:contract -->", "```", "",
        "<!-- fastship:node task-1 -->", "### Task 1", "body", "",
        "<!-- fastship:contract -->", "```json", json.dumps(_CONTRACT), "```",
    ])
    fb, fe = pt.extract_contract_block(fenced)
    turn("NEGATIVE: marker inside code fence ignored (only the real one counts)",
         fb is not None and fe is None and [n["id"] for n in fb["nodes"]] == ["task-1", "task-2", "task-3"],
         str(fe), response="fenced marker ignored")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-o", "--out", default="/tmp/plan_tree_e2e_result.json")
    args = ap.parse_args()

    # ── env + tmp isolation (do not inherit a live fastship session) ──────────
    for var in ("FASTSHIP_SESSION", "FASTSHIP_STATE_HOME", "FASTSHIP_REPO_ROOT",
                "CLAUDE_PROJECT_DIR"):
        os.environ.pop(var, None)
    os.environ["FASTSHIP_PLAN_HTML_OPEN"] = "never"

    with tempfile.TemporaryDirectory(prefix="plan-tree-e2e-") as tmp:
        run(tmp)

    all_turns = [t for sc in scenarios for t in sc["rounds"][0]["turns"]]
    passed = sum(1 for t in all_turns if t["passed"])
    failed = len(all_turns) - passed
    result = {
        "scenarios": scenarios,
        "turns": len(all_turns),
        "passed": passed,
        "failed": failed,
        "timestamp": datetime.now().isoformat(),
    }
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(json.dumps({"scenarios": len(scenarios), "turns": len(all_turns),
                      "passed": passed, "failed": failed, "out": args.out},
                     ensure_ascii=False))
    for sc in scenarios:
        tns = sc["rounds"][0]["turns"]
        ok = all(t["passed"] for t in tns)
        print(f"  {'✅' if ok else '❌'} {sc['name']}  ({sum(t['passed'] for t in tns)}/{len(tns)})")
        for t in tns:
            if not t["passed"]:
                print(f"      ❌ {t['action']}  {t['detail']}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
