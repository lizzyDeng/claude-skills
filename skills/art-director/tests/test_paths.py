# tests/test_paths.py
import pytest
from paths import validate_asset_path
@pytest.mark.parametrize("ok",["assets/gen/a.png","assets/gen/sub/b.png"])
def test_valid(ok): validate_asset_path(ok)
@pytest.mark.parametrize("bad",[
    "/etc/passwd","assets/gen/../x.png","assets/gen/sub/../a.png","../x.png",
    "other/a.png","assets/gen/a.jpg","assets/gen/a.png\x00","assets/gen/a b.png","./assets/gen/a.png"])
def test_invalid(bad):
    with pytest.raises(ValueError): validate_asset_path(bad)
