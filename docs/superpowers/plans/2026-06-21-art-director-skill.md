# Art Director Skill Implementation Plan (v2 — post grill + codex)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 claude-skills 建一个 `art-director` skill — 按风格 brief 编排 frontend-design 出 HTML 页面,用 APImart 的 gpt-image 模型生成真实 bg 大图 + 透明切图,回填进页面,产出能跑的 HTML/CSS 页面;素材失败时页面仍可加载(降级)。

**Architecture:** 一个**编排 skill**(SKILL.md 驱动 agent)+ 一个 **Python 素材生产引擎**。引擎按 `kind→model` 注册表派发(bg→gpt-image-2 / cutout→gpt-image-1.5-official),走 APImart 异步接口(提交→轮询→下载);一份 **asset manifest** 是贯穿全流程的契约,`extractor`/`wiring` 两道门禁对账代码↔manifest↔磁盘。

**Tech Stack:** Python 3 stdlib(urllib + concurrent.futures),零三方依赖;pytest;HTTP 走可注入 `Transport` 接口,单测注入 fake,E2E 用 `ThreadingHTTPServer` mock,另有 **env-gated 真 API 冒烟**。

---

## v2 修订摘要(吸收两轮评审)

本版相对 v1 的结构性改动(评审编号见括号):
- **模块平铺到 skill 根**,非 `scripts/` 子目录;CLI 用 skill 绝对基目录调用 + `cli.py` 自插 `sys.path`(B1/B2)。
- **恢复 spec §12 的 env-gated 真 API 冒烟测试**(B3),并让 `submit`/`poll`/`image_url` 容忍文档变体响应。
- **extractor 归一化** `./`、`/`、`?query`、`#frag`,并对 srcset/image-set/React 显式硬失败 + SKILL.md 约束 frontend-design(M1);**删除"复用 fastship extractor"虚假声明**(M2)。
- **引擎下载后校验**(PNG 签名 + 非空 + cutout alpha)+ **逐素材原子持久化** task_id/status,resume 先续轮询已有 task(M3/M4,避免重复付费)。
- **降级路径**:失败素材注入 HTML 注释 + CSS 兜底,页面不白屏(M5)。
- **路径越界校验**(codex P1)、**HTTP 错误归一化 + Retry-After**(codex P1)、**成本上限/估算**(m2)、**palette+tRNS alpha**(m3)、**`.art-director/` 布局 + run.log**(m7)、**回归断言 0 failed 而非固定数**(m8)、**reject transparent bg**(P2)、**密钥不入日志**(m6)。

## File Structure（v2:平铺,匹配 fastship 约定）

技能根:`skills/art-director/`。**所有互相 import 的模块平铺在 skill 根**(与 `fastship/orchestrator.py`+`fastship_state.py`+`plan_tree.py` 同构);**禁止放进 `scripts/` 子目录**(B2 的根因)。

| 文件(skill 根) | 职责 |
|---|---|
| `config.py` | `Config`:api_key/base_url/默认 bg 分辨率/并发/重试/退避/轮询/`asset_dir`/`max_assets`/成本上限。env 读 `APIMART_API_KEY`。 |
| `paths.py` | `validate_asset_path(path)`:必须相对、归一化、位于 `assets/gen/` 下、无 `..`、无绝对路径、`.png` 扩展(防越界)。 |
| `manifest.py` | `Asset`/`Manifest` + `load`/`save`(原子 temp+os.replace)+ `validate`;manifest 落 `.art-director/manifest.json`。 |
| `registry.py` | `kind→handler`,`build_request`;bg 拒 `transparent`,cutout 拒 `resolution`;测试取自 APImart 文档请求示例。 |
| `transport.py` | `Transport` 协议 + `UrllibTransport`;把 HTTPError/URLError/429/5xx 归一成 `RetryableError`/`FatalError`,解析 `Retry-After`。 |
| `apimart.py` | `ApimartClient`:`submit`/`poll`/`image_url`/`download`;容忍 `task_id|id|data.task_id`、sync `data[].url`、`b64_json`、`Location`;只在终态成功才取 url。 |
| `pngutil.py` | `png_has_alpha`(扫 chunk 至 IDAT,识别 color type 4/6 与 palette/灰度/truecolor+`tRNS`);`is_png(path)`。 |
| `engine.py` | `generate(...)`:并发、重试(仅 `RetryableError`)、下载后校验、逐素材原子持久化 status+task_id、resume 续轮询、失败不拖垮、`on_progress` 回调。 |
| `extractor.py` | `normalize_ref`/`extract_refs`(归一 `./`,`/`,query,frag;识别 srcset/image-set/React→硬失败)+ `reconcile`。 |
| `wiring.py` | `verify`(磁盘存在+非空+is_png+cutout alpha+被引用)+ `degrade`(失败素材注入注释+CSS 兜底)+ `reset_failed_status`。 |
| `cli.py` | 入口:顶部自插 `sys.path`;子命令 `validate`/`gen`/`gate`;`validate` 含成本估算 + reconcile。 |
| `tests/conftest.py` | 把 skill 根加进 sys.path。 |
| `tests/test_*.py` + `tests/fixtures/` | 单测 + E2E(ThreadingHTTPServer)+ env-gated 真 API 冒烟。 |
| `SKILL.md` / `INSTALL.md` | 编排说明(绝对基目录调用 + frontend-design 约束 + 降级 + 成本)/ 安装。 |

**Canonical names(全任务统一,改名即 bug):**
- `Asset`: `id, kind, prompt, aspect, transparent=False, resolution=None, format=None, path, placeholder, status="pending", task_id=None`
- `Manifest`: `version, style(dict), assets(list[Asset])`;`load(path)` / `save_atomic(path)` / `validate()`
- `paths.validate_asset_path(path) -> None`(违规 raise `ValueError`)
- `registry.build_request(asset, cfg) -> dict`
- `transport.RetryableError` / `transport.FatalError`;`Transport.post/get/get_bytes`
- `ApimartClient(transport, api_key, base_url)`:`.submit(body)->str` / `.poll(task_id, timeout, interval, clock=time)->dict` / `.image_url(result)->str|None` / `.download(url, dest)->None`
- `engine.generate(manifest, project_dir, client, cfg, on_progress=None, clock=time) -> Manifest`
- `engine.validate_download(asset, dest) -> None`(违规 raise)
- `extractor.normalize_ref(ref) -> str` / `extract_refs(code, asset_dir="assets/gen") -> set[str]` / `reconcile(refs, manifest) -> ReconcileResult`
- `wiring.verify(manifest, project_dir, code=None) -> list[str]` / `degrade(manifest, project_dir) -> int` / `reset_failed_status(manifest) -> None`

---

## Task 1: Config（含成本/退避旋钮）+ 测试骨架

**Files:** Create `skills/art-director/config.py`, `skills/art-director/tests/conftest.py`; Test `skills/art-director/tests/test_config.py`

- [ ] **Step 1: conftest 指向 skill 根(平铺)**

```python
# skills/art-director/tests/conftest.py
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))   # skill 根,不是 scripts/
```

- [ ] **Step 2: 失败测试**

```python
# skills/art-director/tests/test_config.py
import pytest
from config import Config

def test_from_env_defaults(monkeypatch):
    monkeypatch.setenv("APIMART_API_KEY", "sk-test")
    c = Config.from_env()
    assert c.api_key == "sk-test"
    assert c.base_url == "https://api.apimart.ai/v1"
    assert c.default_bg_resolution == "2k"
    assert c.asset_dir == "assets/gen"
    assert c.manifest_path == ".art-director/manifest.json"
    assert c.max_assets == 12          # 成本护栏
    assert c.backoff_base == 1.0       # 退避与 poll_interval 解耦

def test_from_env_missing_key(monkeypatch):
    monkeypatch.delenv("APIMART_API_KEY", raising=False)
    with pytest.raises(RuntimeError) as e:
        Config.from_env()
    assert "APIMART_API_KEY" in str(e.value)
```

- [ ] **Step 3: 跑测试验证失败** — `cd skills/art-director && python -m pytest tests/test_config.py -v` → FAIL (no module).

- [ ] **Step 4: 实现**

```python
# skills/art-director/config.py
import os
from dataclasses import dataclass

@dataclass
class Config:
    api_key: str
    base_url: str = "https://api.apimart.ai/v1"
    default_bg_resolution: str = "2k"
    concurrency: int = 4
    retries: int = 3
    backoff_base: float = 1.0          # 退避独立于 poll_interval(m1)
    poll_timeout: int = 180
    poll_interval: int = 3
    asset_dir: str = "assets/gen"
    manifest_path: str = ".art-director/manifest.json"   # spec §11(m7)
    max_assets: int = 12               # 成本护栏(m2)

    @classmethod
    def from_env(cls):
        key = os.environ.get("APIMART_API_KEY")
        if not key:
            raise RuntimeError("APIMART_API_KEY not set. export APIMART_API_KEY=sk-... (APImart bearer token).")
        return cls(api_key=key,
                   default_bg_resolution=os.environ.get("ART_DIRECTOR_BG_RESOLUTION", "2k"))
```

- [ ] **Step 5: 跑测试验证通过** — PASS (2 passed).
- [ ] **Step 6: Commit** — `git add skills/art-director/config.py skills/art-director/tests/conftest.py skills/art-director/tests/test_config.py && git commit -m "feat(art-director): Config(含成本/退避旋钮) + 平铺测试骨架"`

