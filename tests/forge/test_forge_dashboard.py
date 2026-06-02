import json, os, subprocess, tempfile, unittest, importlib.util

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


if __name__ == "__main__":
    unittest.main()
