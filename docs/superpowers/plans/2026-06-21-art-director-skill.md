# Art Director Skill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 claude-skills 建一个 `art-director` skill — 按风格 brief 编排 frontend-design 出页面,用 APImart 的 gpt-image 模型生成真实 bg 大图 + 透明切图,并回填进页面代码,产出能跑的 HTML/CSS 页面。

**Architecture:** 一个**编排 skill**(SKILL.md 驱动 agent)+ 一个 **Python 素材生产引擎**(scripts/)。引擎按 `kind→model` 注册表派发(bg→gpt-image-2 / cutout→gpt-image-1.5-official),通过 APImart 异步接口(提交→轮询→下载)生成素材;一份 **asset manifest** 是贯穿全流程的契约,`extractor`/`wiring` 两道门禁对账代码↔manifest↔磁盘。设计参考:spec `docs/superpowers/specs/2026-06-21-art-director-design-skill.md`。

**Tech Stack:** Python 3(stdlib only — `urllib`/`json`/`concurrent.futures`,零三方依赖,便于 skill 运行时免安装)、pytest、markdown(SKILL.md)。HTTP 走可注入的 `Transport` 接口,单测注入 fake,E2E 用本地 `http.server` mock APImart。

---

## File Structure（先锁分解,再定任务）

技能根:`skills/art-director/`

| 文件 | 职责(单一) |
|---|---|
| `scripts/config.py` | `Config` dataclass:api_key/base_url/默认 bg 分辨率/并发/重试/轮询超时/asset_dir。从 env 读 `APIMART_API_KEY`。 |
| `scripts/manifest.py` | `Asset` / `Manifest` dataclass + `load()` + `validate()`(纯数据契约,无网络) |
| `scripts/registry.py` | `kind→handler` 注册表,`build_request(asset, cfg)→dict`(APImart 请求体);分辨率/透明校验落在 handler |
| `scripts/transport.py` | `Transport` 协议(post/get/get_bytes)+ 默认 `UrllibTransport`(stdlib) |
| `scripts/apimart.py` | `ApimartClient`:`submit()`/`poll()`/`download()`,退避;只懂 APImart 协议,不懂 manifest |
| `scripts/pngutil.py` | `png_has_alpha(path)→bool`(解析 PNG IHDR color type,零依赖) |
| `scripts/engine.py` | `generate(manifest, project_dir, client, cfg)`:并发、重试、逐素材落盘、resume、per-asset status |
| `scripts/extractor.py` | `extract_refs(code)` + `reconcile(refs, manifest)`(代码↔manifest 双向对账) |
| `scripts/wiring.py` | `verify(manifest, project_dir)→list[Problem]`(磁盘存在+非空+cutout 有 alpha+被引用) |
| `scripts/cli.py` | CLI 入口:`validate`/`gen`/`gate` 子命令 |
| `tests/conftest.py` | 把 `scripts/` 加进 sys.path |
| `tests/test_*.py` | 各模块单测 + `test_e2e_mock.py` 全链路 mock |
| `SKILL.md` | 编排说明(4 阶段流水线 + 注入 frontend-design 的素材约定 + 命令序列) |
| `INSTALL.md` | 安装/依赖/env 说明 |

**Canonical names(全任务统一,改名即 bug):**
- `Asset`: `id, kind, prompt, aspect, resolution(opt), transparent, format(opt), path, placeholder, status="pending"`
- `Manifest`: `version, style(dict), assets(list[Asset])`
- `registry.build_request(asset, cfg) -> dict`
- `ApimartClient(transport, api_key, base_url).submit(body)->str` / `.poll(task_id, timeout, interval)->dict` / `.download(url, dest)->None`
- `engine.generate(manifest, project_dir, client, cfg) -> Manifest`
- `extractor.extract_refs(code, asset_dir="assets/gen") -> set[str]` / `reconcile(refs, manifest) -> ReconcileResult`
- `wiring.verify(manifest, project_dir) -> list[str]`

---

## Task 1: Config 与技能骨架

**Files:**
- Create: `skills/art-director/scripts/config.py`
- Create: `skills/art-director/tests/conftest.py`
- Test: `skills/art-director/tests/test_config.py`

- [ ] **Step 1: conftest 让 scripts 可导入**

```python
# skills/art-director/tests/conftest.py
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))
```

- [ ] **Step 2: Write the failing test**

```python
# skills/art-director/tests/test_config.py
import os, pytest
from config import Config

def test_from_env_reads_api_key(monkeypatch):
    monkeypatch.setenv("APIMART_API_KEY", "sk-test")
    cfg = Config.from_env()
    assert cfg.api_key == "sk-test"
    assert cfg.base_url == "https://api.apimart.ai/v1"
    assert cfg.default_bg_resolution == "2k"
    assert cfg.asset_dir == "assets/gen"

def test_from_env_missing_key_raises(monkeypatch):
    monkeypatch.delenv("APIMART_API_KEY", raising=False)
    with pytest.raises(RuntimeError) as e:
        Config.from_env()
    assert "APIMART_API_KEY" in str(e.value)
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd skills/art-director && python -m pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'config'`

- [ ] **Step 4: Write minimal implementation**

```python
# skills/art-director/scripts/config.py
import os
from dataclasses import dataclass

@dataclass
class Config:
    api_key: str
    base_url: str = "https://api.apimart.ai/v1"
    default_bg_resolution: str = "2k"
    concurrency: int = 4
    retries: int = 3
    poll_timeout: int = 180      # seconds
    poll_interval: int = 3       # seconds
    asset_dir: str = "assets/gen"

    @classmethod
    def from_env(cls) -> "Config":
        key = os.environ.get("APIMART_API_KEY")
        if not key:
            raise RuntimeError(
                "APIMART_API_KEY not set. export APIMART_API_KEY=sk-... (APImart bearer token)."
            )
        bg = os.environ.get("ART_DIRECTOR_BG_RESOLUTION", "2k")
        return cls(api_key=key, default_bg_resolution=bg)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd skills/art-director && python -m pytest tests/test_config.py -v`
Expected: PASS (2 passed)

- [ ] **Step 6: Commit**

```bash
git add skills/art-director/scripts/config.py skills/art-director/tests/conftest.py skills/art-director/tests/test_config.py
git commit -m "feat(art-director): Config + 测试骨架"
```

---

## Task 2: Manifest 数据契约

**Files:**
- Create: `skills/art-director/scripts/manifest.py`
- Test: `skills/art-director/tests/test_manifest.py`

