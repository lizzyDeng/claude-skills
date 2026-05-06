#!/usr/bin/env python3
"""
forge_gate.py — Project-level roadmap gate script

Manages the /forge state machine: roadmap I/O, feature state transitions,
gate validation, hook handlers, and roadmap.md generation.

Internal CLI (called by /forge skill, not by users directly):
  status                 — Print roadmap overview + overdue harvest reminders
  activate <slug>        — Set active feature
  transition <slug> <status> — Attempt state transition with gate check
  generate-view          — Regenerate roadmap.md from roadmap.json
  reset                  — Clear active feature state

Hook handlers (called by Claude Code hooks):
  pre_edit               — Protect .forge-state.json from tampering
  post_edit              — Detect metric.json/harvest.json/roadmap.json writes
  post_bash              — Check overdue harvests, print reminder

State file: {repo_root}/.claude/.forge-state.json (derived cache)
Source of truth: {repo_root}/project-roadmap/roadmap.json
"""

import sys
import os
import json
import subprocess
from datetime import datetime, timedelta


# ========== Helpers ==========

def get_repo_root():
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def roadmap_dir():
    root = get_repo_root()
    return os.path.join(root, "project-roadmap") if root else None


def roadmap_path():
    d = roadmap_dir()
    return os.path.join(d, "roadmap.json") if d else None


def state_path():
    root = get_repo_root()
    return os.path.join(root, ".claude", ".forge-state.json") if root else None


def fastship_state_path():
    root = get_repo_root()
    return os.path.join(root, ".claude", ".ship-verify-state.json") if root else None


def read_stdin():
    if sys.stdin.isatty():
        return {}
    try:
        raw = sys.stdin.read().strip()
        return json.loads(raw) if raw else {}
    except Exception:
        return {}


# ========== Roadmap I/O ==========

def load_roadmap():
    p = roadmap_path()
    if not p or not os.path.exists(p):
        return None
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return None


def save_roadmap(data):
    p = roadmap_path()
    if not p:
        return
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def find_feature(roadmap, slug):
    for f in roadmap.get("features", []):
        if f.get("slug") == slug:
            return f
    return None


# ========== Validation (Gates) ==========

METRIC_REQUIRED_FIELDS = ["metric_name", "event_name", "baseline", "target", "harvest_days", "data_source"]
HARVEST_REQUIRED_FIELDS = ["harvested_at", "actual", "baseline", "target", "verdict", "notes", "next_action"]
VALID_VERDICTS = {"achieved", "partial", "missed"}
VALID_NEXT_ACTIONS = {"done", "iterate", "pivot"}


def validate_metric(metric):
    """Validate metric.json structure (Gate 1). Returns (ok, errors)."""
    errors = []
    for field in METRIC_REQUIRED_FIELDS:
        if field not in metric or metric[field] is None or metric[field] == "":
            errors.append(f"Missing required field: {field}")
    if "baseline" in metric and metric["baseline"] is not None:
        if not isinstance(metric["baseline"], (int, float)):
            errors.append("baseline must be numeric")
    if "target" in metric and metric["target"] is not None:
        if not isinstance(metric["target"], (int, float)):
            errors.append("target must be numeric")
    if "harvest_days" in metric and metric["harvest_days"] is not None:
        if not isinstance(metric["harvest_days"], int) or metric["harvest_days"] < 1:
            errors.append("harvest_days must be a positive integer")
    return (len(errors) == 0, errors)


def validate_harvest(harvest):
    """Validate harvest.json structure (Gate 6). Returns (ok, errors)."""
    errors = []
    for field in HARVEST_REQUIRED_FIELDS:
        if field not in harvest or harvest[field] is None or harvest[field] == "":
            errors.append(f"Missing required field: {field}")
    if harvest.get("verdict") and harvest["verdict"] not in VALID_VERDICTS:
        errors.append(f"verdict must be one of: {VALID_VERDICTS}")
    if harvest.get("next_action") and harvest["next_action"] not in VALID_NEXT_ACTIONS:
        errors.append(f"next_action must be one of: {VALID_NEXT_ACTIONS}")
    if "actual" in harvest and harvest["actual"] is not None:
        if not isinstance(harvest["actual"], (int, float)):
            errors.append("actual must be numeric")
    return (len(errors) == 0, errors)


# ========== State Machine ==========

