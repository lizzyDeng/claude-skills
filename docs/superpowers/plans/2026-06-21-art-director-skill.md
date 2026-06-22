# Art Director Skill Implementation Plan (v3 — post 2 rounds grill + codex)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 claude-skills 建一个 `art-director` skill — 按风格 brief 编排 frontend-design 出 HTML 页面,用 APImart 的 gpt-image 模型生成真实 bg 大图 + 透明切图,回填进页面,产出能跑的 HTML/CSS 页面;素材失败时页面仍可加载(降级)。

**Architecture:** 编排 skill（SKILL.md 驱动 agent）+ 纯 stdlib Python 引擎。`kind→model` 注册表派发（bg→gpt-image-2 / cutout→gpt-image-1.5-official），走 APImart 异步接口（提交→轮询→下载/解码）。**契约的源头是 manifest 里每个 asset 的 `placeholder` 字符串**：Stage 1 让 frontend-design 把这个字符串**原样**写进页面代码,Stage 2 用**字符串包含（containment）**对账,不解析 HTML（消除大小写/引号/空格/srcset 的正则脆弱性）。`task_id` 是一台"付费任务状态机"：已提交=已付费,只在 submit 前失败或终态失败时才丢弃重生。

**Tech Stack:** Python 3 stdlib（urllib + concurrent.futures + base64）零三方依赖;pytest;HTTP 经可注入 `Transport`（返回 status+body+headers）单测注入 fake,E2E 用 `ThreadingHTTPServer`（per-test 状态可设）+ env-gated 真 API 冒烟。

---

## v3 修订摘要（吸收两轮 grill + codex,17 confirmed majors）

**v2 已确认修好的（不回退）:** CLI 打包/调用（B1/B2,codex+grill 均 SHIP）。

**v3 五大主题修复:**
1. **seam 改字符串包含契约**：删掉对 HTML 的正则抓取做"被引用"判定;改为「manifest 每个 `placeholder` 必须原样出现在代码里」。`UnsupportedMarkup` 只在**承载 assets/gen 的**构造上硬失败（非整页）。manifest 路径语法与提取语法统一（禁空格）。SKILL.md 加 Stage-2↔1 迭代上限 + 确定性兜底。〔B1/M7/M8/heuristics〕
2. **task_id 付费状态机**：终态/校验失败 → 清 task_id（resume 重生）+ unlink 残件;poll 期 RetryableError → 保留 task_id 续轮询（不重复付费）;poll TimeoutError → 可重试续轮询（合 spec §9/§10）;submit 成功即持久化 task_id;honor `retry_after`。〔M1/M2/M5/persist/unlink〕
3. **APImart 响应契约容忍**：`fetch_image` 支持 `url` / `b64_json`（gpt-image 默认!）/ `data` 为 dict / 列表任意元素;poll 开放世界状态（有图且非 pending=完成;error 信封快失败;未知状态不再死等);submit 兜底 `Location` 头;`Transport` 返回 headers。`run.log` 落地（含首跑原始 JSON 抓取,供核对契约）。〔M3/M4/M13/run.log〕
4. **degrade 健壮 + page 参数**：`</head>` 大小写/空白容忍,缺失则前置;`body` 兜底底色保证不白屏;页面文件名端到端参数化（`--page`),`_cmd_gen` 检查返回值。〔M6〕
5. **测试/门禁诚实**：修 `_cut` 双 kwarg、degrade 测试;补 fake-clock 退避测试、E2E stuck-task 隔离用例（per-test Handler 状态）、smoke 用不同 aspect + 负向断言 + 低 timeout;回归只断言 `0 failed`,逐任务计数实跑后更新。〔M9/M10/M11/M12/M14 + minors〕

---

## File Structure（skill 根平铺,与 fastship 同构;禁 scripts/ 子目录）

技能根:`skills/art-director/`。互相 import 的模块**平铺在 skill 根**。

| 文件 | 职责 |
|---|---|
| `config.py` | `Config`：env `APIMART_API_KEY`;base_url/默认 bg 分辨率/并发/重试/退避/轮询/`asset_dir`/`manifest_path`/`max_assets`。 |
| `paths.py` | `validate_asset_path`：相对、无 `.`/`..` 分量、**无空格**（与提取语法 round-trip）、`assets/gen/` 前缀、`.png` 后缀。 |
| `manifest.py` | `Asset`/`Manifest` + `load` + `save_atomic`(temp+os.replace) + `validate`(含 max_assets + 调 registry 预检)。 |
| `registry.py` | `kind→handler`,`build_request`;bg 拒 transparent,cutout 拒 resolution;测试取自文档请求体。 |
| `transport.py` | `Transport` + `UrllibTransport`：post/get 返回 **(status, body, headers)**;HTTPError/URLError/429/5xx → `RetryableError`(带 retry_after)/`FatalError`。 |
| `apimart.py` | `ApimartClient`：`submit`(含 Location 兜底)/`poll`(开放世界)/`fetch_image`(url 或 b64→bytes→dest)。 |
| `pngutil.py` | `is_png` + `png_has_alpha`(扫 chunk,识别 ct 4/6 与 palette/grayscale/truecolor+`tRNS`)。 |
| `engine.py` | `generate`：并发 + task_id 状态机 + temp→validate→replace + 逐转移持久化 + resume + 失败隔离。 |
| `extractor.py` | `reconcile(code, manifest)`：**placeholder 包含**(forward) + assets/gen token 扫描(reverse) + 承载资产的 `UnsupportedMarkup`。 |
| `wiring.py` | `verify`(磁盘+is_png+cutout alpha+placeholder 被引用) + `degrade`(健壮注入,page 参数) + `reset_failed`。 |
| `cli.py` | 顶部自插 sys.path;`validate`(含 build_request 预检 + 成本估算)/`gen`(原子持久化+run.log+degrade)/`gate`。 |
| `tests/` | conftest + 各单测 + E2E(per-test Handler) + env-gated 真 API 冒烟。 |
| `SKILL.md`/`INSTALL.md` | 编排(绝对基目录 + placeholder 约定 + 迭代上限兜底 + 降级 + 成本)/安装。 |

**Canonical names（全任务统一）:**
- `Asset`: `id, kind, prompt, aspect, transparent=False, resolution=None, format=None, path, placeholder, status="pending", task_id=None`
- `paths.validate_asset_path(path) -> None`（违规 raise `ValueError`）
- `registry.build_request(asset, cfg) -> dict`
- `transport.RetryableError(msg, retry_after=None)` / `transport.FatalError`;`Transport.post/get -> (status, dict, dict)`;`get_bytes(url) -> bytes`
- `ApimartClient(transport, api_key, base_url)`：`.submit(body)->str` / `.poll(task_id, timeout, interval, clock=time)->dict` / `.fetch_image(result, dest)->None`（无 url/b64 → `FatalError`）
- `engine.generate(manifest, project_dir, client, cfg, on_progress=None, clock=time) -> Manifest`;`engine.validate_download(asset, path)->None`
- `extractor.reconcile(code, manifest) -> ReconcileResult(ok, missing_in_manifest, not_wired)`;`extractor.UnsupportedMarkup`
- `wiring.verify(manifest, project_dir, code=None) -> list[str]`;`wiring.degrade(manifest, project_dir, page) -> int`;`wiring.reset_failed(manifest) -> None`

---

## Task 1: Config + 测试骨架

**Files:** Create `config.py`, `tests/conftest.py`; Test `tests/test_config.py`

- [ ] **Step 1: conftest（指向 skill 根）**

```python
# tests/conftest.py
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
```

- [ ] **Step 2: 失败测试**

```python
# tests/test_config.py
import pytest
from config import Config
def test_from_env_defaults(monkeypatch):
    monkeypatch.setenv("APIMART_API_KEY","sk-test")
    c=Config.from_env()
    assert c.api_key=="sk-test" and c.base_url=="https://api.apimart.ai/v1"
    assert c.default_bg_resolution=="2k" and c.asset_dir=="assets/gen"
    assert c.manifest_path==".art-director/manifest.json" and c.max_assets==12 and c.backoff_base==1.0
def test_from_env_missing(monkeypatch):
    monkeypatch.delenv("APIMART_API_KEY",raising=False)
    with pytest.raises(RuntimeError) as e: Config.from_env()
    assert "APIMART_API_KEY" in str(e.value)
```

- [ ] **Step 3: 跑失败** — `cd skills/art-director && python -m pytest tests/test_config.py -v` → FAIL（no module）。

- [ ] **Step 4: 实现**

```python
# config.py
import os
from dataclasses import dataclass
@dataclass
class Config:
    api_key: str
    base_url: str = "https://api.apimart.ai/v1"
    default_bg_resolution: str = "2k"
    concurrency: int = 4
    retries: int = 3
    backoff_base: float = 1.0
    poll_timeout: int = 180
    poll_interval: int = 3
    asset_dir: str = "assets/gen"
    manifest_path: str = ".art-director/manifest.json"
    max_assets: int = 12
    @classmethod
    def from_env(cls):
        k=os.environ.get("APIMART_API_KEY")
        if not k: raise RuntimeError("APIMART_API_KEY not set. export APIMART_API_KEY=sk-... (APImart bearer token).")
        return cls(api_key=k, default_bg_resolution=os.environ.get("ART_DIRECTOR_BG_RESOLUTION","2k"))
```

- [ ] **Step 5: 跑通过** — PASS (2). **Step 6: Commit** — `git commit -m "feat(art-director): Config + 测试骨架"`

---

## Task 2: 路径校验（无 .. 分量/无空格）+ Manifest（原子写 + registry 预检）

**Files:** Create `paths.py`, `manifest.py`; Test `tests/test_paths.py`, `tests/test_manifest.py`

- [ ] **Step 1: 失败测试(paths)**

```python
# tests/test_paths.py
import pytest
from paths import validate_asset_path
@pytest.mark.parametrize("ok",["assets/gen/a.png","assets/gen/sub/b.png"])
def test_valid(ok): validate_asset_path(ok)
@pytest.mark.parametrize("bad",[
    "/etc/passwd","assets/gen/../x.png","assets/gen/sub/../a.png","../x.png",
    "other/a.png","assets/gen/a.jpg","assets/gen/a.png\x00","assets/gen/a b.png","./assets/gen/a.png"])
def test_invalid(bad):
    with pytest.raises(ValueError): validate_asset_path(bad)
```