- [ ] **Step 1: Write the failing test**

```python
# skills/art-director/tests/test_manifest.py
import json, pytest
from manifest import Asset, Manifest

BG = {"id":"hero-bg","kind":"bg","prompt":"neon alley","aspect":"16:9",
      "resolution":"4k","transparent":False,"path":"assets/gen/hero-bg.png",
      "placeholder":"url(assets/gen/hero-bg.png)"}
CUT = {"id":"mascot","kind":"cutout","prompt":"fox","aspect":"2:3",
       "transparent":True,"format":"png","path":"assets/gen/mascot.png",
       "placeholder":'src="assets/gen/mascot.png"'}

def test_load_roundtrip(tmp_path):
    p = tmp_path/"m.json"
    p.write_text(json.dumps({"version":1,"style":{"brief":"x"},"assets":[BG,CUT]}))
    m = Manifest.load(str(p))
    assert m.version == 1
    assert [a.id for a in m.assets] == ["hero-bg","mascot"]
    assert m.assets[0].status == "pending"      # default
    assert m.assets[1].transparent is True

def test_validate_ok():
    m = Manifest(version=1, style={"brief":"x"}, assets=[Asset(**BG), Asset(**CUT)])
    assert m.validate() == []

def test_validate_duplicate_id():
    m = Manifest(version=1, style={}, assets=[Asset(**BG), Asset(**{**CUT,"id":"hero-bg"})])
    errs = m.validate()
    assert any("duplicate" in e for e in errs)

def test_validate_bad_kind():
    m = Manifest(version=1, style={}, assets=[Asset(**{**BG,"kind":"video"})])
    errs = m.validate()
    assert any("kind" in e for e in errs)

def test_validate_cutout_must_be_transparent():
    m = Manifest(version=1, style={}, assets=[Asset(**{**CUT,"transparent":False})])
    errs = m.validate()
    assert any("transparent" in e for e in errs)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd skills/art-director && python -m pytest tests/test_manifest.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'manifest'`

- [ ] **Step 3: Write minimal implementation**

```python
# skills/art-director/scripts/manifest.py
import json
from dataclasses import dataclass, field, asdict
from typing import Optional

VALID_KINDS = {"bg", "cutout"}

@dataclass
class Asset:
    id: str
    kind: str
    prompt: str
    aspect: str
    path: str
    placeholder: str
    transparent: bool = False
    resolution: Optional[str] = None
    format: Optional[str] = None
    status: str = "pending"          # pending | done | failed

    def to_dict(self) -> dict:
        return {k: v for k, v in asdict(self).items() if v is not None}

@dataclass
class Manifest:
    version: int
    style: dict
    assets: list

    @classmethod
    def load(cls, path: str) -> "Manifest":
        data = json.loads(open(path, encoding="utf-8").read())
        assets = [Asset(**a) for a in data.get("assets", [])]
        return cls(version=data.get("version", 1), style=data.get("style", {}), assets=assets)

    def save(self, path: str) -> None:
        out = {"version": self.version, "style": self.style,
               "assets": [a.to_dict() for a in self.assets]}
        open(path, "w", encoding="utf-8").write(json.dumps(out, ensure_ascii=False, indent=2))

    def validate(self) -> list:
        errs = []
        seen = set()
        for a in self.assets:
            if a.id in seen:
                errs.append(f"duplicate asset id: {a.id}")
            seen.add(a.id)
            if a.kind not in VALID_KINDS:
                errs.append(f"asset {a.id}: invalid kind {a.kind!r} (must be bg|cutout)")
            if a.kind == "cutout" and not a.transparent:
                errs.append(f"asset {a.id}: cutout must be transparent=true")
            if not a.path or not a.placeholder:
                errs.append(f"asset {a.id}: path and placeholder are required")
        return errs
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd skills/art-director && python -m pytest tests/test_manifest.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add skills/art-director/scripts/manifest.py skills/art-director/tests/test_manifest.py
git commit -m "feat(art-director): asset manifest 数据契约 + 校验"
```

---

## Task 3: 模型派发注册表（kind→model，无 if-else）

**Files:**
- Create: `skills/art-director/scripts/registry.py`
- Test: `skills/art-director/tests/test_registry.py`

- [ ] **Step 1: Write the failing test**

```python
# skills/art-director/tests/test_registry.py
import pytest
from config import Config
from manifest import Asset
from registry import build_request

CFG = Config(api_key="k")

def _bg(**kw): return Asset(id="b",kind="bg",prompt="p",aspect="16:9",
    path="assets/gen/b.png",placeholder="url(assets/gen/b.png)", **kw)
def _cut(**kw): return Asset(id="c",kind="cutout",prompt="p",aspect="2:3",transparent=True,
    format="png",path="assets/gen/c.png",placeholder='src="assets/gen/c.png"', **kw)

def test_bg_routes_to_gpt_image_2():
    body = build_request(_bg(), CFG)
    assert body["model"] == "gpt-image-2"
    assert body["size"] == "16:9"
    assert body["resolution"] == "2k"           # default from cfg
    assert "background" not in body             # gpt-image-2 不支持

def test_bg_per_asset_resolution_override():
    body = build_request(_bg(resolution="4k"), CFG)
    assert body["resolution"] == "4k"

def test_bg_bad_resolution_rejected():
    with pytest.raises(ValueError):
        build_request(_bg(resolution="8k"), CFG)

def test_cutout_routes_to_1_5_official_transparent():
    body = build_request(_cut(), CFG)
    assert body["model"] == "gpt-image-1.5-official"
    assert body["background"] == "transparent"
    assert body["output_format"] == "png"
    assert body["size"] == "2:3"

def test_cutout_resolution_is_rejected():
    with pytest.raises(ValueError):
        build_request(_cut(resolution="2k"), CFG)

def test_cutout_bad_size_rejected():
    with pytest.raises(ValueError):
        build_request(_cut(aspect="16:9"), CFG)   # cutout 仅 1:1/2:3/3:2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd skills/art-director && python -m pytest tests/test_registry.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'registry'`

- [ ] **Step 3: Write minimal implementation**

