import json, os, tempfile, time, unittest, importlib.util
from collections import Counter

MOD = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "skills", "session-radar", "session_dashboard.py")
spec = importlib.util.spec_from_file_location("session_dashboard", MOD)
sd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(sd)


class StripCommandShellTest(unittest.TestCase):
    def test_extracts_real_args_from_fastship_shell(self):
        raw = ("<command-message>fastship</command-message>\n"
               "<command-name>/fastship</command-name>\n"
               "<command-args>做 session 维度可视化雷达</command-args>")
        self.assertEqual(sd.strip_command_shell(raw), "做 session 维度可视化雷达")

    def test_drops_local_command_caveat_block(self):
        raw = ("<local-command-caveat>Caveat: messages below were generated "
               "while running local commands. DO NOT respond.</local-command-caveat>")
        self.assertEqual(sd.strip_command_shell(raw), "")

    def test_plain_human_prompt_passes_through_untouched(self):
        self.assertEqual(sd.strip_command_shell("Please add a retry to the client"),
                         "Please add a retry to the client")

    def test_plain_prompt_with_angle_brackets_not_mangled(self):
        # no command-shell tags -> leave content alone (only whitespace-normalize)
        self.assertEqual(sd.strip_command_shell("fix List<Item> generic   parse"),
                         "fix List<Item> generic parse")

    def test_non_string_returns_empty(self):
        self.assertEqual(sd.strip_command_shell(None), "")
        self.assertEqual(sd.strip_command_shell({"a": 1}), "")


class ExtractOpeningTest(unittest.TestCase):
    def test_skips_caveat_returns_real_args(self):
        objs = [
            {"type": "user", "message": {"role": "user",
                "content": "<local-command-caveat>Caveat: ...</local-command-caveat>"}},
            {"type": "user", "message": {"role": "user",
                "content": "<command-message>fastship</command-message>"
                           "<command-args>build the radar</command-args>"}},
        ]
        self.assertEqual(sd.extract_opening(objs), "build the radar")

    def test_skips_tool_result_user_turns(self):
        objs = [
            {"type": "user", "message": {"role": "user",
                "content": [{"type": "tool_result", "content": "ok"}]}},
            {"type": "user", "message": {"role": "user",
                "content": [{"type": "text", "text": "real first ask"}]}},
        ]
        self.assertEqual(sd.extract_opening(objs), "real first ask")

    def test_returns_empty_when_no_human_turn(self):
        objs = [{"type": "assistant", "message": {"role": "assistant",
                 "content": [{"type": "text", "text": "hi"}]}}]
        self.assertEqual(sd.extract_opening(objs), "")


class ExtractActivityTest(unittest.TestCase):
    def test_latest_tool_use_becomes_activity_with_cwd_branch(self):
        objs = [
            {"type": "assistant", "cwd": "/repo", "gitBranch": "main",
             "message": {"role": "assistant", "content": [{"type": "text", "text": "older"}]}},
            {"type": "assistant", "cwd": "/repo/x", "gitBranch": "feat/y",
             "message": {"role": "assistant", "content": [
                 {"type": "tool_use", "name": "Edit", "input": {"file_path": "/repo/x/a.py"}}]}},
        ]
        activity, cwd, branch, errored = sd.extract_activity(objs)
        self.assertIn("Edit", activity)
        self.assertIn("a.py", activity)
        self.assertEqual(cwd, "/repo/x")
        self.assertEqual(branch, "feat/y")
        self.assertFalse(errored)

    def test_api_error_tail_flags_errored(self):
        objs = [
            {"type": "assistant", "cwd": "/repo", "gitBranch": "main",
             "message": {"role": "assistant", "content": [
                 {"type": "text", "text": "API Error: 529 overloaded_error"}]}},
        ]
        activity, cwd, branch, errored = sd.extract_activity(objs)
        self.assertTrue(errored)
        self.assertTrue(activity.startswith("⚠"))

    def test_no_assistant_returns_empty_activity(self):
        objs = [{"type": "user", "cwd": "/repo", "message": {"role": "user", "content": "hi"}}]
        activity, cwd, branch, errored = sd.extract_activity(objs)
        self.assertEqual(activity, "")

    def test_idle_reply_shows_last_action_not_chat(self):
        # latest turn is a prose reply to the user; NOW must show the last ACTION,
        # not the chat text.
        objs = [
            {"type": "assistant", "cwd": "/r", "gitBranch": "main",
             "message": {"role": "assistant", "content": [
                 {"type": "tool_use", "name": "Bash", "input": {"command": "gh pr create"}}]}},
            {"type": "assistant", "cwd": "/r", "gitBranch": "main",
             "message": {"role": "assistant", "content": [
                 {"type": "text", "text": "Done — created PR #24, you can review it now."}]}},
        ]
        activity, cwd, branch, errored = sd.extract_activity(objs)
        self.assertIn("Bash", activity)         # the real action
        self.assertNotIn("PR #24", activity)    # NOT the chat reply
        self.assertFalse(errored)

    def test_pure_conversational_falls_back_to_marked_reply(self):
        # no tool ever used -> show the reply, but marked 💬 (a reply, not an action)
        objs = [{"type": "assistant", "cwd": "/r",
                 "message": {"role": "assistant", "content": [
                     {"type": "text", "text": "Here is the explanation you asked for."}]}}]
        activity, cwd, branch, errored = sd.extract_activity(objs)
        self.assertTrue(activity.startswith("💬"))
        self.assertFalse(errored)


class IsErrorTextTest(unittest.TestCase):
    def test_detects_common_error_prefixes(self):
        for s in ["API Error: overloaded", "  request failed: timeout",
                  "Error: boom", "HTTP 500 server error", "Rate limit exceeded"]:
            self.assertTrue(sd.is_error_text(s), s)

    def test_normal_text_not_error(self):
        self.assertFalse(sd.is_error_text("Editing the error handler in client.py"))


def _write(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)


class BgJobsTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()

    def test_reads_state_intent_cwd_and_tolerates_missing(self):
        _write(os.path.join(self.home, "jobs", "32ea05ca", "state.json"),
               {"state": "blocked", "intent": "review F4", "cwd": "/x",
                "updatedAt": "2026-06-03T09:07:17Z",
                "linkScanPath": "/p/32ea05ca-aaa.jsonl"})
        os.makedirs(os.path.join(self.home, "jobs", "06d739b8"))  # dir, no state.json
        jobs = sd.bg_jobs(self.home)
        self.assertEqual(jobs["32ea05ca"]["state"], "blocked")
        self.assertEqual(jobs["32ea05ca"]["intent"], "review F4")
        self.assertEqual(jobs["32ea05ca"]["cwd"], "/x")
        self.assertEqual(jobs["32ea05ca"]["link_path"], "/p/32ea05ca-aaa.jsonl")
        self.assertIn("06d739b8", jobs)          # still recognized as a bg job
        self.assertIsNone(jobs["06d739b8"]["state"])

    def test_missing_jobs_dir_returns_empty(self):
        self.assertEqual(sd.bg_jobs(os.path.join(self.home, "nope")), {})


class LivenessTest(unittest.TestCase):
    def test_foreground_uses_mtime_buckets(self):
        self.assertEqual(sd.liveness(10), "active")
        self.assertEqual(sd.liveness(200), "idle")
        self.assertEqual(sd.liveness(9999), "dormant")

    def test_bg_active_overrides_stale_mtime(self):
        # transcript 1 hour idle, but the job says it is active -> working, not dormant
        self.assertEqual(sd.liveness(3600, is_bg=True, bg_state="active"), "working")

    def test_bg_blocked_is_distinct_alive_state(self):
        self.assertEqual(sd.liveness(3600, is_bg=True, bg_state="blocked"), "blocked")

    def test_bg_done_is_done(self):
        self.assertEqual(sd.liveness(5, is_bg=True, bg_state="done"), "done")

    def test_errored_beats_everything(self):
        self.assertEqual(sd.liveness(5, is_bg=True, bg_state="active", errored=True), "errored")

    def test_unknown_bg_state_is_unknown_never_mtime_active(self):
        # a fresh-mtime bg job with an unrecognized state must NOT read 'active'
        self.assertEqual(sd.liveness(10, is_bg=True, bg_state="weird"), "unknown")

    def test_missing_bg_state_is_unknown(self):
        self.assertEqual(sd.liveness(0.0, is_bg=True, bg_state=None), "unknown")


class RepoWorktreeTest(unittest.TestCase):
    def test_worktree_from_claude_worktrees_path(self):
        cwd = "/Users/me/works/claude-skills/.claude/worktrees/session-radar"
        self.assertEqual(sd.worktree_of(cwd), "session-radar")
        self.assertEqual(sd.repo_of(cwd), "claude-skills ⟨wt:session-radar⟩")

    def test_plain_repo_uses_basename(self):
        self.assertIsNone(sd.worktree_of("/Users/me/works/aifriends"))
        self.assertEqual(sd.repo_of("/Users/me/works/aifriends"), "aifriends")

    def test_none_cwd(self):
        self.assertIsNone(sd.worktree_of(None))
        self.assertEqual(sd.repo_of(None), "—")


class DriftTest(unittest.TestCase):
    def test_diverged_opening_and_now_is_drift(self):
        self.assertTrue(sd.compute_drift("build a session radar dashboard",
                                         "Bash(git push origin pr-branch) open pull request"))

    def test_aligned_opening_and_now_no_drift(self):
        self.assertFalse(sd.compute_drift("fix the retry backoff in client",
                                          "Edit(client.py) retry backoff jitter"))

    def test_empty_side_is_not_drift(self):
        self.assertFalse(sd.compute_drift("", "anything"))
        self.assertFalse(sd.compute_drift("anything", ""))

    def test_cjk_opening_participates_in_drift(self):  # the seed's own opening is Chinese
        # diverged: Chinese "build the radar" intent vs opening-a-PR action -> drift
        self.assertTrue(sd.compute_drift("做 session 维度可视化雷达", "Bash(git push) open PR 提交"))
        # aligned: shared Chinese tokens -> no drift
        self.assertFalse(sd.compute_drift("修复 重试 逻辑 backoff", "Edit 修复 重试 逻辑 jitter"))


class ProjectAttributionTest(unittest.TestCase):
    def test_project_of_path_fallback(self):
        # git unavailable for these fake paths -> path fallback
        self.assertEqual(sd.project_of("/Users/me/works/aifriends"), "aifriends")
        # a worktree placed under another repo's tree -> the repo BEFORE /.claude/worktrees
        self.assertEqual(sd.project_of("/x/aifriends/.claude/worktrees/forge-dashboard"), "aifriends")
        self.assertEqual(sd.project_of(None), "(no cwd)")

    def test_now_other_repo_detects_explicit_cross_repo_command(self):
        # latest action runs a WT=<abs path> targeting a different repo
        objs = [{"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash",
             "input": {"command": "WT=/Users/zz/works/claude-skills-a4-s git -C $WT push"}}]}}]
        self.assertEqual(sd._now_other_repo(objs, "aifriends"), "claude-skills-a4-s")

    def test_now_other_repo_none_for_same_repo_or_no_switch(self):
        objs = [{"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Read", "input": {"file_path": "sdk.py"}}]}}]
        self.assertIsNone(sd._now_other_repo(objs, "aifriends"))
        objs2 = [{"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash",
             "input": {"command": "cd /Users/zz/works/aifriends && ls"}}]}}]
        self.assertIsNone(sd._now_other_repo(objs2, "aifriends"))  # same repo -> no marker


def _jsonl(path, objs):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for o in objs:
            f.write(json.dumps(o, ensure_ascii=False) + "\n")


class BuildSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        proj = os.path.join(self.home, "projects", "-Users-me-works-claude-skills")
        # FG session in a worktree, /fastship opening, current action = open PR (drift)
        self._fg = "aaaaaaaa-1111-2222-3333-444444444444"
        _jsonl(os.path.join(proj, self._fg + ".jsonl"), [
            {"type": "user", "cwd": "/Users/me/works/claude-skills/.claude/worktrees/session-radar",
             "gitBranch": "feat/session-radar-dashboard",
             "message": {"role": "user",
                "content": "<command-message>fastship</command-message>"
                           "<command-args>build the session radar dashboard</command-args>"}},
            {"type": "assistant",
             "cwd": "/Users/me/works/aifriends", "gitBranch": "fix/provider",
             "message": {"role": "assistant", "content": [
                 {"type": "tool_use", "name": "Bash", "input": {"command": "git push origin pr"}}]}},
        ])
        # BG job: transcript is 1h stale, but job state = active -> must be 'working'
        self._bg = "32ea05ca-71ef-40de-8b31-b1c929992902"
        bgp = os.path.join(proj, self._bg + ".jsonl")
        _jsonl(bgp, [
            {"type": "user", "cwd": "/Users/me/works/aifriends", "gitBranch": "main",
             "message": {"role": "user", "content": "review F4 alignment"}},
            {"type": "assistant", "cwd": "/Users/me/works/aifriends", "gitBranch": "main",
             "message": {"role": "assistant", "content": [
                 {"type": "tool_use", "name": "Read", "input": {"file_path": "sdk.py"}}]}},
        ])
        old = time.time() - 3600
        os.utime(bgp, (old, old))
        _write(os.path.join(self.home, "jobs", "32ea05ca", "state.json"),
               {"state": "active", "intent": "review F4 alignment", "cwd": "/Users/me/works/aifriends",
                "updatedAt": "2026-06-03T09:00:00Z", "linkScanPath": bgp})
        # Errored FG session: tail is an API error
        self._err = "bbbbbbbb-5555-6666-7777-888888888888"
        _jsonl(os.path.join(proj, self._err + ".jsonl"), [
            {"type": "user", "cwd": "/repo", "gitBranch": "main",
             "message": {"role": "user", "content": "do the thing"}},
            {"type": "assistant", "cwd": "/repo", "gitBranch": "main",
             "message": {"role": "assistant", "content": [
                 {"type": "text", "text": "API Error: 529 overloaded_error"}]}},
        ])
        # FG session whose CURRENT action is inside a .claude/worktrees/<name> cwd
        self._wt = "dddddddd-aaaa-bbbb-cccc-dddddddddddd"
        _jsonl(os.path.join(proj, self._wt + ".jsonl"), [
            {"type": "user", "cwd": "/Users/me/works/claude-skills",
             "gitBranch": "main", "message": {"role": "user", "content": "ship the radar"}},
            {"type": "assistant",
             "cwd": "/Users/me/works/claude-skills/.claude/worktrees/session-radar",
             "gitBranch": "feat/session-radar-dashboard",
             "message": {"role": "assistant", "content": [
                 {"type": "tool_use", "name": "Edit", "input": {"file_path": "session_dashboard.py"}}]}},
        ])
        # DONE background job with NO live transcript in projects/ — must still surface
        self._done = "cafe1234"
        _write(os.path.join(self.home, "jobs", self._done, "state.json"),
               {"state": "done", "intent": "earlier finished job",
                "cwd": "/Users/me/works/other", "updatedAt": "2026-06-01T00:00:00Z"})
        # BLOCKED background job (awaiting input) — alive, distinct from done
        self._blk = "beef5678"
        _write(os.path.join(self.home, "jobs", self._blk, "state.json"),
               {"state": "blocked", "intent": "waiting on permission",
                "cwd": "/Users/me/works/other"})
        # STATELESS background job: dir exists, NO state.json — liveness must be 'unknown',
        # never 'active' (no transcript -> fabricated age must not leak through)
        self._nost = "0d0d0d0d"
        os.makedirs(os.path.join(self.home, "jobs", self._nost))

    def _by_short(self, snap, short):
        return next(s for s in snap["sessions"] if s["short"] == short)

    def test_top_level_shape(self):
        snap = sd.build_snapshot(self.home, window_min=120)
        for k in ("generated_at", "claude_home", "counts", "sessions", "window_min",
                  "projects", "stale_unknown"):
            self.assertIn(k, snap)
        # 2 plain fg + 1 worktree fg + 4 bg (active, done, blocked, stateless) = 7
        self.assertEqual(snap["counts"]["total"], 7)
        self.assertEqual(snap["counts"]["bg"], 4)
        self.assertEqual(snap["counts"]["projects"], len(snap["projects"]))

    def test_sessions_grouped_by_git_project(self):
        snap = sd.build_snapshot(self.home, window_min=120)
        pmap = {g["project"]: g for g in snap["projects"]}
        # the two aifriends-cwd sessions (drifted fg + alive bg) group together
        self.assertIn("aifriends", pmap)
        aif = {s["short"] for s in pmap["aifriends"]["sessions"]}
        self.assertIn("aaaaaaaa", aif)
        self.assertIn("32ea05ca", aif)
        # the worktree session attributes to its main repo (claude-skills)
        self.assertIn("claude-skills", pmap)
        self.assertIn("dddddddd", {s["short"] for s in pmap["claude-skills"]["sessions"]})
        # each group carries a count matching its session list
        self.assertTrue(all(g["count"] == len(g["sessions"]) for g in snap["projects"]))

    def test_stateless_job_collapsed_into_stale_count(self):
        snap = sd.build_snapshot(self.home, window_min=120)
        self.assertEqual(snap["stale_unknown"], 1)  # the no-state 0d0d0d0d job
        grouped = {s["short"] for g in snap["projects"] for s in g["sessions"]}
        self.assertNotIn("0d0d0d0d", grouped)        # collapsed, not in any group
        self.assertIn("0d0d0d0d", {s["short"] for s in snap["sessions"]})  # still in flat list

    def test_p0_2_worktree_row_exposes_worktree_and_repo(self):  # P0-2
        snap = sd.build_snapshot(self.home, window_min=120)
        wt = self._by_short(snap, "dddddddd")
        self.assertEqual(wt["worktree"], "session-radar")
        self.assertEqual(wt["repo"], "claude-skills ⟨wt:session-radar⟩")
        self.assertEqual(wt["branch"], "feat/session-radar-dashboard")

    def test_p0_4_done_bg_job_without_transcript_still_surfaces(self):  # P0-4
        snap = sd.build_snapshot(self.home, window_min=120)
        done = self._by_short(snap, "cafe1234")
        self.assertTrue(done["is_bg"])
        self.assertEqual(done["liveness"], "done")
        self.assertEqual(done["opening"], "earlier finished job")  # from job intent

    def test_p0_4_blocked_bg_job_surfaces_as_blocked(self):  # P0-4 + P0-1
        snap = sd.build_snapshot(self.home, window_min=120)
        blk = self._by_short(snap, "beef5678")
        self.assertTrue(blk["is_bg"])
        self.assertEqual(blk["liveness"], "blocked")

    def test_p0_1_stateless_bg_job_is_unknown_not_active(self):  # P0-1 hard problem #2
        snap = sd.build_snapshot(self.home, window_min=120)
        nost = self._by_short(snap, "0d0d0d0d")
        self.assertTrue(nost["is_bg"])
        self.assertEqual(nost["liveness"], "unknown")  # NOT 'active' off a fabricated age

    def test_window_min_zero_shows_all_foreground_including_old(self):
        # The window is a recency lens, not a scope cap: --window-min 0 disables it.
        proj = os.path.join(self.home, "projects", "-Users-me-works-claude-skills")
        oldp = os.path.join(proj, "eeeeeeee-old.jsonl")
        _jsonl(oldp, [{"type": "user", "cwd": "/old/repo", "gitBranch": "main",
                       "message": {"role": "user", "content": "ancient task"}},
                      {"type": "assistant", "cwd": "/old/repo", "gitBranch": "main",
                       "message": {"role": "assistant", "content": [
                           {"type": "text", "text": "done long ago"}]}}])
        old = time.time() - 30 * 24 * 3600
        os.utime(oldp, (old, old))
        hidden = {s["short"] for s in sd.build_snapshot(self.home, window_min=120)["sessions"]}
        self.assertNotIn("eeeeeeee", hidden)                       # default window hides it
        shown = {s["short"] for s in sd.build_snapshot(self.home, window_min=0)["sessions"]}
        self.assertIn("eeeeeeee", shown)                           # window 0 = full scope

    def test_subagent_transcripts_excluded(self):
        # A subagent transcript lives 4 levels deep; it must NOT count as a session.
        proj = os.path.join(self.home, "projects", "-Users-me-works-claude-skills")
        _jsonl(os.path.join(proj, self._fg, "subagents", "agent-deadbeef.jsonl"), [
            {"type": "assistant", "cwd": "/x", "message": {"role": "assistant",
             "content": [{"type": "text", "text": "child work"}]}}])
        snap = sd.build_snapshot(self.home, window_min=120)
        shorts = [s["short"] for s in snap["sessions"]]
        self.assertNotIn("agent-de", shorts)
        self.assertEqual(snap["counts"]["total"], 7)  # unchanged

    def test_command_shell_stripped_and_drift_flagged(self):  # P0-3
        snap = sd.build_snapshot(self.home, window_min=120)
        fg = self._by_short(snap, "aaaaaaaa")
        self.assertEqual(fg["opening"], "build the session radar dashboard")
        self.assertNotIn("<command", fg["opening"])
        self.assertTrue(fg["drift"])

    def test_current_repo_worktree_branch_from_tail(self):  # P0-2
        snap = sd.build_snapshot(self.home, window_min=120)
        fg = self._by_short(snap, "aaaaaaaa")
        # current action is in aifriends on fix/provider (NOT the worktree it opened in)
        self.assertEqual(fg["repo"], "aifriends")
        self.assertEqual(fg["branch"], "fix/provider")

    def test_bg_active_but_stale_is_working_not_dormant(self):  # P0-1 + P0-4
        snap = sd.build_snapshot(self.home, window_min=120)
        bg = self._by_short(snap, "32ea05ca")
        self.assertTrue(bg["is_bg"])
        self.assertEqual(bg["liveness"], "working")
        self.assertGreaterEqual(snap["counts"]["bg"], 1)

    def test_errored_tail_classified_errored(self):  # P0-1
        snap = sd.build_snapshot(self.home, window_min=120)
        err = self._by_short(snap, "bbbbbbbb")
        self.assertEqual(err["liveness"], "errored")
        self.assertTrue(err["errored"])
        self.assertGreaterEqual(snap["counts"]["errored"], 1)

    def test_window_filter_keeps_alive_bg_despite_stale_transcript(self):
        # The BG job's transcript mtime is 1h old; a 30-min window would drop a
        # plain fg session that old, but the bg job survives because its state is
        # alive. The fresh fg sessions remain present.
        snap = sd.build_snapshot(self.home, window_min=30)
        shorts = {s["short"] for s in snap["sessions"]}
        self.assertIn("32ea05ca", shorts)   # bg alive -> always surfaced despite 1h-old transcript
        self.assertIn("aaaaaaaa", shorts)    # fresh fg
        self.assertEqual(next(s for s in snap["sessions"] if s["short"] == "32ea05ca")["liveness"],
                         "working")

    def test_malformed_jsonl_does_not_crash(self):
        proj = os.path.join(self.home, "projects", "-Users-me-works-claude-skills")
        with open(os.path.join(proj, "cccccccc-9999.jsonl"), "w") as f:
            f.write("{not json\n")
        snap = sd.build_snapshot(self.home, window_min=120)  # must not raise
        self.assertIsInstance(snap["sessions"], list)