- [ ] **Step 2: 跑失败 → 实现 paths**

```python
# paths.py
def validate_asset_path(path: str) -> None:
    if not path or "\x00" in path: raise ValueError(f"bad path: {path!r}")
    if path.startswith(("/","\\")) or (len(path)>1 and path[1]==":"): raise ValueError(f"must be relative: {path!r}")
    parts=path.split("/")
    if "." in parts or ".." in parts: raise ValueError(f"no . or .. components: {path!r}")
    if any(ch.isspace() for ch in path): raise ValueError(f"no whitespace (extractor round-trip): {path!r}")
    if not path.startswith("assets/gen/"): raise ValueError(f"must be under assets/gen/: {path!r}")
    if not path.endswith(".png"): raise ValueError(f"must end with .png: {path!r}")
```

- [ ] **Step 3: 跑 paths 通过。**

- [ ] **Step 4: 失败测试(manifest)** — 覆盖 load/save_atomic 往返(含 task_id)、duplicate id、bad kind、cutout 非透明、bg 透明、bad path、max_assets、**registry 预检**(bad aspect 在 validate 即被抓,不拖到 gen)。

```python
# tests/test_manifest.py
import json, pytest
from manifest import Asset, Manifest
BG={"id":"hero-bg","kind":"bg","prompt":"neon","aspect":"16:9","resolution":"4k","transparent":False,"path":"assets/gen/hero-bg.png","placeholder":"url(assets/gen/hero-bg.png)"}
CUT={"id":"mascot","kind":"cutout","prompt":"fox","aspect":"2:3","transparent":True,"format":"png","path":"assets/gen/mascot.png","placeholder":'src="assets/gen/mascot.png"'}
def test_load_roundtrip(tmp_path):
    p=tmp_path/"m.json"; p.write_text(json.dumps({"version":1,"style":{"brief":"x"},"assets":[BG,CUT]}))
    m=Manifest.load(str(p)); assert [a.id for a in m.assets]==["hero-bg","mascot"] and m.assets[0].task_id is None
def test_save_atomic(tmp_path):
    m=Manifest(version=1,style={},assets=[Asset(**BG)]); m.assets[0].status="done"; m.assets[0].task_id="T1"
    d=tmp_path/"sub"/".art-director"/"m.json"; m.save_atomic(str(d))
    b=Manifest.load(str(d)); assert b.assets[0].status=="done" and b.assets[0].task_id=="T1"
def test_validate_ok(): assert Manifest(version=1,style={},assets=[Asset(**BG),Asset(**CUT)]).validate()==[]
def test_dup(): assert any("duplicate" in e for e in Manifest(version=1,style={},assets=[Asset(**BG),Asset(**{**CUT,"id":"hero-bg"})]).validate())
def test_bad_kind(): assert any("kind" in e for e in Manifest(version=1,style={},assets=[Asset(**{**BG,"kind":"video"})]).validate())
def test_cutout_not_transparent(): assert any("transparent" in e for e in Manifest(version=1,style={},assets=[Asset(**{**CUT,"transparent":False})]).validate())
def test_bg_transparent(): assert any("bg" in e and "transparent" in e for e in Manifest(version=1,style={},assets=[Asset(**{**BG,"transparent":True})]).validate())
def test_bad_path(): assert any("path" in e or ".." in e for e in Manifest(version=1,style={},assets=[Asset(**{**BG,"path":"assets/gen/../x.png"})]).validate())
def test_registry_precheck_bad_aspect():
    # cutout 只允许 1:1/2:3/3:2 — validate 必须在 gen 之前抓住(codex P2)
    assert any("aspect" in e for e in Manifest(version=1,style={},assets=[Asset(**{**CUT,"aspect":"16:9"})]).validate())
def test_max(): 
    big=[Asset(**{**BG,"id":f"b{i}","path":f"assets/gen/b{i}.png"}) for i in range(20)]
    assert any("max_assets" in e for e in Manifest(version=1,style={},assets=big).validate(max_assets=12))
```

- [ ] **Step 5: 跑失败 → 实现 manifest**

```python
# manifest.py
import json, os, tempfile
from dataclasses import dataclass, asdict
from typing import Optional
from paths import validate_asset_path
VALID_KINDS={"bg","cutout"}
@dataclass
class Asset:
    id: str; kind: str; prompt: str; aspect: str; path: str; placeholder: str
    transparent: bool=False; resolution: Optional[str]=None; format: Optional[str]=None
    status: str="pending"; task_id: Optional[str]=None
    def to_dict(self): return {k:v for k,v in asdict(self).items() if v is not None}
@dataclass
class Manifest:
    version: int; style: dict; assets: list
    @classmethod
    def load(cls, path):
        d=json.loads(open(path,encoding="utf-8").read())
        return cls(d.get("version",1), d.get("style",{}), [Asset(**a) for a in d.get("assets",[])])
    def save_atomic(self, path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        out={"version":self.version,"style":self.style,"assets":[a.to_dict() for a in self.assets]}
        fd,tmp=tempfile.mkstemp(dir=os.path.dirname(path) or ".", suffix=".tmp")
        with os.fdopen(fd,"w",encoding="utf-8") as f: f.write(json.dumps(out,ensure_ascii=False,indent=2))
        os.replace(tmp, path)
    def validate(self, max_assets=None):
        from registry import build_request   # 延迟导入避免环
        from config import Config
        errs,seen=[],set(); cfg=Config(api_key="x")
        if max_assets is not None and len(self.assets)>max_assets:
            errs.append(f"too many assets: {len(self.assets)} > max_assets={max_assets} (cost guard)")
        for a in self.assets:
            if a.id in seen: errs.append(f"duplicate asset id: {a.id}")
            seen.add(a.id)
            if a.kind not in VALID_KINDS: errs.append(f"asset {a.id}: invalid kind {a.kind!r}"); continue
            if a.kind=="cutout" and not a.transparent: errs.append(f"asset {a.id}: cutout must be transparent=true")
            if a.kind=="bg" and a.transparent: errs.append(f"asset {a.id}: bg cannot be transparent")
            if not a.placeholder: errs.append(f"asset {a.id}: placeholder required")
            try: validate_asset_path(a.path)
            except ValueError as e: errs.append(f"asset {a.id}: {e}")
            try: build_request(a, cfg)          # registry 预检:aspect/resolution 等(codex P2)
            except ValueError as e: errs.append(f"asset {a.id}: {e}")
        return errs
```

- [ ] **Step 6: 跑通过 → Commit** — `git commit -m "feat(art-director): 路径校验(无.. /无空格) + manifest(原子写 + registry 预检)"`

---

## Task 3: 注册表派发（修 _cut 双 kwarg 陷阱）

**Files:** Create `registry.py`; Test `tests/test_registry.py`

- [ ] **Step 1: 失败测试** — 🔴 `_cut`/`_bg` helper **不得**把 `aspect` 写死后又 `**k`（否则 `_cut(aspect=...)` 双传 → TypeError,M9）。用默认参数。

```python
# tests/test_registry.py
import pytest
from config import Config
from manifest import Asset
from registry import build_request
CFG=Config(api_key="k")
def _bg(aspect="16:9", **k): return Asset(id="b",kind="bg",prompt="p",aspect=aspect,path="assets/gen/b.png",placeholder="url(assets/gen/b.png)",**k)
def _cut(aspect="2:3", **k): return Asset(id="c",kind="cutout",prompt="p",aspect=aspect,transparent=True,format="png",path="assets/gen/c.png",placeholder='src="assets/gen/c.png"',**k)
def test_bg_shape(): assert build_request(_bg(),CFG)=={"model":"gpt-image-2","prompt":"p","size":"16:9","resolution":"2k","n":1}
def test_bg_override(): assert build_request(_bg(resolution="4k"),CFG)["resolution"]=="4k"
def test_bg_bad_res():
    with pytest.raises(ValueError): build_request(_bg(resolution="8k"),CFG)
def test_bg_transparent_rejected():
    with pytest.raises(ValueError): build_request(_bg(transparent=True),CFG)
def test_cutout_shape(): assert build_request(_cut(),CFG)=={"model":"gpt-image-1.5-official","prompt":"p","size":"2:3","background":"transparent","output_format":"png","n":1}
def test_cutout_res_rejected():
    with pytest.raises(ValueError): build_request(_cut(resolution="2k"),CFG)
def test_cutout_bad_size():
    with pytest.raises(ValueError): build_request(_cut(aspect="16:9"),CFG)   # 现在能真正跑到 build_request
```

- [ ] **Step 2: 跑失败 → 实现**

```python
# registry.py
from manifest import Asset
from config import Config
BG_RESOLUTIONS={"1k","2k","4k"}
BG_SIZES={"1:1","3:2","2:3","4:3","3:4","5:4","4:5","16:9","9:16","2:1","1:2","3:1","1:3","21:9","9:21","auto"}
CUTOUT_SIZES={"1:1","2:3","3:2"}
def build_bg_request(a,cfg):
    if a.transparent: raise ValueError(f"{a.id}: bg cannot be transparent")
    if a.aspect not in BG_SIZES: raise ValueError(f"{a.id}: bg aspect {a.aspect!r} invalid")
    res=a.resolution or cfg.default_bg_resolution
    if res not in BG_RESOLUTIONS: raise ValueError(f"{a.id}: bg resolution {res!r} must be 1k|2k|4k")
    return {"model":"gpt-image-2","prompt":a.prompt,"size":a.aspect,"resolution":res,"n":1}
def build_cutout_request(a,cfg):
    if a.resolution is not None: raise ValueError(f"{a.id}: cutout has no resolution tier")
    if a.aspect not in CUTOUT_SIZES: raise ValueError(f"{a.id}: cutout aspect {a.aspect!r} must be 1:1|2:3|3:2")
    return {"model":"gpt-image-1.5-official","prompt":a.prompt,"size":a.aspect,"background":"transparent","output_format":"png","n":1}
HANDLERS={"bg":build_bg_request,"cutout":build_cutout_request}
def build_request(a,cfg):
    h=HANDLERS.get(a.kind)
    if h is None: raise ValueError(f"{a.id}: no handler for kind {a.kind!r}")
    return h(a,cfg)
```