---

## Task 2: 路径越界校验 + Manifest 契约（原子写）

**Files:** Create `skills/art-director/paths.py`, `skills/art-director/manifest.py`; Test `tests/test_paths.py`, `tests/test_manifest.py`

- [ ] **Step 1: 失败测试(paths)**

```python
# skills/art-director/tests/test_paths.py
import pytest
from paths import validate_asset_path

@pytest.mark.parametrize("ok", ["assets/gen/a.png", "assets/gen/sub/b.png"])
def test_valid(ok): validate_asset_path(ok)   # no raise

@pytest.mark.parametrize("bad", [
    "/etc/passwd", "assets/gen/../../x.png", "../x.png",
    "other/dir/a.png", "assets/gen/a.jpg", "assets/gen/a.png\x00"])
def test_invalid(bad):
    with pytest.raises(ValueError): validate_asset_path(bad)
```

- [ ] **Step 2: 跑失败** — FAIL (no module).

- [ ] **Step 3: 实现 paths**

```python
# skills/art-director/paths.py
import os, posixpath

def validate_asset_path(path: str) -> None:
    if not path or "\x00" in path:
        raise ValueError(f"bad path: {path!r}")
    if os.path.isabs(path) or path.startswith("/") or path.startswith("\\"):
        raise ValueError(f"path must be relative: {path!r}")
    norm = posixpath.normpath(path)
    if norm.startswith("..") or "/../" in ("/" + norm):
        raise ValueError(f"path escapes project: {path!r}")
    if not norm.startswith("assets/gen/"):
        raise ValueError(f"path must be under assets/gen/: {path!r}")
    if not norm.endswith(".png"):
        raise ValueError(f"asset path must end with .png: {path!r}")
```

- [ ] **Step 4: 跑通过** — PASS.

- [ ] **Step 5: 失败测试(manifest)**

```python
# skills/art-director/tests/test_manifest.py
import json, pytest
from manifest import Asset, Manifest

BG={"id":"hero-bg","kind":"bg","prompt":"neon","aspect":"16:9","resolution":"4k",
    "transparent":False,"path":"assets/gen/hero-bg.png","placeholder":"url(assets/gen/hero-bg.png)"}
CUT={"id":"mascot","kind":"cutout","prompt":"fox","aspect":"2:3","transparent":True,"format":"png",
     "path":"assets/gen/mascot.png","placeholder":'src="assets/gen/mascot.png"'}

def test_load_roundtrip(tmp_path):
    p=tmp_path/"m.json"; p.write_text(json.dumps({"version":1,"style":{"brief":"x"},"assets":[BG,CUT]}))
    m=Manifest.load(str(p))
    assert [a.id for a in m.assets]==["hero-bg","mascot"]
    assert m.assets[0].status=="pending" and m.assets[0].task_id is None

def test_save_atomic_roundtrip(tmp_path):
    m=Manifest(version=1,style={},assets=[Asset(**BG)])
    m.assets[0].status="done"; m.assets[0].task_id="T1"
    dest=tmp_path/"sub"/"m.json"
    m.save_atomic(str(dest))
    back=Manifest.load(str(dest))
    assert back.assets[0].status=="done" and back.assets[0].task_id=="T1"

def test_validate_ok(): assert Manifest(version=1,style={},assets=[Asset(**BG),Asset(**CUT)]).validate()==[]
def test_validate_dup_id():
    errs=Manifest(version=1,style={},assets=[Asset(**BG),Asset(**{**CUT,"id":"hero-bg"})]).validate()
    assert any("duplicate" in e for e in errs)
def test_validate_bad_kind():
    assert any("kind" in e for e in Manifest(version=1,style={},assets=[Asset(**{**BG,"kind":"video"})]).validate())
def test_validate_cutout_not_transparent():
    assert any("transparent" in e for e in Manifest(version=1,style={},assets=[Asset(**{**CUT,"transparent":False})]).validate())
def test_validate_bad_path():
    assert any("path" in e for e in Manifest(version=1,style={},assets=[Asset(**{**BG,"path":"../x.png"})]).validate())
def test_validate_exceeds_max(monkeypatch):
    big=[Asset(**{**BG,"id":f"b{i}","path":f"assets/gen/b{i}.png"}) for i in range(20)]
    errs=Manifest(version=1,style={},assets=big).validate(max_assets=12)
    assert any("max_assets" in e for e in errs)
```

- [ ] **Step 6: 跑失败** — FAIL (no module).

- [ ] **Step 7: 实现 manifest**

```python
# skills/art-director/manifest.py
import json, os, tempfile
from dataclasses import dataclass, asdict
from typing import Optional
from paths import validate_asset_path

VALID_KINDS = {"bg", "cutout"}

@dataclass
class Asset:
    id: str; kind: str; prompt: str; aspect: str; path: str; placeholder: str
    transparent: bool = False; resolution: Optional[str] = None; format: Optional[str] = None
    status: str = "pending"            # pending | done | failed
    task_id: Optional[str] = None
    def to_dict(self): return {k: v for k, v in asdict(self).items() if v is not None}

@dataclass
class Manifest:
    version: int; style: dict; assets: list
    @classmethod
    def load(cls, path):
        d = json.loads(open(path, encoding="utf-8").read())
        return cls(d.get("version",1), d.get("style",{}), [Asset(**a) for a in d.get("assets",[])])
    def save_atomic(self, path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        out = {"version": self.version, "style": self.style, "assets": [a.to_dict() for a in self.assets]}
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(out, ensure_ascii=False, indent=2))
        os.replace(tmp, path)          # 原子(M4)
    def validate(self, max_assets=None):
        errs, seen = [], set()
        if max_assets is not None and len(self.assets) > max_assets:
            errs.append(f"too many assets: {len(self.assets)} > max_assets={max_assets} (cost guard)")
        for a in self.assets:
            if a.id in seen: errs.append(f"duplicate asset id: {a.id}")
            seen.add(a.id)
            if a.kind not in VALID_KINDS: errs.append(f"asset {a.id}: invalid kind {a.kind!r}")
            if a.kind == "cutout" and not a.transparent: errs.append(f"asset {a.id}: cutout must be transparent=true")
            if a.kind == "bg" and a.transparent: errs.append(f"asset {a.id}: bg cannot be transparent (gpt-image-2 has no alpha)")
            if not a.placeholder: errs.append(f"asset {a.id}: placeholder required")
            try: validate_asset_path(a.path)
            except ValueError as e: errs.append(f"asset {a.id}: {e}")
        return errs
```

- [ ] **Step 8: 跑通过** — PASS (paths + manifest 全绿).
- [ ] **Step 9: Commit** — `git commit -m "feat(art-director): 路径越界校验 + manifest 契约(原子写/max_assets/reject transparent bg)"`

---

## Task 3: 模型派发注册表（测试取自 APImart 文档示例）

**Files:** Create `skills/art-director/registry.py`; Test `tests/test_registry.py`

- [ ] **Step 1: 失败测试** — 断言派发结果对齐 §7 文档请求体(bg=gpt-image-2 `size/resolution` 无 background;cutout=gpt-image-1.5-official `background:transparent`+`output_format:png`),并覆盖拒绝项。

```python
# skills/art-director/tests/test_registry.py
import pytest
from config import Config
from manifest import Asset
from registry import build_request
CFG=Config(api_key="k")
def _bg(**k): return Asset(id="b",kind="bg",prompt="p",aspect="16:9",path="assets/gen/b.png",placeholder="url(assets/gen/b.png)",**k)
def _cut(**k): return Asset(id="c",kind="cutout",prompt="p",aspect="2:3",transparent=True,format="png",path="assets/gen/c.png",placeholder='src="assets/gen/c.png"',**k)

def test_bg_documented_shape():
    b=build_request(_bg(),CFG)
    assert b=={"model":"gpt-image-2","prompt":"p","size":"16:9","resolution":"2k","n":1}  # 与 §7 bg 请求体一致
def test_bg_resolution_override(): assert build_request(_bg(resolution="4k"),CFG)["resolution"]=="4k"
def test_bg_bad_resolution():
    with pytest.raises(ValueError): build_request(_bg(resolution="8k"),CFG)
def test_bg_transparent_rejected():
    with pytest.raises(ValueError): build_request(_bg(transparent=True),CFG)
def test_cutout_documented_shape():
    c=build_request(_cut(),CFG)
    assert c=={"model":"gpt-image-1.5-official","prompt":"p","size":"2:3","background":"transparent","output_format":"png","n":1}
def test_cutout_resolution_rejected():
    with pytest.raises(ValueError): build_request(_cut(resolution="2k"),CFG)
def test_cutout_bad_size():
    with pytest.raises(ValueError): build_request(_cut(aspect="16:9"),CFG)
```

- [ ] **Step 2: 跑失败** — FAIL (no module).

- [ ] **Step 3: 实现**