```python
# skills/art-director/scripts/registry.py
from manifest import Asset
from config import Config

BG_RESOLUTIONS = {"1k", "2k", "4k"}
BG_SIZES = {"1:1","3:2","2:3","4:3","3:4","5:4","4:5","16:9","9:16",
            "2:1","1:2","3:1","1:3","21:9","9:21","auto"}
CUTOUT_SIZES = {"1:1", "2:3", "3:2"}

def build_bg_request(asset: Asset, cfg: Config) -> dict:
    if asset.aspect not in BG_SIZES:
        raise ValueError(f"{asset.id}: bg aspect {asset.aspect!r} not in {sorted(BG_SIZES)}")
    res = asset.resolution or cfg.default_bg_resolution
    if res not in BG_RESOLUTIONS:
        raise ValueError(f"{asset.id}: bg resolution {res!r} must be one of {sorted(BG_RESOLUTIONS)}")
    return {"model": "gpt-image-2", "prompt": asset.prompt,
            "size": asset.aspect, "resolution": res, "n": 1}

def build_cutout_request(asset: Asset, cfg: Config) -> dict:
    if asset.resolution is not None:
        raise ValueError(f"{asset.id}: cutout has no resolution tier (remove 'resolution')")
    if asset.aspect not in CUTOUT_SIZES:
        raise ValueError(f"{asset.id}: cutout aspect {asset.aspect!r} must be one of {sorted(CUTOUT_SIZES)}")
    return {"model": "gpt-image-1.5-official", "prompt": asset.prompt,
            "size": asset.aspect, "background": "transparent",
            "output_format": "png", "n": 1}

HANDLERS = {"bg": build_bg_request, "cutout": build_cutout_request}

def build_request(asset: Asset, cfg: Config) -> dict:
    handler = HANDLERS.get(asset.kind)
    if handler is None:
        raise ValueError(f"{asset.id}: no handler for kind {asset.kind!r}")
    return handler(asset, cfg)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd skills/art-director && python -m pytest tests/test_registry.py -v`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**

```bash
git add skills/art-director/scripts/registry.py skills/art-director/tests/test_registry.py
git commit -m "feat(art-director): kind→model 注册表派发 + 参数校验"
```

---

## Task 4: Transport 接口 + APImart 客户端

**Files:**
- Create: `skills/art-director/scripts/transport.py`
- Create: `skills/art-director/scripts/apimart.py`
- Test: `skills/art-director/tests/test_apimart.py`

- [ ] **Step 1: Write the failing test**

```python
# skills/art-director/tests/test_apimart.py
import pytest
from apimart import ApimartClient

class FakeTransport:
    """记录调用,按脚本返回。poll 序列模拟 running→done。"""
    def __init__(self, poll_seq):
        self.poll_seq = list(poll_seq)
        self.posts = []
        self.downloads = []
    def post(self, url, headers, body):
        self.posts.append((url, headers, body))
        return 200, {"task_id": "T1"}
    def get(self, url, headers):
        return 200, self.poll_seq.pop(0)
    def get_bytes(self, url):
        self.downloads.append(url)
        return b"PNGBYTES"

def test_submit_returns_task_id():
    t = FakeTransport([])
    c = ApimartClient(t, api_key="k", base_url="https://api.apimart.ai/v1")
    tid = c.submit({"model": "gpt-image-2", "prompt": "x"})
    assert tid == "T1"
    url, headers, body = t.posts[0]
    assert url.endswith("/images/generations")
    assert headers["Authorization"] == "Bearer k"
    assert body["model"] == "gpt-image-2"

def test_poll_waits_until_done():
    seq = [{"status":"running","progress":10},
           {"status":"running","progress":60},
           {"status":"succeeded","url":"https://cdn/x.png"}]
    t = FakeTransport(seq)
    c = ApimartClient(t, api_key="k", base_url="https://api.apimart.ai/v1")
    res = c.poll("T1", timeout=10, interval=0)   # interval 0 → 不真 sleep
    assert res["url"] == "https://cdn/x.png"

def test_poll_failure_raises():
    t = FakeTransport([{"status":"failed","error":"nsfw"}])
    c = ApimartClient(t, api_key="k", base_url="https://api.apimart.ai/v1")
    with pytest.raises(RuntimeError) as e:
        c.poll("T1", timeout=10, interval=0)
    assert "failed" in str(e.value).lower()

def test_poll_timeout_raises():
    t = FakeTransport([{"status":"running"}]*100)
    c = ApimartClient(t, api_key="k", base_url="https://api.apimart.ai/v1")
    with pytest.raises(TimeoutError):
        c.poll("T1", timeout=0, interval=0)      # timeout 0 → 立即超时

def test_download_writes_bytes(tmp_path):
    t = FakeTransport([])
    c = ApimartClient(t, api_key="k", base_url="https://api.apimart.ai/v1")
    dest = tmp_path/"a.png"
    c.download("https://cdn/x.png", str(dest))
    assert dest.read_bytes() == b"PNGBYTES"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd skills/art-director && python -m pytest tests/test_apimart.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'apimart'`

- [ ] **Step 3: Write minimal implementation**

```python
# skills/art-director/scripts/transport.py
import json, urllib.request

class UrllibTransport:
    """默认 stdlib HTTP。post/get 返回 (status, json_dict);get_bytes 返回原始字节。"""
    def post(self, url, headers, body):
        data = json.dumps(body).encode()
        req = urllib.request.Request(url, data=data, headers={**headers, "Content-Type":"application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode())
    def get(self, url, headers):
        req = urllib.request.Request(url, headers=headers, method="GET")
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode())
    def get_bytes(self, url):
        with urllib.request.urlopen(url, timeout=120) as r:
            return r.read()
```

```python
# skills/art-director/scripts/apimart.py
import time

# 终态判定:成功/失败的可能字段值(APImart 文档措辞可能演化,宽松匹配)
DONE_OK = {"succeeded", "success", "completed", "done", "finished"}
DONE_FAIL = {"failed", "error", "canceled", "cancelled"}

def _image_url(result: dict):
    if result.get("url"):
        return result["url"]
    # 兼容 list 形态: {"images":[{"url":..}]} 或 {"data":[{"url":..}]}
    for key in ("images", "data", "results"):
        seq = result.get(key)
        if isinstance(seq, list) and seq and isinstance(seq[0], dict) and seq[0].get("url"):
            return seq[0]["url"]
    return None

class ApimartClient:
    def __init__(self, transport, api_key: str, base_url: str):
        self.t = transport
        self.base = base_url.rstrip("/")
        self.headers = {"Authorization": f"Bearer {api_key}"}

    def submit(self, body: dict) -> str:
        status, data = self.t.post(f"{self.base}/images/generations", self.headers, body)
        if status >= 400 or "task_id" not in data:
            raise RuntimeError(f"submit failed: HTTP {status} {data}")
        return data["task_id"]

    def poll(self, task_id: str, timeout: int, interval: int) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            status, data = self.t.get(f"{self.base}/tasks/{task_id}", self.headers)
            st = str(data.get("status", "")).lower()
            if st in DONE_FAIL:
                raise RuntimeError(f"task {task_id} failed: {data.get('error', data)}")
            if st in DONE_OK or _image_url(data):
                if not _image_url(data):
                    raise RuntimeError(f"task {task_id} done but no image url: {data}")
                return data
            if time.monotonic() >= deadline:
                raise TimeoutError(f"task {task_id} not done within {timeout}s (last status={st!r})")
            if interval:
                time.sleep(interval)

    def download(self, url: str, dest: str) -> None:
        data = self.t.get_bytes(url)
        with open(dest, "wb") as f:
            f.write(data)

    def image_url(self, result: dict):
        return _image_url(result)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd skills/art-director && python -m pytest tests/test_apimart.py -v`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add skills/art-director/scripts/transport.py skills/art-director/scripts/apimart.py skills/art-director/tests/test_apimart.py