VALID_STATUSES = ["draft", "planned", "in_progress", "shipped", "measuring", "concluded"]

TRANSITIONS = {
    ("draft", "planned"),
    ("planned", "in_progress"),
    ("in_progress", "shipped"),
    ("shipped", "measuring"),
    ("measuring", "concluded"),
}


def check_g1_metric(slug, repo_root):
    """Gate 1: metric.json must exist and be valid before entering draft."""
    if not repo_root:
        return (False, "Gate 1: cannot determine repo root")
    metric_path = os.path.join(repo_root, "project-roadmap", "features", slug, "metric.json")
    if not os.path.exists(metric_path):
        return (False, f"Gate 1: metric.json not found at {metric_path}")
    try:
        with open(metric_path) as f:
            metric = json.load(f)
        ok, errors = validate_metric(metric)
        if not ok:
            return (False, f"Gate 1: metric.json invalid — {'; '.join(errors)}")
    except Exception as e:
        return (False, f"Gate 1: cannot read metric.json — {e}")
    return (True, "")


def check_g6_harvest(slug, repo_root):
    """Gate 6: harvest.json must exist and be valid before concluding."""
    if not repo_root:
        return (False, "Gate 6: cannot determine repo root")
    harvest_path = os.path.join(repo_root, "project-roadmap", "features", slug, "harvest.json")
    if not os.path.exists(harvest_path):
        return (False, f"Gate 6: harvest.json not found at {harvest_path}")
    try:
        with open(harvest_path) as f:
            harvest = json.load(f)
        ok, errors = validate_harvest(harvest)
        if not ok:
            return (False, f"Gate 6: harvest.json invalid — {'; '.join(errors)}")
    except Exception as e:
        return (False, f"Gate 6: cannot read harvest.json — {e}")
    return (True, "")


def can_transition(slug, current_status, target_status, repo_root, fastship_state=None):
    """Check if a state transition is allowed. Returns (ok, reason)."""
    if (current_status, target_status) not in TRANSITIONS:
        return (False, f"Not allowed: {current_status} → {target_status}. "
                       f"Valid transitions from {current_status}: "
                       f"{[t for (s, t) in TRANSITIONS if s == current_status]}")

    if fastship_state is None:
        fastship_state = {}

    # Gate 2: draft → planned (fastship Phase 1 complete)
    if current_status == "draft" and target_status == "planned":
        if not fastship_state.get("plan_ready"):
            return (False, "Gate 2: fastship plan not yet ready (plan_ready=false)")

    # Gate 4: in_progress → shipped (fastship Phase 3 complete)
    if current_status == "in_progress" and target_status == "shipped":
        missing = []
        if not fastship_state.get("test_passed"):
            missing.append("test_passed")
        if not fastship_state.get("e2e_executed"):
            missing.append("e2e_executed")
        if not fastship_state.get("knowledge_acknowledged"):
            missing.append("knowledge_acknowledged")
        if missing:
            return (False, f"Gate 4: fastship not complete. Missing: {', '.join(missing)}")

    # Gate 6: measuring → concluded (harvest.json must exist and be valid)
    if current_status == "measuring" and target_status == "concluded":
        ok, reason = check_g6_harvest(slug, repo_root)
        if not ok:
            return (False, reason)

    return (True, "")


# ========== Overdue Harvest Detection ==========

def get_overdue_harvests(features, today_str=None):
    """Return list of features in measuring status past harvest_due date."""
    if today_str is None:
        today_str = datetime.now().strftime("%Y-%m-%d")
    today = datetime.strptime(today_str, "%Y-%m-%d").date()
    overdue = []
    for f in features:
        if f.get("status") != "measuring":
            continue
        due = f.get("harvest_due")
        if due:
            due_date = datetime.strptime(due, "%Y-%m-%d").date()
            if today >= due_date:
                overdue.append(f)
    return overdue


# ========== State Derivation ==========

def load_fastship_state():
    p = fastship_state_path()
    if not p or not os.path.exists(p):
        return {}
    try:
        with open(p) as f:
            return json.load(f)
    except Exception:
        return {}