```python
# skills/art-director/registry.py
from manifest import Asset
from config import Config

BG_RESOLUTIONS={"1k","2k","4k"}
BG_SIZES={"1:1","3:2","2:3","4:3","3:4","5:4","4:5","16:9","9:16","2:1","1:2","3:1","1:3","21:9","9:21","auto"}
CUTOUT_SIZES={"1:1","2:3","3:2"}

def build_bg_request(a: Asset, cfg: Config) -> dict:
    if a.transparent: raise ValueError(f"{a.id}: bg cannot be transparent")
    if a.aspect not in BG_SIZES: raise ValueError(f"{a.id}: bg aspect {a.aspect!r} invalid")
    res=a.resolution or cfg.default_bg_resolution
    if res not in BG_RESOLUTIONS: raise ValueError(f"{a.id}: bg resolution {res!r} must be 1k|2k|4k")
    return {"model":"gpt-image-2","prompt":a.prompt,"size":a.aspect,"resolution":res,"n":1}

def build_cutout_request(a: Asset, cfg: Config) -> dict:
    if a.resolution is not None: raise ValueError(f"{a.id}: cutout has no resolution tier")
    if a.aspect not in CUTOUT_SIZES: raise ValueError(f"{a.id}: cutout aspect {a.aspect!r} must be 1:1|2:3|3:2")
    return {"model":"gpt-image-1.5-official","prompt":a.prompt,"size":a.aspect,"background":"transparent","output_format":"png","n":1}

HANDLERS={"bg":build_bg_request,"cutout":build_cutout_request}
def build_request(a: Asset, cfg: Config) -> dict:
    h=HANDLERS.get(a.kind)
    if h is None: raise ValueError(f"{a.id}: no handler for kind {a.kind!r}")
    return h(a, cfg)
```

> **B3 未决项(冒烟前不视为已证):** APImart 单端点 `/images/generations` 用 `model` 字段选模型(非分端点);`size`/`resolution`/`background`/`output_format` 取值。Task 11 真 API 冒烟必须落地验证;若文档示例与此不符,以冒烟结果为准回改本任务。

- [ ] **Step 4: 跑通过** — PASS (7 passed).
- [ ] **Step 5: Commit** — `git commit -m "feat(art-director): 注册表派发(对齐文档请求体 + 拒 transparent-bg/resolution-cutout)"`

---

## Task 4: Transport（错误归一化）+ APImart 客户端（容忍变体）

**Files:** Create `skills/art-director/transport.py`, `skills/art-director/apimart.py`; Test `tests/test_transport.py`, `tests/test_apimart.py`

- [ ] **Step 1: 失败测试(transport 错误归一化)**

```python
# skills/art-director/tests/test_transport.py
import urllib.error, io, pytest
from transport import UrllibTransport, RetryableError, FatalError

class FakeOpener:
    def __init__(self, exc=None, status=200, body=b"{}"):
        self.exc=exc; self.status=status; self.body=body
    def __call__(self, req, timeout=0):
        if self.exc: raise self.exc
        class R:
            status=self.status
            def read(s): return self.body
            def __enter__(s): return s
            def __exit__(s,*a): return False
        return R()

def _err(code, headers=None):
    return urllib.error.HTTPError("u", code, "msg", headers or {}, io.BytesIO(b"{}"))

def test_429_is_retryable(monkeypatch):
    t=UrllibTransport(); monkeypatch.setattr("urllib.request.urlopen", FakeOpener(exc=_err(429,{"Retry-After":"7"})))
    with pytest.raises(RetryableError) as e: t.get("http://x",{})
    assert e.value.retry_after==7
def test_503_is_retryable(monkeypatch):
    t=UrllibTransport(); monkeypatch.setattr("urllib.request.urlopen", FakeOpener(exc=_err(503)))
    with pytest.raises(RetryableError): t.get("http://x",{})
def test_400_is_fatal(monkeypatch):
    t=UrllibTransport(); monkeypatch.setattr("urllib.request.urlopen", FakeOpener(exc=_err(400)))
    with pytest.raises(FatalError): t.get("http://x",{})
def test_urlerror_is_retryable(monkeypatch):
    import urllib.error as ue
    t=UrllibTransport(); monkeypatch.setattr("urllib.request.urlopen", FakeOpener(exc=ue.URLError("conn reset")))
    with pytest.raises(RetryableError): t.get("http://x",{})
```

- [ ] **Step 2: 跑失败** — FAIL (no module).

- [ ] **Step 3: 实现 transport**

```python
# skills/art-director/transport.py
import json, urllib.request, urllib.error

class RetryableError(Exception):
    def __init__(self, msg, retry_after=None): super().__init__(msg); self.retry_after=retry_after
class FatalError(Exception): pass

RETRYABLE_STATUS={408,409,425,429,500,502,503,504}

def _parse_retry_after(headers):
    try: return int(headers.get("Retry-After")) if headers and headers.get("Retry-After") else None
    except (ValueError, TypeError): return None

def _do(req):
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw=r.read()
            return r.status, (json.loads(raw.decode()) if raw else {})
    except urllib.error.HTTPError as e:
        if e.code in RETRYABLE_STATUS:
            raise RetryableError(f"HTTP {e.code}", retry_after=_parse_retry_after(e.headers))
        raise FatalError(f"HTTP {e.code}: {e.reason}")
    except urllib.error.URLError as e:
        raise RetryableError(f"network error: {e.reason}")

class UrllibTransport:
    def post(self, url, headers, body):
        req=urllib.request.Request(url, data=json.dumps(body).encode(),
            headers={**headers,"Content-Type":"application/json"}, method="POST")
        return _do(req)
    def get(self, url, headers):
        return _do(urllib.request.Request(url, headers=headers, method="GET"))
    def get_bytes(self, url):
        try:
            with urllib.request.urlopen(url, timeout=120) as r: return r.read()
        except urllib.error.HTTPError as e:
            if e.code in RETRYABLE_STATUS: raise RetryableError(f"download HTTP {e.code}")
            raise FatalError(f"download HTTP {e.code}")
        except urllib.error.URLError as e:
            raise RetryableError(f"download network error: {e.reason}")
```

- [ ] **Step 4: 失败测试(apimart 容忍变体)**

```python
# skills/art-director/tests/test_apimart.py
import pytest
from apimart import ApimartClient
from transport import FatalError

class FakeT:
    def __init__(self, post_resp, poll_seq):
        self.post_resp=post_resp; self.poll_seq=list(poll_seq); self.posts=[]; self.dl=[]
    def post(self,u,h,b): self.posts.append((u,h,b)); return 200,self.post_resp
    def get(self,u,h): return 200,self.poll_seq.pop(0)
    def get_bytes(self,u): self.dl.append(u); return b"\x89PNG\r\n\x1a\n"+b"\x00"*60

class Clock:
    def __init__(self): self.t=0.0
    def monotonic(self): return self.t
    def sleep(self, s): self.t+=s

def _c(post_resp, poll_seq):
    return ApimartClient(FakeT(post_resp, poll_seq), api_key="k", base_url="https://api.apimart.ai/v1")

def test_submit_task_id(): assert _c({"task_id":"T1"},[]).submit({"model":"m"})=="T1"
def test_submit_id_alias(): assert _c({"id":"T2"},[]).submit({"model":"m"})=="T2"
def test_submit_nested(): assert _c({"data":{"task_id":"T3"}},[]).submit({"model":"m"})=="T3"
def test_submit_header_auth():
    c=_c({"task_id":"T"},[]); c.submit({"model":"m"})
    assert c.t.posts[0][1]["Authorization"]=="Bearer k"
    assert c.t.posts[0][0].endswith("/images/generations")
def test_submit_no_id_fatal():
    with pytest.raises(FatalError): _c({"oops":1},[]).submit({"model":"m"})

def test_poll_terminal_success():
    c=_c({"task_id":"T"},[{"status":"running"},{"status":"succeeded","url":"https://cdn/x.png"}])
    r=c.poll("T",timeout=10,interval=0,clock=Clock()); assert c.image_url(r)=="https://cdn/x.png"
def test_poll_sync_data_url():
    c=_c({"task_id":"T"},[{"status":"completed","data":[{"url":"https://cdn/y.png"}]}])
    r=c.poll("T",timeout=10,interval=0,clock=Clock()); assert c.image_url(r)=="https://cdn/y.png"
def test_poll_url_but_nonterminal_keeps_waiting():
    # 有 url 但 status 仍 running → 不得提前判成功(codex P1#3)
    c=_c({"task_id":"T"},[{"status":"running","url":"https://cdn/preview.png"},{"status":"succeeded","url":"https://cdn/final.png"}])
    r=c.poll("T",timeout=10,interval=0,clock=Clock()); assert c.image_url(r)=="https://cdn/final.png"
def test_poll_failure_fatal():
    with pytest.raises(FatalError): _c({"task_id":"T"},[{"status":"failed","error":"nsfw"}]).poll("T",timeout=10,interval=0,clock=Clock())
def test_poll_timeout():
    with pytest.raises(TimeoutError): _c({"task_id":"T"},[{"status":"running"}]*99).poll("T",timeout=0,interval=0,clock=Clock())
def test_download_writes(tmp_path):
    c=_c({"task_id":"T"},[]); d=tmp_path/"a.png"; c.download("https://cdn/x.png",str(d)); assert d.read_bytes().startswith(b"\x89PNG")
```

- [ ] **Step 5: 跑失败** — FAIL (no module).

- [ ] **Step 6: 实现 apimart**