git commit -m "feat(art-director): Transport 接口 + APImart 异步客户端(submit/poll/download)"
```

---

## Task 5: PNG alpha 校验（零依赖）

**Files:**
- Create: `skills/art-director/scripts/pngutil.py`
- Test: `skills/art-director/tests/test_pngutil.py`

PNG 结构:8 字节签名 + IHDR chunk(长度 4 + "IHDR" 4 + 数据 13)。IHDR 数据第 9 字节(整文件偏移 25)是 color type:`4`=灰度+alpha,`6`=真彩+alpha,`3`=调色板(可能有 tRNS),`0`/`2`=无 alpha。

- [ ] **Step 1: Write the failing test**

```python
# skills/art-director/tests/test_pngutil.py
import struct, pytest
from pngutil import png_has_alpha

SIG = b"\x89PNG\r\n\x1a\n"

def _png(color_type: int) -> bytes:
    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, color_type, 0, 0, 0)  # w,h,bitdepth,colortype,...
    chunk = struct.pack(">I", 13) + b"IHDR" + ihdr + b"\x00\x00\x00\x00"
    return SIG + chunk

def test_truecolor_alpha_is_true(tmp_path):
    p = tmp_path/"a.png"; p.write_bytes(_png(6))
    assert png_has_alpha(str(p)) is True

def test_grayscale_alpha_is_true(tmp_path):
    p = tmp_path/"a.png"; p.write_bytes(_png(4))
    assert png_has_alpha(str(p)) is True

def test_truecolor_no_alpha_is_false(tmp_path):
    p = tmp_path/"a.png"; p.write_bytes(_png(2))
    assert png_has_alpha(str(p)) is False

def test_not_a_png_is_false(tmp_path):
    p = tmp_path/"a.png"; p.write_bytes(b"JFIF not png")
    assert png_has_alpha(str(p)) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd skills/art-director && python -m pytest tests/test_pngutil.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'pngutil'`

- [ ] **Step 3: Write minimal implementation**

```python
# skills/art-director/scripts/pngutil.py
PNG_SIG = b"\x89PNG\r\n\x1a\n"
ALPHA_COLOR_TYPES = {4, 6}    # 4=gray+alpha, 6=truecolor+alpha
PALETTE = 3

def png_has_alpha(path: str) -> bool:
    with open(path, "rb") as f:
        head = f.read(33)
    if not head.startswith(PNG_SIG):
        return False
    if head[12:16] != b"IHDR":
        return False
    color_type = head[25]
    if color_type in ALPHA_COLOR_TYPES:
        return True
    if color_type == PALETTE:
        # 调色板:需扫 tRNS chunk 才算透明。保守起见——cutout 我们强制 truecolor+alpha,
        # 调色板透明少见,这里返回 False 触发门禁让人工确认。
        return False
    return False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd skills/art-director && python -m pytest tests/test_pngutil.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add skills/art-director/scripts/pngutil.py skills/art-director/tests/test_pngutil.py
git commit -m "feat(art-director): PNG alpha 通道校验(解析 IHDR,零依赖)"
```

---

## Task 6: 生成引擎（并发 + 重试 + 逐素材落盘 + resume）

**Files:**
- Create: `skills/art-director/scripts/engine.py`
- Test: `skills/art-director/tests/test_engine.py`

- [ ] **Step 1: Write the failing test**

```python
# skills/art-director/tests/test_engine.py
import json, pytest
from config import Config
from manifest import Asset, Manifest
from apimart import ApimartClient
from engine import generate

CFG = Config(api_key="k", concurrency=2, retries=2, poll_timeout=10, poll_interval=0)

def _man():
    return Manifest(version=1, style={}, assets=[
        Asset(id="b",kind="bg",prompt="p",aspect="16:9",
              path="assets/gen/b.png",placeholder="url(assets/gen/b.png)"),
        Asset(id="c",kind="cutout",prompt="p",aspect="2:3",transparent=True,format="png",
              path="assets/gen/c.png",placeholder='src="assets/gen/c.png"'),
    ])

class ScriptedTransport:
    """submit→task,poll 立即 succeeded,get_bytes 返回固定字节。可设 fail_ids 让某素材失败。"""
    def __init__(self, fail_ids=()):
        self.fail_ids = set(fail_ids); self.posts = 0; self.bodies = []
    def post(self, url, headers, body):
        self.posts += 1; self.bodies.append(body)
        return 200, {"task_id": f"T{self.posts}"}
    def get(self, url, headers):
        # task_id 编码进 url 末尾;用 bodies 顺序判断 fail
        return 200, {"status":"succeeded","url":"https://cdn/x.png"}
    def get_bytes(self, url):
        return b"\x89PNG\r\n\x1a\n" + b"\x00"*60

def test_generate_writes_all_and_marks_done(tmp_path):
    t = ScriptedTransport()
    c = ApimartClient(t, api_key="k", base_url="https://api.apimart.ai/v1")
    m = generate(_man(), str(tmp_path), c, CFG)
    assert (tmp_path/"assets/gen/b.png").exists()
    assert (tmp_path/"assets/gen/c.png").exists()
    assert all(a.status == "done" for a in m.assets)

def test_generate_resume_skips_done(tmp_path):
    # 预置一个已完成素材的文件 + manifest status=done
    (tmp_path/"assets/gen").mkdir(parents=True)
    (tmp_path/"assets/gen/b.png").write_bytes(b"x")
    m = _man(); m.assets[0].status = "done"
    t = ScriptedTransport()
    c = ApimartClient(t, api_key="k", base_url="https://api.apimart.ai/v1")
    generate(m, str(tmp_path), c, CFG)
    assert t.posts == 1     # 只为未完成的 cutout 提交一次,bg 被 resume 跳过

