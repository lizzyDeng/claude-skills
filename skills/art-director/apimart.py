# apimart.py
import base64, time
from transport import FatalError, RetryableError
DONE_FAIL={"failed","error","canceled","cancelled","rejected","timeout","expired","content_policy_violation","moderation_blocked"}
NON_TERMINAL={"running","queued","pending","processing","in_progress","started","submitted","waiting","created"}
def _task_id(resp):
    # 真实 APImart submit 形状:{"code":200,"data":[{"status":"submitted","task_id":...}]}(data 是 list)
    # 同时容忍 顶层 task_id/id、data 为 dict 的旧设想。
    cands=[]
    if isinstance(resp,dict):
        cands.append(resp)
        inner=resp.get("data")
        if isinstance(inner,dict): cands.append(inner)
        elif isinstance(inner,list): cands.extend(x for x in inner if isinstance(x,dict))
    for d in cands:
        if isinstance(d.get("task_id"),str): return d["task_id"]
        if isinstance(d.get("id"),str): return d["id"]
    return None
def _envelope(resp):
    # 真实 poll 形状:{"code":200,"data":{...task...}}。data 是 dict 才解包,否则原样(兼容裸 task 对象/错误信封)。
    if isinstance(resp,dict) and isinstance(resp.get("data"),dict): return resp["data"]
    return resp
def _first_url(u):
    if isinstance(u,str): return u
    if isinstance(u,list):                      # 真实形状:"url":["https://..."](数组)
        for x in u:
            if isinstance(x,str): return x
    return None
def _img_from_item(it):
    if not isinstance(it,dict): return None
    u=_first_url(it.get("url")) or _first_url(it.get("image_url"))
    if u: return ("url",u)
    if isinstance(it.get("b64_json"),str): return ("b64",it["b64_json"])
    return None
def _extract_image(r):
    """-> ('url',str) | ('b64',str) | None。真实图位于 r.result.images[i].url(url 可为 list);保留开放世界兜底。"""
    if isinstance(r,list):
        for it in r:
            g=_img_from_item(it)
            if g: return g
        return None
    if not isinstance(r,dict): return None
    g=_img_from_item(r)
    if g: return g
    res=r.get("result")                          # 真实 APImart:result.images[].url
    if isinstance(res,dict):
        imgs=res.get("images") or res.get("data") or res.get("output")
        if isinstance(imgs,list):
            g=_extract_image(imgs)
            if g: return g
    for key in ("data","images","results","output"):   # 开放世界兜底
        v=r.get(key)
        if isinstance(v,dict): v=[v]
        if isinstance(v,list):
            g=_extract_image(v)
            if g: return g
    return None
def _is_failure(d):
    if not isinstance(d,dict): return False
    if str(d.get("status","")).lower() in DONE_FAIL: return True
    if d.get("error"): return True
    code=d.get("code") or d.get("status_code")
    if isinstance(code,int) and code>=400 and code!=429: return True
    return False
class ApimartClient:
    def __init__(self,transport,api_key,base_url):
        self.t=transport; self.base=base_url.rstrip("/"); self.headers={"Authorization":f"Bearer {api_key}"}
    def submit(self,body):
        status,data,headers=self.t.post(f"{self.base}/images/generations",self.headers,body)
        tid=_task_id(data)
        if not tid and headers:
            loc=headers.get("Location") or headers.get("location")
            if loc: tid=loc.rstrip("/").rsplit("/",1)[-1]
        if not tid:
            shape=sorted(data.keys()) if isinstance(data,dict) else type(data).__name__
            raise FatalError(f"submit: no task_id/id/Location (shape={shape})")
        return tid
    def poll(self,task_id,timeout,interval,clock=time):
        deadline=clock.monotonic()+timeout
        while True:
            _,resp,_=self.t.get(f"{self.base}/tasks/{task_id}",self.headers)
            inner=_envelope(resp)                # 解开 {"code":..,"data":{...}} 信封
            if _is_failure(inner) or _is_failure(resp):
                st=inner.get("status") if isinstance(inner,dict) else None
                raise FatalError(f"task {task_id} failed: status={st}")
            st=str((inner.get("status") if isinstance(inner,dict) else "") or "").lower()
            img=_extract_image(inner)
            if img is not None and st not in NON_TERMINAL: return inner   # 有图且非显式 pending → 完成
            if clock.monotonic()>=deadline: raise TimeoutError(f"task {task_id} not done within {timeout}s (status={st!r})")
            if interval: clock.sleep(interval)
    def fetch_image(self,result,dest):
        img=_extract_image(result)
        if img is None: raise FatalError("no image url/b64 in result")
        kind,val=img
        data=self.t.get_bytes(val) if kind=="url" else base64.b64decode(val)
        with open(dest,"wb") as f: f.write(data)
