# Forge Dashboard: Branch/Worktree + Other Sessions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show each forge feature's git branch + worktree (always visible, `—` when unknown), and surface fastship sessions not linked to any forge feature under a separate "Other" group.

**Architecture:** Pure-additive changes to the read-only stdlib dashboard `skills/forge/forge_dashboard.py`. Enrich session summaries with `branch`/`base_sha`/`worktree`; add a `git worktree list --porcelain` parser + lookup; resolve each feature's branch/worktree (worktree-session→live-porcelain-branch first, main-checkout-session next, slug→worktree fallback last); compute `other_sessions` (scanned sessions minus linked, excluding the `default` placeholder). The web UI is **client-rendered** (browser JS fetches `/api/state`), so verification asserts the **data contract** (`/api/state` content) + template wiring; full DOM rendering is out of scope for a stdlib (no-JS-engine) E2E and is intentionally not asserted.

**Tech Stack:** Python 3 stdlib (argparse/glob/json/os/re/subprocess/http.server), unittest, hermetic git-fixture E2E (real `git init` + real `git worktree add`).

---

### Task 1: git worktree parser + lookup

**Files:**
- Modify: `skills/forge/forge_dashboard.py` (add `import re`; add helpers after `_mtime`, ~line 63)
- Test: `tests/forge/test_forge_dashboard.py`

- [ ] **Step 1: Write the failing test**

```python
class WorktreeParseTest(unittest.TestCase):
    def test_parse_tolerates_branch_detached_bare_and_empty(self):
        out = ("worktree /a\nHEAD abc\nbranch refs/heads/feat/x\n\n"
               "worktree /b\nHEAD def\ndetached\n\n"
               "worktree /c\nbare\n")
        rows = fd._parse_worktree_list(out)
        self.assertEqual(rows[0]["path"], "/a")
        self.assertEqual(rows[0]["branch"], "feat/x")
        self.assertIsNone(rows[1]["branch"])        # detached -> None
        self.assertTrue(rows[2]["is_bare"])
        self.assertEqual(fd._parse_worktree_list(""), [])
        self.assertEqual(fd._parse_worktree_list(None), [])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.forge.test_forge_dashboard.WorktreeParseTest -v`
Expected: FAIL — `_parse_worktree_list` not defined.

- [ ] **Step 3: Write minimal implementation**

Add `import re` to imports. Add:

```python
def _parse_worktree_list(output):
    """Parse `git worktree list --porcelain` -> [{path, head, branch, is_bare}].
    Tolerant: detached -> branch None; bare -> is_bare True; missing fields ignored."""
    out, cur = [], None
    for line in (output or "").splitlines():
        if line.startswith("worktree "):
            if cur:
                out.append(cur)
            cur = {"path": line[9:].strip(), "head": None, "branch": None, "is_bare": False}
        elif cur is None:
            continue
        elif line.startswith("HEAD "):
            cur["head"] = line[5:].strip()
        elif line.startswith("branch "):
            b = line[7:].strip()
            cur["branch"] = b[11:] if b.startswith("refs/heads/") else b
        elif line.strip() == "bare":
            cur["is_bare"] = True
    if cur:
        out.append(cur)
    return out


def _worktrees(repo_root):
    """basename(worktree path) -> {path, branch} from porcelain output."""
    rows = _parse_worktree_list(_git(["worktree", "list", "--porcelain"], repo_root))
    return {os.path.basename(r["path"].rstrip("/")): {"path": r["path"], "branch": r["branch"]}
            for r in rows if r.get("path")}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.forge.test_forge_dashboard.WorktreeParseTest -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/forge/forge_dashboard.py tests/forge/test_forge_dashboard.py
git commit -m "feat(forge): worktree --porcelain parser for dashboard"
```

---

### Task 2: session summary carries branch/base_sha/worktree

**Files:**
- Modify: `skills/forge/forge_dashboard.py` add `_worktree_from_path` near `_mtime`; extend `_session_summary` (~line 114)
- Test: `tests/forge/test_forge_dashboard.py`

- [ ] **Step 1: Write the failing test**