def test_generate_failed_asset_does_not_block_others(tmp_path, monkeypatch):
    import engine
    calls = {"n": 0}
    def flaky_submit(self, body):
        calls["n"] += 1
        if body["model"] == "gpt-image-2":
            raise RuntimeError("boom")   # bg 每次都失败
        return "T"
    monkeypatch.setattr(ApimartClient, "submit", flaky_submit)
    monkeypatch.setattr(ApimartClient, "poll", lambda self,*a,**k: {"url":"https://cdn/x.png"})
    monkeypatch.setattr(ApimartClient, "download",
        lambda self,u,d: open(d,"wb").write(b"\x89PNG\r\n\x1a\n"+b"\x00"*60))
    t = ScriptedTransport()
    c = ApimartClient(t, api_key="k", base_url="https://api.apimart.ai/v1")
    m = generate(_man(), str(tmp_path), c, CFG)
    by_id = {a.id: a.status for a in m.assets}
    assert by_id["b"] == "failed"     # bg retries 用尽 → failed
    assert by_id["c"] == "done"       # cutout 不受影响
    assert calls["n"] == 1 + CFG.retries   # 1 + retries 次尝试
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd skills/art-director && python -m pytest tests/test_engine.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine'`

- [ ] **Step 3: Write minimal implementation**

```python
# skills/art-director/scripts/engine.py
import os, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from registry import build_request

def _abs(project_dir: str, rel: str) -> str:
    return os.path.join(project_dir, rel)

def _gen_one(asset, project_dir, client, cfg) -> str:
    """生成单个素材,成功返回 'done',失败抛异常。带 retries 指数退避。"""
    body = build_request(asset, cfg)          # 校验错误不重试(直接抛)
    last = None
    for attempt in range(cfg.retries + 1):
        try:
            tid = client.submit(body)
            result = client.poll(tid, cfg.poll_timeout, cfg.poll_interval)
            url = client.image_url(result)
            dest = _abs(project_dir, asset.path)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            client.download(url, dest)
            return "done"
        except (RuntimeError, TimeoutError) as e:
            last = e
            if attempt < cfg.retries and cfg.poll_interval:
                time.sleep(min(2 ** attempt, 10))
    raise last

def generate(manifest, project_dir, client, cfg):
    """并发生成所有未完成素材;每个完成即更新 status;resume 跳过已 done 且文件存在的。"""
    todo = []
    for a in manifest.assets:
        dest = _abs(project_dir, a.path)
        if a.status == "done" and os.path.exists(dest):
            continue                          # resume
        todo.append(a)

    with ThreadPoolExecutor(max_workers=cfg.concurrency) as ex:
        futs = {ex.submit(_gen_one, a, project_dir, client, cfg): a for a in todo}
        for fut in as_completed(futs):
            a = futs[fut]
            try:
                a.status = fut.result()
            except Exception:
                a.status = "failed"
    return manifest
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd skills/art-director && python -m pytest tests/test_engine.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add skills/art-director/scripts/engine.py skills/art-director/tests/test_engine.py
git commit -m "feat(art-director): 生成引擎 — 并发/重试/逐素材落盘/resume"
```

---

## Task 7: Extractor（代码↔manifest 双向对账）

**Files:**
- Create: `skills/art-director/scripts/extractor.py`
- Test: `skills/art-director/tests/test_extractor.py`

- [ ] **Step 1: Write the failing test**

```python
# skills/art-director/tests/test_extractor.py
from manifest import Asset, Manifest
from extractor import extract_refs, reconcile

def _man(paths):
    return Manifest(version=1, style={}, assets=[
        Asset(id=f"a{i}",kind="bg",prompt="p",aspect="16:9",
              path=p, placeholder=f"url({p})") for i,p in enumerate(paths)])

CODE = """
<style>.hero{background-image:url(assets/gen/hero-bg.png);}</style>
<img src="assets/gen/mascot.png" alt="m">
<img src="https://external.cdn/logo.svg">
"""

def test_extract_refs_only_assets_gen():
    refs = extract_refs(CODE)
    assert refs == {"assets/gen/hero-bg.png", "assets/gen/mascot.png"}

def test_reconcile_ok():
    m = _man(["assets/gen/hero-bg.png","assets/gen/mascot.png"])
    r = reconcile(extract_refs(CODE), m)
    assert r.ok and not r.missing_in_manifest and not r.unreferenced

def test_reconcile_ref_without_manifest_entry():
    m = _man(["assets/gen/hero-bg.png"])           # 漏登记 mascot
    r = reconcile(extract_refs(CODE), m)
    assert not r.ok
    assert "assets/gen/mascot.png" in r.missing_in_manifest

def test_reconcile_manifest_entry_unreferenced():
    m = _man(["assets/gen/hero-bg.png","assets/gen/mascot.png","assets/gen/ghost.png"])
    r = reconcile(extract_refs(CODE), m)
    assert not r.ok
    assert "assets/gen/ghost.png" in r.unreferenced
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd skills/art-director && python -m pytest tests/test_extractor.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'extractor'`

- [ ] **Step 3: Write minimal implementation**

```python
# skills/art-director/scripts/extractor.py
import re
from dataclasses import dataclass

# 匹配 assets/gen/<file> 出现在 url(...) 或 src="..."/src='...' 或裸路径
_REF = re.compile(r"""(?:url\(\s*['"]?|src\s*=\s*['"]|['"\s(])(assets/gen/[^'"\)\s]+)""")

@dataclass
class ReconcileResult:
    ok: bool
    missing_in_manifest: set     # 代码引用了但 manifest 没有
    unreferenced: set            # manifest 有但代码没引用

def extract_refs(code: str, asset_dir: str = "assets/gen") -> set:
    return {m.group(1) for m in _REF.finditer(code) if m.group(1).startswith(asset_dir)}

def reconcile(refs: set, manifest) -> ReconcileResult:
    manifest_paths = {a.path for a in manifest.assets}
    missing = refs - manifest_paths
    unreferenced = manifest_paths - refs
    return ReconcileResult(ok=not missing and not unreferenced,
                           missing_in_manifest=missing, unreferenced=unreferenced)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd skills/art-director && python -m pytest tests/test_extractor.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add skills/art-director/scripts/extractor.py skills/art-director/tests/test_extractor.py
git commit -m "feat(art-director): extractor — 代码↔manifest 双向对账门禁"
```