class RenderAndCliTest(unittest.TestCase):
    def test_render_html_is_selfcontained_and_fetches_api(self):
        html = sd.render_html()
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn("/api/state", html)
        self.assertIn("Session Radar", html)

    def test_render_table_groups_by_project_with_rows(self):
        snap = {"generated_at": "t",
                "counts": {"total": 1, "bg": 1, "errored": 0, "drift": 1,
                           "projects": 1, "stale_unknown": 2},
                "stale_unknown": 2,
                "projects": [{"project": "aifriends", "count": 1, "sessions": [
                    {"liveness_label": "🟢 working", "is_bg": True, "worktree": "forge-dashboard",
                     "branch": "main", "opening": "review F4", "now": "Read(sdk.py)",
                     "acting_on": "claude-skills", "drift": True}]}],
                "sessions": []}
        out = sd.render_table(snap)
        self.assertIn("Session Radar", out)
        self.assertIn("📁 aifriends", out)          # project group header
        self.assertIn("forge-dashboard", out)        # worktree shown per row
        self.assertIn("↗claude-skills", out)         # cross-repo marker
        self.assertIn("DRIFT", out)
        self.assertIn("+ 2", out)                    # collapsed stale count line

    def test_main_json_prints_snapshot(self):
        import io, contextlib
        home = tempfile.mkdtemp()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = sd.main(["--claude-home", home, "--json"])
        self.assertEqual(rc, 0)
        data = json.loads(buf.getvalue())
        self.assertIn("sessions", data)

    def test_main_once_prints_table(self):
        import io, contextlib
        home = tempfile.mkdtemp()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = sd.main(["--claude-home", home, "--once"])
        self.assertEqual(rc, 0)
        self.assertIn("Session Radar", buf.getvalue())


