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

## The pipeline (Stage 1 → 2 → optional 1.5 → 3 → 3.5 → 4)

`design → validate (reconcile + cost) →` *optional* `art-direction preview (Stage 1.5, cheap 1k probe) →` `generate (full-res, paid) →` **`asset confirm (Stage 3.5, mandatory human gate) →`** `wiring gate`. Stage 1.5 is a non-blocking cost gate: `validate` *suggests* it when the full-res set is expensive or large; you preview-then-lock the direction before spending on full-res gen. Stage 3.5 is a **blocking human gate**: after the real assets are generated, you show every cutout to the user and get explicit approval before wiring/finishing — the automated gate checks file structure, never whether the artwork is actually right.

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

If `validate` prints a `💡 PREVIEW SUGGESTED:` line (the full-resolution estimate is high or there are many assets — gated by `preview_cost_threshold`/`preview_asset_threshold`), do **Stage 1.5** before spending money on full-res gen. This is a non-blocking hint: `VALIDATE: PASS/FAIL` is unchanged either way.

### Stage 1.5 — art-direction preview (show, don't tell)

**When to run:** when `validate` emits `💡 PREVIEW SUGGESTED`, or any time the asset set is large/expensive and the art direction is still unsettled. Don't burn a full 4K render budget on a hunch — probe the direction cheaply first, let the user *see* it, then commit.

**Mechanism — low-res hero probe:** pick the manifest's **style carrier** (the first `kind:"bg"` asset) and render N different *prompt-style variants* of it, each forced to **1k** resolution (the cheapest tier, $0.02 each). The user looks at the actual images and picks a direction; the chosen style language is written back into every asset's prompt; then the original full-resolution `gen` runs with a locked, agreed direction.

**Driver's job — write N variant prompts for the carrier.** For the style carrier, author N (default 3) *deliberately divergent* full generation prompts that pull the aesthetic in clearly different directions (e.g. painterly oil vs. neon-noir photography vs. flat vector). Each variant is:

```json
[
  {"label":"painterly","prompt":"<full carrier generation prompt in this style>","style_suffix":"<short style phrase reusable on other assets>"},
  {"label":"neon-noir","prompt":"...","style_suffix":"..."},
  {"label":"flat-vector","prompt":"...","style_suffix":"..."}
]
```

- `prompt` is the carrier's *complete* generation prompt for that direction.
- `style_suffix` is a short modifier phrase (e.g. `loose oil-painting brushwork, warm muted palette`) that can be appended to *other* assets so the whole page coheres.
- 🔴 **Fight AI slop:** make the variants genuinely different — different medium, lighting, palette, rendering. Three near-identical "safe" options waste a probe round. Pull them apart so the choice is real.

**Run the probe (cheap, 1k):**

```
python3 "<SKILL_BASE>/cli.py" preview --manifest .art-director/manifest.json --project-dir . --variants-file variants.json
```

Generates one 1k PNG per variant into `.art-director/preview/<carrier_id>__<label>.png`, writes `.art-director/preview/previews.json` (the variant records, for `lock-style`), prints each generated path, and prints the total probe cost. **Open the images and show them to the user** — this is the "show, don't tell" step. Let them pick a `label`.

**Lock the chosen direction back into the manifest:**

```
python3 "<SKILL_BASE>/cli.py" lock-style --manifest .art-director/manifest.json --project-dir . --variant <chosen-label>
```

Sets the carrier asset's prompt to the chosen variant's full prompt, and idempotently appends that variant's `style_suffix` to every *other* asset's prompt (re-running is a no-op). Then re-run `validate` if you like, and proceed to Stage 3 (`gen`) — now at full resolution with a direction the user actually approved.

**🔴 Two honest limits — tell the user both:**

1. The preview locks the **art direction (the prompt language)**, **not the final pixels**. `gpt-image` recomposes on every call, so the final full-resolution hero will differ from the 1k preview image. What you're choosing is the *style*, not the finished artwork.
2. Cutouts have **no cheap resolution tier** ($0.06 is the only/正式 price). The probe therefore uses a **bg carrier** by default. Only probe a cutout when the cutout style is the main open question — and when you do, tell the user this step does **not** save money.