---

## Task 8: Wiring Gate（磁盘存在 + alpha + 被引用）

**Files:**
- Create: `skills/art-director/scripts/wiring.py`
- Test: `skills/art-director/tests/test_wiring.py`

- [ ] **Step 1: Write the failing test**

```python
# skills/art-director/tests/test_wiring.py
import struct, pytest
from manifest import Asset, Manifest
from wiring import verify

SIG = b"\x89PNG\r\n\x1a\n"
def _png(ct): 
    ihdr = struct.pack(">IIBBBBB",1,1,8,ct,0,0,0)
    return SIG + struct.pack(">I",13)+b"IHDR"+ihdr+b"\x00\x00\x00\x00"

def _man():
    return Manifest(version=1, style={}, assets=[
        Asset(id="b",kind="bg",prompt="p",aspect="16:9",
              path="assets/gen/b.png",placeholder="url(assets/gen/b.png)",status="done"),
        Asset(id="c",kind="cutout",prompt="p",aspect="2:3",transparent=True,format="png",
              path="assets/gen/c.png",placeholder='src="assets/gen/c.png"',status="done"),
    ])

def _write(tmp, rel, data):
    p = tmp/rel; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(data); return p

def test_verify_all_good(tmp_path):
    _write(tmp_path,"assets/gen/b.png", _png(2))     # bg 无需 alpha
    _write(tmp_path,"assets/gen/c.png", _png(6))     # cutout 有 alpha
    assert verify(_man(), str(tmp_path)) == []

def test_verify_missing_file(tmp_path):
    _write(tmp_path,"assets/gen/c.png", _png(6))     # bg 缺失
    probs = verify(_man(), str(tmp_path))
    assert any("b" in p and "missing" in p for p in probs)

def test_verify_empty_file(tmp_path):
    _write(tmp_path,"assets/gen/b.png", b"")
    _write(tmp_path,"assets/gen/c.png", _png(6))
    probs = verify(_man(), str(tmp_path))
    assert any("b" in p and "empty" in p for p in probs)

def test_verify_cutout_without_alpha(tmp_path):
    _write(tmp_path,"assets/gen/b.png", _png(2))
    _write(tmp_path,"assets/gen/c.png", _png(2))     # cutout 却没 alpha
    probs = verify(_man(), str(tmp_path))
    assert any("c" in p and "alpha" in p for p in probs)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd skills/art-director && python -m pytest tests/test_wiring.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'wiring'`

- [ ] **Step 3: Write minimal implementation**

```python
# skills/art-director/scripts/wiring.py
import os
from pngutil import png_has_alpha

def verify(manifest, project_dir: str) -> list:
    """返回问题字符串列表;空 = 全部就绪可跑。"""
    problems = []
    for a in manifest.assets:
        dest = os.path.join(project_dir, a.path)
        if not os.path.exists(dest):
            problems.append(f"asset {a.id}: file missing at {a.path}")
            continue
        if os.path.getsize(dest) == 0:
            problems.append(f"asset {a.id}: file empty at {a.path}")
            continue
        if a.kind == "cutout" and not png_has_alpha(dest):
            problems.append(f"asset {a.id}: cutout has no alpha channel ({a.path})")
    return problems
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd skills/art-director && python -m pytest tests/test_wiring.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add skills/art-director/scripts/wiring.py skills/art-director/tests/test_wiring.py
git commit -m "feat(art-director): wiring gate — 磁盘/非空/cutout-alpha 终验"
```

---

## Task 9: CLI 入口（validate / gen / gate）

**Files:**
- Create: `skills/art-director/scripts/cli.py`
- Test: `skills/art-director/tests/test_cli.py`

- [ ] **Step 1: Write the failing test**

```python
# skills/art-director/tests/test_cli.py
import json, struct, pytest
from cli import main

SIG=b"\x89PNG\r\n\x1a\n"
def _png(ct):
    ihdr=struct.pack(">IIBBBBB",1,1,8,ct,0,0,0)
    return SIG+struct.pack(">I",13)+b"IHDR"+ihdr+b"\x00\x00\x00\x00"

MAN = {"version":1,"style":{"brief":"x"},"assets":[
    {"id":"b","kind":"bg","prompt":"p","aspect":"16:9",
     "path":"assets/gen/b.png","placeholder":"url(assets/gen/b.png)","status":"done"},
    {"id":"c","kind":"cutout","prompt":"p","aspect":"2:3","transparent":True,"format":"png",
     "path":"assets/gen/c.png","placeholder":'src="assets/gen/c.png"',"status":"done"}]}

def test_validate_reports_manifest_errors(tmp_path, capsys):
    mp = tmp_path/"m.json"
    bad = {**MAN, "assets":[{**MAN["assets"][0],"kind":"video"}]}
    mp.write_text(json.dumps(bad))
    rc = main(["validate","--manifest",str(mp)])
    assert rc != 0
    assert "kind" in capsys.readouterr().out

def test_validate_with_code_reconcile(tmp_path, capsys):
    mp = tmp_path/"m.json"; mp.write_text(json.dumps(MAN))
    code = tmp_path/"index.html"
    code.write_text('<img src="assets/gen/b.png"><div style="background:url(assets/gen/c.png)">')
    rc = main(["validate","--manifest",str(mp),"--code",str(code)])
    assert rc == 0

def test_gate_fails_on_missing_alpha(tmp_path, capsys):
    mp = tmp_path/"m.json"; mp.write_text(json.dumps(MAN))
    (tmp_path/"assets/gen").mkdir(parents=True)
    (tmp_path/"assets/gen/b.png").write_bytes(_png(2))
    (tmp_path/"assets/gen/c.png").write_bytes(_png(2))   # cutout 无 alpha
    rc = main(["gate","--manifest",str(mp),"--project-dir",str(tmp_path)])
    assert rc != 0
    assert "alpha" in capsys.readouterr().out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd skills/art-director && python -m pytest tests/test_cli.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'cli'`

- [ ] **Step 3: Write minimal implementation**