def _asst(blocks, cwd=None, branch=None):
    o = {"type": "assistant", "message": {"role": "assistant", "content": blocks}}
    if cwd:
        o["cwd"], o["gitBranch"] = cwd, branch
    return o


def _tool(name, inp):
    return {"type": "tool_use", "name": name, "input": inp}


class WorkHelpersTest(unittest.TestCase):
    def test_humanize_strips_date_and_seps(self):
        self.assertEqual(sd._humanize("2026-06-03-session-radar-work-summary.md"),
                         "session radar work summary")
        self.assertEqual(sd._humanize("feat_provider_429"), "feat provider 429")

    def test_commit_subject_inline_and_heredoc_and_none(self):
        self.assertEqual(sd._commit_subject('git commit -m "feat: add radar summary"'),
                         "feat: add radar summary")
        cmd = "git commit -m \"$(cat <<'EOF'\nfix: provider 429 ejection\n\nbody\nEOF\n)\""
        self.assertEqual(sd._commit_subject(cmd), "fix: provider 429 ejection")
        self.assertIsNone(sd._commit_subject("git status"))

    def test_work_type_branch_then_commit_then_opening(self):
        self.assertEqual(sd.work_type("feat/session-radar", [], "")[0], "feature")
        self.assertEqual(sd.work_type("fix/x", [], "")[0], "bugfix")
        self.assertEqual(sd.work_type("refactor/x", [], "")[0], "refactor")
        self.assertEqual(sd.work_type("main", ["fix: crash"], "")[0], "bugfix")
        self.assertEqual(sd.work_type("main", [], "修复 重试 backoff")[0], "bugfix")
        self.assertEqual(sd.work_type("main", [], "just chatting")[0], "task")

    def test_work_title_branch_then_plan_then_opening(self):
        self.assertEqual(sd.work_title("feat/session-radar-dashboard", [], "x"),
                         "session radar dashboard")
        self.assertEqual(sd.work_title("main", ["2026-06-03-pay-bridge.md"], "x"), "pay bridge")
        self.assertEqual(sd.work_title("main", [], "build the thing"), "build the thing")

    def test_reconstruct_tasks_from_create_and_update(self):
        creates = [{"subject": "scaffold", "activeForm": "Scaffolding"},
                   {"subject": "wire", "activeForm": "Wiring"},
                   {"subject": "render", "activeForm": "Rendering"}]
        updates = [{"taskId": "1", "status": "in_progress"},
                   {"taskId": "1", "status": "completed"},
                   {"taskId": "2", "status": "in_progress"}]
        tasks = sd._reconstruct_tasks(creates, updates)
        self.assertEqual([t["status"] for t in tasks],
                         ["completed", "in_progress", "pending"])
        self.assertEqual(tasks[1]["activeForm"], "Wiring")

    def test_work_doing_picks_in_progress_then_pending(self):
        todos = [{"status": "completed", "content": "a", "activeForm": "Doing a"},
                 {"status": "in_progress", "content": "b", "activeForm": "Doing b"}]
        self.assertEqual(sd.work_doing(todos), "Doing b")
        self.assertEqual(sd.work_doing([{"status": "pending", "activeForm": "Doing only"}]),
                         "Doing only")
        self.assertEqual(sd.work_doing([]), "")

    def test_work_progress_counts_completed(self):
        self.assertEqual(sd.work_progress([{"status": "completed"}, {"status": "in_progress"},
                                           {"status": "pending"}]), "1/3")
        self.assertEqual(sd.work_progress([]), "")

    def test_work_detail_focus_file_and_commits(self):
        ec = Counter({"session_dashboard.py": 6, "test_x.py": 1})
        self.assertEqual(sd.work_detail(ec, ["c1", "c2"]),
                         "session_dashboard.py×6(+1) · 2 commits")
        self.assertEqual(sd.work_detail(Counter(), []), "")


class CollectSignalsTest(unittest.TestCase):
    def test_collects_taskflow_commits_edits_plans(self):
        objs = [
            _asst([_tool("Edit", {"file_path": "/r/session_dashboard.py"})]),
            _asst([_tool("Edit", {"file_path": "/r/session_dashboard.py"})]),
            _asst([_tool("Write", {"file_path": "/r/tests/test_x.py"})]),
            _asst([_tool("TaskCreate", {"subject": "scaffold", "activeForm": "Scaffolding"})]),
            _asst([_tool("TaskCreate", {"subject": "wire", "activeForm": "把 NOW 改成工作单元摘要"})]),
            _asst([_tool("TaskUpdate", {"taskId": "1", "status": "completed"})]),
            _asst([_tool("TaskUpdate", {"taskId": "2", "status": "in_progress"})]),
            _asst([_tool("Bash", {"command": 'git commit -m "feat: distill work unit"'})]),
            _asst([_tool("Read", {"file_path": "/r/docs/superpowers/plans/2026-06-03-radar.md"})]),
        ]
        sig = sd._collect_signals(objs)
        self.assertEqual(sig["edit_counts"]["session_dashboard.py"], 2)
        self.assertIn("feat: distill work unit", sig["commits"])
        self.assertEqual([t["status"] for t in sig["todos"]], ["completed", "in_progress"])
        self.assertEqual(sd.work_doing(sig["todos"]), "把 NOW 改成工作单元摘要")
        self.assertIn("2026-06-03-radar.md", sig["plan_refs"])

    def test_todowrite_snapshot_also_supported(self):
        objs = [_asst([_tool("TodoWrite", {"todos": [
            {"status": "in_progress", "content": "x", "activeForm": "Doing x"}]})])]
        sig = sd._collect_signals(objs)
        self.assertEqual(sd.work_doing(sig["todos"]), "Doing x")

    def test_latest_task_source_wins_over_stale_todowrite(self):
        # an early TodoWrite snapshot, then a LATER TaskCreate/TaskUpdate lifecycle
        objs = [
            _asst([_tool("TodoWrite", {"todos": [
                {"status": "in_progress", "content": "stale", "activeForm": "stale step"}]})]),
            _asst([_tool("TaskCreate", {"subject": "real", "activeForm": "real current step"})]),
            _asst([_tool("TaskUpdate", {"taskId": "1", "status": "in_progress"})]),
        ]
        sig = sd._collect_signals(objs)
        self.assertEqual(sd.work_doing(sig["todos"]), "real current step")


