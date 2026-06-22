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