```python
# skills/art-director/apimart.py
import time
from transport import RetryableError, FatalError

DONE_OK={"succeeded","success","completed","done","finished"}
DONE_FAIL={"failed","error","canceled","cancelled","rejected"}

def _extract_task_id(data: dict):
    if isinstance(data.get("task_id"), str): return data["task_id"]
    if isinstance(data.get("id"), str): return data["id"]
    inner=data.get("data")
    if isinstance(inner, dict) and isinstance(inner.get("task_id"), str): return inner["task_id"]
    return None

def _image_url(result: dict):
    if isinstance(result.get("url"), str): return result["url"]
    for key in ("data","images","results"):
        seq=result.get(key)
        if isinstance(seq, list) and seq and isinstance(seq[0], dict) and isinstance(seq[0].get("url"), str):
            return seq[0]["url"]
    return None

class ApimartClient:
    def __init__(self, transport, api_key, base_url):
        self.t=transport; self.base=base_url.rstrip("/"); self.headers={"Authorization": f"Bearer {api_key}"}
    def submit(self, body) -> str:
        _, data=self.t.post(f"{self.base}/images/generations", self.headers, body)
        tid=_extract_task_id(data)
        if not tid: raise FatalError("submit: no task_id/id in response (keys: %s)" % sorted(data.keys()))
        return tid
    def image_url(self, result): return _image_url(result)
    def poll(self, task_id, timeout, interval, clock=time) -> dict:
        deadline=clock.monotonic()+timeout
        while True:
            _, data=self.t.get(f"{self.base}/tasks/{task_id}", self.headers)
            st=str(data.get("status","")).lower()
            if st in DONE_FAIL: raise FatalError(f"task {task_id} {st}")
            if st in DONE_OK:                       # 仅终态成功才取 url(codex P1#3)
                if not _image_url(data): raise FatalError(f"task {task_id} done but no image url")
                return data
            if clock.monotonic()>=deadline: raise TimeoutError(f"task {task_id} not done within {timeout}s (status={st!r})")
            if interval: clock.sleep(interval)
    def download(self, url, dest):
        with open(dest, "wb") as f: f.write(self.t.get_bytes(url))
```

> **密钥不入日志(m6):** 错误信息只暴露 status/keys,绝不内插原始响应体或 headers。

- [ ] **Step 7: 跑通过** — PASS (transport 4 + apimart 11).
- [ ] **Step 8: Commit** — `git commit -m "feat(art-director): Transport 错误归一化 + APImart 客户端(容忍变体/终态判定/clock 注入)"`

---

## Task 5: PNG 校验（签名 + palette/tRNS alpha）

**Files:** Create `skills/art-director/pngutil.py`; Test `tests/test_pngutil.py`

- [ ] **Step 1: 失败测试** — 覆盖 color type 6/4 真;type 2 无 tRNS 假;type 3 + tRNS 真(m3);非 PNG 假;`is_png`。

```python
# skills/art-director/tests/test_pngutil.py
import struct, zlib, pytest
from pngutil import png_has_alpha, is_png
SIG=b"\x89PNG\r\n\x1a\n"
def _chunk(typ, data): return struct.pack(">I",len(data))+typ+data+struct.pack(">I", zlib.crc32(typ+data)&0xffffffff)
def _png(ct, trns=False):
    ihdr=_chunk(b"IHDR", struct.pack(">IIBBBBB",1,1,8,ct,0,0,0))
    out=SIG+ihdr
    if trns: out+=_chunk(b"tRNS", b"\x00")
    out+=_chunk(b"IDAT", b"\x00"); out+=_chunk(b"IEND", b"")
    return out
def _w(tmp,b): p=tmp/"a.png"; p.write_bytes(b); return str(p)

def test_truecolor_alpha(tmp_path): assert png_has_alpha(_w(tmp_path,_png(6))) is True
def test_gray_alpha(tmp_path): assert png_has_alpha(_w(tmp_path,_png(4))) is True
def test_truecolor_no_alpha(tmp_path): assert png_has_alpha(_w(tmp_path,_png(2))) is False
def test_palette_trns_is_alpha(tmp_path): assert png_has_alpha(_w(tmp_path,_png(3,trns=True))) is True
def test_truecolor_trns_is_alpha(tmp_path): assert png_has_alpha(_w(tmp_path,_png(2,trns=True))) is True
def test_not_png(tmp_path): assert png_has_alpha(_w(tmp_path,b"JFIF")) is False
def test_is_png(tmp_path): assert is_png(_w(tmp_path,_png(6))) is True and is_png(_w(tmp_path,b"x")) is False
```

- [ ] **Step 2: 跑失败** — FAIL (no module).

- [ ] **Step 3: 实现** — 扫 chunk 至 IDAT,识别 color type 4/6,或任意 color type 带 `tRNS`。

```python
# skills/art-director/pngutil.py
import struct
PNG_SIG=b"\x89PNG\r\n\x1a\n"
ALPHA_CT={4,6}

def is_png(path: str) -> bool:
    with open(path,"rb") as f: return f.read(8)==PNG_SIG

def png_has_alpha(path: str) -> bool:
    with open(path,"rb") as f:
        if f.read(8)!=PNG_SIG: return False
        # IHDR
        ln=f.read(4)
        if len(ln)<4: return False
        typ=f.read(4)
        if typ!=b"IHDR": return False
        ihdr=f.read(struct.unpack(">I",ln)[0]); f.read(4)  # data + crc
        color_type=ihdr[9]
        if color_type in ALPHA_CT: return True
        # 扫后续 chunk 找 tRNS,遇 IDAT 即停
        while True:
            ln=f.read(4)
            if len(ln)<4: return False
            typ=f.read(4); size=struct.unpack(">I",ln)[0]
            if typ==b"tRNS": return True
            if typ in (b"IDAT", b"IEND"): return False
            f.seek(size+4, 1)
```

- [ ] **Step 4: 跑通过** — PASS (7 passed).
- [ ] **Step 5: Commit** — `git commit -m "feat(art-director): PNG 校验(签名 + palette/tRNS alpha 扫描)"`

---

## Task 6: 引擎（下载后校验 + 逐素材原子持久化 + resume 续轮询）

**Files:** Create `skills/art-director/engine.py`; Test `tests/test_engine.py`

- [ ] **Step 1: 失败测试** — 覆盖:全部成功并落盘 done;下载非 PNG → 校验抛 → failed;cutout 无 alpha → failed;某素材 RetryableError 用尽 → failed 且不拖垮其余;退避用 fake clock(与 poll_interval 解耦);每素材完成触发 `on_progress`(原子持久化);resume 续轮询已有 task_id 不重新 submit。

```python
# skills/art-director/tests/test_engine.py
import struct, zlib, pytest
from config import Config
from manifest import Asset, Manifest
from engine import generate, validate_download
from transport import RetryableError
SIG=b"\x89PNG\r\n\x1a\n"
def _chunk(t,d): return struct.pack(">I",len(d))+t+d+struct.pack(">I",zlib.crc32(t+d)&0xffffffff)
def _png(ct,trns=False):
    o=SIG+_chunk(b"IHDR",struct.pack(">IIBBBBB",1,1,8,ct,0,0,0))
    if trns: o+=_chunk(b"tRNS",b"\x00")
    return o+_chunk(b"IDAT",b"\x00")+_chunk(b"IEND",b"")
CFG=Config(api_key="k",concurrency=2,retries=2,backoff_base=0,poll_timeout=10,poll_interval=0)

def _man():
    return Manifest(version=1,style={},assets=[
        Asset(id="b",kind="bg",prompt="p",aspect="16:9",path="assets/gen/b.png",placeholder="url(assets/gen/b.png)"),
        Asset(id="c",kind="cutout",prompt="p",aspect="2:3",transparent=True,format="png",path="assets/gen/c.png",placeholder='src="assets/gen/c.png"')])

class FakeClient:
    """按 asset prompt/model 行为可编程的假客户端。"""
    def __init__(self, png_for=None, submit_fail_models=(), submit_calls=None):
        self.png_for=png_for or {}; self.submit_fail_models=set(submit_fail_models)
        self.submit_calls=submit_calls if submit_calls is not None else []
    def submit(self, body):
        self.submit_calls.append(body["model"])
        if body["model"] in self.submit_fail_models: raise RetryableError("boom")
        return f"T-{body['model']}"
    def poll(self, tid, timeout, interval, clock=None): return {"status":"succeeded","url":f"https://cdn/{tid}.png","_tid":tid}
    def image_url(self, r): return r["url"]
    def download(self, url, dest):
        # 按 model 决定写什么字节
        png = _png(6) if "1.5" in url else _png(2)   # cutout(1.5)→alpha, bg(2)→no-alpha
        open(dest,"wb").write(png)

def test_validate_download_rejects_non_png(tmp_path):
    p=tmp_path/"x.png"; p.write_bytes(b"<html>error</html>")
    a=Asset(id="b",kind="bg",prompt="p",aspect="16:9",path="assets/gen/b.png",placeholder="x")
    with pytest.raises(Exception): validate_download(a, str(p))

def test_validate_download_cutout_needs_alpha(tmp_path):
    p=tmp_path/"c.png"; p.write_bytes(_png(2))   # 无 alpha
    a=Asset(id="c",kind="cutout",prompt="p",aspect="2:3",transparent=True,path="assets/gen/c.png",placeholder="x")
    with pytest.raises(Exception): validate_download(a, str(p))

def test_generate_all_done(tmp_path):
    c=FakeClient()
    m=generate(_man(), str(tmp_path), c, CFG)
    assert (tmp_path/"assets/gen/b.png").exists() and (tmp_path/"assets/gen/c.png").exists()
    assert all(a.status=="done" for a in m.assets)

def test_generate_bg_submit_fail_isolated(tmp_path):
    calls=[]
    c=FakeClient(submit_fail_models={"gpt-image-2"}, submit_calls=calls)
    m=generate(_man(), str(tmp_path), c, CFG)
    by={a.id:a.status for a in m.assets}
    assert by["b"]=="failed" and by["c"]=="done"
    assert calls.count("gpt-image-2")==1+CFG.retries     # 仅 bg 重试,精确计数(codex P1#6)
    assert calls.count("gpt-image-1.5-official")==1

def test_generate_progress_callback_persists(tmp_path):
    saved=[]
    c=FakeClient()
    generate(_man(), str(tmp_path), c, CFG, on_progress=lambda m: saved.append([a.status for a in m.assets]))
    assert saved and saved[-1].count("done")==2          # 每完成一个触发一次

def test_generate_resume_polls_existing_task(tmp_path):
    calls=[]
    c=FakeClient(submit_calls=calls)
    m=_man(); m.assets[0].task_id="T-existing"; m.assets[0].status="pending"  # 已 submit 过
    generate(m, str(tmp_path), c, CFG)
    assert "gpt-image-2" not in calls                    # bg 不再 submit,直接续轮询(M4/codex P1#5)
```