def derive_state(roadmap, active_feature=None):
    """Derive .forge-state.json from roadmap.json + filesystem checks."""
    root = get_repo_root()
    state = {
        "active_feature": active_feature,
        "phase": None,
        "g1_metric_defined": False,
        "g2_plan_ready": False,
        "g3_dev_started": False,
        "g4_shipped": False,
        "g5_measuring": False,
        "g6_harvested": False,
    }

    if not active_feature or not roadmap:
        return state

    feature = find_feature(roadmap, active_feature)
    if not feature:
        return state

    status = feature.get("status", "")

    # Derive phase
    if status in ("draft", "planned"):
        state["phase"] = "planning"
    elif status == "in_progress":
        state["phase"] = "developing"
    elif status in ("shipped", "measuring"):
        state["phase"] = "harvesting"
    elif status == "concluded":
        state["phase"] = "done"

    # Check G1: metric.json exists and valid
    if root:
        metric_path = os.path.join(root, "project-roadmap", "features", active_feature, "metric.json")
        if os.path.exists(metric_path):
            try:
                with open(metric_path) as f:
                    metric = json.load(f)
                ok, _ = validate_metric(metric)
                state["g1_metric_defined"] = ok
            except Exception:
                pass

    # Check G2: fastship plan_ready
    fs_state = load_fastship_state()
    state["g2_plan_ready"] = bool(fs_state.get("plan_ready"))

    # Check G3-G6 from roadmap status
    state["g3_dev_started"] = status in ("in_progress", "shipped", "measuring", "concluded")
    state["g4_shipped"] = status in ("shipped", "measuring", "concluded")
    state["g5_measuring"] = status in ("measuring", "concluded")

    # Check G6: harvest.json exists and valid
    if root:
        harvest_path = os.path.join(root, "project-roadmap", "features", active_feature, "harvest.json")
        if os.path.exists(harvest_path):
            try:
                with open(harvest_path) as f:
                    harvest = json.load(f)
                ok, _ = validate_harvest(harvest)
                state["g6_harvested"] = ok
            except Exception:
                pass

    return state


def load_forge_state():
    p = state_path()
    if p and os.path.exists(p):
        try:
            with open(p) as f:
                return json.load(f)
        except Exception:
            pass
    return {"active_feature": None}


def save_forge_state(state):
    p = state_path()
    if not p:
        return
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        json.dump(state, f, indent=2)


# ========== Roadmap.md Generation ==========

STATUS_EMOJI = {
    "draft": "📋",
    "planned": "📐",
    "in_progress": "🔄",
    "shipped": "🚀",
    "measuring": "⏳",
    "concluded": "✅",
}


def generate_roadmap_md(roadmap):
    """Generate roadmap.md content from roadmap.json."""
    proj = roadmap.get("project", {})
    lines = [
        f"# {proj.get('name', 'Project')} Roadmap",
        "",
        f"> North Star: {proj.get('north_star', '(undefined)')}",
        "",
    ]

    objectives = roadmap.get("objectives", [])
    features = roadmap.get("features", [])
    root = get_repo_root()  # Hoist outside loop

    for obj in objectives:
        lines.append(f"## {obj.get('name', '(unnamed)')}")
        lines.append(f"Target: {obj.get('target_metric', '(undefined)')}")
        lines.append("")
        lines.append("| Feature | Status | Shipped | Harvest |")
        lines.append("|---------|--------|---------|---------|")

        obj_features = [f for f in features if f.get("objective_id") == obj.get("id")]
        for f in obj_features:
            status = f.get("status", "")
            emoji = STATUS_EMOJI.get(status, "")
            shipped = f.get("shipped_at", "-") or "-"
            if shipped != "-":
                shipped = shipped[5:]  # MM-DD
            harvest = "-"
            if f.get("harvest_due"):
                due = f["harvest_due"]
                harvest = f"Due {due[5:]}"
            if status == "concluded":
                if root:
                    hp = os.path.join(root, "project-roadmap", "features", f["slug"], "harvest.json")
                    if os.path.exists(hp):
                        try:
                            with open(hp) as hf:
                                hdata = json.load(hf)
                            harvest = hdata.get("verdict", "done")
                        except Exception:
                            harvest = "done"
            lines.append(f"| {f.get('name', f['slug'])} | {emoji} {status} | {shipped} | {harvest} |")

        lines.append("")

    # Summary
    counts = {}
    for f in features:
        s = f.get("status", "unknown")
        counts[s] = counts.get(s, 0) + 1

    summary_parts = []
    for s in VALID_STATUSES:
        if counts.get(s, 0) > 0:
            summary_parts.append(f"{STATUS_EMOJI.get(s, '')} {s}: {counts[s]}")
    lines.append("## Summary")
    lines.append(" | ".join(summary_parts) if summary_parts else "No features yet.")
    lines.append("")

    return "\n".join(lines)