class SummarizeSessionTest(unittest.TestCase):
    def _rich_objs(self):
        return [
            {"type": "user", "message": {"role": "user", "content": "build the radar"}},
            _asst([_tool("Edit", {"file_path": "/r/session_dashboard.py"})]),
            _asst([_tool("Edit", {"file_path": "/r/session_dashboard.py"})]),
            _asst([_tool("TaskCreate", {"subject": "scaffold", "activeForm": "Scaffolding"})]),
            _asst([_tool("TaskCreate", {"subject": "wire", "activeForm": "把 NOW 改成工作单元摘要"})]),
            _asst([_tool("TaskUpdate", {"taskId": "1", "status": "completed"})]),
            _asst([_tool("TaskUpdate", {"taskId": "2", "status": "in_progress"})]),
            _asst([_tool("Bash", {"command": 'git commit -m "feat: work unit summary"'})]),
            _asst([_tool("Bash", {"command": 'git commit -m "feat: render badge"'})]),
            _asst([{"type": "text", "text": "好的，我接着改下一处。"}]),  # chatter tail
        ]

    def test_distills_unit_of_work_not_last_message(self):
        w = sd.summarize_session(self._rich_objs(), None, "build the radar",
                                 "feat/work-unit-summary", False, "💬 好的")
        self.assertEqual(w["type"], "feature")
        self.assertEqual(w["source"], "heuristic")
        self.assertEqual(w["title"], "work unit summary")          # branch slug
        self.assertEqual(w["doing"], "把 NOW 改成工作单元摘要")       # in-progress task
        self.assertEqual(w["progress"], "1/2")
        self.assertIn("session_dashboard.py", w["detail"])
        self.assertIn("2 commits", w["detail"])
        self.assertNotIn("好的", w["summary"])                      # NOT the chatter
        self.assertIn("work unit summary", w["summary"])

    def test_bg_job_summarizes_from_intent_no_llm(self):
        job = {"state": "active", "intent": "review F4 SDK alignment", "cwd": "/r"}
        w = sd.summarize_session([], job, "review F4 SDK alignment", "main", False, "")
        self.assertEqual(w["title"], "review F4 SDK alignment")
        self.assertIn("bg:active", w["detail"])

    def test_degrades_to_fallback_when_no_signal(self):
        objs = [{"type": "user", "message": {"role": "user", "content": ""}}]
        w = sd.summarize_session(objs, None, "", "main", False, "Read(sdk.py)")
        self.assertEqual(w["title"], "Read(sdk.py)")


class LlmLayerTest(unittest.TestCase):
    def setUp(self):
        sd._LLM_CACHE.clear()
        sd._LLM_PENDING.clear()

    def test_claim_pending_is_atomic_and_cache_helpers_lock(self):
        fp = "sid|feature|t|d|x"
        self.assertTrue(sd._claim_pending(fp))     # first claim wins
        self.assertFalse(sd._claim_pending(fp))    # already pending → no duplicate job
        sd._cache_set(fp, "短语")
        self.assertEqual(sd._cache_get(fp), "短语")
        sd._release_pending(fp)
        # already cached → claim refuses (no re-compute of a known fingerprint)
        self.assertFalse(sd._claim_pending(fp))

    def test_prompt_includes_signals(self):
        work = {"type": "feature", "title": "work summary", "doing": "wiring",
                "detail": "session_dashboard.py×6 · 2 commits"}
        p = sd.build_llm_prompt(work, "build the radar", "feat/x", "claude-skills")
        self.assertIn("work summary", p)
        self.assertIn("wiring", p)
        self.assertIn("build the radar", p)

    def test_llm_refine_replaces_summary_and_marks_source(self):
        work = {"type": "feature", "icon": "🟢", "title": "work summary", "doing": "wiring",
                "detail": "d", "progress": "1/2", "summary": "🟢 feature · work summary",
                "source": "heuristic"}
        out = sd.llm_refine("sid1", work, "open", "feat/x", "repo",
                            llm=lambda prompt: "为雷达增加工作单元摘要")
        self.assertEqual(out["summary"], "为雷达增加工作单元摘要")
        self.assertEqual(out["source"], "llm")
        self.assertEqual(out["type"], "feature")          # badge preserved
        self.assertEqual(out["detail"], "d")              # detail preserved

    def test_llm_refine_falls_back_when_llm_returns_none(self):
        work = {"type": "feature", "icon": "🟢", "title": "t", "doing": "", "detail": "",
                "progress": "", "summary": "🟢 feature · t", "source": "heuristic"}
        out = sd.llm_refine("sid2", work, "open", "feat/x", "repo", llm=lambda prompt: None)
        self.assertEqual(out["summary"], "🟢 feature · t")
        self.assertEqual(out["source"], "heuristic")

    def test_llm_refine_is_cached_by_signal_fingerprint(self):
        calls = []

        def llm(prompt):
            calls.append(prompt)
            return "缓存短语"

        work = {"type": "feature", "icon": "🟢", "title": "t", "doing": "d1", "detail": "x",
                "progress": "", "summary": "s", "source": "heuristic"}
        sd.llm_refine("sidC", dict(work), "o", "b", "r", llm=llm)
        sd.llm_refine("sidC", dict(work), "o", "b", "r", llm=llm)  # same fingerprint → cached
        self.assertEqual(len(calls), 1)
        work2 = dict(work, doing="d2")                            # changed signal → re-call
        sd.llm_refine("sidC", work2, "o", "b", "r", llm=llm)
        self.assertEqual(len(calls), 2)


