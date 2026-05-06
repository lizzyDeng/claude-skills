# /forge — Project-Level Harness Design Spec

> Date: 2026-05-06
> Status: Draft
> Author: Human + Claude (brainstorming)

## Overview

`/forge` is a project-level harness skill that wraps `/fastship` to drive the full project lifecycle — from goal definition to benefit harvesting. While fastship ensures reliable delivery of individual features, forge manages the roadmap, tracks feature outcomes, and closes the feedback loop between delivery and business impact.

**Core metaphor**: A forge shapes raw material (requirements) through fire and hammering (gate system) into reliable products, iterating until the result meets the standard.

**Core principle**: Product constraints over process constraints. Every state transition is gated by verifiable artifacts, not LLM prompt compliance.

## Problem Statement

Fastship solves feature-level quality: brainstorm → plan → dev → E2E verification → knowledge closure. But it lacks:

1. **Project continuity** — no roadmap tracking across features
2. **Benefit harvesting** — no mechanism to verify if shipped features achieved their business goals
3. **Feedback loop** — no way for outcome data to inform the next feature's planning
4. **Metrics enforcement** — no gate requiring data instrumentation before development begins

Forge adds these four capabilities as a layer above fastship.

## Architecture

### Wrapper Model

Forge wraps fastship without modifying it. Integration is through data bridging:

```
/forge                                    /fastship
┌──────────────────┐                ┌──────────────────┐
│  roadmap.json    │───read/write──▶│                  │
│  metric.json     │───inject ctx──▶│  Phase 1: Plan   │
│                  │                │  Phase 2: Dev    │
│                  │◀──read state───│  Phase 3: Verify │
│  harvest.json    │                │                  │
└──────────────────┘                └──────────────────┘
     forge_gate.py                   ship_verify_gate.py
     (project gate)                  (feature gate)
```

**Unidirectional dependency**: forge reads fastship state; fastship does not know forge exists. Fastship remains independently usable.

### State Machine

Each feature follows a strict state machine with gate-enforced transitions:

```
draft ──→ planned ──→ in_progress ──→ shipped ──→ measuring ──→ concluded
  │          │            │              │            │              │
  ▼          ▼            ▼              ▼            ▼              ▼
 Gate 1    Gate 2       Gate 3         Gate 4       Gate 5        Gate 6
```

| Gate | Transition | Required Artifact |
|------|------------|-------------------|
| G1 | → draft | `metric.json` exists and valid: metric_name, baseline, target, event_name, harvest_days, data_source |
| G2 | draft → planned | fastship Phase 1 complete: plan file committed + grill passed + user sign-off |
| G3 | planned → in_progress | Automatic: triggered when entering fastship Phase 2 |
| G4 | → shipped | fastship Phase 3 complete: E2E pass + KNOWLEDGE.md closure |
| G5 | → measuring | Automatic: immediately after shipped, records ship timestamp, calculates harvest due date |
| G6 | → concluded | `harvest.json` exists and valid: actual data, baseline vs actual comparison, verdict, next_action |

### Integration Points with Fastship

**1. Before fastship (context injection)**:
When user runs `/forge plan <feature>`:
- Inject `metric.json` instrumentation requirements into fastship Phase 1 context
- Inject the feature's parent objective from roadmap ("why are we building this")
- Append instrumentation implementation as part of AC

**2. During fastship (no intervention)**:
Forge does not interfere with fastship Phase 2/3. It passively reads `.ship-verify-state.json` to track progress.

**3. After fastship (state harvesting)**:
When fastship completes (test_passed + e2e_executed + knowledge_acknowledged all true):
- Forge transitions feature to `shipped → measuring`
- Records ship timestamp
- Calculates harvest due date

## Data Structures

### Project Data Directory

```
project-roadmap/
├── roadmap.json          # Source of truth (gates read this)
├── roadmap.md            # Auto-generated human-readable view
├── features/
│   ├── <feature-slug>/
│   │   ├── metric.json   # Instrumentation definition (pre-dev)
│   │   └── harvest.json  # Benefit harvest results (post-ship)
│   └── ...
└── retrospectives/       # Periodic cross-feature retrospectives
```

### roadmap.json

