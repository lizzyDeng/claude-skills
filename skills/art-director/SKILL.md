---
name: art-director
description: Use when starting a new project that needs complex pages with REAL image assets — generates a styled HTML page (via frontend-design) plus real raster assets (complex background images + transparent-channel cutouts) from the APImart gpt-image API, and wires the assets into the page. Triggers include "出 design + 切图", "复杂背景图", "透明素材/抠图", "生成页面素材", "art director", "new project needs hero bg + cutouts".
---

# Art Director

Orchestrates the existing `frontend-design` skill to produce an HTML page, then generates the real raster image assets that page needs — complex background images and transparent-channel cutouts — via the APImart gpt-image API, and wires the generated files back into the page. Produces a runnable HTML/CSS page whose visuals are real generated art, not CSS-faked gradients.

## When to use

Starting a new project (or a new page) that needs **real image assets**: a complex full-bleed background (`bg`, opaque, up to 4K) and/or transparent-channel cutouts (`cutout`: characters, objects, decorative elements, real alpha PNG). If you only need layout/code with CSS-faked atmosphere, use `frontend-design` directly — this skill is for when you need actual generated raster art.

## Prerequisites

- `export APIMART_API_KEY=...` (APImart bearer token). Optional: `ART_DIRECTOR_BG_RESOLUTION` (default `2k`).
- The `frontend-design` skill must be available.
- Python 3.

## Invocation convention (IMPORTANT)

All commands run the skill's CLI by its **absolute base directory** — the path printed in the `Base directory for this skill:` line at the top of this skill's load message. Substitute that real absolute path for `<SKILL_BASE>`:

```
python3 "<SKILL_BASE>/cli.py" <subcommand> ...
```

**Replace `<SKILL_BASE>` with the actual absolute path** (e.g. `python3 "/Users/you/.claude/plugins/.../skills/art-director/cli.py" validate ...`). Do NOT run the command with the literal text `<SKILL_BASE>`, and do NOT use `python3 cli.py` (a cwd-relative path fails — the skill runs in the user's project dir, not here).

## The 4-stage pipeline

### Stage 1 — design (frontend-design + asset convention)

Invoke `frontend-design` to produce the page as **HTML/CSS** (v1 targets a single `index.html`). Inject this **hard asset convention** into its task:

- Wherever the design calls for a real raster image (a complex background, a character/object/decorative cutout), do **not** fake it with CSS gradients/noise. Instead reference a generated asset by a literal path under `assets/gen/`:
  - background → `background-image:url(assets/gen/<id>.png)`
  - cutout/object → `<img src="assets/gen/<id>.png">`
- Use **only** these literal forms. Do **not** wire generated assets via `srcset`, `<picture>`/`<source>`, CSS `image-set()`, Tailwind `bg-[url(...)]`, or React `src={...}` — the reconcile gate hard-fails on an asset wired through those.
- Asset paths must contain **no spaces**.
- For every generated asset, append one entry to `.art-director/manifest.json`:
  ```json
  {"id":"hero-bg","kind":"bg","prompt":"<vivid generation prompt>","aspect":"16:9","transparent":false,
   "path":"assets/gen/hero-bg.png","placeholder":"url(assets/gen/hero-bg.png)"}
  ```
  - `bg` → `kind:"bg"`, `transparent:false`; aspect = any of the 15 ratios; default resolution `2k`, set `"resolution":"4k"` on a hero you want at 4K.
  - cutout → `kind:"cutout"`, `transparent:true`; aspect must be `1:1` / `2:3` / `3:2` (capped at 1536×1024, PNG only).
  - 🔴 **`placeholder` must be the exact string you wrote into the code** (same case, same quotes). Reconcile checks the page by literal containment of this string — if it differs by a character, the gate fails.

### Stage 2 — reconcile

```
python3 "<SKILL_BASE>/cli.py" validate --manifest .art-director/manifest.json --code index.html
```

Must print `VALIDATE: PASS`. Check the `[cost]` estimate line; exceeding `max_assets` (12) is rejected as a cost guard. On `FAIL`/unsupported-markup, return to Stage 1.

### Stage 3 — generate

```
python3 "<SKILL_BASE>/cli.py" gen --manifest .art-director/manifest.json --project-dir . --page index.html
```

Submits each asset to APImart (async: submit → poll → download/decode), validates each download (real PNG; cutouts must carry an alpha channel), and persists per-asset status + task_id atomically as it goes. On partial failure the page is auto-degraded (a fallback background so it never white-screens) and you can rerun to resume only the failed assets (already-paid tasks are reused, not re-billed).

### Stage 4 — wiring gate

```
python3 "<SKILL_BASE>/cli.py" gate --manifest .art-director/manifest.json --project-dir . --code index.html
```

Must print `GATE: PASS` (every asset file exists, is a valid PNG, cutouts have alpha, and each placeholder is actually wired in the code).

## 🔴 Iteration cap (avoid infinite loops)

If Stage 2 fails or raises unsupported-markup, return to Stage 1 and ask `frontend-design` to use the literal `url()`/`src` forms — **at most 2 times**. If it still won't comply, deterministically rewrite the asset-bearing constructs into literal `url(assets/gen/<id>.png)` / `src="assets/gen/<id>.png"` yourself. Do not bounce back to `frontend-design` indefinitely.

## Output

- `index.html` — the runnable page, assets wired in.
- `assets/gen/*.png` — the generated background(s) and transparent cutout(s).
- `.art-director/manifest.json` — per-asset status + task_id.
- `.art-director/run.log` — submit/poll/result/cost trace (api key redacted).

## Red lines

- If Stage 4 (`gate`) is not `PASS`, do **not** claim the page is ready.
- Report every failed asset by id; never silently drop one.
- If the cost estimate is high or `max_assets` is exceeded, tell the user before generating.

## Knobs

- Global default bg resolution: `--bg-resolution 1k|2k|4k` (or `ART_DIRECTOR_BG_RESOLUTION`); per-asset override via manifest `resolution`.
- Cutouts have no resolution tier — only size (capped at 1536×1024), PNG only, no 4K (model limitation).
