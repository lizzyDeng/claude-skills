"""Plan tree decomposition — turn a single bloated plan.md into a 计划树.

Spec: skills/fastship/docs/2026-06-16-plan-tree-decomposition-design.md

The plan is ALREADY a flattened tree (shared root sections + per-task bodies +
a contract block). This module splits it, WITHOUT reducing per-task richness, so
that the Phase-2 execution layer never holds the whole plan:

  root.md          — shared layer (设计决策 / AC 契约 / File structure). Every node reads it.
  nodes/<id>.md    — one task's FULL body, verbatim. Only that task's subagent reads it.
  briefs/<id>.md   — root + this node + dep-output contracts. Pre-assembled for the subagent.
  skeleton.json    — node id/deps/inputs/outputs/files + progress status. ONLY the driver holds it.

All functions here are pure (no orchestrator import) so they unit-test in isolation.
The only side effect is materialize_plan_tree(), which writes the derived files.

Authoring contract (emitted by the 1.4 instruction, enforced here):
  - Each task body is preceded by an HTML-comment anchor:  <!-- fastship:node <id> -->
  - The shared root layer is everything before the first node anchor (an optional
    explicit <!-- fastship:root --> / <!-- fastship:/root --> pair is tolerated and stripped).
  - Exactly one contract block, marked by  <!-- fastship:contract -->  immediately
    above its ```json fence, carrying nodes + ac_mapping + exclusive_forks together.
HTML-comment anchors are invisible in rendered markdown/HTML and skipped inside code
fences, so they survive round-tripping and never collide with real headings.
"""

import hashlib
import json
import os
import re
import shutil

# ── anchors / markers ────────────────────────────────────────────────────────
# \A..\Z (not ^..$): Python's $ matches before a trailing \n, which would let an id
# carry a newline and become a control-char filename nodes/<id>.md.
NODE_ID_RE = re.compile(r"\A[a-z0-9][a-z0-9_-]*\Z")
# Fence lines tolerate ≤3 leading spaces (CommonMark), so a marker inside a slightly
# indented code fence is correctly treated as code, not a real anchor.
_FENCE_LINE_RE = re.compile(r"^[ ]{0,3}(`{3,}|~{3,})(.*)$")
_NODE_ANCHOR_RE = re.compile(r"^\s*<!--\s*fastship:node\s+(\S+)\s*-->\s*$")
_ROOT_OPEN_RE = re.compile(r"^\s*<!--\s*fastship:root\s*-->\s*$")
_ROOT_CLOSE_RE = re.compile(r"^\s*<!--\s*fastship:/root\s*-->\s*$")
_CONTRACT_RE = re.compile(r"^\s*<!--\s*fastship:contract\s*-->\s*$")
_JSON_FENCE_OPEN_RE = re.compile(r"^[ ]{0,3}(`{3,}|~{3,})\s*json\s*$", re.IGNORECASE)

SKELETON_VERSION = 1
# Only real wildcards reject a node.files declaration. Literal brackets are legal in
# framework filenames (Next.js/Remix dynamic routes: app/routes/[id].tsx), so they are
# NOT treated as glob — a changed file like [id].tsx must canonicalize, not vanish.
_GLOB_CHARS = set("*?")


# ── fence-aware line scanning ────────────────────────────────────────────────
def _scan_lines(md):
    """Yield (idx, line, in_fence) for each line. A column-0 run of ≥3 backticks
    or ≥3 tildes toggles the fence; the matching close needs the SAME char & ≥ len.
    Mirrors orchestrator._FENCE_LINE_RE semantics so the two agree on what is code."""
    lines = md.split("\n")
    fence = None  # (char, length) when open
    out = []
    for idx, line in enumerate(lines):
        m = _FENCE_LINE_RE.match(line)
        if m:
            run = m.group(1)
            char, length = run[0], len(run)
            if fence is None:
                fence = (char, length)
                out.append((idx, line, True))  # the opening fence line is code
                continue
            elif char == fence[0] and length >= fence[1] and m.group(2).strip() == "":
                fence = None
                out.append((idx, line, True))  # the closing fence line is code
                continue
            out.append((idx, line, True))
            continue
        out.append((idx, line, fence is not None))
    return lines, out


