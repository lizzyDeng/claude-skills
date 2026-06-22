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
