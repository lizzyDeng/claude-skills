import json, os, shutil, subprocess, tempfile, unittest, importlib.util

FORGE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "skills", "forge")
spec = importlib.util.spec_from_file_location("forge_dashboard", os.path.join(FORGE_DIR, "forge_dashboard.py"))
fd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fd)


def write(p, obj):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(obj, f)


class SnapshotShapeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        write(os.path.join(self.tmp, "project-roadmap", "roadmap.json"), {
            "north_star": "NS",
            "objectives": [{"id": "obj-1", "name": "O1", "description": "d", "target_metric": "m"}],
            "features": [{"slug": "f1", "name": "F1", "objective_id": "obj-1", "status": "draft"}],
        })

    def test_top_level_keys(self):
        snap = fd.build_snapshot(self.tmp)
        for k in ("generated_at", "repo_root", "north_star", "objectives", "counts"):
            self.assertIn(k, snap)
        self.assertEqual(snap["north_star"], "NS")
        self.assertEqual(len(snap["objectives"]), 1)
        self.assertEqual(snap["objectives"][0]["id"], "obj-1")


class SessionLinkageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q", self.tmp], check=True)
        write(os.path.join(self.tmp, "project-roadmap", "roadmap.json"), {
            "north_star": "NS",
            "objectives": [{"id": "obj-1", "name": "O1"}],
            "features": [{"slug": "telegram-binding", "name": "TG", "objective_id": "obj-1", "status": "in_progress"}],
        })
        sess = os.path.join(self.tmp, ".git", "fastship", "sessions", "telegram-binding")
        write(os.path.join(sess, "orchestrator.json"), {
            "session_id": "telegram-binding", "current_step": "2.0", "phase": 2,
            "completed_steps": ["1.0", "1.1", "1.2", "1.3", "1.4", "1.5", "1.5c", "1.6"],
            "skipped_steps": ["1.3d"],
        })
        write(os.path.join(sess, "gate.json"), {
            "forge_feature": "telegram-binding", "test_passed": True,
            "e2e_executed": False, "e2e_gate_passed": False,
        })

    def test_feature_links_to_session(self):
        snap = fd.build_snapshot(self.tmp)
        feat = snap["objectives"][0]["features"][0]
        self.assertIsNotNone(feat["fastship"])
        self.assertEqual(feat["fastship"]["current_step"], "2.0")
        self.assertEqual(feat["fastship"]["phase"], 2)
        self.assertEqual(feat["fastship"]["completed_count"], 8)
        self.assertEqual(feat["fastship"]["applicable_steps"], 17)
        self.assertTrue(feat["fastship"]["test_passed"])
        self.assertEqual(snap["counts"]["sessions"], 1)

    def test_link_picks_freshest_by_mtime(self):
        op_old = os.path.join(self.tmp, ".git", "fastship", "sessions", "telegram-binding", "orchestrator.json")
        sess2 = os.path.join(self.tmp, ".git", "fastship", "sessions", "telegram-binding-newer")
        op_new = os.path.join(sess2, "orchestrator.json")
        write(op_new, {
            "session_id": "telegram-binding-newer", "current_step": "3.4", "phase": 3,
            "completed_steps": ["1.0", "1.1", "1.2", "1.3", "1.4", "1.5", "1.5c", "1.6", "2.0", "3.0", "3.1"],
            "skipped_steps": ["1.3d"],
        })
        # freshness = max(orchestrator, gate) mtime; age BOTH files of the old session
        gp_old = os.path.join(self.tmp, ".git", "fastship", "sessions", "telegram-binding", "gate.json")
        os.utime(op_old, (1_000_000, 1_000_000))
        os.utime(gp_old, (1_000_000, 1_000_000))
        os.utime(op_new, (2_000_000, 2_000_000))
        snap = fd.build_snapshot(self.tmp)
        feat = snap["objectives"][0]["features"][0]
        self.assertEqual(feat["fastship"]["session_id"], "telegram-binding-newer")
        self.assertEqual(feat["fastship"]["current_step"], "3.4")

    def test_malformed_steps_do_not_crash(self):
        op = os.path.join(self.tmp, ".git", "fastship", "sessions", "telegram-binding", "orchestrator.json")
        d = json.load(open(op)); d["completed_steps"] = "oops"; d["skipped_steps"] = None
        json.dump(d, open(op, "w"))
        snap = fd.build_snapshot(self.tmp)
        feat = snap["objectives"][0]["features"][0]
        self.assertEqual(feat["fastship"]["completed_count"], 0)
        self.assertEqual(feat["fastship"]["applicable_steps"], 18)


class RollupTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        subprocess.run(["git", "init", "-q", self.tmp], check=True)
        feats = [
            {"slug": "f-a", "name": "A", "objective_id": "obj-4", "status": "concluded"},
            {"slug": "f-b", "name": "B", "objective_id": "obj-4", "status": "in_progress"},
            {"slug": "f-c", "name": "C", "objective_id": "obj-4", "status": "draft"},
            {"slug": "f-d", "name": "D", "objective_id": "obj-4", "status": "draft"},
        ]
        write(os.path.join(self.tmp, "project-roadmap", "roadmap.json"), {
            "north_star": "NS", "objectives": [{"id": "obj-4", "name": "Big SDK"}], "features": feats,
        })
        write(os.path.join(self.tmp, "project-roadmap", "features", "f-a", "metric.json"),
              {"baseline": 0, "target": 1, "metric_name": "x"})
        write(os.path.join(self.tmp, "project-roadmap", "features", "f-a", "harvest.json"),
              {"actual": 1, "verdict": "achieved", "next_action": "done"})

    def test_rollup_counts_and_todo(self):
        snap = fd.build_snapshot(self.tmp)
        roll = snap["objectives"][0]["rollup"]
        self.assertEqual(roll["total"], 4)
        self.assertEqual(roll["done"], 1)
        self.assertEqual(roll["in_progress"], 1)
        self.assertEqual(len(roll["todo"]), 3)
        self.assertEqual({t["slug"] for t in roll["todo"]}, {"f-b", "f-c", "f-d"})
        self.assertTrue(0 <= roll["overall_progress"] <= 100)

    def test_metric_and_harvest_attached(self):
        snap = fd.build_snapshot(self.tmp)
        fa = next(f for f in snap["objectives"][0]["features"] if f["slug"] == "f-a")
        self.assertEqual(fa["metric"]["target"], 1)
        self.assertEqual(fa["harvest"]["verdict"], "achieved")
        self.assertEqual(fa["feature_progress"], 100.0)


class MalformedRoadmapTest(unittest.TestCase):
    def test_non_list_and_non_dict_entries_do_not_crash(self):
        tmp = tempfile.mkdtemp()
        write(os.path.join(tmp, "project-roadmap", "roadmap.json"), {
            "north_star": "NS",
            "objectives": "oops",                       # not a list
            "features": [{"slug": "ok", "name": "Ok", "objective_id": "obj-x", "status": "draft"},
                         "garbage", 42, None],           # mixed junk entries
        })
        snap = fd.build_snapshot(tmp)                    # must not raise
        self.assertEqual(snap["objectives"], [])
        self.assertEqual(snap["counts"]["features"], 1)  # only the one valid dict counted
        self.assertEqual(snap["orphan_features"][0]["slug"], "ok")


class RenderAndCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        write(os.path.join(self.tmp, "project-roadmap", "roadmap.json"),
              {"north_star": "NS", "objectives": [], "features": []})

    def test_render_html_is_selfcontained(self):
        html = fd.render_html()
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn("/api/state", html)
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)

    def test_once_prints_json(self):
        import io, contextlib
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = fd.main(["--once", "--repo-root", self.tmp])
        self.assertEqual(rc, 0)
        parsed = json.loads(buf.getvalue())
        self.assertEqual(parsed["north_star"], "NS")


