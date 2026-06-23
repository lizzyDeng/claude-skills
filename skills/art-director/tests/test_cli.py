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
    # placeholder-containment 契约:code 必须含 b 的 placeholder url(...) 与 c 的 placeholder src="..."(逐字)
    code=tmp_path/"index.html"; code.write_text('<div style="background-image:url(assets/gen/b.png)"></div><img src="assets/gen/c.png">')
    assert main(["validate","--manifest",str(mp),"--code",str(code)])==0
def test_gate_alpha(tmp_path, capsys):
    mp=tmp_path/"m.json"; mp.write_text(json.dumps(MAN))
    (tmp_path/"assets/gen").mkdir(parents=True)
    (tmp_path/"assets/gen/b.png").write_bytes(_png(2)); (tmp_path/"assets/gen/c.png").write_bytes(_png(2))
    assert main(["gate","--manifest",str(mp),"--project-dir",str(tmp_path)])!=0 and "alpha" in capsys.readouterr().out
# ---- Stage 1.5 preview gate ----
def test_validate_preview_suggested_high_cost(tmp_path, capsys):
    # 一张 4k bg ($0.21) + 一张 cutout ($0.06) = $0.27 < 0.3, 但加第二张 4k → 超阈值
    assets=[{"id":f"b{i}","kind":"bg","prompt":"p","aspect":"16:9","resolution":"4k","path":f"assets/gen/b{i}.png","placeholder":f"url(assets/gen/b{i}.png)","status":"done"} for i in range(2)]
    mp=tmp_path/"m.json"; mp.write_text(json.dumps({**MAN,"assets":assets}))
    assert main(["validate","--manifest",str(mp)])==0
    assert "PREVIEW SUGGESTED" in capsys.readouterr().out
def test_validate_preview_suggested_many_assets(tmp_path, capsys):
    # 7 张便宜 1k bg → 成本低 ($0.14) 但资产数 > 6 → 仍建议
    assets=[{"id":f"b{i}","kind":"bg","prompt":"p","aspect":"16:9","resolution":"1k","path":f"assets/gen/b{i}.png","placeholder":f"url(assets/gen/b{i}.png)","status":"done"} for i in range(7)]
    mp=tmp_path/"m.json"; mp.write_text(json.dumps({**MAN,"assets":assets}))
    main(["validate","--manifest",str(mp)])
    out=capsys.readouterr().out
    assert "PREVIEW SUGGESTED" in out and "7 个资产" in out
def test_validate_no_preview_suggested_low(tmp_path, capsys):
    # 默认 MAN: bg 2k ($0.06) + cutout ($0.06) = $0.12, 2 资产 → 不建议
    mp=tmp_path/"m.json"; mp.write_text(json.dumps(MAN)); main(["validate","--manifest",str(mp)])
    assert "PREVIEW SUGGESTED" not in capsys.readouterr().out
def test_validate_preview_does_not_change_verdict(tmp_path, capsys):
    # 高成本但合法 → 仍 PASS;高成本且非法 kind → 仍 FAIL(门控不改判定)
    bad=[{"id":f"b{i}","kind":"video","prompt":"p","aspect":"16:9","resolution":"4k","path":f"assets/gen/b{i}.png","placeholder":"x"} for i in range(2)]
    mp=tmp_path/"m.json"; mp.write_text(json.dumps({**MAN,"assets":bad}))
    assert main(["validate","--manifest",str(mp)])!=0
    out=capsys.readouterr().out; assert "PREVIEW SUGGESTED" in out and "VALIDATE: FAIL" in out
class _FakeClient:
    def __init__(self,*a,**k): self.bodies=[]
    def submit(self,body): self.bodies.append(body); return f"T-{len(self.bodies)}"
    def poll(self,tid,timeout,interval,clock=None): return {"status":"succeeded"}
    def fetch_image(self,result,dest): open(dest,"wb").write(_png(2))
def _vf(tmp_path):
    vf=tmp_path/"variants.json"
    vf.write_text(json.dumps([
        {"label":"painterly","prompt":"oil alley","style_suffix":"oil brushwork"},
        {"label":"neon","prompt":"neon alley","style_suffix":"neon noir"}]))
    return vf
