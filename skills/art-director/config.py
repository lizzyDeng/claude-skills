import os
from dataclasses import dataclass
@dataclass
class Config:
    api_key: str
    base_url: str = "https://api.apimart.ai/v1"
    default_bg_resolution: str = "2k"
    concurrency: int = 4
    retries: int = 3
    backoff_base: float = 1.0
    poll_timeout: int = 180
    poll_interval: int = 3
    asset_dir: str = "assets/gen"
    manifest_path: str = ".art-director/manifest.json"
    max_assets: int = 12
    @classmethod
    def from_env(cls):
        k=os.environ.get("APIMART_API_KEY")
        if not k: raise RuntimeError("APIMART_API_KEY not set. export APIMART_API_KEY=sk-... (APImart bearer token).")
        return cls(api_key=k, default_bg_resolution=os.environ.get("ART_DIRECTOR_BG_RESOLUTION","2k"))
