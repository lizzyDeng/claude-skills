# apimart.py
import base64, time
from transport import FatalError, RetryableError
DONE_FAIL={"failed","error","canceled","cancelled","rejected","timeout","expired","content_policy_violation","moderation_blocked"}
NON_TERMINAL={"running","queued","pending","processing","in_progress","started","submitted","waiting","created"}
def _task_id(d):
    if isinstance(d.get("task_id"),str): return d["task_id"]
    if isinstance(d.get("id"),str): return d["id"]
    inner=d.get("data")
    if isinstance(inner,dict) and isinstance(inner.get("task_id"),str): return inner["task_id"]
    return None
def _extract_image(r):
    """-> ('url',str) | ('b64',str) | None"""
    if isinstance(r.get("url"),str): return ("url",r["url"])
    if isinstance(r.get("b64_json"),str): return ("b64",r["b64_json"])
    for key in ("data","images","results","output"):
        v=r.get(key)
        if isinstance(v,dict): v=[v]
        if isinstance(v,list):
            for it in v:
                if isinstance(it,dict):
                    if isinstance(it.get("url"),str): return ("url",it["url"])
                    if isinstance(it.get("b64_json"),str): return ("b64",it["b64_json"])
    return None
def _is_failure(d):
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
        if not tid: raise FatalError(f"submit: no task_id/id/Location (keys={sorted(data.keys())})")
        return tid
    def poll(self,task_id,timeout,interval,clock=time):
        deadline=clock.monotonic()+timeout
        while True:
            _,data,_=self.t.get(f"{self.base}/tasks/{task_id}",self.headers)
            if _is_failure(data): raise FatalError(f"task {task_id} failed: status={data.get('status')} err={bool(data.get('error'))}")
            st=str(data.get("status","")).lower()
            img=_extract_image(data)
            if img is not None and st not in NON_TERMINAL: return data     # 有图且非显式 pending → 完成
            if clock.monotonic()>=deadline: raise TimeoutError(f"task {task_id} not done within {timeout}s (status={st!r})")
            if interval: clock.sleep(interval)
    def fetch_image(self,result,dest):
        img=_extract_image(result)
        if img is None: raise FatalError("no image url/b64 in result")
        kind,val=img
        data=self.t.get_bytes(val) if kind=="url" else base64.b64decode(val)
        with open(dest,"wb") as f: f.write(data)
