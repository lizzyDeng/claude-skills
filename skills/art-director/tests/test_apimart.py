# tests/test_apimart.py
import base64, struct, zlib, pytest
from apimart import ApimartClient
from transport import FatalError, RetryableError
SIG=b"\x89PNG\r\n\x1a\n"
def _chunk(t,d): return struct.pack(">I",len(d))+t+d+struct.pack(">I",zlib.crc32(t+d)&0xffffffff)
def _png(): return SIG+_chunk(b"IHDR",struct.pack(">IIBBBBB",1,1,8,6,0,0,0))+_chunk(b"IDAT",b"\x00")+_chunk(b"IEND",b"")
class FakeT:
    def __init__(self, post=(200,{"task_id":"T1"},{}), polls=(), img=b""):
        self.post_r=post; self.polls=list(polls); self.img=img; self.posts=[]; self.gets=[]
    def post(self,u,h,b): self.posts.append((u,h,b)); return self.post_r
    def get(self,u,h): self.gets.append(u); return (200, self.polls.pop(0), {})
    def get_bytes(self,u): return self.img
class Clock:
    def __init__(self): self.t=0.0
    def monotonic(self): return self.t
    def sleep(self,s): self.t+=s
def _c(post=(200,{"task_id":"T1"},{}), polls=(), img=b""): return ApimartClient(FakeT(post,polls,img),"k","https://api.apimart.ai/v1")
def test_submit_task_id(): assert _c().submit({"model":"m"})=="T1"
def test_submit_id_alias(): assert _c(post=(200,{"id":"T2"},{})).submit({"model":"m"})=="T2"
def test_submit_nested(): assert _c(post=(200,{"data":{"task_id":"T3"}},{})).submit({"model":"m"})=="T3"
def test_submit_location(): assert _c(post=(202,{},{"Location":"/v1/tasks/T9"})).submit({"model":"m"})=="T9"
def test_submit_auth_header():
    c=_c(); c.submit({"model":"m"}); assert c.t.posts[0][1]["Authorization"]=="Bearer k" and c.t.posts[0][0].endswith("/images/generations")
def test_submit_none_fatal():
    with pytest.raises(FatalError): _c(post=(200,{"oops":1},{})).submit({"model":"m"})
def test_poll_terminal_with_url():
    r=_c(polls=[{"status":"running"},{"status":"succeeded","url":"u"}]).poll("T",10,0,clock=Clock()); assert r["url"]=="u"
def test_poll_url_present_unknown_status_is_done():
    r=_c(polls=[{"status":"weird-but-final","url":"u"}]).poll("T",10,0,clock=Clock()); assert r["url"]=="u"
def test_poll_url_but_running_keeps_waiting():
    r=_c(polls=[{"status":"running","url":"preview"},{"status":"succeeded","url":"final"}]).poll("T",10,0,clock=Clock()); assert r["url"]=="final"
def test_poll_error_envelope_fast_fail():
    with pytest.raises(FatalError): _c(polls=[{"error":{"message":"nsfw"}}]).poll("T",10,0,clock=Clock())
def test_poll_failure_status():
    with pytest.raises(FatalError): _c(polls=[{"status":"failed"}]).poll("T",10,0,clock=Clock())
def test_poll_timeout():
    with pytest.raises(TimeoutError): _c(polls=[{"status":"running"}]*99).poll("T",0,0,clock=Clock())
def test_fetch_image_url(tmp_path):
    c=_c(img=_png()); d=tmp_path/"a.png"; c.fetch_image({"url":"u"},str(d)); assert d.read_bytes().startswith(SIG)
def test_fetch_image_b64_data_list(tmp_path):
    c=_c(); d=tmp_path/"a.png"; c.fetch_image({"data":[{"b64_json":base64.b64encode(_png()).decode()}]},str(d)); assert d.read_bytes().startswith(SIG)
def test_fetch_image_data_dict_url(tmp_path):
    c=_c(img=_png()); d=tmp_path/"a.png"; c.fetch_image({"data":{"url":"u"}},str(d)); assert d.read_bytes().startswith(SIG)
def test_fetch_image_url_later_element(tmp_path):
    c=_c(img=_png()); d=tmp_path/"a.png"; c.fetch_image({"data":[{"foo":1},{"url":"u"}]},str(d)); assert d.read_bytes().startswith(SIG)
def test_fetch_image_none_fatal(tmp_path):
    with pytest.raises(FatalError): _c().fetch_image({"status":"succeeded"}, str(tmp_path/"a.png"))
# ---- 真实 APImart 形状(2026-06-22 实测捕获)----
def test_submit_real_data_list():
    # 真实 submit:{"code":200,"data":[{"status":"submitted","task_id":...}]}
    real=(200,{"code":200,"data":[{"status":"submitted","task_id":"task_REAL"}]},{})
    assert _c(post=real).submit({"model":"m"})=="task_REAL"
def test_poll_real_envelope_pending_then_completed_e2e(tmp_path):
    # 真实 poll:信封 {"code":200,"data":{...}};完成形 result.images[].url 为 list
    c=_c(polls=[{"code":200,"data":{"status":"pending","progress":0}},
                {"code":200,"data":{"status":"completed","progress":100,
                                    "result":{"images":[{"url":["https://up/x.png"]}]}}}], img=_png())
    r=c.poll("T",10,0,clock=Clock())
    assert str(r.get("status")).lower()=="completed"
    d=tmp_path/"a.png"; c.fetch_image(r,str(d)); assert d.read_bytes().startswith(SIG)   # url-as-list 端到端可取图
def test_fetch_image_real_result_images_urllist(tmp_path):
    c=_c(img=_png()); d=tmp_path/"a.png"
    c.fetch_image({"status":"completed","result":{"images":[{"url":["https://up/x.png"]}]}}, str(d))
    assert d.read_bytes().startswith(SIG)
def test_poll_real_envelope_failure_nested_status():
    # 信封内嵌 status=failed 必须被识别(解包后判失败)
    with pytest.raises(FatalError):
        _c(polls=[{"code":200,"data":{"status":"failed"}}]).poll("T",10,0,clock=Clock())