def build_home_snapshot_helper(test, use_llm=False, llm=None, llm_block=False):
    """Build a tiny fake ~/.claude home (one foreground transcript with a TaskCreate
    + a couple of Edits, plus one background state.json) and return a snapshot over
    it. Modeled on BuildSnapshotTest.setUp; the key assertions are work.source and
    the now override, not the home layout."""
    home = tempfile.mkdtemp()
    test.addCleanup(lambda: __import__("shutil").rmtree(home, ignore_errors=True))
    proj = os.path.join(home, "projects", "-Users-me-works-claude-skills")
    cwd, branch = "/Users/me/works/claude-skills", "feat/work-unit-summary"
    fg = "f0f0f0f0-1111-2222-3333-444444444444"
    _jsonl(os.path.join(proj, fg + ".jsonl"), [
        {"type": "user", "cwd": cwd, "gitBranch": branch,
         "message": {"role": "user", "content": "build the work unit summary"}},
        _asst([_tool("Edit", {"file_path": cwd + "/session_dashboard.py"})], cwd, branch),
        _asst([_tool("Edit", {"file_path": cwd + "/session_dashboard.py"})], cwd, branch),
        _asst([_tool("TaskCreate", {"subject": "wire", "activeForm": "wiring the summary"})],
              cwd, branch),
        _asst([_tool("TaskUpdate", {"taskId": "1", "status": "in_progress"})], cwd, branch),
    ])
    # BG job: alive state so it always surfaces; non-stale bg rows are distilled.
    bg = "32ea05ca-71ef-40de-8b31-b1c929992902"
    bgp = os.path.join(proj, bg + ".jsonl")
    _jsonl(bgp, [
        {"type": "user", "cwd": "/Users/me/works/aifriends", "gitBranch": "main",
         "message": {"role": "user", "content": "review F4 alignment"}},
        _asst([_tool("Read", {"file_path": "sdk.py"})], "/Users/me/works/aifriends", "main"),
    ])
    _write(os.path.join(home, "jobs", "32ea05ca", "state.json"),
           {"state": "active", "intent": "review F4 alignment",
            "cwd": "/Users/me/works/aifriends",
            "updatedAt": "2026-06-03T09:00:00Z", "linkScanPath": bgp})
    return sd.build_snapshot(home, window_min=0, now=1_700_000_000.0,
                             use_llm=use_llm, llm=llm, llm_block=llm_block)


class RowWorkFieldTest(unittest.TestCase):
    def _objs(self, opening="build the work unit summary", branch="feat/work-unit-summary"):
        return [
            {"type": "user", "message": {"role": "user", "content": opening},
             "cwd": "/r", "gitBranch": branch},
            _asst([_tool("Edit", {"file_path": "/r/session_dashboard.py"})], "/r", branch),
            _asst([_tool("TaskCreate", {"subject": "wire", "activeForm": "wiring the summary"})],
                  "/r", branch),
            _asst([_tool("TaskUpdate", {"taskId": "1", "status": "in_progress"})], "/r", branch),
            _asst([{"type": "text", "text": "完成了，等你确认。"}], "/r", branch),
        ]

    def test_row_now_is_work_summary_not_chatter(self):
        row = sd._row_from_objs("sid12345", 10.0, self._objs(), None)
        self.assertIn("work", row)
        self.assertEqual(row["work"]["type"], "feature")
        self.assertEqual(row["work"]["doing"], "wiring the summary")
        self.assertNotIn("完成了", row["now"])
        self.assertEqual(row["now"], row["work"]["summary"])

    def test_row_drift_false_when_aligned(self):
        # opening shares tokens (work/unit/summary) with the work-unit title → no drift
        row = sd._row_from_objs("sid12345", 10.0, self._objs(), None)
        self.assertFalse(row["drift"])

    def test_row_drift_true_when_diverged(self):
        # opening about login auth; work unit is the summary feature → diverged
        objs = self._objs(opening="fix the login auth token bug")
        row = sd._row_from_objs("sid12345", 10.0, objs, None)
        self.assertTrue(row["drift"])


class SnapshotLlmTest(unittest.TestCase):
    def setUp(self):
        sd._LLM_CACHE.clear()

    def test_build_snapshot_default_is_heuristic_no_llm(self):
        snap = build_home_snapshot_helper(self)   # see helper note below
        for s in snap["sessions"]:
            self.assertEqual(s["work"]["source"], "heuristic")

    def test_build_snapshot_with_injected_llm_refines_fg_and_bg(self):
        snap = build_home_snapshot_helper(self, use_llm=True,
                                          llm=lambda p: "注入的LLM摘要", llm_block=True)
        fg = [s for s in snap["sessions"] if not s["is_bg"]]
        self.assertTrue(fg)
        self.assertTrue(any(s["work"]["source"] == "llm"
                            and s["now"] == "注入的LLM摘要" for s in fg))
        # bg rows are distilled too now (user directive: a verbatim intent echo
        # has no value). Only info-less stale jobs stay heuristic.
        bg = [s for s in snap["sessions"] if s["is_bg"] and not s["is_stale"]]
        self.assertTrue(bg)
        for s in bg:
            self.assertEqual(s["work"]["source"], "llm")
            self.assertEqual(s["now"], "注入的LLM摘要")


class RenderWorkTest(unittest.TestCase):
    def test_html_has_manual_refresh_and_no_5s_interval_and_workcell(self):
        html = sd.render_html()
        self.assertIn('id="refresh"', html)
        self.assertNotIn("setInterval(load, 5000)", html)
        self.assertIn("workCell", html)

    def test_render_table_shows_summary_and_progress(self):
        snap = {
            "counts": {"total": 1, "projects": 1, "bg": 0, "errored": 0, "drift": 0},
            "generated_at": "now", "stale_unknown": 0,
            "projects": [{"project": "claude-skills", "count": 1, "sessions": [{
                "liveness_label": "🟢 active", "is_bg": False, "worktree": None,
                "branch": "feat/x", "opening": "build the radar",
                "now": "为雷达增加工作单元摘要",
                "work": {"type": "feature", "icon": "🟢", "title": "work summary",
                         "doing": "wiring", "detail": "session_dashboard.py×6",
                         "progress": "2/5", "summary": "为雷达增加工作单元摘要",
                         "source": "llm"},
                "acting_on": "aon-main", "drift": False}]}]}
        out = sd.render_table(snap)
        self.assertIn("为雷达增加工作单元摘要", out)
        self.assertIn("2/5", out)
        self.assertIn("↗aon-main", out)

    def test_main_no_llm_flag_parses(self):
        # --no-llm + --once on an empty home must not raise and must print a table
        import io
        from contextlib import redirect_stdout
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = sd.main(["--claude-home", d, "--once", "--no-llm"])
            self.assertEqual(rc, 0)
            self.assertIn("Session Radar", buf.getvalue())


