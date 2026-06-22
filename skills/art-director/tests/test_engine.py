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
        self.polls.append(tid)
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
    assert not (tmp_path/"assets/gen/b.png.part").exists()
def test_submit_fail_isolated(tmp_path):
    c=FakeClient({"gpt-image-2":{"submit_raise":RetryableError("boom")}})
    m=generate(_man(), str(tmp_path), c, CFG)
    by={a.id:a.status for a in m.assets}; assert by["b"]=="failed" and by["c"]=="done"
    assert c.submits.count("gpt-image-2")==1+CFG.retries and c.submits.count("gpt-image-1.5-official")==1
def test_poll_retry_reuses_task_no_rebill(tmp_path):
    c=FakeClient({"gpt-image-2":{"poll_raise":RetryableError("flaky"),"poll_raise_times":1}})
    m=generate(_man(), str(tmp_path), c, CFG)
    assert {a.id:a.status for a in m.assets}["b"]=="done"
    assert c.submits.count("gpt-image-2")==1
def test_terminal_fail_clears_task_id(tmp_path):
    c=FakeClient({"gpt-image-2":{"poll_raise":FatalError("rejected"),"poll_raise_times":99}})
    m=generate(_man(), str(tmp_path), c, CFG)
    a=[x for x in m.assets if x.id=="b"][0]; assert a.status=="failed" and a.task_id is None
def test_resume_reuses_existing_task(tmp_path):
    c=FakeClient(); m=_man(); m.assets[0].task_id="T-existing"
    generate(m, str(tmp_path), c, CFG); assert "gpt-image-2" not in c.submits
def test_progress_incremental(tmp_path):
    saved=[]; generate(_man(), str(tmp_path), FakeClient(), CFG, on_progress=lambda m:saved.append([a.status for a in m.assets]))
    assert len(saved)>=2 and any(s.count("done")==1 for s in saved)
def test_backoff_honors_clock(tmp_path):
    clk=Clock(); cfg=Config(api_key="k",concurrency=1,retries=2,backoff_base=2,poll_timeout=10,poll_interval=0)
    c=FakeClient({"gpt-image-2":{"submit_raise":RetryableError("x")},"gpt-image-1.5-official":{"submit_raise":RetryableError("y")}})
    generate(_man(), str(tmp_path), c, cfg, clock=clk)
    assert 2 in clk.sleeps and 4 in clk.sleeps
def test_retry_after_honored(tmp_path):
    clk=Clock(); cfg=Config(api_key="k",concurrency=1,retries=1,backoff_base=2,poll_timeout=10,poll_interval=0)
    c=FakeClient({"gpt-image-2":{"submit_raise":RetryableError("x",retry_after=9)},"gpt-image-1.5-official":{"submit_raise":RetryableError("y",retry_after=9)}})
    generate(_man(), str(tmp_path), c, cfg, clock=clk)
    assert 9 in clk.sleeps
