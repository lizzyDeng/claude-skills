import os, pytest
from config import Config
from manifest import Asset, Manifest
from transport import UrllibTransport
from apimart import ApimartClient
from engine import generate
from registry import build_request
from pngutil import is_png, png_has_alpha
def test_transparent_bg_is_rejected_by_registry():
    # 负向(不依赖网络,常跑):bg 不可透明(证 gpt-image-2 无 alpha 这一 §7 声明的设计意图)
    with pytest.raises(ValueError):
        build_request(Asset(id="x",kind="bg",prompt="p",aspect="1:1",transparent=True,path="assets/gen/x.png",placeholder="x"), Config(api_key="k"))
@pytest.mark.skipif(os.environ.get("ART_DIRECTOR_SMOKE")!="1" or not os.environ.get("APIMART_API_KEY"),
    reason="real-API smoke: set ART_DIRECTOR_SMOKE=1 + APIMART_API_KEY")
def test_real_apimart_bg_and_cutout(tmp_path, capsys):
    cfg=Config.from_env(); cfg.default_bg_resolution="1k"; cfg.poll_timeout=30   # 低 timeout 快失败
    client=ApimartClient(UrllibTransport(),cfg.api_key,cfg.base_url)
    m=Manifest(version=1,style={"brief":"smoke"},assets=[
        Asset(id="smoke-bg",kind="bg",prompt="a simple blue gradient background",aspect="16:9",path="assets/gen/smoke-bg.png",placeholder="url(assets/gen/smoke-bg.png)"),
        Asset(id="smoke-cut",kind="cutout",prompt="a single red apple, centered",aspect="2:3",transparent=True,format="png",path="assets/gen/smoke-cut.png",placeholder='src="assets/gen/smoke-cut.png"')])
    m=generate(m,str(tmp_path),client,cfg)
    statuses={a.id:a.status for a in m.assets}
    assert statuses=={"smoke-bg":"done","smoke-cut":"done"}, f"smoke failed: {statuses} (check .art-director/run.log for raw API shape)"
    assert is_png(str(tmp_path/"assets/gen/smoke-bg.png"))
    assert png_has_alpha(str(tmp_path/"assets/gen/smoke-cut.png")), "cutout must have alpha — verifies gpt-image-1.5-official transparent contract"
