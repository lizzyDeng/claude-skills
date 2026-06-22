# art-director — install & dev

## Dependencies

Zero third-party Python dependencies — pure stdlib (`urllib`, `concurrent.futures`, `base64`, `json`, `struct`). Python 3.8+ (developed on 3.11). The only consumer dependency is the **`frontend-design`** skill (used in Stage 1).

`pytest` is needed for the test suite only (not at runtime).

## Environment

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `APIMART_API_KEY` | yes (for `gen`) | — | APImart bearer token. `validate`/`gate` are offline and don't need it. |
| `ART_DIRECTOR_BG_RESOLUTION` | no | `2k` | Global default bg resolution (`1k`/`2k`/`4k`). |
| `ART_DIRECTOR_SMOKE` | no | — | Set to `1` (plus a key) to run the real-API smoke test. |

## Run the tests

```bash
cd skills/art-director
python3 -m pytest -q
```

Expected: all green, with `tests/test_smoke_real_api.py::test_real_apimart_bg_and_cutout` **skipped** (it needs a live API key). The negative registry check in that file runs offline.

## Real-API smoke (costs a little money)

```bash
cd skills/art-director
ART_DIRECTOR_SMOKE=1 APIMART_API_KEY=sk-... python3 -m pytest tests/test_smoke_real_api.py -v
```

This is the only test that touches the real APImart contract. Run it once before trusting the request/response assumptions; if it fails, inspect `.art-director/run.log` for the raw submit/poll JSON and reconcile `apimart.py` / `registry.py` against the observed shapes.

## Layout

Modules are flat at the skill root (`config.py`, `paths.py`, `manifest.py`, `registry.py`, `transport.py`, `apimart.py`, `pngutil.py`, `engine.py`, `extractor.py`, `wiring.py`, `cli.py`); tests under `tests/`. `cli.py` self-inserts its real directory (via `realpath`) onto `sys.path`, so it works invoked by absolute path or through a symlink.
