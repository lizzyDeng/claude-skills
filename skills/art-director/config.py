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
    preview_cost_threshold: float = 0.3   # validate 估算超此值 → 建议 preview(非阻断)
    preview_asset_threshold: int = 6      # 资产数超此值 → 建议 preview(非阻断)
    preview_variants: int = 3             # 默认探针变体数(driver 通常据此写 N 个变体)
    @classmethod
    def from_env(cls):
        k=os.environ.get("APIMART_API_KEY")
        if not k: raise RuntimeError("APIMART_API_KEY not set. export APIMART_API_KEY=sk-... (APImart bearer token).")
        return cls(api_key=k, default_bg_resolution=os.environ.get("ART_DIRECTOR_BG_RESOLUTION","2k"),
                   preview_cost_threshold=float(os.environ.get("ART_DIRECTOR_PREVIEW_COST_THRESHOLD","0.3")),
                   preview_asset_threshold=int(os.environ.get("ART_DIRECTOR_PREVIEW_ASSET_THRESHOLD","6")),
                   preview_variants=int(os.environ.get("ART_DIRECTOR_PREVIEW_VARIANTS","3")))
