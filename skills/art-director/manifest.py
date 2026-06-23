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
    def style_carrier(self):
        """Stage 1.5 风格载体:manifest 中第一个 kind=='bg' 的 asset。无 bg → ValueError(preview 仅支持 bg 载体)。"""
        c=next((a for a in self.assets if a.kind=="bg"), None)
        if c is None: raise ValueError("no bg asset to use as style carrier; preview only supports a bg style carrier")
        return c
    def carrier_by_id(self, asset_id):
        a=next((x for x in self.assets if x.id==asset_id), None)
        if a is None: raise ValueError(f"carrier asset {asset_id!r} not found in manifest")
        if a.kind!="bg": raise ValueError(f"carrier {asset_id!r} is kind={a.kind!r}; preview only supports a bg style carrier")
        return a
    def apply_style_suffix(self, style_suffix, skip_ids=()):
        """把风格修饰串幂等 append 进所有(除 skip_ids 外)asset.prompt。已含则不重复加。返回被改动的 asset id 列表。"""
        suffix=(style_suffix or "").strip()
        if not suffix: return []
        changed=[]
        for a in self.assets:
            if a.id in skip_ids: continue
            base=a.prompt or ""
            if suffix in base: continue                 # 幂等:已含该修饰 → 跳过
            a.prompt=(base+" "+suffix).strip() if base else suffix
            changed.append(a.id)
        return changed
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