```json
{
  "project": {
    "name": "project-name",
    "north_star": "The ultimate goal this project serves",
    "created_at": "2026-05-06"
  },
  "objectives": [
    {
      "id": "obj-1",
      "name": "Objective name",
      "description": "What this objective means",
      "target_metric": "metric_name >= threshold",
      "features": ["feature-slug-a", "feature-slug-b"]
    }
  ],
  "features": [
    {
      "slug": "feature-slug-a",
      "name": "Human-readable feature name",
      "objective_id": "obj-1",
      "status": "draft|planned|in_progress|shipped|measuring|concluded",
      "created_at": "2026-05-06",
      "shipped_at": null,
      "harvest_due": null,
      "concluded_at": null,
      "previous_feature": null
    }
  ]
}
```

Feature detail data (metric, harvest) lives in `features/<slug>/` directories. roadmap.json stores only index and status.

`previous_feature` records the iteration chain when a feature is created via iterate.

### metric.json

```json
{
  "metric_name": "Human-readable metric description",
  "event_name": "analytics_event_name",
  "baseline": 0.32,
  "target": 0.45,
  "harvest_days": 7,
  "data_source": "api|manual",
  "data_endpoint": "/api/analytics/endpoint"
}
```

- `data_source: "api"` — forge can auto-fetch data via endpoint
- `data_source: "manual"` — user manually provides data from third-party platforms (GA, Mixpanel, etc.)
- `data_endpoint` — only required when data_source is "api"

### harvest.json

```json
{
  "harvested_at": "2026-05-13",
  "actual": 0.41,
  "baseline": 0.32,
  "target": 0.45,
  "verdict": "achieved|partial|missed",
  "notes": "Free-form analysis of the results",
  "next_action": "done|iterate|pivot",
  "next_feature": "new-feature-slug-or-null"
}
```

When `next_action` is "iterate":
- Forge auto-creates a new feature draft
- Inherits the same metric (updates baseline to current actual)
- References original feature's harvest.json as context

When `next_action` is "pivot":
- Feature is concluded
- Parent objective is flagged for re-evaluation

## Benefit Harvesting

### Automatic Reminders

Every time `/forge` is invoked, it scans all features in `measuring` status:
- If current_date >= shipped_at + harvest_days → highlight as overdue
- Otherwise → show days remaining

No cron or external scheduler. Pure data-state + current-time calculation.

### Manual Harvest Flow: `/forge harvest <feature>`

```
1. Read metric.json → get data_source and data_endpoint
2. Based on data_source:
   ├── "api" → auto-call endpoint, populate harvest.json
   └── "manual" → prompt user to provide data from third-party platform
3. Calculate baseline vs actual
4. Guide user to determine verdict (achieved/partial/missed)
5. Guide user to decide next_action:
   ├── "done" → concluded, update objective progress
   ├── "iterate" → concluded + auto-create new feature draft (inherit context)
   └── "pivot" → concluded + flag objective for re-evaluation
6. Write harvest.json → Gate 6 passes → status transitions to concluded
7. Auto-update roadmap.json + regenerate roadmap.md
```

### The Iterate Loop

This is the core feedback mechanism:

```
Feature A (baseline: 32%) → ship → harvest (actual: 41%, target: 45%)
  → verdict: partial → next_action: iterate
  → Feature A-v2 auto-created (baseline: 41%, target: 45%)
    → plan → dev → ship → harvest (actual: 47%)
    → verdict: achieved → done
```

Each iteration inherits context and narrows the gap. The `previous_feature` chain provides full traceability.

## Gate Script: forge_gate.py

### State File: `.claude/.forge-state.json`

```json
{
  "active_feature": "feature-slug-a",
  "phase": "planning|developing|harvesting",
  "g1_metric_defined": true,
  "g2_plan_ready": false,
  "g4_shipped": false,
  "g6_harvested": false
}
```

### Hook Triggers

| Hook | Check |
|------|-------|
| pre_edit | Protect `.forge-state.json` and `roadmap.json` from manual tampering |
| post_edit | Detect `metric.json` write → validate structure → set g1_metric_defined=true |
| post_edit | Detect `harvest.json` write → validate required fields → set g6_harvested=true |
| post_edit | Detect `roadmap.json` change → auto-regenerate roadmap.md |

### CLI Commands

