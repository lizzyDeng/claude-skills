import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "skills" / "lark-relay" / "scripts"
sys.path.insert(0, str(SCRIPTS))   # 脚本之间平铺 import（import store），同 router
# router 的测试也有平铺的 store：同一进程里先被别的目录 import 过就清掉，避免拿错模块
for _m in ("store", "parse", "lark_api", "relay", "listener"):
    _mod = sys.modules.get(_m)
    if _mod is not None and not str(getattr(_mod, "__file__", "")).startswith(str(SCRIPTS)):
        del sys.modules[_m]


@pytest.fixture(autouse=True)
def relay_home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    monkeypatch.setenv("LARK_RELAY_HOME", str(h))
    return h
