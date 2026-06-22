import json, struct, zlib, threading, shutil, pathlib, http.server, pytest
from config import Config
from manifest import Manifest
from transport import UrllibTransport
from apimart import ApimartClient
from engine import generate
from wiring import verify
from extractor import reconcile
from pngutil import png_has_alpha
FIX=pathlib.Path(__file__).parent/"fixtures"
SIG=b"\x89PNG\r\n\x1a\n"
def _chunk(t,d): return struct.pack(">I",len(d))+t+d+struct.pack(">I",zlib.crc32(t+d)&0xffffffff)
def _png(ct): return SIG+_chunk(b"IHDR",struct.pack(">IIBBBBB",1,1,8,ct,0,0,0))+_chunk(b"IDAT",b"\x00")+_chunk(b"IEND",b"")
PNG_BG=_png(2); PNG_CUT=_png(6)
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self,*a): pass
    def _j(self,code,obj):
        b=json.dumps(obj).encode(); self.send_response(code); self.send_header("Content-Type","application/json"); self.send_header("Content-Length",str(len(b))); self.end_headers(); self.wfile.write(b)
    def do_POST(self):
        assert self.headers.get("Authorization")=="Bearer k"
        assert self.path.endswith("/images/generations")
        body=json.loads(self.rfile.read(int(self.headers.get("Content-Length",0)))); model=body["model"]
        assert model in ("gpt-image-2","gpt-image-1.5-official")
        if model=="gpt-image-1.5-official": assert body.get("background")=="transparent" and body.get("output_format")=="png"
        sc=self.server.scenario; tid=f"T-{model}"
        with sc["lock"]: sc["running"].setdefault(tid, sc["initial_running"].get(model,1))
        self._j(200,{"task_id":tid})
    def do_GET(self):
        if self.path.endswith(".png"):
            data=PNG_CUT if "1.5" in self.path else PNG_BG
            self.send_response(200); self.send_header("Content-Type","image/png"); self.send_header("Content-Length",str(len(data))); self.end_headers(); self.wfile.write(data); return
        tid=self.path.rsplit("/",1)[-1]; sc=self.server.scenario
        with sc["lock"]:
            rem=sc["running"].get(tid,0)
            if rem==float("inf") or rem>0:
                if rem!=float("inf"): sc["running"][tid]=rem-1
                self._j(200,{"status":"running"}); return
        self._j(200,{"status":"succeeded","url":f"http://127.0.0.1:{self.server.server_address[1]}/img-{tid}.png"})
@pytest.fixture
def server():
    httpd=http.server.ThreadingHTTPServer(("127.0.0.1",0),Handler)
    httpd.scenario={"lock":threading.Lock(),"running":{},"initial_running":{}}
    threading.Thread(target=httpd.serve_forever,daemon=True).start()
    yield httpd; httpd.shutdown()
def _setup(tmp_path):
    shutil.copy(FIX/"index.html", tmp_path/"index.html"); mp=tmp_path/"manifest.json"; shutil.copy(FIX/"manifest.json", mp); return mp
def test_full_pipeline(tmp_path, server):
    mp=_setup(tmp_path)
    cfg=Config(api_key="k",base_url=f"http://127.0.0.1:{server.server_address[1]}",poll_interval=0,poll_timeout=10,concurrency=2)
    client=ApimartClient(UrllibTransport(),cfg.api_key,cfg.base_url)
    m=Manifest.load(str(mp)); assert m.validate(max_assets=cfg.max_assets)==[]
    code=(tmp_path/"index.html").read_text(); assert reconcile(code,m).ok
    m=generate(m,str(tmp_path),client,cfg); assert all(a.status=="done" for a in m.assets)
    assert verify(m,str(tmp_path),code=code)==[]
    assert png_has_alpha(str(tmp_path/"assets/gen/mascot.png")) and not png_has_alpha(str(tmp_path/"assets/gen/hero-bg.png"))
def test_stuck_task_isolation(tmp_path, server):
    # bg 永远 running → 超时 failed;cutout 仍 done(真驱 ApimartClient,M12)
    mp=_setup(tmp_path); server.scenario["initial_running"]={"gpt-image-2":float("inf"),"gpt-image-1.5-official":0}
    cfg=Config(api_key="k",base_url=f"http://127.0.0.1:{server.server_address[1]}",poll_interval=0,poll_timeout=0,retries=0,concurrency=2)
    client=ApimartClient(UrllibTransport(),cfg.api_key,cfg.base_url)
    m=generate(Manifest.load(str(mp)),str(tmp_path),client,cfg)
    by={a.id:a.status for a in m.assets}; assert by["hero-bg"]=="failed" and by["mascot"]=="done"