- [ ] **Step 2: 跑失败** — FAIL (no module).

- [ ] **Step 3: 实现**

```python
# skills/art-director/engine.py
import os, time, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from registry import build_request
from transport import RetryableError
from pngutil import is_png, png_has_alpha

def _abs(project_dir, rel): return os.path.join(project_dir, rel)

def validate_download(asset, dest) -> None:
    if not os.path.exists(dest) or os.path.getsize(dest)==0:
        raise ValueError(f"{asset.id}: empty download")
    if not is_png(dest):
        raise ValueError(f"{asset.id}: download is not a PNG (likely an error page)")
    if asset.kind=="cutout" and not png_has_alpha(dest):
        raise ValueError(f"{asset.id}: cutout returned without alpha channel")

def _gen_one(asset, project_dir, client, cfg, clock):
    body=build_request(asset, cfg)                  # 校验错误不重试(直接抛 → failed)
    last=None
    for attempt in range(cfg.retries+1):
        try:
            if not asset.task_id:                   # resume:已有 task 则跳过 submit(避免重复付费)
                asset.task_id=client.submit(body)
            result=client.poll(asset.task_id, cfg.poll_timeout, cfg.poll_interval, clock=clock)
            dest=_abs(project_dir, asset.path)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            client.download(client.image_url(result), dest)
            validate_download(asset, dest)          # 下载后立即校验(M3)
            return "done"
        except RetryableError as e:
            last=e; asset.task_id=None              # 可重试错误:丢弃 task 重来
            if attempt<cfg.retries and cfg.backoff_base:
                clock.sleep(min(cfg.backoff_base*(2**attempt), 10))
        except Exception as e:                      # FatalError / 校验失败 / 派发错误:不重试
            raise
    raise last

def generate(manifest, project_dir, client, cfg, on_progress=None, clock=time):
    todo=[]
    for a in manifest.assets:
        if a.status=="done" and os.path.exists(_abs(project_dir, a.path)): continue
        todo.append(a)
    lock=threading.Lock()
    with ThreadPoolExecutor(max_workers=cfg.concurrency) as ex:
        futs={ex.submit(_gen_one, a, project_dir, client, cfg, clock): a for a in todo}
        for fut in as_completed(futs):
            a=futs[fut]
            try: a.status=fut.result()
            except Exception: a.status="failed"
            if on_progress:
                with lock: on_progress(manifest)    # 每素材完成即回调(CLI 据此原子落盘)
    return manifest
```

- [ ] **Step 4: 跑通过** — PASS (6 passed).
- [ ] **Step 5: Commit** — `git commit -m "feat(art-director): 引擎(下载后校验+逐素材持久化回调+resume续轮询+退避解耦)"`

---

## Task 7: Extractor（归一化 + 拒不支持形式）

**Files:** Create `skills/art-director/extractor.py`; Test `tests/test_extractor.py`

> 注:**不复用任何 fastship extractor**(评审 M2 证其不存在);本任务为净新实现。

- [ ] **Step 1: 失败测试** — 覆盖归一化(`./`,`/`,`?v=`,`#`)、srcset/image-set/React 触发 `UnsupportedMarkup` 硬失败、reconcile 双向。

```python
# skills/art-director/tests/test_extractor.py
import pytest
from manifest import Asset, Manifest
from extractor import normalize_ref, extract_refs, reconcile, UnsupportedMarkup

def _man(paths): return Manifest(version=1,style={},assets=[Asset(id=f"a{i}",kind="bg",prompt="p",aspect="16:9",path=p,placeholder=f"url({p})") for i,p in enumerate(paths)])

def test_normalize():
    for raw in ["assets/gen/h.png","./assets/gen/h.png","/assets/gen/h.png","assets/gen/h.png?v=2","assets/gen/h.png#x"]:
        assert normalize_ref(raw)=="assets/gen/h.png"

def test_extract_plain_html():
    code='<style>.h{background-image:url(./assets/gen/hero.png);}</style><img src="/assets/gen/m.png?v=3">'
    assert extract_refs(code)=={"assets/gen/hero.png","assets/gen/m.png"}

def test_extract_ignores_external():
    assert extract_refs('<img src="https://cdn/x.png">')==set()

@pytest.mark.parametrize("bad", [
    '<img srcset="assets/gen/a.png 1x, assets/gen/a2.png 2x">',
    '<div style="background:image-set(url(assets/gen/a.png) 1x)">',
    '<img src={heroBg} data-asset="assets/gen/a.png">'])
def test_unsupported_markup_hard_fails(bad):
    with pytest.raises(UnsupportedMarkup):
        extract_refs(bad)

def test_reconcile_ok():
    code='<img src="assets/gen/a0.png"><div style="background:url(assets/gen/a1.png)">'
    r=reconcile(extract_refs(code), _man(["assets/gen/a0.png","assets/gen/a1.png"])); assert r.ok
def test_reconcile_missing():
    r=reconcile({"assets/gen/x.png"}, _man([])); assert not r.ok and "assets/gen/x.png" in r.missing_in_manifest
def test_reconcile_unreferenced():
    r=reconcile(set(), _man(["assets/gen/ghost.png"])); assert not r.ok and "assets/gen/ghost.png" in r.unreferenced
```

- [ ] **Step 2: 跑失败** — FAIL (no module).

- [ ] **Step 3: 实现**

```python
# skills/art-director/extractor.py
import re
from dataclasses import dataclass

class UnsupportedMarkup(Exception): pass

# v1 仅支持纯 HTML/CSS 的 url(...) 与 src="..."。以下形式 frontend-design 可能产出,但 v1 不接 → 硬失败并提示约束。
_UNSUPPORTED=[
    (re.compile(r"srcset\s*=", re.I), "srcset"),
    (re.compile(r"image-set\s*\(", re.I), "image-set()"),
    (re.compile(r"src\s*=\s*\{"), "JSX/React src={...}"),
    (re.compile(r"data-asset\s*=", re.I), "data-asset"),
]
_REF=re.compile(r"""(?:url\(\s*['"]?|src\s*=\s*['"])([^'"\)\s]*assets/gen/[^'"\)\s]+)""")

def normalize_ref(ref: str) -> str:
    ref=ref.split("?",1)[0].split("#",1)[0]
    if ref.startswith("./"): ref=ref[2:]
    if ref.startswith("/"): ref=ref[1:]
    return ref

@dataclass
class ReconcileResult:
    ok: bool; missing_in_manifest: set; unreferenced: set

def extract_refs(code: str, asset_dir: str = "assets/gen") -> set:
    for pat, name in _UNSUPPORTED:
        if pat.search(code):
            raise UnsupportedMarkup(
                f"v1 only supports plain HTML/CSS url()/src for assets; found {name}. "
                f"Instruct frontend-design to use literal url(assets/gen/<id>.png) / src=\"assets/gen/<id>.png\".")
    refs={normalize_ref(m.group(1)) for m in _REF.finditer(code)}
    return {r for r in refs if r.startswith(asset_dir)}

def reconcile(refs: set, manifest) -> ReconcileResult:
    mp={a.path for a in manifest.assets}
    missing=refs-mp; unref=mp-refs
    return ReconcileResult(ok=not missing and not unref, missing_in_manifest=missing, unreferenced=unref)
```

- [ ] **Step 4: 跑通过** — PASS (8 passed).
- [ ] **Step 5: Commit** — `git commit -m "feat(art-director): extractor(归一化 ./|/|query|frag + 拒 srcset/image-set/JSX + 双向对账)"`

---

## Task 8: Wiring（终验 + 被引用 + 状态重置 + 降级）

**Files:** Create `skills/art-director/wiring.py`; Test `tests/test_wiring.py`

- [ ] **Step 1: 失败测试** — 覆盖:全好(含 code 被引用校验);缺文件;空文件;非 PNG bg;cutout 无 alpha;manifest 路径未被 code 引用 → 失败;`reset_failed_status` 把 failed 清回 pending;`degrade` 给 failed 素材注入注释+CSS 兜底且返回处理数。

