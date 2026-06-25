# tests/test_regen.py — Stage 3.5 定向重生:清掉指定 asset 的 task_id+status 强制重新计费生成,其它 done 的复用
import json, struct, zlib, os, pathlib, pytest
from cli import main
from manifest import Manifest
SIG=b"\x89PNG\r\n\x1a\n"
def _chunk(t,d): return struct.pack(">I",len(d))+t+d+struct.pack(">I",zlib.crc32(t+d)&0xffffffff)
def _png(ct): return SIG+_chunk(b"IHDR",struct.pack(">IIBBBBB",1,1,8,ct,0,0,0))+_chunk(b"IDAT",b"\x00")+_chunk(b"IEND",b"")
MAN={"version":1,"style":{"brief":"x"},"assets":[
 {"id":"b","kind":"bg","prompt":"pb","aspect":"16:9","resolution":"2k","path":"assets/gen/b.png","placeholder":"url(assets/gen/b.png)","status":"done","task_id":"OLD-B"},
 {"id":"c","kind":"cutout","prompt":"pc","aspect":"2:3","transparent":True,"format":"png","path":"assets/gen/c.png","placeholder":'src="assets/gen/c.png"',"status":"done","task_id":"OLD-C"}]}
SUBMITS=[]
class _RecClient:
    """记录每次 submit 的请求体,用于断言『只有被命名的 asset 重新计费』。"""
    def __init__(self,*a,**k): pass
    def submit(self,body): SUBMITS.append(body); return f"NEW-{len(SUBMITS)}"
    def poll(self,tid,timeout,interval,clock=None): return {"status":"succeeded"}
    def fetch_image(self,result,dest):
        os.makedirs(os.path.dirname(dest),exist_ok=True); open(dest,"wb").write(_png(6))   # 色型6=RGBA,cutout 需 alpha
def _setup(tmp_path):
    # 按真实约定:manifest 在 project_dir/.art-director/manifest.json,gen/regen 回写同一文件
    ad=tmp_path/".art-director"; ad.mkdir(parents=True)
    mp=ad/"manifest.json"; mp.write_text(json.dumps(MAN))
    g=tmp_path/"assets/gen"; g.mkdir(parents=True)
    (g/"b.png").write_bytes(_png(2)); (g/"c.png").write_bytes(_png(2))   # 两张都已生成在盘
    return mp
def _patch(monkeypatch):
    monkeypatch.setenv("APIMART_API_KEY","sk-test")
    import apimart; monkeypatch.setattr(apimart,"ApimartClient",_RecClient)
    SUBMITS.clear()
def test_regen_rebills_named_only(tmp_path, capsys, monkeypatch):
    _patch(monkeypatch); mp=_setup(tmp_path)
    rc=main(["regen","--manifest",str(mp),"--project-dir",str(tmp_path),"--asset","c"])
    out=capsys.readouterr().out
    assert rc==0 and "REGEN: PASS" in out
    assert len(SUBMITS)==1                                          # 只有 c 被重新提交=重新计费
    by={a.id:a for a in Manifest.load(str(mp)).assets}
    assert by["c"].task_id=="NEW-1" and by["c"].status=="done"      # c 拿到全新 task
    assert by["b"].task_id=="OLD-B" and by["b"].status=="done"      # b 原样复用,不重新计费
def test_regen_with_prompt_overwrites(tmp_path, capsys, monkeypatch):
    _patch(monkeypatch); mp=_setup(tmp_path)
    rc=main(["regen","--manifest",str(mp),"--project-dir",str(tmp_path),"--asset","c","--prompt","a fluffy white cat"])
    assert rc==0
    by={a.id:a for a in Manifest.load(str(mp)).assets}
    assert by["c"].prompt=="a fluffy white cat" and by["b"].prompt=="pb"   # 只覆盖命名 asset
def test_regen_unknown_asset_errors_before_spend(tmp_path, capsys, monkeypatch):
    _patch(monkeypatch); mp=_setup(tmp_path)
    rc=main(["regen","--manifest",str(mp),"--project-dir",str(tmp_path),"--asset","nope"])
    assert rc==1 and "not found" in capsys.readouterr().out
    assert len(SUBMITS)==0                                          # 未知 id → 花钱前拦住
def test_regen_prompt_requires_single_asset(tmp_path, capsys, monkeypatch):
    _patch(monkeypatch); mp=_setup(tmp_path)
    rc=main(["regen","--manifest",str(mp),"--project-dir",str(tmp_path),"--asset","b","--asset","c","--prompt","x"])
    assert rc==1 and "single" in capsys.readouterr().out.lower()
    assert len(SUBMITS)==0                                          # --prompt 配多 asset → 拒绝,不花钱
def test_regen_multiple_assets(tmp_path, capsys, monkeypatch):
    _patch(monkeypatch); mp=_setup(tmp_path)
    rc=main(["regen","--manifest",str(mp),"--project-dir",str(tmp_path),"--asset","b","--asset","c"])
    assert rc==0 and "REGEN: PASS" in capsys.readouterr().out
    assert len(SUBMITS)==2                                          # 两张都重生
    by={a.id:a for a in Manifest.load(str(mp)).assets}
    assert by["b"].task_id.startswith("NEW-") and by["c"].task_id.startswith("NEW-")
