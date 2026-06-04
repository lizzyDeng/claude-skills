---
name: session-radar
description: Stdlib-only dashboard for ALL local Claude sessions (foreground transcripts + background jobs). Shows liveness, the repo/worktree/branch each session is acting on right now, and opening-intent→current-action drift. Triggers: "session radar", "what are my sessions doing", "show running claude sessions".
---

# Session Radar

Scans `~/.claude/projects/*/*.jsonl` + `~/.claude/jobs/` and surfaces, per session:

- **Liveness** — active / idle / dormant / errored. Background jobs trust their own
  `state` (`working` / `blocked` / `done`) instead of transcript mtime, so a job that
  is alive but quiet between turns is not mislabelled dormant.
- **Current repo / worktree / branch** — derived from the *latest* transcript event,
  so it reflects where the session is acting now, not where it started.
- **opening → NOW drift** — the real human opening intent (command-shell stripped)
  beside the current action; flagged when they diverge.

## Usage

```bash
# Web dashboard (port polls upward from 7575 if busy), client-rendered, 5s refresh
python3 skills/session-radar/session_dashboard.py
# or via launcher
skills/session-radar/session-radar

# One-shot terminal table
python3 skills/session-radar/session_dashboard.py --once

# Raw snapshot JSON
python3 skills/session-radar/session_dashboard.py --json

# Options
#   --claude-home DIR   root of ~/.claude (default ~/.claude)
#   --port N            base port (default 7575)
#   --window-min N      recency lens for foreground sessions (default 120; 0 = show ALL; bg always shown)
```

Pure Python stdlib — no dependencies, no build step.