> **B3 未决:** 请求体/端点/状态/url 位置/模型选择,Task 11 真 API 冒烟前不视为已证。冒烟若不符 → 以冒烟为准回改本任务 + apimart。

- [ ] **Step 3: 跑通过 (7) → Commit** — `git commit -m "feat(art-director): 注册表派发(修 _cut 双kwarg + 文档请求体 + 拒透明bg/cutout分辨率)"`

---

## Task 4: Transport（返回 headers + 错误归一化）+ APImart（开放世界 + b64/Location 容忍）

**Files:** Create `transport.py`, `apimart.py`; Test `tests/test_transport.py`, `tests/test_apimart.py`

- [ ] **Step 1: 失败测试(transport)**

```python
# tests/test_transport.py
import urllib.error, io, pytest
from transport import UrllibTransport, RetryableError, FatalError
class Fake:
    def __init__(self, exc=None, status=200, body=b"{}", headers=None): self.exc=exc; self.status=status; self.body=body; self.headers=headers or {}
    def __call__(self, req, timeout=0):
        if self.exc: raise self.exc
        outer=self
        class R:
            status=outer.status; headers=outer.headers
            def read(s): return outer.body
            def __enter__(s): return s
            def __exit__(s,*a): return False
        return R()
def _err(code, h=None): return urllib.error.HTTPError("u",code,"m",h or {},io.BytesIO(b"{}"))
def test_429_retryable(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", Fake(exc=_err(429,{"Retry-After":"7"})))
    with pytest.raises(RetryableError) as e: UrllibTransport().get("http://x",{})
    assert e.value.retry_after==7
def test_503_retryable(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", Fake(exc=_err(503)))
    with pytest.raises(RetryableError): UrllibTransport().get("http://x",{})
def test_400_fatal(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", Fake(exc=_err(400)))
    with pytest.raises(FatalError): UrllibTransport().get("http://x",{})
def test_urlerror_retryable(monkeypatch):
    import urllib.error as ue; monkeypatch.setattr("urllib.request.urlopen", Fake(exc=ue.URLError("reset")))
    with pytest.raises(RetryableError): UrllibTransport().get("http://x",{})
def test_returns_headers(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", Fake(status=202, body=b"", headers={"Location":"/v1/tasks/T9"}))
    status, body, headers = UrllibTransport().post("http://x",{},{"a":1})
    assert status==202 and body=={} and headers.get("Location")=="/v1/tasks/T9"
```

- [ ] **Step 2: 实现 transport**

```python
# transport.py
import json, urllib.request, urllib.error
class RetryableError(Exception):
    def __init__(self,msg,retry_after=None): super().__init__(msg); self.retry_after=retry_after
class FatalError(Exception): pass
RETRYABLE_STATUS={408,409,425,429,500,502,503,504}
def _retry_after(h):
    try: return int(h.get("Retry-After")) if h and h.get("Retry-After") else None
    except (ValueError,TypeError): return None
def _do(req):
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw=r.read(); body=json.loads(raw.decode()) if raw else {}
            return r.status, body, dict(r.headers or {})
    except urllib.error.HTTPError as e:
        if e.code in RETRYABLE_STATUS: raise RetryableError(f"HTTP {e.code}", retry_after=_retry_after(e.headers))
        raise FatalError(f"HTTP {e.code}: {e.reason}")
    except urllib.error.URLError as e:
        raise RetryableError(f"network error: {e.reason}")
class UrllibTransport:
    def post(self,url,headers,body):
        req=urllib.request.Request(url,data=json.dumps(body).encode(),headers={**headers,"Content-Type":"application/json"},method="POST")
        return _do(req)
    def get(self,url,headers): return _do(urllib.request.Request(url,headers=headers,method="GET"))
    def get_bytes(self,url):
        try:
            with urllib.request.urlopen(url,timeout=120) as r: return r.read()
        except urllib.error.HTTPError as e:
            if e.code in RETRYABLE_STATUS: raise RetryableError(f"download HTTP {e.code}")
            raise FatalError(f"download HTTP {e.code}")
        except urllib.error.URLError as e: raise RetryableError(f"download network error: {e.reason}")
```

- [ ] **Step 3: 失败测试(apimart)** — 容忍 task_id/id/data.task_id/Location;poll 开放世界(有图非pending→done;running+url 不早成功;error 信封快失败;未知状态死等到 timeout);fetch_image 支持 url 与 b64(含 data 为 dict / 列表非首元素);无 url/b64 → FatalError。

```python
# tests/test_apimart.py
import base64, struct, zlib, pytest
from apimart import ApimartClient
from transport import FatalError, RetryableError
SIG=b"\x89PNG\r\n\x1a\n"
def _chunk(t,d): return struct.pack(">I",len(d))+t+d+struct.pack(">I",zlib.crc32(t+d)&0xffffffff)
def _png(): return SIG+_chunk(b"IHDR",struct.pack(">IIBBBBB",1,1,8,6,0,0,0))+_chunk(b"IDAT",b"\x00")+_chunk(b"IEND",b"")
class FakeT:
    def __init__(self, post=(200,{"task_id":"T1"},{}), polls=(), img=b""):
        self.post_r=post; self.polls=list(polls); self.img=img; self.posts=[]; self.gets=[]
    def post(self,u,h,b): self.posts.append((u,h,b)); return self.post_r
    def get(self,u,h): self.gets.append(u); return (200, self.polls.pop(0), {})
    def get_bytes(self,u): return self.img
class Clock:
    def __init__(self): self.t=0.0
    def monotonic(self): return self.t
    def sleep(self,s): self.t+=s
def _c(post=(200,{"task_id":"T1"},{}), polls=(), img=b""): return ApimartClient(FakeT(post,polls,img),"k","https://api.apimart.ai/v1")
def test_submit_task_id(): assert _c().submit({"model":"m"})=="T1"
def test_submit_id_alias(): assert _c(post=(200,{"id":"T2"},{})).submit({"model":"m"})=="T2"
def test_submit_nested(): assert _c(post=(200,{"data":{"task_id":"T3"}},{})).submit({"model":"m"})=="T3"
def test_submit_location(): assert _c(post=(202,{},{"Location":"/v1/tasks/T9"})).submit({"model":"m"})=="T9"
def test_submit_auth_header():
    c=_c(); c.submit({"model":"m"}); assert c.t.posts[0][1]["Authorization"]=="Bearer k" and c.t.posts[0][0].endswith("/images/generations")
def test_submit_none_fatal():
    with pytest.raises(FatalError): _c(post=(200,{"oops":1},{})).submit({"model":"m"})
def test_poll_terminal_with_url():
    r=_c(polls=[{"status":"running"},{"status":"succeeded","url":"u"}]).poll("T",10,0,clock=Clock()); assert r["url"]=="u"
def test_poll_url_present_unknown_status_is_done():
    r=_c(polls=[{"status":"weird-but-final","url":"u"}]).poll("T",10,0,clock=Clock()); assert r["url"]=="u"
def test_poll_url_but_running_keeps_waiting():
    r=_c(polls=[{"status":"running","url":"preview"},{"status":"succeeded","url":"final"}]).poll("T",10,0,clock=Clock()); assert r["url"]=="final"
def test_poll_error_envelope_fast_fail():
    with pytest.raises(FatalError): _c(polls=[{"error":{"message":"nsfw"}}]).poll("T",10,0,clock=Clock())
def test_poll_failure_status():
    with pytest.raises(FatalError): _c(polls=[{"status":"failed"}]).poll("T",10,0,clock=Clock())
def test_poll_timeout():
    with pytest.raises(TimeoutError): _c(polls=[{"status":"running"}]*99).poll("T",0,0,clock=Clock())
def test_fetch_image_url(tmp_path):
    c=_c(img=_png()); d=tmp_path/"a.png"; c.fetch_image({"url":"u"},str(d)); assert d.read_bytes().startswith(SIG)
def test_fetch_image_b64_data_list(tmp_path):
    c=_c(); d=tmp_path/"a.png"; c.fetch_image({"data":[{"b64_json":base64.b64encode(_png()).decode()}]},str(d)); assert d.read_bytes().startswith(SIG)
def test_fetch_image_data_dict_url(tmp_path):
    c=_c(img=_png()); d=tmp_path/"a.png"; c.fetch_image({"data":{"url":"u"}},str(d)); assert d.read_bytes().startswith(SIG)
def test_fetch_image_url_later_element(tmp_path):
    c=_c(img=_png()); d=tmp_path/"a.png"; c.fetch_image({"data":[{"foo":1},{"url":"u"}]},str(d)); assert d.read_bytes().startswith(SIG)
def test_fetch_image_none_fatal(tmp_path):
    with pytest.raises(FatalError): _c().fetch_image({"status":"succeeded"}, str(tmp_path/"a.png"))
```

- [ ] **Step 4: 实现 apimart**