class RadarHelperExclusionTest(unittest.TestCase):
    """The radar's own `claude -p` summary calls write transcripts into
    ~/.claude/projects; scanning them would summarize-the-summarizer (feedback loop)."""

    def test_detects_helper_session_by_prompt_signature(self):
        helper = [{"type": "user", "message": {"role": "user",
                   "content": sd.build_llm_prompt({"type": "feature", "title": "t"},
                                                  "o", "feat/x", "repo")}}]
        self.assertTrue(sd._is_radar_helper_session(helper))
        real = [{"type": "user", "message": {"role": "user",
                 "content": "帮我修一下登录的 bug"}}]
        self.assertFalse(sd._is_radar_helper_session(real))

    def test_build_snapshot_excludes_helper_sessions(self):
        home = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(home, ignore_errors=True))
        proj = os.path.join(home, "projects", "-Users-me-works-claude-skills")
        cwd, branch = "/Users/me/works/claude-skills", "feat/x"
        # a real session
        _jsonl(os.path.join(proj, "aaaaaaaa-0000-0000-0000-000000000000.jsonl"), [
            {"type": "user", "cwd": cwd, "gitBranch": branch,
             "message": {"role": "user", "content": "build the radar"}}])
        # a radar helper session (our own prompt) — must be excluded
        _jsonl(os.path.join(proj, "bbbbbbbb-0000-0000-0000-000000000000.jsonl"), [
            {"type": "user", "cwd": cwd, "gitBranch": branch,
             "message": {"role": "user", "content": sd.build_llm_prompt(
                 {"type": "feature", "title": "t"}, "o", branch, "repo")}}])
        snap = sd.build_snapshot(home, window_min=0, now=1_700_000_000.0)
        shorts = {s["short"] for s in snap["sessions"]}
        self.assertIn("aaaaaaaa", shorts)
        self.assertNotIn("bbbbbbbb", shorts)


class HandlerLlmBindingTest(unittest.TestCase):
    """Regression: a plain function assigned as a bare class attribute becomes a
    BOUND METHOD on instance access (self.llm), injecting `self` as a phantom first
    arg → every server LLM call raised TypeError and silently fell back to heuristic.
    serve() must wrap it in staticmethod. (Direct build_snapshot(llm=...) calls never
    bind, so the rest of the suite couldn't catch this — only the server path did.)"""

    def tearDown(self):
        sd._Handler.llm = None

    def test_handler_llm_access_does_not_bind_self(self):
        calls = []

        def fn(prompt):
            calls.append(prompt)
            return "提炼短语"

        sd._Handler.llm = staticmethod(fn) if fn else None   # mirror serve()
        inst = sd._Handler.__new__(sd._Handler)              # no __init__ (skips socket)
        got = inst.llm                                        # the do_GET access pattern
        self.assertTrue(callable(got))
        self.assertEqual(got("p"), "提炼短语")                 # 1 positional arg, no phantom self
        self.assertEqual(calls, ["p"])

    def test_bare_function_attr_would_bind(self):
        # documents the trap: WITHOUT staticmethod, self.llm binds and raises
        def fn(prompt):
            return "x"
        sd._Handler.llm = fn
        inst = sd._Handler.__new__(sd._Handler)
        with self.assertRaises(TypeError):
            inst.llm("p")   # becomes fn(inst, "p") -> too many args


class ConsolidateWorkUnitsTest(unittest.TestCase):
    """N session windows on the same work unit (project+branch) collapse to one row."""

    def _row(self, short, project, branch, liveness, age, is_bg=False, drift=False):
        return {"short": short, "project": project, "branch": branch,
                "liveness": liveness, "age_s": age, "is_bg": is_bg, "drift": drift}

    def test_same_project_branch_merge_with_count(self):
        rows = [
            self._row("a1", "claude-skills", "feat/radar", "dormant", 50, drift=False),
            self._row("a2", "claude-skills", "feat/radar", "active", 10, drift=True),
            self._row("a3", "claude-skills", "feat/radar", "idle", 30),
        ]
        out = sd.consolidate_work_units(rows)
        self.assertEqual(len(out), 1)
        m = out[0]
        self.assertEqual(m["session_count"], 3)
        self.assertEqual(m["short"], "a2")          # liveliest (active) represents
        self.assertEqual(m["age_s"], 10)            # freshest age
        self.assertTrue(m["drift"])                 # drift unioned
        self.assertEqual(set(m["merged_shorts"]), {"a1", "a2", "a3"})

    def test_singletons_and_bg_and_generic_pass_through(self):
        rows = [
            self._row("s1", "p", "feat/x", "active", 5),                 # singleton unit
            self._row("s2", "p", "main", "active", 5),                   # generic branch
            self._row("b1", "p", "feat/x", "working", 5, is_bg=True),    # bg never merged
            self._row("s3", "q", "feat/x", "active", 5),                 # different project
        ]
        out = sd.consolidate_work_units(rows)
        self.assertEqual(len(out), 4)               # nothing merges
        for r in out:
            self.assertEqual(r["session_count"], 1)

    def test_build_snapshot_consolidates_and_keeps_total(self):
        home = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(home, ignore_errors=True))
        proj = os.path.join(home, "projects", "-Users-me-works-claude-skills")
        cwd, branch = "/Users/me/works/claude-skills", "feat/radar"
        for i in range(3):
            _jsonl(os.path.join(proj, f"{i}{i}{i}{i}{i}{i}{i}{i}-0000-0000-0000-000000000000.jsonl"), [
                {"type": "user", "cwd": cwd, "gitBranch": branch,
                 "message": {"role": "user", "content": f"window {i} of the radar work"}}])
        snap = sd.build_snapshot(home, window_min=0, now=1_700_000_000.0)
        self.assertEqual(snap["counts"]["total"], 3)            # 3 real sessions
        g = next(p for p in snap["projects"] if p["project"] == "claude-skills")
        self.assertEqual(len(g["sessions"]), 1)                 # one consolidated row
        self.assertEqual(g["sessions"][0]["session_count"], 3)
        self.assertEqual(g["count"], 3)                         # header still totals sessions
        self.assertEqual(g["units"], 1)