```python
class SessionBranchTest(unittest.TestCase):
    def _s(self, sdir, orch, gate):
        return fd._session_summary("sid", sdir, orch, gate, 1.0)
    def test_branch_from_orch_and_worktree_from_path(self):
        s = self._s("/r/.git/worktrees/wt-foo/fastship/sessions/sid",
                    {"branch": "feat/a", "base_sha": "deadbeef"}, {})
        self.assertEqual(s["branch"], "feat/a")
        self.assertEqual(s["base_sha"], "deadbeef")
        self.assertEqual(s["worktree"], "wt-foo")
    def test_branch_falls_back_to_gate_main_has_no_worktree(self):
        s = self._s("/r/.git/fastship/sessions/sid", {}, {"branch": "feat/b"})
        self.assertEqual(s["branch"], "feat/b")
        self.assertIsNone(s["worktree"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.forge.test_forge_dashboard.SessionBranchTest -v`
Expected: FAIL — `KeyError: 'branch'`.

- [ ] **Step 3: Write minimal implementation**

Add near `_mtime`:

```python
def _worktree_from_path(path):
    m = re.search(r"/worktrees/([^/]+)/fastship/", (path or "") + "/")
    return m.group(1) if m else None
```

In `_session_summary`, add to the returned dict:

```python
        "branch": orch.get("branch") or gate.get("branch"),
        "base_sha": orch.get("base_sha"),
        "worktree": _worktree_from_path(sdir),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 -m unittest tests.forge.test_forge_dashboard.SessionBranchTest -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add skills/forge/forge_dashboard.py tests/forge/test_forge_dashboard.py
git commit -m "feat(forge): session summary carries branch/base_sha/worktree"
```

---

### Task 3: per-feature branch/worktree + other_sessions (real-worktree fallback tested)

**Files:**
- Modify: `skills/forge/forge_dashboard.py` `_feature_record` (~165), `build_snapshot` (~211); add `_feature_branch_worktree`
- Test: `tests/forge/test_forge_dashboard.py` (ensure `tempfile, shutil, subprocess` imported; add `_w` json-writer helper if absent)

- [ ] **Step 1: Write the failing test (covers linked-session, REAL-worktree fallback, orphan split)**

