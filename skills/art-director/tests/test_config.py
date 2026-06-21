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