```python
# skills/art-director/scripts/cli.py
import argparse, sys
from config import Config
from manifest import Manifest
from extractor import extract_refs, reconcile
from wiring import verify

def _cmd_validate(args) -> int:
    m = Manifest.load(args.manifest)
    errs = m.validate()
    for e in errs:
        print(f"[manifest] {e}")
    if args.code:
        code = open(args.code, encoding="utf-8").read()
        r = reconcile(extract_refs(code), m)
        for p in sorted(r.missing_in_manifest):
            print(f"[reconcile] code references {p} but no manifest entry")
        for p in sorted(r.unreferenced):
            print(f"[reconcile] manifest entry {p} not referenced in code")
        if not r.ok:
            errs.append("reconcile failed")
    if errs:
        print(f"VALIDATE: FAIL ({len(errs)} problem(s))")
        return 1
    print("VALIDATE: PASS")
    return 0

def _cmd_gen(args) -> int:
    from transport import UrllibTransport
    from apimart import ApimartClient
    from engine import generate
    cfg = Config.from_env()
    if args.bg_resolution:
        cfg.default_bg_resolution = args.bg_resolution
    m = Manifest.load(args.manifest)
    errs = m.validate()
    if errs:
        for e in errs: print(f"[manifest] {e}")
        print("GEN: FAIL (invalid manifest)")
        return 1
    client = ApimartClient(UrllibTransport(), cfg.api_key, cfg.base_url)
    m = generate(m, args.project_dir, client, cfg)
    m.save(args.manifest)                 # 落盘 per-asset status,支持 resume
    failed = [a.id for a in m.assets if a.status != "done"]
    if failed:
        print(f"GEN: PARTIAL — failed: {failed} (rerun to resume)")
        return 1
    print("GEN: PASS (all assets generated)")
    return 0

def _cmd_gate(args) -> int:
    m = Manifest.load(args.manifest)
    probs = verify(m, args.project_dir)
    for p in probs:
        print(f"[gate] {p}")
    if probs:
        print(f"GATE: FAIL ({len(probs)} problem(s))")
        return 1
    print("GATE: PASS")
    return 0

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="art-director")
    sub = ap.add_subparsers(dest="cmd", required=True)

    v = sub.add_parser("validate"); v.add_argument("--manifest", required=True)
    v.add_argument("--code"); v.set_defaults(fn=_cmd_validate)

    g = sub.add_parser("gen"); g.add_argument("--manifest", required=True)
    g.add_argument("--project-dir", required=True); g.add_argument("--bg-resolution")
    g.set_defaults(fn=_cmd_gen)

    t = sub.add_parser("gate"); t.add_argument("--manifest", required=True)
    t.add_argument("--project-dir", required=True); t.set_defaults(fn=_cmd_gate)

    args = ap.parse_args(argv)
    return args.fn(args)

if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd skills/art-director && python -m pytest tests/test_cli.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add skills/art-director/scripts/cli.py skills/art-director/tests/test_cli.py
git commit -m "feat(art-director): CLI — validate/gen/gate 子命令"
```

---

## Task 10: E2E（本地 mock APImart 全链路）

**Files:**
- Create: `skills/art-director/tests/test_e2e_mock.py`
- Create: `skills/art-director/tests/fixtures/index.html`
- Create: `skills/art-director/tests/fixtures/manifest.json`

E2E 主断言(业务结果,非 smoke):跑完 `gen`→所有 manifest path 落盘为真实 PNG、cutout 带 alpha→`gate` PASS、`validate --code` 与 fixture 页面对账 PASS。用本地 `http.server` 起一个 mock APImart(submit 回 task_id、poll 回 succeeded+url、url 下载回真实 PNG 字节)。

- [ ] **Step 1: Write fixtures**

```json
// skills/art-director/tests/fixtures/manifest.json
{
  "version": 1,
  "style": {"brief": "cyberpunk noir test"},
  "assets": [
    {"id":"hero-bg","kind":"bg","prompt":"neon alley","aspect":"16:9","resolution":"2k",
     "transparent":false,"path":"assets/gen/hero-bg.png","placeholder":"url(assets/gen/hero-bg.png)"},
    {"id":"mascot","kind":"cutout","prompt":"fox mascot","aspect":"2:3","transparent":true,
     "format":"png","path":"assets/gen/mascot.png","placeholder":"src=\"assets/gen/mascot.png\""}
  ]
}
```

```html
<!-- skills/art-director/tests/fixtures/index.html -->
<!doctype html><html><head><style>
.hero{background-image:url(assets/gen/hero-bg.png);}
</style></head><body>
<img src="assets/gen/mascot.png" alt="mascot">
</body></html>
```

- [ ] **Step 2: Write the failing E2E test**

```python
# skills/art-director/tests/test_e2e_mock.py
import json, struct, threading, shutil, pathlib, http.server, socketserver
import pytest
from config import Config
from manifest import Manifest
from transport import UrllibTransport
from apimart import ApimartClient
from engine import generate
from wiring import verify
from extractor import extract_refs, reconcile

FIX = pathlib.Path(__file__).parent / "fixtures"
SIG = b"\x89PNG\r\n\x1a\n"
def _png_alpha():
    ihdr = struct.pack(">IIBBBBB",2,2,8,6,0,0,0)   # color type 6 = truecolor+alpha
    return SIG + struct.pack(">I",13)+b"IHDR"+ihdr+b"\x00\x00\x00\x00"

class MockApimart(http.server.BaseHTTPRequestHandler):
    PNG = _png_alpha()
    def log_message(self, *a): pass
    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type","application/json")
        self.send_header("Content-Length",str(len(body))); self.end_headers(); self.wfile.write(body)
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length",0)))
        self._json(200, {"task_id":"T-e2e"})
    def do_GET(self):
        if self.path.endswith(".png"):
            self.send_response(200); self.send_header("Content-Type","image/png")
            self.send_header("Content-Length",str(len(self.PNG))); self.end_headers()
            self.wfile.write(self.PNG)
        else:  # /tasks/T-e2e
            self._json(200, {"status":"succeeded","url":f"http://127.0.0.1:{self.server.server_address[1]}/img.png"})

@pytest.fixture
def mock_server():
    with socketserver.TCPServer(("127.0.0.1",0), MockApimart) as httpd:
        port = httpd.server_address[1]
        th = threading.Thread(target=httpd.serve_forever, daemon=True); th.start()
        yield f"http://127.0.0.1:{port}"
        httpd.shutdown()

def test_full_pipeline(tmp_path, mock_server):
    # 准备工程目录 + manifest + 页面
    shutil.copy(FIX/"index.html", tmp_path/"index.html")
    mpath = tmp_path/"manifest.json"; shutil.copy(FIX/"manifest.json", mpath)

    cfg = Config(api_key="k", base_url=mock_server, poll_interval=0, poll_timeout=10)
    client = ApimartClient(UrllibTransport(), cfg.api_key, cfg.base_url)
    m = Manifest.load(str(mpath))
    assert m.validate() == []

    # 1) 接线对账:fixture 页面引用 == manifest
    code = (tmp_path/"index.html").read_text()
    assert reconcile(extract_refs(code), m).ok

    # 2) 生成
    m = generate(m, str(tmp_path), client, cfg)
    assert all(a.status == "done" for a in m.assets)

    # 3) wiring gate:文件真实存在 + cutout 有 alpha
    assert verify(m, str(tmp_path)) == []
    assert (tmp_path/"assets/gen/hero-bg.png").exists()
    assert (tmp_path/"assets/gen/mascot.png").exists()
```