```python
class BranchWorktreeOtherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        subprocess.run(["git", "init", "-q", "-b", "main", self.tmp], check=True)
        subprocess.run(["git", "-C", self.tmp, "commit", "-q", "--allow-empty", "-m", "init"], check=True,
                       env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})
        # REAL worktree whose basename == feature slug, on its own branch
        self.wt = os.path.join(self.tmp, "wt", "wt-feat")
        subprocess.run(["git", "-C", self.tmp, "worktree", "add", "-q", "-b", "feat/wt", self.wt], check=True)
        self.swt = os.path.join(self.tmp, "wt", "stale-feat")  # live branch feat/live
        subprocess.run(["git", "-C", self.tmp, "worktree", "add", "-q", "-b", "feat/live", self.swt], check=True)
        rm = {"north_star": "ns", "objectives": [{"id": "obj-1", "name": "O1"}],
              "features": [
                {"slug": "linked-feat", "name": "L", "objective_id": "obj-1", "status": "in_progress"},
                {"slug": "wt-feat", "name": "W", "objective_id": "obj-1", "status": "in_progress"},
                {"slug": "nada-feat", "name": "N", "objective_id": "obj-1", "status": "draft"},
                {"slug": "stale-feat", "name": "S", "objective_id": "obj-1", "status": "in_progress"},
              ]}
        _w(os.path.join(self.tmp, "project-roadmap", "roadmap.json"), rm)
        common = subprocess.run(["git", "-C", self.tmp, "rev-parse", "--git-common-dir"],
                                capture_output=True, text=True).stdout.strip()
        common = common if os.path.isabs(common) else os.path.join(self.tmp, common)
        sd = os.path.join(common, "fastship", "sessions", "linked-feat")
        _w(os.path.join(sd, "orchestrator.json"),
           {"session_id": "linked-feat", "current_step": "2.0", "branch": "feat/linked",
            "completed_steps": ["1.0"], "skipped_steps": []})
        _w(os.path.join(sd, "gate.json"), {"forge_feature": "linked-feat"})
        # stale DUPLICATE of linked-feat (matches via "linked-feat-" prefix) -> still linked, NOT Other
        du = os.path.join(common, "fastship", "sessions", "linked-feat-old")
        _w(os.path.join(du, "orchestrator.json"),
           {"session_id": "linked-feat-old", "current_step": "1.4",
            "completed_steps": [], "skipped_steps": []})
        _w(os.path.join(du, "gate.json"), {})
        od = os.path.join(common, "fastship", "sessions", "lab-orphan")
        _w(os.path.join(od, "orchestrator.json"),
           {"session_id": "lab-orphan", "current_step": "1.4", "branch": "feat/lab",
            "completed_steps": [], "skipped_steps": []})
        _w(os.path.join(od, "gate.json"), {})
        # worktree session recording a STALE branch; live worktree stale-feat is on feat/live
        ssd = os.path.join(common, "worktrees", "stale-feat", "fastship", "sessions", "stale-feat")
        _w(os.path.join(ssd, "orchestrator.json"),
           {"session_id": "stale-feat", "current_step": "2.0", "branch": "feat/STALE",
            "completed_steps": ["1.0"], "skipped_steps": []})
        _w(os.path.join(ssd, "gate.json"), {"forge_feature": "stale-feat"})
        # the 'default' placeholder must NOT appear in Other
        dd = os.path.join(common, "fastship", "sessions", "default")
        _w(os.path.join(dd, "orchestrator.json"), {"session_id": "default", "current_step": None,
                                                   "completed_steps": [], "skipped_steps": []})

    def _feats(self):
        snap = fd.build_snapshot(self.tmp)
        return snap, {f["slug"]: f for f in snap["objectives"][0]["features"]}

    def test_every_feature_has_branch_and_worktree_keys(self):
        _, feats = self._feats()
        for f in feats.values():
            self.assertIn("branch", f); self.assertIn("worktree", f)

    def test_feature_branch_from_linked_session(self):
        _, feats = self._feats()
        self.assertEqual(feats["linked-feat"]["branch"], "feat/linked")

    def test_feature_branch_from_real_worktree_fallback(self):
        _, feats = self._feats()
        self.assertEqual(feats["wt-feat"]["worktree"], "wt-feat")
        self.assertEqual(feats["wt-feat"]["branch"], "feat/wt")   # from `git worktree list --porcelain`

    def test_worktree_session_prefers_live_porcelain_branch_over_stale_record(self):
        _, feats = self._feats()
        self.assertEqual(feats["stale-feat"]["worktree"], "stale-feat")
        self.assertEqual(feats["stale-feat"]["branch"], "feat/live")  # live porcelain beats recorded feat/STALE

    def test_feature_no_source_branch_and_worktree_none(self):
        _, feats = self._feats()
        self.assertIsNone(feats["nada-feat"]["branch"])
        self.assertIsNone(feats["nada-feat"]["worktree"])

    def test_other_sessions_excludes_linked_and_default_includes_orphan(self):
        snap, _ = self._feats()
        ids = {s["session_id"] for s in snap["other_sessions"]}
        self.assertIn("lab-orphan", ids)
        self.assertNotIn("linked-feat", ids)   # linked excluded
        self.assertNotIn("default", ids)        # placeholder excluded
        self.assertNotIn("linked-feat-old", ids)  # stale DUPLICATE of a linked feature excluded
        orphan = next(s for s in snap["other_sessions"] if s["session_id"] == "lab-orphan")
        self.assertEqual(orphan["branch"], "feat/lab")
        self.assertIn("step_progress", orphan)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.forge.test_forge_dashboard.BranchWorktreeOtherTest -v`
Expected: FAIL — `KeyError: 'branch'` / `'other_sessions'`.

- [ ] **Step 3: Write minimal implementation**

```python
def _session_matches_slug(s, slug):
    """A session is LINKED to a feature if its forge_feature, exact id, or id-prefix matches."""
    return bool(slug) and (s.get("forge_feature") == slug
                           or s.get("session_id") == slug
                           or str(s.get("session_id", "")).startswith(slug + "-"))


def _feature_branch_worktree(slug, fs, worktrees):
    # worktree-session: prefer LIVE porcelain branch (recorded branch may be stale)
    if fs and fs.get("worktree"):
        wt = worktrees.get(fs["worktree"])
        return ((wt["branch"] if wt and wt.get("branch") else fs.get("branch")), fs["worktree"])
    # main-checkout session
    if fs and fs.get("branch"):
        return fs.get("branch"), None
    # fallback: a worktree whose basename == feature slug
    if slug and slug in worktrees:
        return worktrees[slug]["branch"], slug
    return None, None
```

