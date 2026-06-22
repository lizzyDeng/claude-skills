# tests/test_registry.py
import pytest
from config import Config
from manifest import Asset
from registry import build_request
CFG=Config(api_key="k")
def _bg(aspect="16:9", **k): return Asset(id="b",kind="bg",prompt="p",aspect=aspect,path="assets/gen/b.png",placeholder="url(assets/gen/b.png)",**k)
def _cut(aspect="2:3", **k): return Asset(id="c",kind="cutout",prompt="p",aspect=aspect,transparent=True,format="png",path="assets/gen/c.png",placeholder='src="assets/gen/c.png"',**k)
def test_bg_shape(): assert build_request(_bg(),CFG)=={"model":"gpt-image-2","prompt":"p","size":"16:9","resolution":"2k","n":1}
def test_bg_override(): assert build_request(_bg(resolution="4k"),CFG)["resolution"]=="4k"
def test_bg_bad_res():
    with pytest.raises(ValueError): build_request(_bg(resolution="8k"),CFG)
def test_bg_transparent_rejected():
    with pytest.raises(ValueError): build_request(_bg(transparent=True),CFG)
def test_cutout_shape(): assert build_request(_cut(),CFG)=={"model":"gpt-image-1.5-official","prompt":"p","size":"2:3","background":"transparent","output_format":"png","n":1}
def test_cutout_res_rejected():
    with pytest.raises(ValueError): build_request(_cut(resolution="2k"),CFG)
def test_cutout_bad_size():
    with pytest.raises(ValueError): build_request(_cut(aspect="16:9"),CFG)   # 现在能真正跑到 build_request