```python
# apimart.py
import base64, time
from transport import FatalError, RetryableError
DONE_FAIL={"failed","error","canceled","cancelled","rejected","timeout","expired","content_policy_violation","moderation_blocked"}
NON_TERMINAL={"running","queued","pending","processing","in_progress","started","submitted","waiting","created"}
def _task_id(d):
    if isinstance(d.get("task_id"),str): return d["task_id"]
    if isinstance(d.get("id"),str): return d["id"]
    inner=d.get("data")
    if isinstance(inner,dict) and isinstance(inner.get("task_id"),str): return inner["task_id"]
    return None
def _extract_image(r):
    """-> ('url',str) | ('b64',str) | None"""
    if isinstance(r.get("url"),str): return ("url",r["url"])
    if isinstance(r.get("b64_json"),str): return ("b64",r["b64_json"])
    for key in ("data","images","results","output"):
        v=r.get(key)
        if isinstance(v,dict): v=[v]
        if isinstance(v,list):
            for it in v:
                if isinstance(it,dict):
                    if isinstance(it.get("url"),str): return ("url",it["url"])
                    if isinstance(it.get("b64_json"),str): return ("b64",it["b64_json"])
    return None
def _is_failure(d):
    if str(d.get("status","")).lower() in DONE_FAIL: return True
    if d.get("error"): return True
    code=d.get("code") or d.get("status_code")
    if isinstance(code,int) and code>=400 and code!=429: return True
    return False
class ApimartClient:
    def __init__(self,transport,api_key,base_url):
        self.t=transport; self.base=base_url.rstrip("/"); self.headers={"Authorization":f"Bearer {api_key}"}
    def submit(self,body):
        status,data,headers=self.t.post(f"{self.base}/images/generations",self.headers,body)
        tid=_task_id(data)
        if not tid and headers:
            loc=headers.get("Location") or headers.get("location")
            if loc: tid=loc.rstrip("/").rsplit("/",1)[-1]
        if not tid: raise FatalError(f"submit: no task_id/id/Location (keys={sorted(data.keys())})")
        return tid
    def poll(self,task_id,timeout,interval,clock=time):
        deadline=clock.monotonic()+timeout
        while True:
            _,data,_=self.t.get(f"{self.base}/tasks/{task_id}",self.headers)
            if _is_failure(data): raise FatalError(f"task {task_id} failed: status={data.get('status')} err={bool(data.get('error'))}")
            st=str(data.get("status","")).lower()
            img=_extract_image(data)
            if img is not None and st not in NON_TERMINAL: return data     # 有图且非显式 pending → 完成
            if clock.monotonic()>=deadline: raise TimeoutError(f"task {task_id} not done within {timeout}s (status={st!r})")
            if interval: clock.sleep(interval)
    def fetch_image(self,result,dest):
        img=_extract_image(result)
        if img is None: raise FatalError("no image url/b64 in result")
        kind,val=img
        data=self.t.get_bytes(val) if kind=="url" else base64.b64decode(val)
        with open(dest,"wb") as f: f.write(data)
```

> **密钥不入日志(m6):** 错误只暴露 status/keys/bool,绝不内插原始 body/headers。

- [ ] **Step 5: 跑通过 → Commit** — `git commit -m "feat(art-director): Transport(返headers+归一化) + APImart(开放世界状态+b64/Location/dict容忍)"`

---

## Task 5: PNG 校验（签名 + palette/tRNS）

**Files:** Create `pngutil.py`; Test `tests/test_pngutil.py`

- [ ] **Step 1: 失败测试** — ct6/4 真;ct2 无 tRNS 假;ct3+tRNS 真;ct2+tRNS 真;非PNG假;`is_png`。

```python
# tests/test_pngutil.py
import struct, zlib
from pngutil import png_has_alpha, is_png
SIG=b"\x89PNG\r\n\x1a\n"
def _chunk(t,d): return struct.pack(">I",len(d))+t+d+struct.pack(">I",zlib.crc32(t+d)&0xffffffff)
def _png(ct,trns=False):
    o=SIG+_chunk(b"IHDR",struct.pack(">IIBBBBB",1,1,8,ct,0,0,0))
    if trns: o+=_chunk(b"tRNS",b"\x00")
    return o+_chunk(b"IDAT",b"\x00")+_chunk(b"IEND",b"")
def _w(t,b): p=t/"a.png"; p.write_bytes(b); return str(p)
def test_ct6(tmp_path): assert png_has_alpha(_w(tmp_path,_png(6))) is True
def test_ct4(tmp_path): assert png_has_alpha(_w(tmp_path,_png(4))) is True
def test_ct2_no(tmp_path): assert png_has_alpha(_w(tmp_path,_png(2))) is False
def test_ct3_trns(tmp_path): assert png_has_alpha(_w(tmp_path,_png(3,trns=True))) is True
def test_ct2_trns(tmp_path): assert png_has_alpha(_w(tmp_path,_png(2,trns=True))) is True
def test_not_png(tmp_path): assert png_has_alpha(_w(tmp_path,b"JFIF")) is False
def test_is_png(tmp_path): assert is_png(_w(tmp_path,_png(6))) and not is_png(_w(tmp_path,b"x"))
```

- [ ] **Step 2: 实现**

```python
# pngutil.py
import struct
PNG_SIG=b"\x89PNG\r\n\x1a\n"
def is_png(path):
    with open(path,"rb") as f: return f.read(8)==PNG_SIG
def png_has_alpha(path):
    with open(path,"rb") as f:
        if f.read(8)!=PNG_SIG: return False
        ln=f.read(4)
        if len(ln)<4 or f.read(4)!=b"IHDR": return False
        ihdr=f.read(struct.unpack(">I",ln)[0]); f.read(4)
        if ihdr[9] in (4,6): return True
        while True:
            ln=f.read(4)
            if len(ln)<4: return False
            typ=f.read(4); size=struct.unpack(">I",ln)[0]
            if typ==b"tRNS": return True
            if typ in (b"IDAT",b"IEND"): return False
            f.seek(size+4,1)
```

- [ ] **Step 3: 跑通过 (7) → Commit** — `git commit -m "feat(art-director): PNG 校验(签名 + palette/tRNS alpha)"`

---

## Task 6: 引擎（task_id 状态机 + temp→validate→replace + 逐转移持久化）

**Files:** Create `engine.py`; Test `tests/test_engine.py`

**状态机（核心）:** 已提交=已付费。
- submit 成功 → 立刻持久化 task_id（kill 不丢付费任务）。
- poll/download 期 `RetryableError` 或 `TimeoutError` → **保留 task_id,续轮询同一任务**（不重复付费）。
- submit 期 `RetryableError` → task_id 仍 None → 重试 submit。
- 终态 `FatalError` / 校验 `ValueError` → **清 task_id + unlink .part**,raise → failed（resume 重生）。
- honor `retry_after`。

- [ ] **Step 1: 失败测试**

```python
# tests/test_engine.py
import base64, struct, zlib, pytest
from config import Config
from manifest import Asset, Manifest
from engine import generate, validate_download
from transport import RetryableError, FatalError
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
class Clock:
    def __init__(self): self.t=0.0; self.sleeps=[]
    def monotonic(self): return self.t
    def sleep(self,s): self.sleeps.append(s); self.t+=s
class FakeClient:
    def __init__(self, behaviors=None): self.behaviors=behaviors or {}; self.submits=[]; self.polls=[]
    def submit(self,body):
        self.submits.append(body["model"])
        b=self.behaviors.get(body["model"],{})
        if b.get("submit_raise"): raise b["submit_raise"]
        return f"T-{body['model']}-{len(self.submits)}"
    def poll(self,tid,timeout,interval,clock=None):
        self.polls.append(tid); 
        b=next((v for k,v in self.behaviors.items() if k in tid),{})
        if b.get("poll_raise") and len([p for p in self.polls if p==tid])<=b.get("poll_raise_times",99): raise b["poll_raise"]
        return {"status":"succeeded","_model":tid}
    def fetch_image(self,result,dest):
        png=_png(6) if "1.5" in result["_model"] else _png(2)
        open(dest,"wb").write(png)
def test_validate_download_non_png(tmp_path):
    p=tmp_path/"x.png"; p.write_bytes(b"<html>err")
    with pytest.raises(Exception): validate_download(Asset(id="b",kind="bg",prompt="p",aspect="16:9",path="assets/gen/b.png",placeholder="x"), str(p))
def test_validate_download_cutout_no_alpha(tmp_path):
    p=tmp_path/"c.png"; p.write_bytes(_png(2))
    with pytest.raises(Exception): validate_download(Asset(id="c",kind="cutout",prompt="p",aspect="2:3",transparent=True,path="assets/gen/c.png",placeholder="x"), str(p))
def test_all_done(tmp_path):
    m=generate(_man(), str(tmp_path), FakeClient(), CFG)
    assert (tmp_path/"assets/gen/b.png").exists() and all(a.status=="done" for a in m.assets)
    assert not (tmp_path/"assets/gen/b.png.part").exists()    # 临时文件已清
def test_submit_fail_isolated(tmp_path):
    c=FakeClient({"gpt-image-2":{"submit_raise":RetryableError("boom")}})
    m=generate(_man(), str(tmp_path), c, CFG)
    by={a.id:a.status for a in m.assets}; assert by["b"]=="failed" and by["c"]=="done"
    assert c.submits.count("gpt-image-2")==1+CFG.retries and c.submits.count("gpt-image-1.5-official")==1
def test_poll_retry_reuses_task_no_rebill(tmp_path):
    # poll 第一次 RetryableError,第二次成功 → 不得重新 submit(不重复付费,M2)
    c=FakeClient({"gpt-image-2":{"poll_raise":RetryableError("flaky"),"poll_raise_times":1}})
    m=generate(_man(), str(tmp_path), c, CFG)
    assert {a.id:a.status for a in m.assets}["b"]=="done"
    assert c.submits.count("gpt-image-2")==1               # 只 submit 一次
def test_terminal_fail_clears_task_id(tmp_path):
    c=FakeClient({"gpt-image-2":{"poll_raise":FatalError("rejected"),"poll_raise_times":99}})
    m=generate(_man(), str(tmp_path), c, CFG)
    a=[x for x in m.assets if x.id=="b"][0]; assert a.status=="failed" and a.task_id is None   # resume 可重生(M1)
def test_resume_reuses_existing_task(tmp_path):
    c=FakeClient(); m=_man(); m.assets[0].task_id="T-existing"
    generate(m, str(tmp_path), c, CFG); assert "gpt-image-2" not in c.submits
def test_progress_incremental(tmp_path):
    saved=[]; generate(_man(), str(tmp_path), FakeClient(), CFG, on_progress=lambda m:saved.append([a.status for a in m.assets]))
    assert len(saved)>=2 and any(s.count("done")==1 for s in saved)   # 中间快照,非单次终态(M-test-strength)
def test_backoff_honors_clock(tmp_path):
    clk=Clock(); cfg=Config(api_key="k",concurrency=1,retries=2,backoff_base=2,poll_timeout=10,poll_interval=0)
    c=FakeClient({"gpt-image-2":{"submit_raise":RetryableError("x")},"gpt-image-1.5-official":{"submit_raise":RetryableError("y")}})
    generate(_man(), str(tmp_path), c, cfg, clock=clk)
    assert 2 in clk.sleeps and 4 in clk.sleeps                # base, base*2 退避真被执行(fake-clock 覆盖)
def test_retry_after_honored(tmp_path):
    clk=Clock(); cfg=Config(api_key="k",concurrency=1,retries=1,backoff_base=2,poll_timeout=10,poll_interval=0)
    c=FakeClient({"gpt-image-2":{"submit_raise":RetryableError("x",retry_after=9)},"gpt-image-1.5-official":{"submit_raise":RetryableError("y",retry_after=9)}})
    generate(_man(), str(tmp_path), c, cfg, clock=clk)
    assert 9 in clk.sleeps                                    # retry_after 覆盖退避
```

