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