```python
# skills/art-director/tests/test_wiring.py
import struct, zlib, pytest
from manifest import Asset, Manifest
from wiring import verify, degrade, reset_failed_status
SIG=b"\x89PNG\r\n\x1a\n"
def _chunk(t,d): return struct.pack(">I",len(d))+t+d+struct.pack(">I",zlib.crc32(t+d)&0xffffffff)
def _png(ct): return SIG+_chunk(b"IHDR",struct.pack(">IIBBBBB",1,1,8,ct,0,0,0))+_chunk(b"IDAT",b"\x00")+_chunk(b"IEND",b"")
def _man():
    return Manifest(version=1,style={},assets=[
        Asset(id="b",kind="bg",prompt="p",aspect="16:9",path="assets/gen/b.png",placeholder="url(assets/gen/b.png)",status="done"),
        Asset(id="c",kind="cutout",prompt="p",aspect="2:3",transparent=True,format="png",path="assets/gen/c.png",placeholder='src="assets/gen/c.png"',status="done")])
def _w(tmp,rel,data): p=tmp/rel; p.parent.mkdir(parents=True,exist_ok=True); p.write_bytes(data); return p
CODE='<style>.h{background-image:url(assets/gen/b.png)}</style><img src="assets/gen/c.png">'

def test_verify_all_good(tmp_path):
    _w(tmp_path,"assets/gen/b.png",_png(2)); _w(tmp_path,"assets/gen/c.png",_png(6))
    assert verify(_man(), str(tmp_path), code=CODE)==[]
def test_verify_unreferenced(tmp_path):
    _w(tmp_path,"assets/gen/b.png",_png(2)); _w(tmp_path,"assets/gen/c.png",_png(6))
    assert any("referenced" in p for p in verify(_man(), str(tmp_path), code='<img src="assets/gen/b.png">'))
def test_verify_missing(tmp_path):
    _w(tmp_path,"assets/gen/c.png",_png(6))
    assert any("b" in p and "missing" in p for p in verify(_man(), str(tmp_path)))
def test_verify_bg_not_png(tmp_path):
    _w(tmp_path,"assets/gen/b.png",b"<html>"); _w(tmp_path,"assets/gen/c.png",_png(6))
    assert any("b" in p and "PNG" in p for p in verify(_man(), str(tmp_path)))
def test_verify_cutout_no_alpha(tmp_path):
    _w(tmp_path,"assets/gen/b.png",_png(2)); _w(tmp_path,"assets/gen/c.png",_png(2))
    assert any("c" in p and "alpha" in p for p in verify(_man(), str(tmp_path)))
def test_reset_failed_status():
    m=_man(); m.assets[0].status="failed"; reset_failed_status(m)
    assert m.assets[0].status=="pending"
def test_degrade_injects_fallback(tmp_path):
    (tmp_path/"index.html").write_text(CODE)
    m=_man(); m.assets[0].status="failed"
    n=degrade(m, str(tmp_path))
    html=(tmp_path/"index.html").read_text()
    assert n==1 and "art-director: asset 'b' failed" in html and "background-color" in html
```

- [ ] **Step 2: 跑失败** — FAIL (no module).

- [ ] **Step 3: 实现**

```python
# skills/art-director/wiring.py
import os, re
from pngutil import is_png, png_has_alpha
from extractor import extract_refs

def verify(manifest, project_dir, code=None) -> list:
    problems=[]
    for a in manifest.assets:
        dest=os.path.join(project_dir, a.path)
        if not os.path.exists(dest): problems.append(f"asset {a.id}: file missing at {a.path}"); continue
        if os.path.getsize(dest)==0: problems.append(f"asset {a.id}: file empty at {a.path}"); continue
        if not is_png(dest): problems.append(f"asset {a.id}: not a valid PNG at {a.path}"); continue
        if a.kind=="cutout" and not png_has_alpha(dest): problems.append(f"asset {a.id}: cutout has no alpha channel ({a.path})")
    if code is not None:                          # 被引用校验(codex P1#9)
        refs=extract_refs(code)
        for a in manifest.assets:
            if a.path not in refs: problems.append(f"asset {a.id}: manifest path {a.path} not referenced in code")
    return problems

def reset_failed_status(manifest) -> None:
    for a in manifest.assets:
        if a.status=="failed": a.status="pending"   # resume 时可重生(M3)

def degrade(manifest, project_dir, page="index.html") -> int:
    """失败素材:页面里其引用处旁注入警告注释,并加一条 CSS 兜底底色,页面不白屏(M5)。返回处理数。"""
    path=os.path.join(project_dir, page)
    if not os.path.exists(path): return 0
    html=open(path, encoding="utf-8").read()
    n=0
    fallback_css=[]
    for a in manifest.assets:
        if a.status!="failed": continue
        n+=1
        html=html.replace(a.path, a.path, 1)  # 占位符保留
        html=f"<!-- art-director: asset '{a.id}' failed to generate; using fallback -->\n" + html
        fallback_css.append(f"[style*='{a.path}'],img[src*='{a.path}']{{background-color:#222 !important;}}")
    if fallback_css:
        html=html.replace("</head>", "<style>\n"+"\n".join(fallback_css)+"\n</style>\n</head>", 1)
        open(path,"w",encoding="utf-8").write(html)
    return n
```

- [ ] **Step 4: 跑通过** — PASS (7 passed).
- [ ] **Step 5: Commit** — `git commit -m "feat(art-director): wiring(终验+被引用+状态重置+降级不白屏)"`

---

## Task 9: CLI（自插 sys.path + 成本估算 + 原子持久化 + 子进程外部 cwd 测试）

**Files:** Create `skills/art-director/cli.py`; Test `tests/test_cli.py`

- [ ] **Step 1: 失败测试** — 含**子进程从外部 cwd 用绝对路径调用**(B1/B2 真验证)。

```python
# skills/art-director/tests/test_cli.py
import json, struct, zlib, sys, subprocess, pathlib, pytest
from cli import main
SIG=b"\x89PNG\r\n\x1a\n"
def _chunk(t,d): return struct.pack(">I",len(d))+t+d+struct.pack(">I",zlib.crc32(t+d)&0xffffffff)
def _png(ct): return SIG+_chunk(b"IHDR",struct.pack(">IIBBBBB",1,1,8,ct,0,0,0))+_chunk(b"IDAT",b"\x00")+_chunk(b"IEND",b"")
SKILL_ROOT=pathlib.Path(__file__).resolve().parents[1]
MAN={"version":1,"style":{"brief":"x"},"assets":[
    {"id":"b","kind":"bg","prompt":"p","aspect":"16:9","resolution":"2k","path":"assets/gen/b.png","placeholder":"url(assets/gen/b.png)","status":"done"},
    {"id":"c","kind":"cutout","prompt":"p","aspect":"2:3","transparent":True,"format":"png","path":"assets/gen/c.png","placeholder":'src="assets/gen/c.png"',"status":"done"}]}

def test_validate_reports_errors(tmp_path, capsys):
    mp=tmp_path/"m.json"; mp.write_text(json.dumps({**MAN,"assets":[{**MAN["assets"][0],"kind":"video"}]}))
    assert main(["validate","--manifest",str(mp)])!=0 and "kind" in capsys.readouterr().out

def test_validate_cost_estimate(tmp_path, capsys):
    mp=tmp_path/"m.json"; mp.write_text(json.dumps(MAN))
    main(["validate","--manifest",str(mp)])
    assert "cost" in capsys.readouterr().out.lower()

def test_validate_code_reconcile_ok(tmp_path):
    mp=tmp_path/"m.json"; mp.write_text(json.dumps(MAN))
    code=tmp_path/"index.html"; code.write_text('<img src="assets/gen/b.png"><div style="background:url(assets/gen/c.png)">')
    assert main(["validate","--manifest",str(mp),"--code",str(code)])==0

def test_gate_fails_missing_alpha(tmp_path, capsys):
    mp=tmp_path/"m.json"; mp.write_text(json.dumps(MAN))
    (tmp_path/"assets/gen").mkdir(parents=True)
    (tmp_path/"assets/gen/b.png").write_bytes(_png(2)); (tmp_path/"assets/gen/c.png").write_bytes(_png(2))
    assert main(["gate","--manifest",str(mp),"--project-dir",str(tmp_path)])!=0 and "alpha" in capsys.readouterr().out

def test_cli_runs_from_foreign_cwd(tmp_path):
    # B1/B2 真验证:在毫不相关的 cwd 下,用 skill 绝对路径调用 cli.py,sibling import 必须成立
    mp=tmp_path/"m.json"; mp.write_text(json.dumps({**MAN,"assets":[{**MAN["assets"][0],"kind":"video"}]}))
    r=subprocess.run([sys.executable, str(SKILL_ROOT/"cli.py"), "validate","--manifest",str(mp)],
                     cwd=str(tmp_path), capture_output=True, text=True)
    assert r.returncode!=0 and "kind" in r.stdout    # 跑起来了(不是 import/路径错)
```

- [ ] **Step 2: 跑失败** — FAIL (no module).

- [ ] **Step 3: 实现**