- [ ] **Step 2: 实现**

```python
# engine.py
import os, time, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from registry import build_request
from transport import RetryableError
from pngutil import is_png, png_has_alpha
def _abs(d,r): return os.path.join(d,r)
def _safe_unlink(p):
    try: os.unlink(p)
    except OSError: pass
def validate_download(asset,dest):
    if not os.path.exists(dest) or os.path.getsize(dest)==0: raise ValueError(f"{asset.id}: empty download")
    if not is_png(dest): raise ValueError(f"{asset.id}: download not a PNG (likely error page)")
    if asset.kind=="cutout" and not png_has_alpha(dest): raise ValueError(f"{asset.id}: cutout without alpha")
def _sleep(clock,cfg,attempt,retry_after):
    if attempt>=cfg.retries: return
    delay=retry_after if retry_after else cfg.backoff_base*(2**attempt)
    if delay: clock.sleep(min(delay,30))
def _gen_one(asset,project_dir,client,cfg,clock,persist):
    body=build_request(asset,cfg)             # ValueError → 不重试
    last=None
    for attempt in range(cfg.retries+1):
        try:
            if not asset.task_id:
                asset.task_id=client.submit(body); persist()      # 提交即持久化(kill 不丢付费任务)
            result=client.poll(asset.task_id,cfg.poll_timeout,cfg.poll_interval,clock=clock)
            dest=_abs(project_dir,asset.path); os.makedirs(os.path.dirname(dest),exist_ok=True)
            tmp=dest+".part"; client.fetch_image(result,tmp); validate_download(asset,tmp); os.replace(tmp,dest)
            return "done"
        except RetryableError as e:
            last=e; _sleep(clock,cfg,attempt,e.retry_after)       # task_id 若已有则保留 → 续轮询;submit 期则 None → 重试 submit
        except TimeoutError as e:
            last=e; _sleep(clock,cfg,attempt,None)                # poll 超时:任务可能仍在跑 → 续轮询同一 task
        except Exception:                                         # FatalError / 校验失败 → 终态
            asset.task_id=None; _safe_unlink(_abs(project_dir,asset.path)+".part"); raise
    asset.task_id=None; _safe_unlink(_abs(project_dir,asset.path)+".part"); raise last  # 重试用尽:也清 task_id 以便重生
def generate(manifest,project_dir,client,cfg,on_progress=None,clock=time):
    todo=[a for a in manifest.assets if not (a.status=="done" and os.path.exists(_abs(project_dir,a.path)))]
    lock=threading.Lock()
    def persist():
        if on_progress:
            with lock: on_progress(manifest)
    with ThreadPoolExecutor(max_workers=cfg.concurrency) as ex:
        futs={ex.submit(_gen_one,a,project_dir,client,cfg,clock,persist):a for a in todo}
        for fut in as_completed(futs):
            a=futs[fut]
            try: a.status=fut.result()
            except Exception: a.status="failed"
            persist()
    return manifest
```

> 注:`persist` 在 submit 后与每素材完成后都调用 → in-flight task_id 落盘（M-persist）。失败素材 task_id 已清 → resume 重生（M1）。

- [ ] **Step 3: 跑通过 → Commit** — `git commit -m "feat(art-director): 引擎(task_id付费状态机+temp→validate→replace+逐转移持久化+退避honor retry_after)"`

---

## Task 7: Extractor（placeholder 包含契约 + 承载资产的硬失败）

**Files:** Create `extractor.py`; Test `tests/test_extractor.py`

**契约（v3 核心改动）:** 不解析 HTML 判"被引用"。`reconcile`:
- **forward（被引用）:** manifest 每个 asset 的 `placeholder` 字符串必须**原样包含**在 code 里 → 大小写/引号/空格全免疫。
- **reverse（漏登记）:** 扫 code 里所有 `assets/gen/...` token(归一 `./`,`/`),不属于任一 manifest path → 失败。
- **UnsupportedMarkup:** 只在**承载 `assets/gen` 的** srcset/image-set/`<source>`/JSX/Tailwind 上硬失败（非整页;合法的非资产 srcset 不拦）。

- [ ] **Step 1: 失败测试**

```python
# tests/test_extractor.py
import pytest
from manifest import Asset, Manifest
from extractor import reconcile, UnsupportedMarkup
def _man(items):  # items: list of (path, placeholder)
    return Manifest(version=1,style={},assets=[Asset(id=f"a{i}",kind="bg",prompt="p",aspect="16:9",path=p,placeholder=ph) for i,(p,ph) in enumerate(items)])
def test_ok_containment():
    m=_man([("assets/gen/h.png","url(assets/gen/h.png)"),("assets/gen/m.png",'src="assets/gen/m.png"')])
    code='<style>.h{background-image:url(assets/gen/h.png)}</style><img src="assets/gen/m.png">'
    assert reconcile(code,m).ok
def test_case_quote_space_immune():
    # placeholder 原样在 code 里 → 即使是 SRC= 也只看包含,不受正则大小写影响
    m=_man([("assets/gen/m.png",'SRC="assets/gen/m.png"')])
    assert reconcile('<img SRC="assets/gen/m.png">', m).ok
def test_not_wired():
    m=_man([("assets/gen/h.png","url(assets/gen/h.png)")])
    r=reconcile("<div>no assets here</div>", m); assert not r.ok and "assets/gen/h.png" in r.not_wired
def test_missing_in_manifest():
    m=_man([("assets/gen/h.png","url(assets/gen/h.png)")])
    code='<style>.h{background:url(assets/gen/h.png)}</style><img src="./assets/gen/ghost.png">'
    r=reconcile(code,m); assert not r.ok and "assets/gen/ghost.png" in r.missing_in_manifest
def test_external_ignored():
    m=_man([("assets/gen/h.png","url(assets/gen/h.png)")])
    assert reconcile('<style>.h{background:url(assets/gen/h.png)}</style><img src="https://cdn/x.png">', m).ok
def test_unsupported_only_when_asset_bearing():
    m=_man([("assets/gen/h.png","url(assets/gen/h.png)")])
    # 合法非资产 srcset(CDN logo)不应拦截
    code='<style>.h{background:url(assets/gen/h.png)}</style><img srcset="https://cdn/logo.png 1x">'
    assert reconcile(code,m).ok
@pytest.mark.parametrize("bad",[
    '<img srcset="assets/gen/a.png 1x">',
    '<source srcset="assets/gen/a.png">',
    '<div style="background:image-set(url(assets/gen/a.png) 1x)">',
    '<img src={heroBg} data-asset="assets/gen/a.png">',
    '<div class="bg-[url(assets/gen/a.png)]">'])
def test_asset_bearing_unsupported_hard_fails(bad):
    m=_man([("assets/gen/a.png","x")])
    with pytest.raises(UnsupportedMarkup): reconcile(bad,m)
```

- [ ] **Step 2: 实现**

```python
# extractor.py
import re
from dataclasses import dataclass
class UnsupportedMarkup(Exception): pass
# 承载 assets/gen 的不支持构造 → 硬失败(只在带资产时)
_UNSUPPORTED=[
    (re.compile(r"srcset\s*=\s*['\"][^'\"]*assets/gen/", re.I), "srcset"),
    (re.compile(r"<source\b[^>]*assets/gen/", re.I), "<source>"),
    (re.compile(r"image-set\(\s*[^)]*assets/gen/", re.I), "image-set()"),
    (re.compile(r"src\s*=\s*\{[^}]*}.{0,40}assets/gen/|data-asset\s*=\s*['\"][^'\"]*assets/gen/", re.I), "JSX/data-asset"),
    (re.compile(r"bg-\[url\([^\]]*assets/gen/", re.I), "Tailwind bg-[url()]"),
]
# reverse:扫 assets/gen token(归一 ./ 与 /)
_TOKEN=re.compile(r"((?:\./|/)?assets/gen/[A-Za-z0-9_./-]+\.png)")
def _norm(t):
    if t.startswith("./"): t=t[2:]
    if t.startswith("/"): t=t[1:]
    return t
@dataclass
class ReconcileResult:
    ok: bool; missing_in_manifest: set; not_wired: set
def reconcile(code, manifest) -> ReconcileResult:
    for pat,name in _UNSUPPORTED:
        if pat.search(code):
            raise UnsupportedMarkup(f"asset wired via unsupported {name}; use literal url(assets/gen/<id>.png) / src=\"assets/gen/<id>.png\"")
    not_wired={a.path for a in manifest.assets if a.placeholder not in code}      # forward: containment
    tokens={_norm(m.group(1)) for m in _TOKEN.finditer(code)}                      # reverse: token scan
    manifest_paths={a.path for a in manifest.assets}
    missing=tokens-manifest_paths
    return ReconcileResult(ok=not not_wired and not missing, missing_in_manifest=missing, not_wired=not_wired)
```

- [ ] **Step 3: 跑通过 → Commit** — `git commit -m "feat(art-director): extractor 改 placeholder 包含契约 + 承载资产的硬失败(非整页)"`

---

## Task 8: Wiring（终验含被引用 + degrade 健壮 + reset_failed）

**Files:** Create `wiring.py`; Test `tests/test_wiring.py`

- [ ] **Step 1: 失败测试** — 全好(含 code 被引用);缺文件;非PNG bg;cutout 无 alpha;manifest placeholder 未现于 code → 失败;`reset_failed`;`degrade` 在**有/无 `</head>`** 两种页面都注入兜底(body 底色 + 注释)且返回数;page 参数生效。