Refactor `_link_session` to build candidates via `_session_matches_slug` (DRY; same matching rule reused for exclusion):

```python
    cands = [s for s in sessions.values() if _session_matches_slug(s, slug)]
```

Change `_feature_record(repo_root, feat, sessions)` -> `_feature_record(repo_root, feat, sessions, worktrees)`. After `fs = _link_session(...)`:

```python
    branch, worktree = _feature_branch_worktree(slug, fs, worktrees)
```

Add `"branch": branch, "worktree": worktree,` to the returned dict.

In `build_snapshot`, after `sessions = _scan_sessions(repo_root)` add `worktrees = _worktrees(repo_root)` and `linked_ids = set()`. In the feature loop:

```python
        rec = _feature_record(repo_root, feat, sessions, worktrees)
        # EVERY session matching this slug is linked (not just the freshest one
        # _link_session selected) — stale duplicates must NOT leak into Other.
        for s in sessions.values():
            if _session_matches_slug(s, feat.get("slug")):
                linked_ids.add(s["session_id"])
```

After the loop:

```python
    other_sessions = sorted(
        [s for s in sessions.values()
         if s["session_id"] not in linked_ids and s["session_id"] != "default"],
        key=lambda s: (s.get("mtime", 0.0), s["session_id"]), reverse=True)
```

Add `"other_sessions": other_sessions,` to the returned snapshot dict.

- [ ] **Step 4: Run full unit suite**

Run: `python3 tests/forge/test_forge_dashboard.py`
Expected: OK (existing 8 + new pass).

- [ ] **Step 5: Commit**

```bash
git add skills/forge/forge_dashboard.py tests/forge/test_forge_dashboard.py
git commit -m "feat(forge): per-feature branch/worktree (+real-wt fallback) + other_sessions"
```

---

### Task 4: render branch/worktree sub-line (always visible) + Other card

**Files:**
- Modify: `skills/forge/forge_dashboard.py` HTML constant (CSS ~294, `featRow` ~313, `load` ~333)
- Test: `tests/forge/test_forge_dashboard.py`

- [ ] **Step 1: Write the failing test**

```python
class OtherRenderTest(unittest.TestCase):
    def test_html_wires_other_sessions_otherCard_and_dashfallback(self):
        h = fd.render_html()
        self.assertIn("other_sessions", h)   # load() consumes it
        self.assertIn("otherCard", h)        # render fn present
        self.assertIn("wtLine", h)           # per-row branch/worktree
        self.assertIn('o.branch||"—"', h)    # unknown -> visible em dash, not blank
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.forge.test_forge_dashboard.OtherRenderTest -v`
Expected: FAIL.

- [ ] **Step 3: Write minimal implementation**

Add CSS (after `.sess` rule): `.wt{font-size:12px;color:var(--mut);margin-top:2px}` and `.b-active{background:#0d2c4d;color:var(--run)}.b-done{background:#1a3a24;color:var(--ok)}.b-stopped,.b-unknown{background:#262b31;color:var(--draft)}`.

Add JS before `featRow` (note: `wtLine` ALWAYS renders so every feature visibly shows branch/worktree, `—` when unknown):

```javascript
function wtLine(o){return `<div class="wt">⎇ ${esc(o.branch||"—")} · 🗂 ${esc(o.worktree||"—")}</div>`;}
function otherCard(list){if(!list||!list.length)return "";
  const rows=list.map(s=>`<tr>
    <td><b>${esc(s.requirement||s.session_id)}</b><div class="mut">${esc(s.session_id)}</div>${wtLine(s)}</td>
    <td><span class="badge b-${esc(s.status)}">${esc(s.status)}</span></td>
    <td style="min-width:120px">${bar(s.step_progress)}<span class="mut">${s.step_progress}%</span></td>
    <td>${fsCell(s)}</td><td><span class="mut">--</span></td></tr>`).join("");
  return `<section class="obj"><h2>Other <span class="mut">未归入 forge</span></h2>
    <div class="tm">fastship session 未关联任何 forge feature（无 metric）</div>
    <table><thead><tr><th>Session</th><th>status</th><th>progress</th><th>fastship</th><th>metric</th></tr></thead>
    <tbody>${rows}</tbody></table></section>`;}
```