# ── path canonicalization ────────────────────────────────────────────────────
def canon_path(p):
    """Canonical repo-relative key for a file path, or None if not a concrete
    repo-relative file (rejects abs / .. / glob / dir-trailing-slash / empty)."""
    if not isinstance(p, str):
        return None
    s = p.strip()
    if not s or s.endswith("/"):
        return None
    if os.path.isabs(s):
        return None
    if any(c in _GLOB_CHARS for c in s):
        return None
    s = s.replace("\\", "/")
    if s.startswith("./"):
        s = s[2:]
    parts = s.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    return "/".join(parts)


# ── contract block extraction (shared by validate / split / grill) ────────────
def extract_contract_block(plan_md):
    """Return (block_dict, err). The plan must carry EXACTLY ONE contract block,
    marked by `<!-- fastship:contract -->` immediately above a ```json fence.

      0 markers  -> (None, None)   — caller decides (feature=FAIL, bugfix=skip).
      >1 markers -> (None, msg)    — ambiguous; never silently take one.
      bad json   -> (None, msg).
    This is marker-driven (not "last block carrying ac_mapping"), so an echoed
    example JSON block in the plan body can never be mistaken for the contract."""
    lines, scan = _scan_lines(plan_md)
    marker_idxs = [idx for idx, line, in_fence in scan
                   if not in_fence and _CONTRACT_RE.match(line)]
    if not marker_idxs:
        return None, None
    if len(marker_idxs) > 1:
        return None, f"plan 含 {len(marker_idxs)} 个 <!-- fastship:contract --> 标记，须且仅一个"
    start = marker_idxs[0]
    # find the json fence after the marker (skip blank lines)
    i = start + 1
    n = len(lines)
    while i < n and lines[i].strip() == "":
        i += 1
    if i >= n or not _JSON_FENCE_OPEN_RE.match(lines[i]):
        return None, "<!-- fastship:contract --> 之后未紧跟 ```json 围栏"
    fence_run = _FENCE_LINE_RE.match(lines[i]).group(1)
    fchar, flen = fence_run[0], len(fence_run)
    body = []
    i += 1
    closed = False
    while i < n:
        m = _FENCE_LINE_RE.match(lines[i])
        if m and m.group(1)[0] == fchar and len(m.group(1)) >= flen and m.group(2).strip() == "":
            closed = True
            break
        body.append(lines[i])
        i += 1
    if not closed:
        return None, "contract ```json 围栏未闭合"
    try:
        obj = json.loads("\n".join(body))
    except json.JSONDecodeError as e:
        return None, f"contract block JSON 解析失败: {e}"
    if not isinstance(obj, dict):
        return None, "contract block 必须是 JSON object"
    return obj, None


# ── node graph validation (pure) ─────────────────────────────────────────────
def _ancestors(node_id, dep_map):
    """All transitive deps of node_id via dep_map (id -> set(dep ids))."""
    seen = set()
    stack = list(dep_map.get(node_id, ()))
    while stack:
        d = stack.pop()
        if d in seen:
            continue
        seen.add(d)
        stack.extend(dep_map.get(d, ()))
    return seen