| Command | Action |
|---------|--------|
| `forge status` | Print global roadmap status + overdue harvest reminders |
| `forge activate <slug>` | Set active feature, subsequent fastship operations associate to this feature |
| `forge transition <slug> <status>` | Attempt state transition, reject with reason if gate check fails |
| `forge generate-view` | Manually trigger roadmap.md regeneration |
| `forge reset` | Clear active feature state (does not affect roadmap data) |

### Coordination with ship_verify_gate.py

Two independent gate scripts coordinating through shared state files:

- forge_gate.py writes `.forge-state.json`, reads `.ship-verify-state.json`
- ship_verify_gate.py writes `.ship-verify-state.json`, does not read `.forge-state.json`
- Unidirectional: forge depends on fastship state, not vice versa

## Commands

| Command | Action | State Transition |
|---------|--------|-----------------|
| `/forge init` | Define North Star + objectives | Creates roadmap.json |
| `/forge add <feature>` | Add feature with AC + instrumentation | → draft |
| `/forge plan <feature>` | Enter fastship Phase 1 (with metric context injection) | draft → planned |
| `/forge dev <feature>` | Enter fastship Phase 2+3 | planned → in_progress |
| `/forge ship <feature>` | Mark as shipped, start harvest countdown | in_progress → shipped → measuring |
| `/forge harvest <feature>` | Execute benefit harvesting | measuring → concluded |
| `/forge status` | Print global roadmap state | None |
| `/forge retro` | Cross-feature retrospective (pattern recognition) | None |

## Auto-Generated View: roadmap.md

Regenerated on every roadmap.json change:

```markdown
# Project Name Roadmap

> North Star: ...

## Objective Name
Target: metric_name >= threshold

| Feature | Status | Shipped | Harvest |
|---------|--------|---------|---------|
| Feature A | measuring | 05-10 | Due 05-17 |
| Feature B | in_progress | - | - |
| Feature C | draft | - | - |

## Summary
- In progress: 1 | Awaiting harvest: 1 | Concluded: 0 | Draft: 1
```

## File Structure

### Skill files (in claude-skills repo)

```
skills/forge/
├── SKILL.md              # Skill definition
├── INSTALL.md            # Installation guide
├── hooks/
│   └── forge_gate.py     # Gate script (state machine + hooks + CLI)
└── templates/
    ├── roadmap.json      # Initial roadmap template
    ├── metric.json       # Instrumentation template
    └── harvest.json      # Harvest result template
```

### Installed in user project

```
project-root/
├── project-roadmap/          # Git-tracked
│   ├── roadmap.json
│   ├── roadmap.md
│   └── features/
├── .claude/
│   ├── hooks/
│   │   ├── forge_gate.py         # Project-level gate
│   │   └── ship_verify_gate.py   # Feature-level gate (fastship)
│   ├── .forge-state.json         # Git-ignored
│   └── .ship-verify-state.json   # Git-ignored
└── .claude/commands/
    └── forge.md                  # Skill definition
```

**Roadmap data (`project-roadmap/`) is git-tracked** for team visibility. **State files are git-ignored** as they are personal runtime state.

## Installation

Provided via `/forge-setup` (same pattern as `/fastship-setup`):

1. Check fastship is installed (forge depends on fastship)
2. Copy `SKILL.md` → `.claude/commands/forge.md`
3. Copy `forge_gate.py` → `.claude/hooks/`
4. Append forge hook config to `settings.local.json` (does not overwrite fastship hooks)
5. Create `project-roadmap/` directory with template files
6. Add `project-roadmap/` to git tracking, `.forge-state.json` to gitignore
7. Verify with `forge status`

## Scope & Non-Goals

**In scope**:
- Roadmap definition and tracking (North Star → objectives → features)
- Feature state machine with gate enforcement
- Instrumentation requirement as a gate before development
- Benefit harvesting with auto-fetch (API) and manual input
- Iterate loop for continuous improvement toward objectives
- Auto-generated roadmap view

**Not in scope (future)**:
- Multi-user role management (PM vs dev views)
- Integration with external project management tools (Linear, Jira)
- Automated A/B testing orchestration
- Code-level entropy management (scheduled cleanup agents)
- Dashboard or web UI for roadmap visualization