In `featRow`, change the first `<td>` to append `${wtLine(f)}`:

```javascript
  <td><b>${esc(f.name)}</b><div class="mut">${esc(f.slug)}</div>${wtLine(f)}</td>
```

In `load`, append after the orphan_features term:

```javascript
    +otherCard(s.other_sessions);
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python3 tests/forge/test_forge_dashboard.py`
Expected: OK.

- [ ] **Step 5: Commit**

```bash
git add skills/forge/forge_dashboard.py tests/forge/test_forge_dashboard.py
git commit -m "feat(forge): always-visible branch/worktree line + Other card"
```

---

### Task 5: E2E with REAL worktree + data-contract assertions

**Files:**
- Modify: `tests/forge/forge_dashboard_e2e.py` (`_build_fixture` + turns)
- Modify gate invocation min-turns

- [ ] **Step 1: Extend fixture — branch field, REAL git worktree (no-session fallback), orphan, default**

In `_build_fixture()`: (a) add `"branch": "feat/telegram-binding"` to the telegram-binding `orchestrator.json`; (b) make an initial commit so worktree add works; (c) create a REAL worktree whose basename == an obj-4 slug with NO session (exercises porcelain fallback); (d) a worktree-home session for another obj-4 slug; (e) an unlinked `lab-experiment` session; (f) a `default` placeholder.

```python
import subprocess
def _commit_init(repo):
    env={**os.environ,"GIT_AUTHOR_NAME":"t","GIT_AUTHOR_EMAIL":"t@t","GIT_COMMITTER_NAME":"t","GIT_COMMITTER_EMAIL":"t@t"}
    subprocess.run(["git","-C",repo,"commit","-q","--allow-empty","-m","init"],check=True,env=env)
def _real_worktree(repo, path, branch):
    subprocess.run(["git","-C",repo,"worktree","add","-q","-b",branch,path],check=True)
# in _build_fixture, AFTER git init + roadmap written:
_commit_init(tmp)
_real_worktree(tmp, os.path.join(tmp,"wt","persona-image-generator"), "feat/img-gen")  # slug match, NO session
_real_worktree(tmp, os.path.join(tmp,"wt","wt-bf"), "feat/bf-live")  # REAL worktree, LIVE branch
common = subprocess.run(["git","-C",tmp,"rev-parse","--git-common-dir"],capture_output=True,text=True).stdout.strip()
common = common if os.path.isabs(common) else os.path.join(tmp, common)
# worktree-home session records a STALE branch -> live porcelain feat/bf-live must win
sd = os.path.join(common,"worktrees","wt-bf","fastship","sessions","boyfriend-chat-route")
_write(os.path.join(sd,"orchestrator.json"),
       {"session_id":"boyfriend-chat-route","current_step":"2.0","branch":"feat/bf-STALE",
        "completed_steps":["1.0","1.1"],"skipped_steps":[]})
_write(os.path.join(sd,"gate.json"),{"forge_feature":"boyfriend-chat-route"})
# stale duplicate of a LINKED feature (telegram-binding) -> must NOT appear in Other
_dup=os.path.join(common,"fastship","sessions","telegram-binding-old")
_write(os.path.join(_dup,"orchestrator.json"),
       {"session_id":"telegram-binding-old","current_step":"1.4","completed_steps":[],"skipped_steps":[]})
_write(os.path.join(_dup,"gate.json"),{})
# orphan + default
for sid,br in (("lab-experiment","feat/lab"),):
    p=os.path.join(common,"fastship","sessions",sid)
    _write(os.path.join(p,"orchestrator.json"),
           {"session_id":sid,"current_step":"1.4","branch":br,"completed_steps":[],"skipped_steps":[]})
    _write(os.path.join(p,"gate.json"),{})
_write(os.path.join(common,"fastship","sessions","default","orchestrator.json"),
       {"session_id":"default","current_step":None,"completed_steps":[],"skipped_steps":[]})
```

- [ ] **Step 2: Add data-contract turns (business content, not tautology)**