def check_plan_node_graph(block):
    """Pure (ok, msg). FAIL on any structural / typing / graph violation so the
    materialize step and Phase-2 driver can trust the skeleton. No I/O."""
    if not isinstance(block, dict):
        return False, "contract block 必须是 JSON object"
    nodes = block.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        return False, "nodes 必须是非空数组"

    ids = []
    files_by_node = {}
    outputs_by_node = {}
    dep_map = {}
    all_outputs = {}  # output symbol -> producing node id
    str_list_fields = ("deps", "inputs", "outputs", "files")
    for n in nodes:
        if not isinstance(n, dict):
            return False, "nodes 含非 object 项"
        nid = n.get("id")
        if not isinstance(nid, str) or not NODE_ID_RE.match(nid):
            return False, f"node id 非法（须匹配 ^[a-z0-9][a-z0-9_-]*$）: {nid!r}"
        if nid in dep_map:
            return False, f"node id 重复: {nid}"
        title = n.get("title")
        if not isinstance(title, str) or not title.strip():
            return False, f"node {nid} 缺少非空 title"
        for fld in str_list_fields:
            v = n.get(fld)
            if not isinstance(v, list):
                return False, f"node {nid} 的 {fld} 必须是数组"
            if fld != "deps" and not v:
                return False, f"node {nid} 的 {fld} 不能为空"
            for x in v:
                if not isinstance(x, str) or not x.strip():
                    return False, f"node {nid} 的 {fld} 含空/非字符串项"
        ids.append(nid)
        dep_map[nid] = set(n["deps"])
        outputs_by_node[nid] = list(n["outputs"])
        # canonical files
        canon = []
        for f in n["files"]:
            ck = canon_path(f)
            if ck is None:
                return False, f"node {nid} 的 files 含非法路径（须 repo-relative 具体文件，禁 glob/目录/绝对/..）: {f!r}"
            canon.append(ck)
        files_by_node[nid] = set(canon)
        for out in n["outputs"]:
            if out.startswith("root:"):
                return False, (f"node {nid} 的 output 不能用 root: 前缀: {out} "
                               "— root: 是 root 层声明,会让下游用 root:x 掩盖真实 node 依赖")
            if out in all_outputs:
                return False, f"outputs 全局重复: {out}（{all_outputs[out]} 与 {nid} 都产出 — 会让下游 input 解析歧义）"
            all_outputs[out] = nid

    id_set = set(ids)
    # dangling deps
    for nid in ids:
        for d in dep_map[nid]:
            if d not in id_set:
                return False, f"node {nid} 的 deps 指向不存在的 node: {d}"
            if d == nid:
                return False, f"node {nid} 依赖自身"
    # cycle detection (Kahn)
    indeg = {nid: 0 for nid in ids}
    for nid in ids:
        for _d in dep_map[nid]:
            indeg[nid] += 1
    queue = [nid for nid in ids if indeg[nid] == 0]
    visited = 0
    # reverse adjacency: who depends on nid
    dependents = {nid: [] for nid in ids}
    for nid in ids:
        for d in dep_map[nid]:
            dependents[d].append(nid)
    while queue:
        cur = queue.pop()
        visited += 1
        for child in dependents[cur]:
            indeg[child] -= 1
            if indeg[child] == 0:
                queue.append(child)
    if visited != len(ids):
        return False, "nodes 依赖图存在环（拓扑排不出）"
    # dangling inputs: each input is root:<sym> OR an output of a transitive ancestor
    for nid in ids:
        anc = _ancestors(nid, dep_map)
        anc_outputs = set()
        for a in anc:
            anc_outputs.update(outputs_by_node[a])
        for inp in nodes_input_list(nodes, nid):
            if inp.startswith("root:"):
                continue
            if inp in anc_outputs:
                continue
            if inp in all_outputs:
                return False, (f"node {nid} 的 input {inp} 由 {all_outputs[inp]} 产出，"
                               f"但 {all_outputs[inp]} 不在 {nid} 的（传递）deps 中 — input/deps 不一致")
            return False, f"node {nid} 的 input 悬空（非 root: 声明、非任何上游 output）: {inp}"
    # ac_mapping.tasks reference + orphan coverage
    ac_mapping = block.get("ac_mapping")
    if ac_mapping is not None and not isinstance(ac_mapping, list):
        return False, "ac_mapping 必须是数组"
    referenced = set()
    ac_ids = set()
    if isinstance(ac_mapping, list):
        for entry in ac_mapping:
            if isinstance(entry, dict):
                aid = entry.get("ac_id")
                if isinstance(aid, str) and aid.strip():
                    ac_ids.add(aid)
                tasks = entry.get("tasks")
                if isinstance(tasks, list):
                    for t in tasks:
                        if isinstance(t, str):
                            referenced.add(t)
    for ref in referenced:
        if ref not in id_set:
            return False, f"ac_mapping.tasks 引用不存在的 node id: {ref}"
    for n in nodes:
        nid = n["id"]
        if nid in referenced:
            continue
        sup = n.get("supporting_for")
        if isinstance(sup, list) and sup and all(isinstance(s, str) and s.strip() for s in sup):
            unknown = sorted(s for s in sup if s not in ac_ids)
            if unknown:
                return False, (f"node {nid} 的 supporting_for 引用不存在的 ac_id: {unknown} "
                               "— 必须引用真实 ac_mapping[].ac_id,否则覆盖归属是空的")
            continue
        return False, (f"游离 node {nid}：既未被 ac_mapping.tasks 引用、又无（引用真实 ac_id 的）"
                       "supporting_for — 无覆盖归属")
    # file overlap without a dependency edge. case-insensitive: macOS APFS / Windows
    # treat src/Foo.rs and src/foo.rs as ONE physical file — two parallel nodes editing
    # it would still collide, which is exactly what this gate prevents.
    casefold_files = {nid: {f.casefold() for f in files_by_node[nid]} for nid in ids}
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = ids[i], ids[j]
            shared = casefold_files[a] & casefold_files[b]
            if not shared:
                continue
            if b in _ancestors(a, dep_map) or a in _ancestors(b, dep_map):
                continue
            return False, (f"node {a} 与 {b} 共享文件 {sorted(shared)} 却无依赖边相连 "
                           "— 并行改同一文件会冲突")
    return True, f"node graph OK（{len(ids)} 节点，DAG 无环/无悬空/文件无未声明重叠）"