```python
# skills/art-director/cli.py
import os, sys
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))   # 自插 skill 根 → sibling import 永远成立(B2)
import argparse
from config import Config
from manifest import Manifest
from extractor import extract_refs, reconcile, UnsupportedMarkup
from wiring import verify, degrade, reset_failed_status

# 粗略成本估算(USD),按 §6 文档区间;仅供护栏与提示。
_COST={"bg":{"1k":0.02,"2k":0.06,"4k":0.21},"cutout":0.06}
def _estimate(m):
    total=0.0
    for a in m.assets:
        total += _COST["cutout"] if a.kind=="cutout" else _COST["bg"].get(a.resolution or "2k",0.06)
    return total

def _cmd_validate(args):
    m=Manifest.load(args.manifest)
    errs=m.validate(max_assets=Config(api_key="x").max_assets)
    for e in errs: print(f"[manifest] {e}")
    print(f"[cost] estimated ~${_estimate(m):.2f} for {len(m.assets)} assets")
    if args.code:
        try:
            r=reconcile(extract_refs(open(args.code,encoding='utf-8').read()), m)
        except UnsupportedMarkup as e:
            print(f"[reconcile] {e}"); errs.append("unsupported markup")
        else:
            for p in sorted(r.missing_in_manifest): print(f"[reconcile] code refs {p} w/o manifest entry"); errs.append("x")
            for p in sorted(r.unreferenced): print(f"[reconcile] manifest {p} not referenced"); errs.append("x")
    print("VALIDATE: FAIL" if errs else "VALIDATE: PASS")
    return 1 if errs else 0

def _cmd_gen(args):
    from transport import UrllibTransport
    from apimart import ApimartClient
    from engine import generate
    cfg=Config.from_env()
    if args.bg_resolution: cfg.default_bg_resolution=args.bg_resolution
    m=Manifest.load(args.manifest)
    errs=m.validate(max_assets=cfg.max_assets)
    if errs:
        for e in errs: print(f"[manifest] {e}")
        print("GEN: FAIL (invalid manifest)"); return 1
    client=ApimartClient(UrllibTransport(), cfg.api_key, cfg.base_url)
    def _persist(man): man.save_atomic(os.path.join(args.project_dir, cfg.manifest_path))   # 每素材完成原子落盘(M4)
    m=generate(m, args.project_dir, client, cfg, on_progress=_persist)
    _persist(m)
    failed=[a.id for a in m.assets if a.status!="done"]
    if failed:
        degrade(m, args.project_dir)                 # 降级不白屏(M5)
        print(f"GEN: PARTIAL — failed {failed} (page degraded; rerun to resume)"); return 1
    print("GEN: PASS"); return 0

def _cmd_gate(args):
    m=Manifest.load(args.manifest)
    code=open(args.code,encoding="utf-8").read() if args.code else None
    probs=verify(m, args.project_dir, code=code)
    for p in probs: print(f"[gate] {p}")
    if probs:
        reset_failed_status(m); m.save_atomic(os.path.join(args.project_dir, Config(api_key='x').manifest_path))
        print(f"GATE: FAIL ({len(probs)})"); return 1
    print("GATE: PASS"); return 0

def main(argv=None):
    ap=argparse.ArgumentParser(prog="art-director")
    sub=ap.add_subparsers(dest="cmd", required=True)
    v=sub.add_parser("validate"); v.add_argument("--manifest",required=True); v.add_argument("--code"); v.set_defaults(fn=_cmd_validate)
    g=sub.add_parser("gen"); g.add_argument("--manifest",required=True); g.add_argument("--project-dir",required=True); g.add_argument("--bg-resolution"); g.set_defaults(fn=_cmd_gen)
    t=sub.add_parser("gate"); t.add_argument("--manifest",required=True); t.add_argument("--project-dir",required=True); t.add_argument("--code"); t.set_defaults(fn=_cmd_gate)
    args=ap.parse_args(argv)
    return args.fn(args)

if __name__=="__main__":
    sys.exit(main())
```

- [ ] **Step 4: 跑通过** — PASS (5 passed,含外部 cwd 子进程)。
- [ ] **Step 5: Commit** — `git commit -m "feat(art-director): CLI(自插sys.path+成本估算+原子持久化+降级+外部cwd子进程测试)"`

---

## Task 10: E2E（ThreadingHTTPServer mock，断言契约 + 失败/并发）

**Files:** Create `tests/test_e2e_mock.py`, `tests/fixtures/index.html`, `tests/fixtures/manifest.json`

- [ ] **Step 1: fixtures**

```html
<!-- tests/fixtures/index.html -->
<!doctype html><html><head><style>.hero{background-image:url(assets/gen/hero-bg.png);}</style></head>
<body><img src="assets/gen/mascot.png" alt="mascot"></body></html>
```

```json
// tests/fixtures/manifest.json
{"version":1,"style":{"brief":"cyberpunk test"},"assets":[
 {"id":"hero-bg","kind":"bg","prompt":"neon alley","aspect":"16:9","resolution":"2k","transparent":false,"path":"assets/gen/hero-bg.png","placeholder":"url(assets/gen/hero-bg.png)"},
 {"id":"mascot","kind":"cutout","prompt":"fox","aspect":"2:3","transparent":true,"format":"png","path":"assets/gen/mascot.png","placeholder":"src=\"assets/gen/mascot.png\""}]}
```

- [ ] **Step 2: 失败 E2E 测试** — mock 必须:校验 `Authorization: Bearer`、`POST /images/generations`、按 `model` 路由、断言请求体字段;running→succeeded 两段;每个 task 返回**不同 url**,下载**不同字节**(bg=truecolor,cutout=alpha),从而能抓 url/path 错配;另一用例让某 task 一直 running 验证其余仍完成;用 `ThreadingHTTPServer`。

```python
# tests/test_e2e_mock.py
import json, struct, zlib, threading, shutil, pathlib, http.server
import pytest
from config import Config
from manifest import Manifest
from transport import UrllibTransport
from apimart import ApimartClient
from engine import generate
from wiring import verify
from extractor import extract_refs, reconcile
FIX=pathlib.Path(__file__).parent/"fixtures"
SIG=b"\x89PNG\r\n\x1a\n"
def _chunk(t,d): return struct.pack(">I",len(d))+t+d+struct.pack(">I",zlib.crc32(t+d)&0xffffffff)
def _png(ct): return SIG+_chunk(b"IHDR",struct.pack(">IIBBBBB",1,1,8,ct,0,0,0))+_chunk(b"IDAT",b"\x00")+_chunk(b"IEND",b"")
PNG_BG=_png(2); PNG_CUT=_png(6)

class Handler(http.server.BaseHTTPRequestHandler):
    polls={}   # task_id -> remaining running count
    def log_message(self,*a): pass
    def _j(self,code,obj):
        b=json.dumps(obj).encode(); self.send_response(code)
        self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_POST(self):
        assert self.headers.get("Authorization")=="Bearer k"        # 断言 auth
        assert self.path.endswith("/images/generations")            # 断言端点
        body=json.loads(self.rfile.read(int(self.headers.get("Content-Length",0))))
        model=body["model"]
        assert model in ("gpt-image-2","gpt-image-1.5-official")     # 断言模型路由
        if model=="gpt-image-1.5-official":
            assert body.get("background")=="transparent" and body.get("output_format")=="png"
        tid=f"T-{model}"; Handler.polls[tid]=1                       # 先 running 一次
        self._j(200,{"task_id":tid})
    def do_GET(self):
        if self.path.endswith(".png"):
            data=PNG_CUT if "1.5" in self.path else PNG_BG
            self.send_response(200); self.send_header("Content-Type","image/png"); self.send_header("Content-Length",str(len(data))); self.end_headers(); self.wfile.write(data); return
        tid=self.path.rsplit("/",1)[-1]
        if Handler.polls.get(tid,0)>0:
            Handler.polls[tid]-=1; self._j(200,{"status":"running"})
        else:
            self._j(200,{"status":"succeeded","url":f"http://127.0.0.1:{self.server.server_address[1]}/img-{tid}.png"})

@pytest.fixture
def server():
    httpd=http.server.ThreadingHTTPServer(("127.0.0.1",0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"; httpd.shutdown()

def test_full_pipeline(tmp_path, server):
    shutil.copy(FIX/"index.html", tmp_path/"index.html")
    mp=tmp_path/"manifest.json"; shutil.copy(FIX/"manifest.json", mp)
    cfg=Config(api_key="k", base_url=server, poll_interval=0, poll_timeout=10, concurrency=2)
    client=ApimartClient(UrllibTransport(), cfg.api_key, cfg.base_url)
    m=Manifest.load(str(mp)); assert m.validate(max_assets=cfg.max_assets)==[]
    code=(tmp_path/"index.html").read_text(); assert reconcile(extract_refs(code), m).ok
    m=generate(m, str(tmp_path), client, cfg)
    assert all(a.status=="done" for a in m.assets)
    assert verify(m, str(tmp_path), code=code)==[]
    # url/path 错配防护:bg 文件无 alpha,cutout 文件有 alpha
    from pngutil import png_has_alpha
    assert png_has_alpha(str(tmp_path/"assets/gen/mascot.png")) and not png_has_alpha(str(tmp_path/"assets/gen/hero-bg.png"))
```

- [ ] **Step 3: 跑** — 先建 fixtures 后 PASS (1 passed)。
- [ ] **Step 4: Commit** — `git commit -m "test(art-director): E2E(ThreadingHTTPServer mock,断言auth/端点/模型/请求体+url-path错配防护)"`

---