```python
allfeats = [f for o in snap["objectives"] for f in o["features"]]
turn("every feature exposes branch+worktree keys (— when unknown)",
     lambda: (all("branch" in f and "worktree" in f for f in allfeats), f"n={len(allfeats)}"))
img = next((f for f in allfeats if f["slug"]=="persona-image-generator"), None)
turn("no-session feature resolves branch via REAL git worktree fallback",
     lambda: (bool(img and img.get("worktree")=="persona-image-generator" and img.get("branch")=="feat/img-gen"),
              f"wt={img and img.get('worktree')} br={img and img.get('branch')}"))
bf = next((f for f in allfeats if f["slug"]=="boyfriend-chat-route"), None)
turn("worktree-session feature prefers LIVE porcelain branch over stale recorded branch",
     lambda: (bool(bf and bf.get("worktree")=="wt-bf" and bf.get("branch")=="feat/bf-live"),
              f"wt={bf and bf.get('worktree')} br={bf and bf.get('branch')}"))
oth = snap.get("other_sessions", [])
oids = {s["session_id"] for s in oth}
turn("other_sessions = unlinked only (has lab-experiment; excludes linked + default)",
     lambda: ("lab-experiment" in oids and "telegram-binding" not in oids
              and "boyfriend-chat-route" not in oids and "default" not in oids
              and "telegram-binding-old" not in oids, f"other={sorted(oids)}"))
lab = next((s for s in oth if s["session_id"]=="lab-experiment"), None)
turn("Other session carries fastship progress + branch (no metric concept)",
     lambda: (bool(lab and lab.get("branch")=="feat/lab" and "step_progress" in lab), f"lab={lab and lab.get('branch')}"))
turn("obj-4 overall_progress still 0 (drafts unaffected by new sessions) — no regression",
     lambda: (next(o["rollup"]["overall_progress"] for o in snap["objectives"] if o["id"]=="obj-4")==0,
              "obj-4=0"))
turn("HTML wires otherCard+wtLine with em-dash fallback",
     lambda: (all(k in html for k in ("other_sessions","otherCard","wtLine")) and 'o.branch||"—"' in html, "html ok"))
```

- [ ] **Step 3: Run E2E + gate**

Run: `python3 tests/forge/forge_dashboard_e2e.py -o /tmp/fd_e2e.json && python3 tests/forge/forge_dashboard_gate.py --result /tmp/fd_e2e.json --min-turns 17`
Expected: all turns OK (existing 11 + new 7 = 18), `GATE PASS`, exit 0. The "no regression" turn confirms existing obj-4 assertions still hold.

- [ ] **Step 4: Commit**

```bash
git add tests/forge/forge_dashboard_e2e.py
git commit -m "test(forge): E2E real-worktree fallback + Other data contract"
```

---

## Self-Review
- **Spec coverage:** R1 branch/worktree → Tasks 1-4 (always-visible `—`); real-worktree fallback exercised in Task 3 unit + Task 5 E2E. R2 Other sessions → Tasks 3-4, data-contract asserted in Task 5 (excludes linked + `default`). ✓
- **Codex round-3 fix:** added real-`git worktree` staleness tests — unit `test_worktree_session_prefers_live_porcelain_branch_over_stale_record` (recorded `feat/STALE` vs live `feat/live`) and E2E `wt-bf` real worktree on `feat/bf-live` with a session recording `feat/bf-STALE`, asserting the LIVE porcelain branch wins.
- **Codex re-review fix:** `linked_ids` now computed from ALL sessions matching any feature slug via `_session_matches_slug` (not just the freshest `_link_session` pick), so stale duplicate sessions of a linked feature never leak into Other. Unit + E2E both assert a stale duplicate is excluded.
- **Codex FAIL items resolved:** (1) `wtLine` always renders `—` [Task 4]; (2) real `git worktree add` fallback tested [Task 3 t_real_worktree, Task 5 img turn]; (3/4/5) tautological checks replaced by data-contract assertions on every feature + Other content + no-regression turn [Task 5]. Client-render DOM is the one acknowledged out-of-scope boundary (stdlib, no JS engine) — verification is on the `/api/state` data contract that drives the UI.
- **Placeholder scan:** every code step has concrete code. ✓
- **Type consistency:** keys `branch`/`base_sha`/`worktree`/`step_progress`/`status`/`other_sessions` consistent across summary, resolver, render, tests. ✓