class WorktreeParseTest(unittest.TestCase):
    def test_parse_tolerates_branch_detached_bare_and_empty(self):
        out = ("worktree /a\nHEAD abc\nbranch refs/heads/feat/x\n\n"
               "worktree /b\nHEAD def\ndetached\n\n"
               "worktree /c\nbare\n")
        rows = fd._parse_worktree_list(out)
        self.assertEqual(rows[0]["path"], "/a")
        self.assertEqual(rows[0]["branch"], "feat/x")
        self.assertIsNone(rows[1]["branch"])         # detached -> None
        self.assertTrue(rows[2]["is_bare"])
        self.assertEqual(fd._parse_worktree_list(""), [])
        self.assertEqual(fd._parse_worktree_list(None), [])


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


class BranchWorktreeOtherTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        subprocess.run(["git", "init", "-q", "-b", "main", self.tmp], check=True)
        subprocess.run(["git", "-C", self.tmp, "commit", "-q", "--allow-empty", "-m", "init"], check=True, env=env)
        # REAL worktree whose basename == feature slug, NO session (exercises porcelain fallback)
        self.wt = os.path.join(self.tmp, "wt", "wt-feat")
        subprocess.run(["git", "-C", self.tmp, "worktree", "add", "-q", "-b", "feat/wt", self.wt], check=True)
        # REAL worktree on LIVE branch feat/live; a session will record a STALE branch
        self.swt = os.path.join(self.tmp, "wt", "stale-feat")
        subprocess.run(["git", "-C", self.tmp, "worktree", "add", "-q", "-b", "feat/live", self.swt], check=True)
        write(os.path.join(self.tmp, "project-roadmap", "roadmap.json"), {
            "north_star": "ns", "objectives": [{"id": "obj-1", "name": "O1"}],
            "features": [
                {"slug": "linked-feat", "name": "L", "objective_id": "obj-1", "status": "in_progress"},
                {"slug": "wt-feat", "name": "W", "objective_id": "obj-1", "status": "in_progress"},
                {"slug": "nada-feat", "name": "N", "objective_id": "obj-1", "status": "draft"},
                {"slug": "stale-feat", "name": "S", "objective_id": "obj-1", "status": "in_progress"},
            ]})
        common = subprocess.run(["git", "-C", self.tmp, "rev-parse", "--git-common-dir"],
                                capture_output=True, text=True).stdout.strip()
        common = common if os.path.isabs(common) else os.path.join(self.tmp, common)
        sd = os.path.join(common, "fastship", "sessions", "linked-feat")
        write(os.path.join(sd, "orchestrator.json"),
              {"session_id": "linked-feat", "current_step": "2.0", "branch": "feat/linked",
               "completed_steps": ["1.0"], "skipped_steps": []})
        write(os.path.join(sd, "gate.json"), {"forge_feature": "linked-feat"})
        # stale DUPLICATE of linked-feat (matches via "linked-feat-" prefix) -> still linked, NOT Other
        du = os.path.join(common, "fastship", "sessions", "linked-feat-old")
        write(os.path.join(du, "orchestrator.json"),
              {"session_id": "linked-feat-old", "current_step": "1.4", "completed_steps": [], "skipped_steps": []})
        write(os.path.join(du, "gate.json"), {})
        for _fn in ("orchestrator.json", "gate.json"):   # stale duplicate = OLDER than linked-feat
            os.utime(os.path.join(du, _fn), (1_000_000, 1_000_000))
        od = os.path.join(common, "fastship", "sessions", "lab-orphan")
        write(os.path.join(od, "orchestrator.json"),
              {"session_id": "lab-orphan", "current_step": "1.4", "branch": "feat/lab",
               "completed_steps": [], "skipped_steps": []})
        write(os.path.join(od, "gate.json"), {})
        # worktree session recording a STALE branch; live worktree stale-feat is on feat/live
        ssd = os.path.join(common, "worktrees", "stale-feat", "fastship", "sessions", "stale-feat")
        write(os.path.join(ssd, "orchestrator.json"),
              {"session_id": "stale-feat", "current_step": "2.0", "branch": "feat/STALE",
               "completed_steps": ["1.0"], "skipped_steps": []})
        write(os.path.join(ssd, "gate.json"), {"forge_feature": "stale-feat"})
        # the 'default' placeholder must NOT appear in Other
        dd = os.path.join(common, "fastship", "sessions", "default")
        write(os.path.join(dd, "orchestrator.json"),
              {"session_id": "default", "current_step": None, "completed_steps": [], "skipped_steps": []})

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
        self.assertEqual(feats["stale-feat"]["branch"], "feat/live")  # live beats recorded feat/STALE

    def test_feature_no_source_branch_and_worktree_none(self):
        _, feats = self._feats()
        self.assertIsNone(feats["nada-feat"]["branch"])
        self.assertIsNone(feats["nada-feat"]["worktree"])

    def test_other_sessions_excludes_linked_and_default_includes_orphan(self):
        snap, _ = self._feats()
        ids = {s["session_id"] for s in snap["other_sessions"]}
        self.assertIn("lab-orphan", ids)
        self.assertNotIn("linked-feat", ids)
        self.assertNotIn("default", ids)
        self.assertNotIn("linked-feat-old", ids)        # stale DUPLICATE of a linked feature excluded
        orphan = next(s for s in snap["other_sessions"] if s["session_id"] == "lab-orphan")
        self.assertEqual(orphan["branch"], "feat/lab")
        self.assertIn("step_progress", orphan)