```python
# tests/test_wiring.py
import struct, zlib, pytest
from manifest import Asset, Manifest
from wiring import verify, degrade, reset_failed
SIG=b"\x89PNG\r\n\x1a\n"
def _chunk(t,d): return struct.pack(">I",len(d))+t+d+struct.pack(">I",zlib.crc32(t+d)&0xffffffff)
def _png(ct): return SIG+_chunk(b"IHDR",struct.pack(">IIBBBBB",1,1,8,ct,0,0,0))+_chunk(b"IDAT",b"\x00")+_chunk(b"IEND",b"")
def _man():
    return Manifest(version=1,style={},assets=[
        Asset(id="b",kind="bg",prompt="p",aspect="16:9",path="assets/gen/b.png",placeholder="url(assets/gen/b.png)",status="done"),
        Asset(id="c",kind="cutout",prompt="p",aspect="2:3",transparent=True,format="png",path="assets/gen/c.png",placeholder='src="assets/gen/c.png"',status="done")])
def _w(t,rel,d): p=t/rel; p.parent.mkdir(parents=True,exist_ok=True); p.write_bytes(d); return p
CODE='<style>.h{background-image:url(assets/gen/b.png)}</style><img src="assets/gen/c.png">'
def test_verify_good(tmp_path):
    _w(tmp_path,"assets/gen/b.png",_png(2)); _w(tmp_path,"assets/gen/c.png",_png(6))
    assert verify(_man(),str(tmp_path),code=CODE)==[]
def test_verify_not_wired(tmp_path):
    _w(tmp_path,"assets/gen/b.png",_png(2)); _w(tmp_path,"assets/gen/c.png",_png(6))
    assert any("wired" in p or "referenced" in p for p in verify(_man(),str(tmp_path),code='<img src="assets/gen/c.png">'))
def test_verify_missing(tmp_path):
    _w(tmp_path,"assets/gen/c.png",_png(6))
    assert any("b" in p and "missing" in p for p in verify(_man(),str(tmp_path)))
def test_verify_bg_not_png(tmp_path):
    _w(tmp_path,"assets/gen/b.png",b"<html>"); _w(tmp_path,"assets/gen/c.png",_png(6))
    assert any("b" in p and "PNG" in p for p in verify(_man(),str(tmp_path)))
def test_verify_cutout_no_alpha(tmp_path):
    _w(tmp_path,"assets/gen/b.png",_png(2)); _w(tmp_path,"assets/gen/c.png",_png(2))
    assert any("c" in p and "alpha" in p for p in verify(_man(),str(tmp_path)))
def test_reset_failed():
    m=_man(); m.assets[0].status="failed"; reset_failed(m); assert m.assets[0].status=="pending"
def test_degrade_with_head(tmp_path):
    (tmp_path/"index.html").write_text("<!doctype html><html><head><title>x</title></head><body>"+CODE+"</body></html>")
    m=_man(); m.assets[0].status="failed"
    n=degrade(m,str(tmp_path),"index.html"); html=(tmp_path/"index.html").read_text()
    assert n==1 and "art-director: asset 'b'" in html and "background-color" in html and "<style>" in html
def test_degrade_no_head_still_protects(tmp_path):
    (tmp_path/"page.html").write_text(CODE)   # 无 </head>,且文件名非 index.html
    m=_man(); m.assets[0].status="failed"
    n=degrade(m,str(tmp_path),"page.html"); html=(tmp_path/"page.html").read_text()
    assert n==1 and "background-color" in html   # 仍注入(前置),不白屏
def test_degrade_none_when_no_failed(tmp_path):
    (tmp_path/"index.html").write_text(CODE)
    assert degrade(_man(),str(tmp_path),"index.html")==0
```

- [ ] **Step 2: 实现**

```python
# wiring.py
import os, re
from pngutil import is_png, png_has_alpha
_HEAD=re.compile(r"</head\s*>", re.I)
def verify(manifest, project_dir, code=None):
    problems=[]
    for a in manifest.assets:
        dest=os.path.join(project_dir,a.path)
        if not os.path.exists(dest): problems.append(f"asset {a.id}: file missing at {a.path}"); continue
        if os.path.getsize(dest)==0: problems.append(f"asset {a.id}: file empty at {a.path}"); continue
        if not is_png(dest): problems.append(f"asset {a.id}: not a valid PNG at {a.path}"); continue
        if a.kind=="cutout" and not png_has_alpha(dest): problems.append(f"asset {a.id}: cutout has no alpha ({a.path})")
    if code is not None:
        for a in manifest.assets:
            if a.placeholder not in code: problems.append(f"asset {a.id}: placeholder not wired/referenced in code")
    return problems
def reset_failed(manifest):
    for a in manifest.assets:
        if a.status=="failed": a.status="pending"
def degrade(manifest, project_dir, page) -> int:
    path=os.path.join(project_dir,page)
    if not os.path.exists(path): return 0
    failed=[a for a in manifest.assets if a.status=="failed"]
    if not failed: return 0
    html=open(path,encoding="utf-8").read()
    rules=["body{background-color:#1a1a1a;}"]   # 兜底:即使具体规则没命中也不白屏
    for a in failed:
        rules.append(f"img[src*='{a.path}'],[style*='{a.path}']{{background-color:#222 !important;}}")
    comments="".join(f"<!-- art-director: asset '{a.id}' failed to generate; using fallback -->\n" for a in failed)
    block=comments+"<style>\n"+"\n".join(rules)+"\n</style>\n"
    m=_HEAD.search(html)
    html=(html[:m.start()]+block+html[m.start():]) if m else (block+html)   # 大小写/空白容忍;缺 head 则前置
    open(path,"w",encoding="utf-8").write(html)
    return len(failed)
```

- [ ] **Step 3: 跑通过 → Commit** — `git commit -m "feat(art-director): wiring(被引用=placeholder包含 + degrade健壮含body兜底+page参数 + reset_failed)"`

---

## Task 9: CLI（自插 sys.path + 预检/成本 + 原子持久化 + run.log + degrade + --page + 外部cwd子进程）

**Files:** Create `cli.py`; Test `tests/test_cli.py`

- [ ] **Step 1: 失败测试** — 含 run.log 落地、`--page` 透传、degrade 返回值被检查、**符号链 + `PYTHONPATH=''` 的子进程测试**(真正压 self-insert 那条线,M-packaging)。

```python
# tests/test_cli.py
import json, struct, zlib, sys, os, subprocess, pathlib, pytest
from cli import main
SIG=b"\x89PNG\r\n\x1a\n"
def _chunk(t,d): return struct.pack(">I",len(d))+t+d+struct.pack(">I",zlib.crc32(t+d)&0xffffffff)
def _png(ct): return SIG+_chunk(b"IHDR",struct.pack(">IIBBBBB",1,1,8,ct,0,0,0))+_chunk(b"IDAT",b"\x00")+_chunk(b"IEND",b"")
ROOT=pathlib.Path(__file__).resolve().parents[1]
MAN={"version":1,"style":{"brief":"x"},"assets":[
 {"id":"b","kind":"bg","prompt":"p","aspect":"16:9","resolution":"2k","path":"assets/gen/b.png","placeholder":"url(assets/gen/b.png)","status":"done"},
 {"id":"c","kind":"cutout","prompt":"p","aspect":"2:3","transparent":True,"format":"png","path":"assets/gen/c.png","placeholder":'src="assets/gen/c.png"',"status":"done"}]}
def test_validate_errors(tmp_path, capsys):
    mp=tmp_path/"m.json"; mp.write_text(json.dumps({**MAN,"assets":[{**MAN["assets"][0],"kind":"video"}]}))
    assert main(["validate","--manifest",str(mp)])!=0 and "kind" in capsys.readouterr().out
def test_validate_cost(tmp_path, capsys):
    mp=tmp_path/"m.json"; mp.write_text(json.dumps(MAN)); main(["validate","--manifest",str(mp)])
    assert "cost" in capsys.readouterr().out.lower()
def test_validate_reconcile(tmp_path):
    mp=tmp_path/"m.json"; mp.write_text(json.dumps(MAN))
    code=tmp_path/"index.html"; code.write_text('<img src="assets/gen/b.png"><div style="background:url(assets/gen/c.png)">')
    assert main(["validate","--manifest",str(mp),"--code",str(code)])==0
def test_gate_alpha(tmp_path, capsys):
    mp=tmp_path/"m.json"; mp.write_text(json.dumps(MAN))
    (tmp_path/"assets/gen").mkdir(parents=True)
    (tmp_path/"assets/gen/b.png").write_bytes(_png(2)); (tmp_path/"assets/gen/c.png").write_bytes(_png(2))
    assert main(["gate","--manifest",str(mp),"--project-dir",str(tmp_path)])!=0 and "alpha" in capsys.readouterr().out
def test_foreign_cwd_via_symlink(tmp_path):
    # 真压 self-insert:把 cli.py 软链到一个【无 sibling 模块】的目录,清空 PYTHONPATH,从无关 cwd 跑
    link_dir=tmp_path/"linkdir"; link_dir.mkdir()
    link=link_dir/"cli.py"
    try: os.symlink(ROOT/"cli.py", link)
    except (OSError,NotImplementedError): pytest.skip("symlink unsupported")
    mp=tmp_path/"m.json"; mp.write_text(json.dumps({**MAN,"assets":[{**MAN["assets"][0],"kind":"video"}]}))
    env={**os.environ,"PYTHONPATH":""}
    r=subprocess.run([sys.executable,str(link),"validate","--manifest",str(mp)],cwd=str(tmp_path),capture_output=True,text=True,env=env)
    assert r.returncode!=0 and "kind" in r.stdout   # realpath 跟随软链找到真 skill 根 → sibling import 成立
```

- [ ] **Step 2: 实现**

