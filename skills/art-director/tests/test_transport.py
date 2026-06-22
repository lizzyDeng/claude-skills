# tests/test_transport.py
import urllib.error, io, pytest
from transport import UrllibTransport, RetryableError, FatalError
class Fake:
    def __init__(self, exc=None, status=200, body=b"{}", headers=None): self.exc=exc; self.status=status; self.body=body; self.headers=headers or {}
    def __call__(self, req, timeout=0):
        if self.exc: raise self.exc
        outer=self
        class R:
            status=outer.status; headers=outer.headers
            def read(s): return outer.body
            def __enter__(s): return s
            def __exit__(s,*a): return False
        return R()
def _err(code, h=None): return urllib.error.HTTPError("u",code,"m",h or {},io.BytesIO(b"{}"))
def test_429_retryable(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", Fake(exc=_err(429,{"Retry-After":"7"})))
    with pytest.raises(RetryableError) as e: UrllibTransport().get("http://x",{})
    assert e.value.retry_after==7
def test_503_retryable(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", Fake(exc=_err(503)))
    with pytest.raises(RetryableError): UrllibTransport().get("http://x",{})
def test_400_fatal(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", Fake(exc=_err(400)))
    with pytest.raises(FatalError): UrllibTransport().get("http://x",{})
def test_urlerror_retryable(monkeypatch):
    import urllib.error as ue; monkeypatch.setattr("urllib.request.urlopen", Fake(exc=ue.URLError("reset")))
    with pytest.raises(RetryableError): UrllibTransport().get("http://x",{})
def test_returns_headers(monkeypatch):
    monkeypatch.setattr("urllib.request.urlopen", Fake(status=202, body=b"", headers={"Location":"/v1/tasks/T9"}))
    status, body, headers = UrllibTransport().post("http://x",{},{"a":1})
    assert status==202 and body=={} and headers.get("Location")=="/v1/tasks/T9"