- [ ] **Step 3: Run test to verify it fails first, then passes**

Run: `cd skills/art-director && python -m pytest tests/test_e2e_mock.py -v`
Expected initially: may FAIL if fixtures missing — create fixtures (Step 1), then PASS (1 passed).

- [ ] **Step 4: Run the whole suite**

Run: `cd skills/art-director && python -m pytest -v`
Expected: ALL PASS (config 2 + manifest 5 + registry 6 + apimart 5 + pngutil 4 + engine 3 + extractor 4 + wiring 4 + cli 3 + e2e 1 = 37 passed)

- [ ] **Step 5: Commit**

```bash
git add skills/art-director/tests/test_e2e_mock.py skills/art-director/tests/fixtures/
git commit -m "test(art-director): E2E 全链路 mock APImart — 生成+对账+wiring gate"
```

---

## Task 11: SKILL.md 编排说明 + INSTALL

**Files:**
- Create: `skills/art-director/SKILL.md`
- Create: `skills/art-director/INSTALL.md`

- [ ] **Step 1: 写 SKILL.md**

内容必须覆盖(prose,非代码):

1. **frontmatter**:`name: art-director` + `description`(触发词:出 design + 切图 + 复杂背景 + 透明素材 + 新工程页面)。
2. **何时用**:起新工程需要复杂页面 + 真实图片素材(bg 大图 / 透明切图)。
3. **前置**:`export APIMART_API_KEY=...`;依赖 `frontend-design` skill 可用;Python3。
4. **4 阶段流水线(agent 按序驱动)**:
   - **Stage 1 design**:invoke `frontend-design` 出页面;**注入素材约定** —— 凡该用真实栅格图处(复杂背景、角色/物体/装饰),不要用 CSS 假造,改为:① 在代码里用约定路径 `assets/gen/<id>.png`(bg 用 `url(...)`,切图用 `<img src>`);② 同步往 `manifest.json` 追加一条 `{id,kind,prompt,aspect,transparent,path,placeholder,(resolution)}`。bg→`kind:"bg"`(aspect 任选 15 种,默认 2k,hero 可写 `"resolution":"4k"`);切图→`kind:"cutout",transparent:true`(aspect 仅 1:1/2:3/3:2,封顶 1536)。
   - **Stage 2 对账门禁**:`python3 scripts/cli.py validate --manifest manifest.json --code index.html` → 必须 PASS(占位符↔manifest 双向一致)。FAIL 则回 Stage 1 修。
   - **Stage 3 生成**:`python3 scripts/cli.py gen --manifest manifest.json --project-dir .` → 异步生成+下载到约定路径;PARTIAL 时重跑 resume。
   - **Stage 4 wiring gate**:`python3 scripts/cli.py gate --manifest manifest.json --project-dir .` → 必须 PASS(文件存在+cutout 有 alpha)。
5. **产物**:能跑的 `index.html` + `assets/gen/*.png` + `manifest.json`(含 status)。
6. **红线**:禁止谎称完成 —— gate 未 PASS 不得声称页面就绪;失败素材如数报告。
7. **分辨率旋钮**:全局 `--bg-resolution`,逐素材 manifest `resolution`;cutout 不可切分辨率。

- [ ] **Step 2: 写 INSTALL.md**

覆盖:env 变量(`APIMART_API_KEY`,可选 `ART_DIRECTOR_BG_RESOLUTION`)、跑测试命令(`cd skills/art-director && python -m pytest`)、frontend-design 依赖说明、零三方依赖(纯 stdlib)。

- [ ] **Step 3: 校验 SKILL.md frontmatter 可被技能系统识别**

Run: `python3 -c "import re,sys; t=open('skills/art-director/SKILL.md').read(); assert t.startswith('---'); assert 'name: art-director' in t; assert 'description:' in t; print('frontmatter OK')"`
Expected: `frontmatter OK`

- [ ] **Step 4: Commit**

```bash
git add skills/art-director/SKILL.md skills/art-director/INSTALL.md
git commit -m "docs(art-director): SKILL.md 编排说明 + INSTALL"
```

---

## Task 12: 全量回归 + 自检

- [ ] **Step 1: 全量测试**

Run: `cd skills/art-director && python -m pytest -v`
Expected: 37 passed, 0 failed

- [ ] **Step 2: 对照 spec 逐条核覆盖**

核对清单(每条指到具体 Task):
- 方案 1 双模型派发 → Task 3
- APImart 异步 submit/poll/download → Task 4
- bg gpt-image-2 / 4K / 15 比例 / 无透明 → Task 3
- cutout gpt-image-1.5-official / transparent PNG / 封顶 1536 → Task 3
- 分辨率两级切换(全局+逐素材),cutout 不可切 → Task 1+3
- manifest 契约 → Task 2
- extractor 双向对账 → Task 7
- wiring gate(磁盘+alpha)→ Task 5(pngutil)+ Task 8
- 并发/重试/逐素材落盘/resume → Task 6
- 错误处理(缺 key/失败不拖垮/部分完成 resume)→ Task 1+6+9
- 编排 SKILL.md(注入 frontend-design 约定 + 命令序列)→ Task 11
- 产物目录(index.html + assets/gen + manifest)→ Task 10+11

- [ ] **Step 3: 最终 commit**

```bash
git add -A
git commit -m "chore(art-director): 全量回归通过,skill 完成"
```

---

## 备注:与 spec 的一致性

- 模型 id 用带后缀 `gpt-image-1.5-official`(APImart 真实取值),bg 用 `gpt-image-2`。
- APImart 全异步:`POST /images/generations`→`task_id`→`GET /tasks/{id}`→图 url→下载。
- 透明只能走 cutout 模型;gpt-image-2 不接 `background` —— 由 registry 结构性保证,wiring gate 二次兜底。
- 纯 stdlib,无 pip 依赖;HTTP 经 `Transport` 接口可注入,单测 fake、E2E 真起本地 mock。