def nodes_input_list(nodes, nid):
    for n in nodes:
        if n.get("id") == nid:
            v = n.get("inputs")
            return v if isinstance(v, list) else []
    return []


# ── fence-aware split into root + node bodies ────────────────────────────────
def split_plan_tree(plan_md, block):
    """Return (root_text, {id: node_body}, err). Anchor-driven, fence-aware.

    root  = content before the first node anchor (explicit <!-- fastship:root -->
            / <!-- fastship:/root --> wrapper lines are stripped if present).
    node  = anchor line (exclusive) .. next node anchor / contract marker / EOF.
    Every block node id must have exactly one anchor and vice versa."""
    nodes = block.get("nodes") or []
    declared = [n.get("id") for n in nodes if isinstance(n, dict)]
    lines, scan = _scan_lines(plan_md)

    node_anchor_at = {}  # idx -> id
    contract_idx = None
    seen_order = []
    for idx, line, in_fence in scan:
        if in_fence:
            continue
        m = _NODE_ANCHOR_RE.match(line)
        if m:
            nid = m.group(1)
            if nid in node_anchor_at.values():
                return None, None, f"node 锚点重复: {nid}"
            node_anchor_at[idx] = nid
            seen_order.append(nid)
            continue
        if _CONTRACT_RE.match(line) and contract_idx is None:
            contract_idx = idx

    anchor_ids = set(node_anchor_at.values())
    if anchor_ids != set(declared):
        missing = set(declared) - anchor_ids
        extra = anchor_ids - set(declared)
        bits = []
        if missing:
            bits.append(f"contract 有但正文缺锚点: {sorted(missing)}")
        if extra:
            bits.append(f"正文有锚点但 contract 无此 node: {sorted(extra)}")
        return None, None, "node 锚点与 contract.nodes 不一致 — " + "；".join(bits)

    anchor_positions = sorted(node_anchor_at.keys())
    first_anchor = anchor_positions[0]

    # The contract block must sit AFTER every node anchor (it is the plan tail). A
    # contract before the last anchor would otherwise be sliced into root.md / a node
    # body, leaking every node's I/O wiring into root and every leaf brief.
    if contract_idx is not None and contract_idx < anchor_positions[-1]:
        return None, None, "contract 块（<!-- fastship:contract -->）须在所有 node 锚点之后"

    # root = lines[0 : first_anchor], stripping any explicit root wrapper lines
    root_lines = []
    for idx in range(0, first_anchor):
        line = lines[idx]
        if _ROOT_OPEN_RE.match(line) or _ROOT_CLOSE_RE.match(line):
            continue
        root_lines.append(line)
    root_text = "\n".join(root_lines).strip() + "\n"

    # node bodies
    boundaries = anchor_positions + [len(lines)]
    node_bodies = {}
    for k, start in enumerate(anchor_positions):
        nid = node_anchor_at[start]
        end = boundaries[k + 1]
        if contract_idx is not None and start < contract_idx < end:
            end = contract_idx
        # drop any stray root-wrapper markers (a misplaced </root> after first anchor)
        body_lines = [ln for ln in lines[start + 1:end]
                      if not (_ROOT_OPEN_RE.match(ln) or _ROOT_CLOSE_RE.match(ln))]
        node_bodies[nid] = "\n".join(body_lines).strip() + "\n"
    return root_text, node_bodies, None


# ── brief assembly (pre-wired per-leaf context) ──────────────────────────────
def build_brief(root_text, node_body, node, nodes_by_id):
    """root + this node body + the declared dep-output contracts. At author time
    deps contribute their DECLARED outputs; Phase 2 replaces these with the actual
    upstream output manifest at run time."""
    parts = [root_text.rstrip(), ""]
    deps = node.get("deps") or []
    if deps:
        parts.append("## 依赖输入契约（来自上游 node 声明的 outputs）")
        for d in deps:
            up = nodes_by_id.get(d, {})
            outs = ", ".join(up.get("outputs", []) or []) or "(none declared)"
            parts.append(f"- **{d}** → {outs}")
        parts.append("")
    parts.append(f"## 本节点实现（node {node.get('id')}）")
    parts.append(node_body.rstrip())
    return "\n".join(parts).strip() + "\n"


