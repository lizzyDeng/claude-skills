# transport.py
import json, urllib.request, urllib.error
class RetryableError(Exception):
    def __init__(self,msg,retry_after=None): super().__init__(msg); self.retry_after=retry_after
class FatalError(Exception): pass
RETRYABLE_STATUS={408,409,425,429,500,502,503,504}
def _retry_after(h):
    try: return int(h.get("Retry-After")) if h and h.get("Retry-After") else None
    except (ValueError,TypeError): return None
def _do(req):
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw=r.read(); body=json.loads(raw.decode()) if raw else {}
            return r.status, body, dict(r.headers or {})
    except urllib.error.HTTPError as e:
        if e.code in RETRYABLE_STATUS: raise RetryableError(f"HTTP {e.code}", retry_after=_retry_after(e.headers))
        raise FatalError(f"HTTP {e.code}: {e.reason}")
    except urllib.error.URLError as e:
        raise RetryableError(f"network error: {e.reason}")
class UrllibTransport:
    def post(self,url,headers,body):
        req=urllib.request.Request(url,data=json.dumps(body).encode(),headers={**headers,"Content-Type":"application/json"},method="POST")
        return _do(req)
    def get(self,url,headers): return _do(urllib.request.Request(url,headers=headers,method="GET"))
    def get_bytes(self,url):
        try:
            with urllib.request.urlopen(url,timeout=120) as r: return r.read()
        except urllib.error.HTTPError as e:
            if e.code in RETRYABLE_STATUS: raise RetryableError(f"download HTTP {e.code}")
            raise FatalError(f"download HTTP {e.code}")
        except urllib.error.URLError as e: raise RetryableError(f"download network error: {e.reason}")