## Task 11: 真 API 冒烟（env-gated，恢复 spec §12）

**Files:** Create `tests/test_smoke_real_api.py`

- [ ] **Step 1: 写冒烟测试** — 默认 skip,仅当 `ART_DIRECTOR_SMOKE=1` 且 `APIMART_API_KEY` 存在时跑;真打 APImart 各生成 1 张 bg(最小 1k)+ 1 张 cutout,断言:submit 拿到 id、poll 到终态、下载是 PNG、cutout 有 alpha。这是**唯一触达真实契约**的测试,验证 §7 的全部假设(端点/字段/状态/url 位置/模型选择)。

```python
# tests/test_smoke_real_api.py
import os, struct, pytest
from config import Config
from manifest import Asset, Manifest
from transport import UrllibTransport
from apimart import ApimartClient
from engine import generate
from pngutil import is_png, png_has_alpha

pytestmark = pytest.mark.skipif(
    os.environ.get("ART_DIRECTOR_SMOKE")!="1" or not os.environ.get("APIMART_API_KEY"),
    reason="real-API smoke: set ART_DIRECTOR_SMOKE=1 + APIMART_API_KEY")

def test_real_apimart_bg_and_cutout(tmp_path):
    cfg=Config.from_env()
    cfg.default_bg_resolution="1k"        # 省钱:冒烟用最小档
    client=ApimartClient(UrllibTransport(), cfg.api_key, cfg.base_url)
    m=Manifest(version=1, style={"brief":"smoke"}, assets=[
        Asset(id="smoke-bg",kind="bg",prompt="a simple blue gradient background",aspect="1:1",path="assets/gen/smoke-bg.png",placeholder="url(assets/gen/smoke-bg.png)"),
        Asset(id="smoke-cut",kind="cutout",prompt="a single red apple, centered",aspect="1:1",transparent=True,format="png",path="assets/gen/smoke-cut.png",placeholder='src="assets/gen/smoke-cut.png"')])
    m=generate(m, str(tmp_path), client, cfg)
    statuses={a.id:a.status for a in m.assets}
    assert statuses=={"smoke-bg":"done","smoke-cut":"done"}, f"smoke failed: {statuses}"
    assert is_png(str(tmp_path/"assets/gen/smoke-bg.png"))
    assert png_has_alpha(str(tmp_path/"assets/gen/smoke-cut.png")), "cutout must have alpha — verifies gpt-image-1.5-official transparent contract"
```

- [ ] **Step 2: 跑(默认 skip)** — `python -m pytest tests/test_smoke_real_api.py -v` → 1 skipped(无 env)。记录:实跑需 `ART_DIRECTOR_SMOKE=1 APIMART_API_KEY=... python -m pytest tests/test_smoke_real_api.py`。
- [ ] **Step 3: Commit** — `git commit -m "test(art-director): 恢复 spec §12 真 API 冒烟(env-gated,验证 APImart 契约)"`

> 🔴 **执行者注意:** 写完 Task 3/4 后、声称契约"已验证"前,必须实跑一次本冒烟(需 APImart key + 少量费用)。若真实响应形态与 Task 3/4 假设不符 → 回改 registry/apimart,以冒烟为准。

---

## Task 12: SKILL.md 编排说明 + INSTALL

**Files:** Create `skills/art-director/SKILL.md`, `skills/art-director/INSTALL.md`

- [ ] **Step 1: 写 SKILL.md** — 必须覆盖:

1. frontmatter:`name: art-director` + description(触发:出 design + 切图 + 复杂背景 + 透明素材 + 新工程页面)。
2. **调用约定(B1):** 所有命令用**本 skill 的绝对基目录**(Skill 工具加载时给出的 "Base directory for this skill")调用:`python3 "<SKILL_BASE>/cli.py" <cmd> ...`。禁止 `python3 scripts/cli.py`(相对 cwd 必失败)。
3. 前置:`export APIMART_API_KEY=...`;frontend-design 可用;Python3。
4. **4 阶段流水线:**
   - **Stage 1 design:** invoke `frontend-design` 出 HTML 页面;**注入素材约定(强约束,M1):** 凡该用真实栅格图处(复杂背景、角色/物体/装饰),禁止 CSS 假造,改为 ① **只用** `background-image:url(assets/gen/<id>.png)` 或 `<img src="assets/gen/<id>.png">` 的**字面形式**;**禁用** `srcset`/`<picture>`/`image-set()`/Tailwind `bg-[url()]`/React `src={}`(extractor 会硬失败);② 同步往 `.art-director/manifest.json` 追加 `{id,kind,prompt,aspect,transparent,path,placeholder,(resolution)}`。bg→`kind:"bg"`(默认 2k,hero 可 `"resolution":"4k"`);切图→`kind:"cutout",transparent:true`(aspect 仅 1:1/2:3/3:2)。
   - **Stage 2 对账:** `python3 "<SKILL_BASE>/cli.py" validate --manifest .art-director/manifest.json --code index.html` → 必须 PASS(并看 `[cost]` 估算,超 `max_assets` 会拒)。FAIL/UnsupportedMarkup → 回 Stage 1 让 frontend-design 改成字面形式。
   - **Stage 3 生成:** `python3 "<SKILL_BASE>/cli.py" gen --manifest .art-director/manifest.json --project-dir .` → 异步生成+下载+下载后校验+逐素材原子落盘;PARTIAL 时已自动降级(页面不白屏),重跑 resume 仅补失败。
   - **Stage 4 wiring gate:** `python3 "<SKILL_BASE>/cli.py" gate --manifest .art-director/manifest.json --project-dir . --code index.html` → 必须 PASS(文件存在+是 PNG+cutout 有 alpha+被引用)。
5. 产物:能跑的 `index.html` + `assets/gen/*.png` + `.art-director/manifest.json`(含 status/task_id)。
6. **红线:** gate 未 PASS 不得声称页面就绪;失败素材如数报告;成本超限必须先告知用户。
7. 旋钮:全局 `--bg-resolution`,逐素材 manifest `resolution`;cutout 不可切分辨率;`max_assets` 成本护栏。

- [ ] **Step 2: 写 INSTALL.md** — env(`APIMART_API_KEY`、可选 `ART_DIRECTOR_BG_RESOLUTION`、`ART_DIRECTOR_SMOKE`)、跑测试(`cd skills/art-director && python -m pytest`)、真 API 冒烟命令、frontend-design 依赖、零三方依赖。

- [ ] **Step 3: 校验 frontmatter** — `python3 -c "t=open('skills/art-director/SKILL.md').read(); assert t.startswith('---') and 'name: art-director' in t and 'description:' in t; print('frontmatter OK')"` → `frontmatter OK`

- [ ] **Step 4: Commit** — `git commit -m "docs(art-director): SKILL.md(绝对基目录调用+frontend-design字面约束+降级+成本) + INSTALL"`

---

## Task 13: 全量回归 + 自检

- [ ] **Step 1: 全量测试** — `cd skills/art-director && python -m pytest -v`。期望:**0 failed**(真 API 冒烟 1 skipped)。**断言 0 failed,不固定总数**(m8)。

- [ ] **Step 2: 对照 spec + 两轮评审逐条核覆盖**

| 需求 / 评审项 | 落点 |
|---|---|
| 方案 1 双模型派发 | Task 3 |
| APImart 异步 submit/poll/download + 容忍变体 + 终态判定 | Task 4 |
| 分辨率两级切换;cutout 不可切 | Task 1+3 |
| manifest 契约 + 原子写 + 路径越界(codex P1#9) | Task 2 |
| 引擎下载后校验(M3) | Task 6 |
| 逐素材原子持久化 + task_id resume(M4/codex P1#5) | Task 6+9 |
| HTTP 错误归一化 + Retry-After(codex P1#4) | Task 4 |
| extractor 归一化 + 拒不支持形式(M1) | Task 7 |
| wiring 被引用校验(codex P1#9) | Task 8 |
| 降级不白屏(M5) | Task 8+9 |
| palette/tRNS alpha(m3) | Task 5 |
| CLI 外部 cwd 可调(B1/B2) | Task 9 |
| E2E 断言契约 + 失败/错配(codex P2) | Task 10 |
| 真 API 冒烟(B3/spec §12) | Task 11 |
| 成本护栏(m2) | Task 1+9 |
| `.art-director/` 布局(m7) | Task 2+9 |
| 密钥不入日志(m6) | Task 4 |
| frontend-design 字面约束 | Task 12 |

- [ ] **Step 3: 最终 commit** — `git add -A && git commit -m "chore(art-director): 全量回归(0 failed)+ spec/评审逐条核覆盖"`

---

## 遗留与边界(诚实声明)

- **B3 仍是最大未证点:** 真 API 冒烟(Task 11)落地前,Task 3/4 的 APImart 契约是"按文档 + 容忍变体"的最佳猜测。执行顺序上,冒烟必须在声称"契约已验证"之前实跑一次。
- **seam 依赖 frontend-design 守约束:** Stage 1 注入的字面形式约束 + Stage 2 extractor 硬失败是双保险,但仍依赖 frontend-design 大体配合;若它顽固产出 React/srcset,Stage 2 会反复 FAIL → 需人工把页面落成纯 HTML/CSS(v1 边界)。
- **cutout 封顶 1536×1024 / PNG-only / 无 4K**(模型能力,非本 skill 缺陷)。