# ── tree hash ────────────────────────────────────────────────────────────────
def compute_tree_hash(root_text, node_bodies, skeleton_core):
    h = hashlib.sha256()
    h.update(b"root\0")
    h.update(root_text.encode("utf-8"))
    for nid in sorted(node_bodies):
        h.update(b"\0node\0")
        h.update(nid.encode("utf-8"))
        h.update(b"\0")
        h.update(node_bodies[nid].encode("utf-8"))
    h.update(b"\0skeleton\0")
    h.update(json.dumps(skeleton_core, sort_keys=True, ensure_ascii=False).encode("utf-8"))
    return h.hexdigest()


# ── materialize (the only side-effecting entry) ──────────────────────────────
def materialize_plan_tree(plan_md, out_dir, source_plan_sha256):
    """Hard step: split + validate + write derived files idempotently.
    Returns (ok, msg, provenance). provenance = {plan_tree_dir, skeleton_path,
    tree_hash, source_plan_sha256}. On any failure returns (False, msg, None) and
    writes nothing partial that could be mistaken for a valid tree."""
    block, err = extract_contract_block(plan_md)
    if err:
        return False, f"contract block: {err}", None
    if block is None:
        return False, "plan 缺少 <!-- fastship:contract --> 契约块", None
    ok, msg = check_plan_node_graph(block)
    if not ok:
        return False, f"node graph: {msg}", None
    root_text, node_bodies, serr = split_plan_tree(plan_md, block)
    if serr:
        return False, f"split: {serr}", None

    nodes = block["nodes"]
    nodes_by_id = {n["id"]: n for n in nodes}

    skeleton_nodes = []
    for n in nodes:
        skeleton_nodes.append({
            "id": n["id"],
            "title": n["title"],
            "deps": list(n["deps"]),
            "inputs": list(n["inputs"]),
            "outputs": list(n["outputs"]),
            "files": [canon_path(f) for f in n["files"]],
            "status": "pending",
            "manifest": None,
        })
    skeleton_core = {
        "version": SKELETON_VERSION,
        "nodes": skeleton_nodes,
        "ac_mapping": block.get("ac_mapping", []),
        "exclusive_forks": block.get("exclusive_forks", []),
    }
    tree_hash = compute_tree_hash(root_text, node_bodies, skeleton_core)
    skeleton_path = os.path.join(out_dir, "skeleton.json")
    prov = {
        "plan_tree_dir": os.path.abspath(out_dir),
        "skeleton_path": os.path.abspath(skeleton_path),
        "tree_hash": tree_hash,
        "source_plan_sha256": source_plan_sha256,
        "node_ids": [n["id"] for n in nodes],
    }

    # Progress preservation: if an existing tree has the SAME tree_hash, the plan is
    # structurally unchanged — keep the driver's node status/manifest, do NOT wipe
    # (a Phase-2 re-validate of an unchanged plan must not reset progress).
    if os.path.exists(skeleton_path):
        try:
            old = json.loads(open(skeleton_path, encoding="utf-8").read())
            if isinstance(old, dict) and old.get("tree_hash") == tree_hash:
                return True, f"计划树未变(tree_hash={tree_hash[:12]})，保留进度", prov
        except (OSError, json.JSONDecodeError):
            pass

    # Atomic rebuild: build into a temp sibling dir and swap in, so a failure leaves
    # the OLD tree intact (never a half-written tree mistaken for valid).
    tmp_dir = out_dir + ".tmp-build"
    try:
        if os.path.isdir(tmp_dir):
            shutil.rmtree(tmp_dir)
        os.makedirs(os.path.join(tmp_dir, "nodes"))
        os.makedirs(os.path.join(tmp_dir, "briefs"))
        _write(os.path.join(tmp_dir, "root.md"), root_text)
        for nid, body in node_bodies.items():
            _write(os.path.join(tmp_dir, "nodes", f"{nid}.md"), body)
            _write(os.path.join(tmp_dir, "briefs", f"{nid}.md"),
                   build_brief(root_text, body, nodes_by_id[nid], nodes_by_id))
        skeleton = dict(skeleton_core)
        skeleton["tree_hash"] = tree_hash
        skeleton["source_plan_sha256"] = source_plan_sha256
        _write(os.path.join(tmp_dir, "skeleton.json"),
               json.dumps(skeleton, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        if os.path.isdir(out_dir):
            shutil.rmtree(out_dir)
        os.rename(tmp_dir, out_dir)
    except OSError as e:
        try:
            if os.path.isdir(tmp_dir):
                shutil.rmtree(tmp_dir)
        except OSError:
            pass
        return False, f"写计划树失败: {e}", None

    return True, f"计划树已生成（{len(node_bodies)} 节点，tree_hash={tree_hash[:12]}）", prov


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def verify_tree_integrity(plan_tree_dir, expected_tree_hash):
    """Recompute tree_hash from on-disk root.md + nodes/*.md + skeleton CORE (status
    and manifest excluded — those are mutable progress) and compare to the trusted
    ledger hash. Detects tampering of node bodies / graph / ac_mapping that a
    status-only edit can't hide. (ok, msg)."""
    try:
        root_text = open(os.path.join(plan_tree_dir, "root.md"), encoding="utf-8").read()
        sk = json.loads(open(os.path.join(plan_tree_dir, "skeleton.json"), encoding="utf-8").read())
    except (OSError, json.JSONDecodeError) as e:
        return False, f"无法读取计划树复算 hash: {e}"
    if not isinstance(sk, dict) or not isinstance(sk.get("nodes"), list):
        return False, "skeleton.json 结构损坏"
    node_bodies = {}
    core_nodes = []
    for n in sk["nodes"]:
        if not isinstance(n, dict):
            return False, "skeleton.json node 结构损坏"
        nid = n.get("id")
        try:
            node_bodies[nid] = open(os.path.join(plan_tree_dir, "nodes", f"{nid}.md"),
                                    encoding="utf-8").read()
        except OSError as e:
            return False, f"无法读取 node 正文复算 hash: {e}"
        core = {k: n.get(k) for k in ("id", "title", "deps", "inputs", "outputs", "files")}
        core["status"] = "pending"
        core["manifest"] = None
        core_nodes.append(core)
    skeleton_core = {
        "version": sk.get("version", SKELETON_VERSION),
        "nodes": core_nodes,
        "ac_mapping": sk.get("ac_mapping", []),
        "exclusive_forks": sk.get("exclusive_forks", []),
    }
    if compute_tree_hash(root_text, node_bodies, skeleton_core) != expected_tree_hash:
        return False, "计划树被篡改：磁盘内容重算 tree_hash 与可信记录不一致"
    return True, "tree integrity OK"


def update_node_status(skeleton_path, node_id, status=None, manifest=None):
    """Single-writer ATOMIC update of one node's status/manifest in skeleton.json —
    the Phase-2 driver calls this instead of hand-writing JSON (spec L113). status and
    manifest are excluded from tree_hash, so integrity stays intact. (ok, msg)."""
    try:
        sk = json.loads(open(skeleton_path, encoding="utf-8").read())
    except (OSError, json.JSONDecodeError) as e:
        return False, f"无法读取 skeleton: {e}"
    nodes = sk.get("nodes") if isinstance(sk, dict) else None
    if not isinstance(nodes, list):
        return False, "skeleton 结构损坏"
    hit = next((n for n in nodes if isinstance(n, dict) and n.get("id") == node_id), None)
    if hit is None:
        return False, f"skeleton 无此 node: {node_id}"
    if status is not None:
        if status not in ("pending", "in_progress", "done", "failed"):
            return False, f"非法 status: {status}"
        hit["status"] = status
    if manifest is not None:
        hit["manifest"] = manifest
    tmp = skeleton_path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(json.dumps(sk, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, skeleton_path)
    except OSError as e:
        return False, f"写 skeleton 失败: {e}"
    return True, f"node {node_id} 更新 (status={status})"


# ── runtime helper: enforce a node's diff stays within its declared files ─────
def files_changed_within(node_files, changed_files):
    """(ok, offending). Both canonicalized; offending = changed - declared. A changed
    file that fails canonicalization is FAIL-CLOSED (counted as offending), never
    silently dropped. Used by the Phase-2 driver's per-node git-diff recheck + 2.5 gate."""
    declared = set(f for f in (canon_path(x) for x in node_files) if f)
    offending = []
    for c in changed_files:
        ck = canon_path(c)
        key = ck if ck is not None else (c or "").strip()
        if key and key not in declared:
            offending.append(key)
    return (not offending), sorted(offending)


def plan_tree_dir_for(plan_path):
    """Sibling dir next to plan.md:  <plan_stem>.plantree/  (like plan.html)."""
    base, _ext = os.path.splitext(plan_path)
    return base + ".plantree"
