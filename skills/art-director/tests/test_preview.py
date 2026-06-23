# tests/test_preview.py — Stage 1.5 art-direction preview
import json, struct, zlib, pytest
from config import Config
from manifest import Asset, Manifest
from registry import build_probe_request
from engine import generate_previews, PREVIEW_DIR
SIG=b"\x89PNG\r\n\x1a\n"
def _chunk(t,d): return struct.pack(">I",len(d))+t+d+struct.pack(">I",zlib.crc32(t+d)&0xffffffff)
def _png(ct,trns=False):
    o=SIG+_chunk(b"IHDR",struct.pack(">IIBBBBB",1,1,8,ct,0,0,0))
    if trns: o+=_chunk(b"tRNS",b"\x00")
    return o+_chunk(b"IDAT",b"\x00")+_chunk(b"IEND",b"")
CFG=Config(api_key="k",concurrency=2,retries=1,backoff_base=0,poll_timeout=10,poll_interval=0)
def _carrier():
    return Asset(id="hero-bg",kind="bg",prompt="base scene",aspect="16:9",resolution="4k",
                 path="assets/gen/hero-bg.png",placeholder="url(assets/gen/hero-bg.png)")
class FakeClient:
    def __init__(self): self.submits=[]; self.bodies=[]
    def submit(self,body): self.submits.append(body); self.bodies.append(body); return f"T-{len(self.submits)}"
    def poll(self,tid,timeout,interval,clock=None): return {"status":"succeeded","_tid":tid}
    def fetch_image(self,result,dest): open(dest,"wb").write(_png(2))
VARIANTS=[
    {"label":"painterly","prompt":"oil painting alley","style_suffix":"loose oil-painting brushwork"},
    {"label":"neon-noir","prompt":"neon rain alley","style_suffix":"high-contrast neon noir"},
    {"label":"flat-vector","prompt":"flat geometric alley","style_suffix":"flat vector minimalism"},
]
def test_generate_previews_writes_to_preview_dir(tmp_path):
    c=FakeClient()
    res=generate_previews(_carrier(),VARIANTS,str(tmp_path),c,CFG)
    # 输出到 .art-director/preview/,文件名 <carrier_id>__<label>.png,不进 assets/gen/
    for v in VARIANTS:
        f=tmp_path/PREVIEW_DIR/f"hero-bg__{v['label']}.png"
        assert f.exists(), f"missing {f}"
    assert not (tmp_path/"assets/gen").exists()
    assert all(r["status"]=="done" for r in res) and len(res)==3
    # 返回保序且带 path/style_suffix
    assert [r["label"] for r in res]==["painterly","neon-noir","flat-vector"]
    assert res[0]["path"]==".art-director/preview/hero-bg__painterly.png"
    assert res[0]["style_suffix"]=="loose oil-painting brushwork"
def test_generate_previews_forces_1k(tmp_path):
    c=FakeClient()
    generate_previews(_carrier(),VARIANTS,str(tmp_path),c,CFG)
    # 每个探针请求都强制 1k,无视 carrier.resolution=4k
    assert c.submits, "no submits"
    for b in c.bodies: assert b["resolution"]=="1k" and b["model"]=="gpt-image-2"
    # variant prompt 被实际使用(非 carrier.prompt)
    prompts={b["prompt"] for b in c.bodies}
    assert "oil painting alley" in prompts and "base scene" not in prompts
def test_generate_previews_rejects_cutout_carrier(tmp_path):
    cut=Asset(id="m",kind="cutout",prompt="p",aspect="2:3",transparent=True,format="png",
              path="assets/gen/m.png",placeholder='src="assets/gen/m.png"')
    with pytest.raises(ValueError) as e: generate_previews(cut,VARIANTS,str(tmp_path),FakeClient(),CFG)
    assert "bg" in str(e.value).lower()
def test_generate_previews_custom_out_dir(tmp_path):
    c=FakeClient()
    res=generate_previews(_carrier(),VARIANTS[:1],str(tmp_path),c,CFG,out_dir="my/preview")
    assert (tmp_path/"my/preview/hero-bg__painterly.png").exists()
    assert res[0]["path"]=="my/preview/hero-bg__painterly.png"
def test_build_probe_request_forces_1k():
    a=_carrier()
    body=build_probe_request(a,CFG)
    assert body["resolution"]=="1k" and body["size"]=="16:9"
    # 不污染原 asset
    assert a.resolution=="4k"
def test_build_probe_request_rejects_cutout():
    cut=Asset(id="m",kind="cutout",prompt="p",aspect="2:3",transparent=True,format="png",
              path="assets/gen/m.png",placeholder="x")
    with pytest.raises(ValueError): build_probe_request(cut,CFG)
