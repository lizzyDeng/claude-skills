# paths.py
def validate_asset_path(path: str) -> None:
    if not path or "\x00" in path: raise ValueError(f"bad path: {path!r}")
    if path.startswith(("/","\\")) or (len(path)>1 and path[1]==":"): raise ValueError(f"must be relative: {path!r}")
    parts=path.split("/")
    if "." in parts or ".." in parts: raise ValueError(f"no . or .. components: {path!r}")
    if any(ch.isspace() for ch in path): raise ValueError(f"no whitespace (extractor round-trip): {path!r}")
    if not path.startswith("assets/gen/"): raise ValueError(f"must be under assets/gen/: {path!r}")
    if not path.endswith(".png"): raise ValueError(f"must end with .png: {path!r}")