class BranchMatchTest(unittest.TestCase):
    """A feature with no session and no slug-named worktree still resolves its
    branch (and the worktree that branch is checked out in) from `git branch`."""
    def setUp(self):
        self.tmp = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
        subprocess.run(["git", "init", "-q", "-b", "main", self.tmp], check=True)
        subprocess.run(["git", "-C", self.tmp, "commit", "-q", "--allow-empty", "-m", "init"], check=True, env=env)
        # plain branch feat/<slug>, NOT checked out anywhere
        subprocess.run(["git", "-C", self.tmp, "branch", "feat/lonely-feat"], check=True)
        # branch checked out in a DIFFERENTLY-named worktree
        subprocess.run(["git", "-C", self.tmp, "worktree", "add", "-q", "-b", "feat/inwt-feat",
                        os.path.join(self.tmp, "wt", "some-dir")], check=True)
        write(os.path.join(self.tmp, "project-roadmap", "roadmap.json"), {
            "north_star": "ns", "objectives": [{"id": "obj-1", "name": "O"}],
            "features": [
                {"slug": "lonely-feat", "name": "L", "objective_id": "obj-1", "status": "in_progress"},
                {"slug": "inwt-feat", "name": "I", "objective_id": "obj-1", "status": "in_progress"},
                {"slug": "ghost-feat", "name": "G", "objective_id": "obj-1", "status": "draft"},
            ]})

    def _f(self):
        return {f["slug"]: f for f in fd.build_snapshot(self.tmp)["objectives"][0]["features"]}

    def test_branch_only_feature_resolves_branch_no_worktree(self):
        f = self._f()["lonely-feat"]
        self.assertEqual(f["branch"], "feat/lonely-feat")
        self.assertIsNone(f["worktree"])

    def test_feature_branch_checked_out_in_other_named_worktree(self):
        f = self._f()["inwt-feat"]
        self.assertEqual(f["branch"], "feat/inwt-feat")
        self.assertEqual(f["worktree"], "some-dir")

    def test_no_branch_no_worktree_stays_none(self):
        f = self._f()["ghost-feat"]
        self.assertIsNone(f["branch"])
        self.assertIsNone(f["worktree"])


class OtherRenderTest(unittest.TestCase):
    def test_html_wires_other_sessions_otherCard_and_dashfallback(self):
        h = fd.render_html()
        self.assertIn("other_sessions", h)
        self.assertIn("otherCard", h)
        self.assertIn("wtLine", h)
        self.assertIn('o.branch||"—"', h)   # unknown -> visible em dash, not blank


if __name__ == "__main__":
    unittest.main()