def save_roadmap_md(roadmap):
    """Write roadmap.md next to roadmap.json."""
    d = roadmap_dir()
    if not d:
        return
    content = generate_roadmap_md(roadmap)
    with open(os.path.join(d, "roadmap.md"), "w") as f:
        f.write(content)


# ========== CLI Commands ==========

def cmd_status():
    """Print global roadmap status + overdue harvest reminders."""
    roadmap = load_roadmap()
    if not roadmap:
        print("❌ No roadmap found. Run /forge init first.")
        sys.exit(0)

    proj = roadmap.get("project", {})
    print(f"🔥 {proj.get('name', 'Project')} — {proj.get('north_star', '')}")
    print()

    features = roadmap.get("features", [])
    for s in VALID_STATUSES:
        group = [f for f in features if f.get("status") == s]
        if group:
            print(f"  {STATUS_EMOJI.get(s, '')} {s}: {', '.join(f.get('name', f['slug']) for f in group)}")

    # Overdue reminders
    today = datetime.now().strftime("%Y-%m-%d")
    overdue = get_overdue_harvests(features, today)
    if overdue:
        print()
        print("⚠️  收益回收到期：")
        for f in overdue:
            shipped = f.get("shipped_at", "?")
            due = f.get("harvest_due", "?")
            print(f"  - {f.get('name', f['slug'])}（上线 {shipped}，回收日期 {due}）")

    # Measuring but not overdue
    measuring = [f for f in features
                 if f.get("status") == "measuring" and f not in overdue]
    if measuring:
        print()
        for f in measuring:
            due = f.get("harvest_due", "?")
            print(f"  ⏳ {f.get('name', f['slug'])} — 回收日期 {due}")

    # Active feature
    forge_state = load_forge_state()
    active = forge_state.get("active_feature")
    if active:
        print(f"\n  🎯 Active: {active}")


def cmd_activate(slug):
    """Set active feature."""
    roadmap = load_roadmap()
    if not roadmap:
        print("❌ No roadmap found.")
        sys.exit(1)
    feature = find_feature(roadmap, slug)
    if not feature:
        print(f"❌ Feature '{slug}' not found in roadmap.")
        sys.exit(1)

    state = derive_state(roadmap, slug)
    save_forge_state(state)
    print(f"✅ Active feature set to: {slug} (status: {feature['status']})")


def cmd_transition(slug, target_status):
    """Attempt state transition with gate check."""
    roadmap = load_roadmap()
    if not roadmap:
        print("❌ No roadmap found.")
        sys.exit(1)

    feature = find_feature(roadmap, slug)
    if not feature:
        print(f"❌ Feature '{slug}' not found.")
        sys.exit(1)

    current = feature["status"]
    if current == target_status:
        print(f"ℹ️  Feature '{slug}' is already in '{target_status}'.")
        sys.exit(0)

    root = get_repo_root()
    fs_state = load_fastship_state()

    ok, reason = can_transition(slug, current, target_status, root, fs_state)
    if not ok:
        print(f"🚫 Transition blocked: {reason}")
        sys.exit(1)

    # Apply transition
    feature["status"] = target_status
    now = datetime.now().strftime("%Y-%m-%d")

    if target_status == "shipped":
        feature["shipped_at"] = now
        # Read metric to calculate harvest_due
        metric_path = os.path.join(root, "project-roadmap", "features", slug, "metric.json") if root else None
        harvest_days = 7
        if metric_path and os.path.exists(metric_path):
            try:
                with open(metric_path) as f:
                    metric = json.load(f)
                harvest_days = metric.get("harvest_days", 7)
            except Exception:
                pass
        due = (datetime.now() + timedelta(days=harvest_days)).strftime("%Y-%m-%d")
        feature["harvest_due"] = due
        # Auto-transition to measuring (G5) — validate through can_transition
        ok_g5, reason_g5 = can_transition(slug, "shipped", "measuring", root, fs_state)
        if not ok_g5:
            print(f"🚫 G5 auto-transition blocked: {reason_g5}")
            sys.exit(1)
        feature["status"] = "measuring"
        target_status = "measuring"

    if target_status == "concluded":
        feature["concluded_at"] = now

    save_roadmap(roadmap)
    save_roadmap_md(roadmap)

    # Re-derive forge state
    active = slug
    state = derive_state(roadmap, active)
    save_forge_state(state)

    print(f"✅ {slug}: {current} → {target_status}")