def test_cli_preview(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("APIMART_API_KEY","sk-test")
    import apimart; monkeypatch.setattr(apimart,"ApimartClient",_FakeClient)
    mp=tmp_path/"m.json"; mp.write_text(json.dumps(MAN)); vf=_vf(tmp_path)
    rc=main(["preview","--manifest",str(mp),"--project-dir",str(tmp_path),"--variants-file",str(vf)])
    out=capsys.readouterr().out
    assert rc==0 and "PREVIEW: PASS" in out
    # previews.json 落盘 + 两张图生成在 .art-director/preview/
    pj=tmp_path/".art-director/preview/previews.json"; assert pj.exists()
    data=json.loads(pj.read_text()); assert data["carrier"]=="b" and len(data["variants"])==2
    assert (tmp_path/".art-director/preview/b__painterly.png").exists()
    assert (tmp_path/".art-director/preview/b__neon.png").exists()
    # 逐张生成路径打印(供 driver open)
    assert "b__painterly.png" in out and "b__neon.png" in out
def test_cli_lock_style(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("APIMART_API_KEY","sk-test")
    import apimart; monkeypatch.setattr(apimart,"ApimartClient",_FakeClient)
    mp=tmp_path/"m.json"; mp.write_text(json.dumps(MAN)); vf=_vf(tmp_path)
    main(["preview","--manifest",str(mp),"--project-dir",str(tmp_path),"--variants-file",str(vf)])
    capsys.readouterr()
    rc=main(["lock-style","--manifest",str(mp),"--project-dir",str(tmp_path),"--variant","painterly"])
    out=capsys.readouterr().out
    assert rc==0 and "LOCK-STYLE: PASS" in out
    from manifest import Manifest
    m2=Manifest.load(str(mp)); by={a.id:a.prompt for a in m2.assets}
    assert by["b"]=="oil alley"                       # carrier.prompt ← 选中变体完整 prompt
    assert by["c"].endswith("oil brushwork")          # 其它 asset append style_suffix
    # 幂等:再 lock 同一个不重复追加
    main(["lock-style","--manifest",str(mp),"--project-dir",str(tmp_path),"--variant","painterly"])
    m3=Manifest.load(str(mp)); assert {a.id:a.prompt for a in m3.assets}["c"].count("oil brushwork")==1
def test_cli_lock_style_via_variants_file(tmp_path, capsys):
    mp=tmp_path/"m.json"; mp.write_text(json.dumps(MAN)); vf=_vf(tmp_path)
    rc=main(["lock-style","--manifest",str(mp),"--variant","neon","--variants-file",str(vf)])
    assert rc==0 and "LOCK-STYLE: PASS" in capsys.readouterr().out
    from manifest import Manifest
    m2=Manifest.load(str(mp)); by={a.id:a.prompt for a in m2.assets}
    assert by["b"]=="neon alley" and by["c"].endswith("neon noir")
def test_cli_lock_style_unknown_variant(tmp_path, capsys):
    mp=tmp_path/"m.json"; mp.write_text(json.dumps(MAN)); vf=_vf(tmp_path)
    rc=main(["lock-style","--manifest",str(mp),"--variant","nope","--variants-file",str(vf)])
    assert rc!=0 and "not found" in capsys.readouterr().out
def test_cli_preview_no_bg_carrier(tmp_path, capsys, monkeypatch):
    # manifest 无 bg → style_carrier ValueError → 干净 [preview] 行,return 1,非 traceback
    monkeypatch.setenv("APIMART_API_KEY","sk-test")
    import apimart; monkeypatch.setattr(apimart,"ApimartClient",_FakeClient)
    man={**MAN,"assets":[MAN["assets"][1]]}  # 只有 cutout
    mp=tmp_path/"m.json"; mp.write_text(json.dumps(man)); vf=_vf(tmp_path)
    rc=main(["preview","--manifest",str(mp),"--project-dir",str(tmp_path),"--variants-file",str(vf)])
    assert rc==1 and "[preview]" in capsys.readouterr().out
def test_cli_preview_bad_variants_file(tmp_path, capsys, monkeypatch):
    # variants-file 缺 prompt → _load_variants ValueError → 干净 [preview] 行,非 traceback
    monkeypatch.setenv("APIMART_API_KEY","sk-test")
    import apimart; monkeypatch.setattr(apimart,"ApimartClient",_FakeClient)
    mp=tmp_path/"m.json"; mp.write_text(json.dumps(MAN))
    bad=tmp_path/"bad.json"; bad.write_text(json.dumps([{"label":"x"}]))
    rc=main(["preview","--manifest",str(mp),"--project-dir",str(tmp_path),"--variants-file",str(bad)])
    assert rc==1 and "[preview]" in capsys.readouterr().out
def test_cli_lock_style_requires_project_dir(tmp_path, capsys):
    # 无 --variants-file 且无 --project-dir → 显式报错(不静默默认 cwd 找错 previews.json)
    mp=tmp_path/"m.json"; mp.write_text(json.dumps(MAN))
    rc=main(["lock-style","--manifest",str(mp),"--variant","painterly"])
    assert rc==1 and "--project-dir required" in capsys.readouterr().out
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
