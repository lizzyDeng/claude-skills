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
