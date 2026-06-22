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