```python
# cli.py
import os, sys
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))   # realpath 跟随软链 → 真 skill 根(symlink/-m 健壮)
import argparse, json, datetime
from config import Config
from manifest import Manifest
from extractor import reconcile, UnsupportedMarkup
from wiring import verify, degrade, reset_failed
# 粗略成本(USD,as-of 2026-06 §6 文档区间;非权威,见 run.log 实际)
_COST={"bg":{"1k":0.02,"2k":0.06,"4k":0.21},"cutout":0.06}
def _estimate(m): return sum(_COST["cutout"] if a.kind=="cutout" else _COST["bg"].get(a.resolution or "2k",0.06) for a in m.assets)
def _log(project_dir, line):
    p=os.path.join(project_dir,".art-director","run.log"); os.makedirs(os.path.dirname(p),exist_ok=True)
    with open(p,"a",encoding="utf-8") as f: f.write(line+"\n")
def _cmd_validate(args):
    m=Manifest.load(args.manifest); errs=m.validate(max_assets=Config(api_key="x").max_assets)
    for e in errs: print(f"[manifest] {e}")
    print(f"[cost] estimated ~${_estimate(m):.2f} for {len(m.assets)} assets (as-of 2026-06 prices; see run.log for actual)")
    if args.code:
        try: r=reconcile(open(args.code,encoding="utf-8").read(), m)
        except UnsupportedMarkup as e: print(f"[reconcile] {e}"); errs.append("unsupported")
        else:
            for p in sorted(r.missing_in_manifest): print(f"[reconcile] code refs {p} without manifest entry"); errs.append("x")
            for p in sorted(r.not_wired): print(f"[reconcile] manifest {p} placeholder not wired in code"); errs.append("x")
    print("VALIDATE: FAIL" if errs else "VALIDATE: PASS"); return 1 if errs else 0
def _cmd_gen(args):
    from transport import UrllibTransport
    from apimart import ApimartClient
    from engine import generate
    cfg=Config.from_env()
    if args.bg_resolution: cfg.default_bg_resolution=args.bg_resolution
    m=Manifest.load(args.manifest); errs=m.validate(max_assets=cfg.max_assets)
    if errs:
        for e in errs: print(f"[manifest] {e}")
        print("GEN: FAIL (invalid manifest)"); return 1
    mp_out=os.path.join(args.project_dir,cfg.manifest_path)
    client=ApimartClient(UrllibTransport(),cfg.api_key,cfg.base_url)
    def persist(man): man.save_atomic(mp_out)
    _log(args.project_dir, f"{datetime.datetime.now().isoformat()} gen start: {len(m.assets)} assets, est ${_estimate(m):.2f}")
    m=generate(m,args.project_dir,client,cfg,on_progress=persist); persist(m)
    for a in m.assets: _log(args.project_dir, f"  {a.id} kind={a.kind} status={a.status} task_id={a.task_id}")
    failed=[a.id for a in m.assets if a.status!="done"]
    if failed:
        n=degrade(m,args.project_dir,args.page)
        print(f"GEN: PARTIAL — failed {failed} ({n} degraded in {args.page}; rerun to resume)"); return 1
    print("GEN: PASS"); return 0
def _cmd_gate(args):
    m=Manifest.load(args.manifest)
    code=open(args.code,encoding="utf-8").read() if args.code else None
    probs=verify(m,args.project_dir,code=code)
    for p in probs: print(f"[gate] {p}")
    if probs:
        reset_failed(m); m.save_atomic(os.path.join(args.project_dir,Config(api_key="x").manifest_path))
        print(f"GATE: FAIL ({len(probs)})"); return 1
    print("GATE: PASS"); return 0
def main(argv=None):
    ap=argparse.ArgumentParser(prog="art-director"); sub=ap.add_subparsers(dest="cmd",required=True)
    v=sub.add_parser("validate"); v.add_argument("--manifest",required=True); v.add_argument("--code"); v.set_defaults(fn=_cmd_validate)
    g=sub.add_parser("gen"); g.add_argument("--manifest",required=True); g.add_argument("--project-dir",required=True); g.add_argument("--bg-resolution"); g.add_argument("--page",default="index.html"); g.set_defaults(fn=_cmd_gen)
    t=sub.add_parser("gate"); t.add_argument("--manifest",required=True); t.add_argument("--project-dir",required=True); t.add_argument("--code"); t.set_defaults(fn=_cmd_gate)
    args=ap.parse_args(argv); return args.fn(args)
if __name__=="__main__": sys.exit(main())
```

- [ ] **Step 3: 跑通过 → Commit** — `git commit -m "feat(art-director): CLI(自插sys.path+预检/成本+原子持久化+run.log+degrade+--page+软链外部cwd测试)"`

---

## Task 10: E2E（ThreadingHTTPServer，per-test 状态 + 全链路 + stuck-task 隔离）

**Files:** Create `tests/test_e2e_mock.py`, `tests/fixtures/index.html`, `tests/fixtures/manifest.json`

**Handler 状态 per-test:** 状态挂在 `httpd` 实例(`httpd.scenario`),fixture 重置;"剩余 running 次数"可由测试设定;tid 命名空间化避免跨测试串扰(M12)。

- [ ] **Step 1: fixtures**

```html
<!-- tests/fixtures/index.html -->
<!doctype html><html><head><title>t</title><style>.hero{background-image:url(assets/gen/hero-bg.png);}</style></head>
<body><img src="assets/gen/mascot.png" alt="mascot"></body></html>
```

```json
// tests/fixtures/manifest.json
{"version":1,"style":{"brief":"cyberpunk test"},"assets":[
 {"id":"hero-bg","kind":"bg","prompt":"neon alley","aspect":"16:9","resolution":"2k","transparent":false,"path":"assets/gen/hero-bg.png","placeholder":"url(assets/gen/hero-bg.png)"},
 {"id":"mascot","kind":"cutout","prompt":"fox","aspect":"2:3","transparent":true,"format":"png","path":"assets/gen/mascot.png","placeholder":"src=\"assets/gen/mascot.png\""}]}
```

- [ ] **Step 2: 失败 E2E 测试** — 用例1 全链路(断言 auth/端点/模型/请求体 + url/path 错配防护:bg 文件无 alpha、cutout 有 alpha);用例2 让 `gpt-image-2`(bg)的 task 永远 running → bg 超时 failed、cutout 仍 done(真驱 ApimartClient 的失败隔离,M12)。

```python
# tests/test_e2e_mock.py
import json, struct, zlib, threading, shutil, pathlib, http.server, pytest
from config import Config
from manifest import Manifest
from transport import UrllibTransport
from apimart import ApimartClient
from engine import generate
from wiring import verify
from extractor import reconcile
from pngutil import png_has_alpha
FIX=pathlib.Path(__file__).parent/"fixtures"
SIG=b"\x89PNG\r\n\x1a\n"
def _chunk(t,d): return struct.pack(">I",len(d))+t+d+struct.pack(">I",zlib.crc32(t+d)&0xffffffff)
def _png(ct): return SIG+_chunk(b"IHDR",struct.pack(">IIBBBBB",1,1,8,ct,0,0,0))+_chunk(b"IDAT",b"\x00")+_chunk(b"IEND",b"")
PNG_BG=_png(2); PNG_CUT=_png(6)
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def _j(self,code,obj):
        b=json.dumps(obj).encode(); self.send_response(code); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_POST(self):
        assert self.headers.get("Authorization")=="Bearer k"
        assert self.path.endswith("/images/generations")
        body=json.loads(self.rfile.read(int(self.headers.get("Content-Length",0)))); model=body["model"]
        assert model in ("gpt-image-2","gpt-image-1.5-official")
        if model=="gpt-image-1.5-official": assert body.get("background")=="transparent" and body.get("output_format")=="png"
        sc=self.server.scenario; tid=f"T-{model}"
        with sc["lock"]: sc["running"].setdefault(tid, sc["initial_running"].get(model,1))
        self._j(200,{"task_id":tid})
    def do_GET(self):
        if self.path.endswith(".png"):
            data=PNG_CUT if "1.5" in self.path else PNG_BG
            self.send_response(200); self.send_header("Content-Type","image/png"); self.send_header("Content-Length",str(len(data))); self.end_headers(); self.wfile.write(data); return
        tid=self.path.rsplit("/",1)[-1]; sc=self.server.scenario
        with sc["lock"]:
            rem=sc["running"].get(tid,0)
            if rem==float("inf") or rem>0:
                if rem!=float("inf"): sc["running"][tid]=rem-1
                self._j(200,{"status":"running"}); return
        self._j(200,{"status":"succeeded","url":f"http://127.0.0.1:{self.server.server_address[1]}/img-{tid}.png"})
@pytest.fixture
def server():
    httpd=http.server.ThreadingHTTPServer(("127.0.0.1",0),Handler)
    httpd.scenario={"lock":threading.Lock(),"running":{},"initial_running":{}}
    threading.Thread(target=httpd.serve_forever,daemon=True).start()
    yield httpd; httpd.shutdown()
def _setup(tmp_path):
    shutil.copy(FIX/"index.html", tmp_path/"index.html"); mp=tmp_path/"manifest.json"; shutil.copy(FIX/"manifest.json", mp); return mp
def test_full_pipeline(tmp_path, server):
    mp=_setup(tmp_path)
    cfg=Config(api_key="k",base_url=f"http://127.0.0.1:{server.server_address[1]}",poll_interval=0,poll_timeout=10,concurrency=2)
    client=ApimartClient(UrllibTransport(),cfg.api_key,cfg.base_url)
    m=Manifest.load(str(mp)); assert m.validate(max_assets=cfg.max_assets)==[]
    code=(tmp_path/"index.html").read_text(); assert reconcile(code,m).ok
    m=generate(m,str(tmp_path),client,cfg); assert all(a.status=="done" for a in m.assets)
    assert verify(m,str(tmp_path),code=code)==[]
    assert png_has_alpha(str(tmp_path/"assets/gen/mascot.png")) and not png_has_alpha(str(tmp_path/"assets/gen/hero-bg.png"))
def test_stuck_task_isolation(tmp_path, server):
    # bg 永远 running → 超时 failed;cutout 仍 done(真驱 ApimartClient,M12)
    mp=_setup(tmp_path); server.scenario["initial_running"]={"gpt-image-2":float("inf")}
    cfg=Config(api_key="k",base_url=f"http://127.0.0.1:{server.server_address[1]}",poll_interval=0,poll_timeout=0,retries=0,concurrency=2)
    client=ApimartClient(UrllibTransport(),cfg.api_key,cfg.base_url)
    m=generate(Manifest.load(str(mp)),str(tmp_path),client,cfg)
    by={a.id:a.status for a in m.assets}; assert by["hero-bg"]=="failed" and by["mascot"]=="done"
```

- [ ] **Step 3: 跑通过 → Commit** — `git commit -m "test(art-director): E2E(per-test Handler状态 + 全链路断言契约 + stuck-task失败隔离)"`

---