def cmd_generate_view():
    """Regenerate roadmap.md."""
    roadmap = load_roadmap()
    if not roadmap:
        print("❌ No roadmap found.")
        sys.exit(1)
    save_roadmap_md(roadmap)
    print("✅ roadmap.md regenerated.")


def cmd_reset():
    """Clear active feature state."""
    state = {"active_feature": None}
    save_forge_state(state)
    print("✅ Active feature cleared.")


# ========== Hook Handlers ==========

def hook_pre_edit():
    """Protect .forge-state.json from manual tampering."""
    data = read_stdin()
    file_path = data.get("tool_input", {}).get("file_path", "")
    if ".forge-state.json" in file_path:
        print("🚫 Forge Gate: .forge-state.json is managed by forge_gate.py. Do not edit manually.")
        sys.exit(1)


def hook_post_edit():
    """Detect metric.json / harvest.json / roadmap.json writes."""
    data = read_stdin()
    file_path = data.get("tool_input", {}).get("file_path", "")

    if not file_path:
        return

    # Detect roadmap.json change → regenerate view
    if file_path.endswith("roadmap.json") and "project-roadmap" in file_path:
        roadmap = load_roadmap()
        if roadmap:
            save_roadmap_md(roadmap)

    # Detect metric.json write → validate
    if file_path.endswith("metric.json") and "project-roadmap" in file_path:
        try:
            with open(file_path) as f:
                metric = json.load(f)
            ok, errors = validate_metric(metric)
            if not ok:
                print(f"⚠️  metric.json validation issues: {'; '.join(errors)}")
        except Exception as e:
            print(f"⚠️  Could not validate metric.json: {e}")

    # Detect harvest.json write → validate
    if file_path.endswith("harvest.json") and "project-roadmap" in file_path:
        try:
            with open(file_path) as f:
                harvest = json.load(f)
            ok, errors = validate_harvest(harvest)
            if not ok:
                print(f"⚠️  harvest.json validation issues: {'; '.join(errors)}")
        except Exception as e:
            print(f"⚠️  Could not validate harvest.json: {e}")


def hook_post_bash():
    """Check overdue harvests after bash commands (lightweight reminder)."""
    roadmap = load_roadmap()
    if not roadmap:
        return
    features = roadmap.get("features", [])
    today = datetime.now().strftime("%Y-%m-%d")
    overdue = get_overdue_harvests(features, today)
    if overdue:
        names = ", ".join(f.get("name", f["slug"]) for f in overdue)
        print(f"⚠️  Forge: 收益回收到期 → {names}. Run /forge harvest <feature>.")


# ========== Main Dispatch ==========

def main():
    if len(sys.argv) < 2:
        print("Usage: forge_gate.py <action> [args...]")
        sys.exit(1)

    action = sys.argv[1]

    if action == "check-g1":
        if len(sys.argv) < 3:
            print("Usage: forge_gate.py check-g1 <slug>")
            sys.exit(1)
        root = get_repo_root()
        ok, reason = check_g1_metric(sys.argv[2], root)
        if ok:
            print(f"✅ Gate 1 passed for {sys.argv[2]}")
        else:
            print(f"🚫 {reason}")
            sys.exit(1)
    elif action == "status":
        cmd_status()
    elif action == "activate":
        if len(sys.argv) < 3:
            print("Usage: forge_gate.py activate <slug>")
            sys.exit(1)
        cmd_activate(sys.argv[2])
    elif action == "transition":
        if len(sys.argv) < 4:
            print("Usage: forge_gate.py transition <slug> <status>")
            sys.exit(1)
        cmd_transition(sys.argv[2], sys.argv[3])
    elif action == "generate-view":
        cmd_generate_view()
    elif action == "reset":
        cmd_reset()
    elif action == "pre_edit":
        hook_pre_edit()
    elif action == "post_edit":
        hook_post_edit()
    elif action == "post_bash":
        hook_post_bash()
    else:
        print(f"Unknown action: {action}")
        sys.exit(1)


if __name__ == "__main__":
    main()
