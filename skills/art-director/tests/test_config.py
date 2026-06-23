import pytest
from config import Config
def test_from_env_defaults(monkeypatch):
    monkeypatch.setenv("APIMART_API_KEY","sk-test")
    c=Config.from_env()
    assert c.api_key=="sk-test" and c.base_url=="https://api.apimart.ai/v1"
    assert c.default_bg_resolution=="2k" and c.asset_dir=="assets/gen"
    assert c.manifest_path==".art-director/manifest.json" and c.max_assets==12 and c.backoff_base==1.0
def test_from_env_missing(monkeypatch):
    monkeypatch.delenv("APIMART_API_KEY",raising=False)
    with pytest.raises(RuntimeError) as e: Config.from_env()
    assert "APIMART_API_KEY" in str(e.value)
def test_preview_defaults():
    c=Config(api_key="x")
    assert c.preview_cost_threshold==0.3 and c.preview_asset_threshold==6 and c.preview_variants==3
def test_preview_env_override(monkeypatch):
    monkeypatch.setenv("APIMART_API_KEY","sk-test")
    monkeypatch.setenv("ART_DIRECTOR_PREVIEW_COST_THRESHOLD","1.25")
    monkeypatch.setenv("ART_DIRECTOR_PREVIEW_ASSET_THRESHOLD","9")
    monkeypatch.setenv("ART_DIRECTOR_PREVIEW_VARIANTS","5")
    c=Config.from_env()
    assert c.preview_cost_threshold==1.25 and c.preview_asset_threshold==9 and c.preview_variants==5