## Task 11: 真 API 冒烟（env-gated；不同 aspect + 负向断言 + 原始JSON抓取 + 低timeout）

**Files:** Create `tests/test_smoke_real_api.py`

- [ ] **Step 1: 写冒烟** — 默认 skip;`ART_DIRECTOR_SMOKE=1` + key 才跑。强化(M14):bg 用 `16:9`、cutout 用 `2:3`(不同档,dispatch 错位会触发 size FatalError);**低 poll_timeout(30s)** 让 enum 不匹配快失败;断言透明 bg 被拒(证 gpt-image-2 无 alpha 声明);打印 submit + 首个 poll 原始 JSON(供核对 DONE 状态/字段/url 位置)。

```python
# tests/test_smoke_real_api.py
import os, pytest
from config import Config
from manifest import Asset, Manifest
from transport import UrllibTransport
from apimart import ApimartClient
from engine import generate
from registry import build_request
from pngutil import is_png, png_has_alpha
pytestmark=pytest.mark.skipif(os.environ.get("ART_DIRECTOR_SMOKE")!="1" or not os.environ.get("APIMART_API_KEY"),
    reason="real-API smoke: set ART_DIRECTOR_SMOKE=1 + APIMART_API_KEY")
def test_transparent_bg_is_rejected_by_registry():
    # 负向:bg 不可透明(证 gpt-image-2 无 alpha 这一 §7 声明的设计意图)
    with pytest.raises(ValueError):
        build_request(Asset(id="x",kind="bg",prompt="p",aspect="1:1",transparent=True,path="assets/gen/x.png",placeholder="x"), Config(api_key="k"))
def test_real_apimart_bg_and_cutout(tmp_path, capsys):
    cfg=Config.from_env(); cfg.default_bg_resolution="1k"; cfg.poll_timeout=30   # 低 timeout 快失败
    client=ApimartClient(UrllibTransport(),cfg.api_key,cfg.base_url)
    m=Manifest(version=1,style={"brief":"smoke"},assets=[
        Asset(id="smoke-bg",kind="bg",prompt="a simple blue gradient background",aspect="16:9",path="assets/gen/smoke-bg.png",placeholder="url(assets/gen/smoke-bg.png)"),
        Asset(id="smoke-cut",kind="cutout",prompt="a single red apple, centered",aspect="2:3",transparent=True,format="png",path="assets/gen/smoke-cut.png",placeholder='src="assets/gen/smoke-cut.png"')])
    m=generate(m,str(tmp_path),client,cfg)
    statuses={a.id:a.status for a in m.assets}
    assert statuses=={"smoke-bg":"done","smoke-cut":"done"}, f"smoke failed: {statuses} (check .art-director/run.log for raw API shape)"
    assert is_png(str(tmp_path/"assets/gen/smoke-bg.png"))
    assert png_has_alpha(str(tmp_path/"assets/gen/smoke-cut.png")), "cutout must have alpha — verifies gpt-image-1.5-official transparent contract"
```

- [ ] **Step 2: 跑(默认 skip)** — `python -m pytest tests/test_smoke_real_api.py -v` → 1 skipped(or 2,负向那条不依赖网络可改为常跑)。

- [ ] **Step 3: Commit** — `git commit -m "test(art-director): 真API冒烟(env-gated;不同aspect+负向断言+低timeout;恢复spec §12)"`

> 🔴 **执行者:** 声称 APImart 契约"已验证"前,必须实跑一次冒烟(需 key + 少量费用),并**核对 run.log 里 submit/poll 的原始 JSON** 与 `apimart._task_id`/`_extract_image`/`DONE_FAIL`/`NON_TERMINAL` 假设。不符 → 回改 Task 4/3。

---

## Task 12: SKILL.md 编排 + INSTALL

**Files:** Create `SKILL.md`, `INSTALL.md`

- [ ] **Step 1: 写 SKILL.md** — 覆盖:

1. frontmatter:`name: art-director` + description(触发:出 design + 切图 + 复杂背景 + 透明素材 + 新工程页面)。
2. **调用约定(B1,带 worked example):** 用本 skill 加载时打印的 "Base directory for this skill:" 那行的**绝对路径**替换 `<SKILL_BASE>`:`python3 "<SKILL_BASE>/cli.py" <cmd>`。明确写:「把 `<SKILL_BASE>` 换成那行的真实绝对路径,**不要**原样保留 `<SKILL_BASE>`」。给一条形如 `python3 "/Users/.../skills/art-director/cli.py" validate ...` 的样例。
3. 前置:`export APIMART_API_KEY=...`;frontend-design 可用;Python3。
4. **4 阶段:**
   - **Stage 1 design:** invoke `frontend-design` 出 HTML;**注入 placeholder 约定(强约束):** 凡该用真实栅格图处,禁 CSS 假造,改为字面 `background-image:url(assets/gen/<id>.png)` 或 `<img src="assets/gen/<id>.png">`;**禁** srcset/`<picture>`/`<source>`/image-set/Tailwind `bg-[url()]`/React `src={}` **承载资产**(extractor 会硬失败);**`placeholder` 字段必须是你写进代码里那段字符串的逐字拷贝**(对账靠字符串包含,大小写/引号必须一致)。同步往 `.art-director/manifest.json` 追加条目。bg→默认 2k(hero 可 4k);cutout→aspect 仅 1:1/2:3/3:2。**素材路径禁空格**。
   - **Stage 2 对账:** `python3 "<SKILL_BASE>/cli.py" validate --manifest .art-director/manifest.json --code index.html` → 必须 PASS(看 `[cost]`,超 max_assets 拒)。
   - **Stage 3 生成:** `... gen --manifest .art-director/manifest.json --project-dir . --page index.html` → 异步生成+下载/解码+校验+逐转移持久化;PARTIAL 已自动降级(不白屏),重跑 resume。
   - **Stage 4 gate:** `... gate --manifest .art-director/manifest.json --project-dir . --code index.html` → 必须 PASS。
5. **🔴 迭代上限兜底:** Stage 2 FAIL/UnsupportedMarkup → 回 Stage 1 让 frontend-design 改字面形式,**但最多 2 次**;仍失败 → agent 手动把承载资产的构造确定性改写成字面 `url()`/`src` 形式(不再无限回弹 frontend-design)。
6. 产物:`index.html` + `assets/gen/*.png` + `.art-director/manifest.json`(status/task_id) + `.art-director/run.log`。
7. 红线:gate 未 PASS 不得声称就绪;失败素材如数报告;成本超限先告知。

- [ ] **Step 2: 写 INSTALL.md** — env(`APIMART_API_KEY`、`ART_DIRECTOR_BG_RESOLUTION`、`ART_DIRECTOR_SMOKE`)、跑测试、真 API 冒烟命令、frontend-design 依赖、零三方依赖。

- [ ] **Step 3: 校验 frontmatter** — `python3 -c "t=open('skills/art-director/SKILL.md').read(); assert t.startswith('---') and 'name: art-director' in t and 'description:' in t; print('frontmatter OK')"` → `frontmatter OK`

- [ ] **Step 4: Commit** — `git commit -m "docs(art-director): SKILL.md(绝对基目录+worked example+placeholder约定+迭代上限兜底+降级) + INSTALL"`

---

## Task 13: 全量回归 + 自检（诚实计数）

- [ ] **Step 1: 全量测试** — `cd skills/art-director && python -m pytest -v`。门禁:**断言 0 failed**(真 API 冒烟 skipped)。逐任务期望计数**以实跑为准**填写,不预先编造。

- [ ] **Step 2: run.log 契约核对(若已跑过 smoke)** — 打开 `.art-director/run.log`,把 submit/poll 原始 JSON 的字段名/状态值与 `apimart._task_id` / `_extract_image` / `DONE_FAIL` / `NON_TERMINAL` 比对;不符则回 Task 3/4 修正,再跑。

- [ ] **Step 3: 对照 spec + 两轮评审逐条核覆盖**

| 需求 / 评审项 | 落点 |
|---|---|
| 双模型派发 + _cut 双kwarg(M9) | Task 3 |
| APImart submit/poll/fetch_image 容忍(b64/dict/Location/开放世界)(M3/M4/M13) | Task 4 |
| 分辨率两级;cutout 不可切 | Task 1+3 |
| manifest 原子写 + 路径无../无空格 + registry 预检 | Task 2 |
| 引擎下载后校验 + temp→replace + unlink | Task 6 |
| task_id 付费状态机(不重复付费/失败重生)(M1/M2/M5/persist) | Task 6 |
| HTTP 归一化 + retry_after honored | Task 4+6 |
| extractor placeholder 包含 + 承载资产硬失败(B1/M7/M8) | Task 7 |
| wiring 被引用 + degrade 健壮 + page 参数(M6) | Task 8+9 |
| 降级不白屏(body 兜底) | Task 8 |
| palette/tRNS | Task 5 |
| CLI 外部cwd(软链+PYTHONPATH='')(B1/B2) | Task 9 |
| E2E per-test 状态 + stuck-task 隔离(M12) | Task 10 |
| 真 API 冒烟(不同aspect+负向+低timeout)(M14/§12) | Task 11 |
| 成本护栏 + run.log(m2/m7) | Task 9 |
| 迭代上限兜底 | Task 12 |

- [ ] **Step 4: 最终 commit** — `git add -A && git commit -m "chore(art-director): 全量回归(0 failed)+ spec/两轮评审逐条核覆盖"`

---

## 遗留与边界(诚实声明)

- **B3 仍是最大未证点:** Task 11 冒烟实跑 + run.log 核对前,Task 3/4 的 APImart 契约是"按文档 + 容忍变体 + 开放世界"的稳妥猜测。冒烟必须在声称"契约已验证"前实跑一次。
- **seam:** placeholder 包含契约消除了正则脆弱性,但仍要 frontend-design 把 placeholder 写一致 + 不用承载资产的高级构造;Stage-2 迭代上限(2 次)后转人工确定性改写,避免无限回弹。
- **degrade 对 `<style>` 规则内的背景** 只能靠 `body` 兜底底色保证不白屏,无法精确替换具体 selector 的 `background-image`(v1 边界;精确替换需 CSS 解析,后置)。
- **cutout 封顶 1536×1024 / PNG-only / 无 4K**(模型能力)。