### Stage 3 — generate

```
python3 "<SKILL_BASE>/cli.py" gen --manifest .art-director/manifest.json --project-dir . --page index.html
```

Submits each asset to APImart (async: submit → poll → download/decode), validates each download (real PNG; cutouts must carry an alpha channel), and persists per-asset status + task_id atomically as it goes. On partial failure the page is auto-degraded (a fallback background so it never white-screens) and you can rerun to resume only the failed assets (already-paid tasks are reused, not re-billed).

### Stage 3.5 — asset confirmation (🔴 mandatory human gate)

`gen` printing `GEN: PASS` does **not** mean the page is ready. The automated `gate` (Stage 4) only checks **structure** — file exists, valid PNG, cutout has an alpha channel, placeholder wired. It can **never** judge whether the cutout is the right subject, well-composed, or not visually broken. Only a human can. So before Stage 4:

1. **Show the user every generated asset.** Open each `assets/gen/<id>.png` and present it inline — **every `cutout` is mandatory to show**, and show the `bg`(s) too. Don't describe them in words; the user must *see* the pixels.
2. **Wait for explicit approval.** Do **not** run Stage 4 `gate` and do **not** claim the page is ready until the user says the assets are good.
3. **On rejection — regenerate just that asset:**

   ```
   python3 "<SKILL_BASE>/cli.py" regen --manifest .art-director/manifest.json --project-dir . --asset <id> [--prompt "<adjusted prompt>"]
   ```

   `regen` clears that asset's `task_id` + `status` so it is **freshly generated (re-billed)**, while every other `done` asset is reused (not re-billed). `--prompt` (single `--asset` only) overwrites the prompt first — use it to steer the redo ("make the cat fluffier", "centered, plain background"). You can name multiple `--asset` to redo several at once (without `--prompt`). On failure the **old image is kept** (atomic swap), so a bad regen never white-screens — just rerun. Then **re-show the new asset and loop** until the user approves.

🔴 Do not skip this gate because the assets "look fine from the manifest" or because the user is in a hurry — a wrong cutout wired into a finished page is exactly the failure this gate exists to catch.

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
- `.art-director/preview/*.png` + `previews.json` — Stage 1.5 art-direction probes (1k), if you ran preview.

## Red lines

- If Stage 4 (`gate`) is not `PASS`, do **not** claim the page is ready.
- **Stage 3.5 is mandatory**: after `gen`, every `cutout` must be shown to the user and explicitly approved before you run Stage 4 or claim completion. Wiring an unconfirmed cutout into a finished page = violation. On rejection, `regen` the asset and re-show; loop until approved.
- Report every failed asset by id; never silently drop one.
- If the cost estimate is high or `max_assets` is exceeded, tell the user before generating. When `validate` prints `💡 PREVIEW SUGGESTED`, offer Stage 1.5 before spending on full-res gen.
- When you do run a preview, tell the user the two honest limits (Stage 1.5): the probe locks the **style, not the final pixels**, and probing **cutouts does not save money** (no cheap tier — bg is the default style carrier).

## Knobs

- Global default bg resolution: `--bg-resolution 1k|2k|4k` (or `ART_DIRECTOR_BG_RESOLUTION`); per-asset override via manifest `resolution`.
- Cutouts have no resolution tier — only size (capped at 1536×1024), PNG only, no 4K (model limitation).
- Stage 1.5 preview gate thresholds (env overrides): `ART_DIRECTOR_PREVIEW_COST_THRESHOLD` (default `0.3`), `ART_DIRECTOR_PREVIEW_ASSET_THRESHOLD` (default `6`), `ART_DIRECTOR_PREVIEW_VARIANTS` (default `3`). Probes are always forced to 1k regardless of the global bg resolution.
