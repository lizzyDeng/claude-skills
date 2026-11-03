"""代码面（surface）与占用表：同一块代码同一时间只给一张票。

代码面的定义在项目配置里（aifriends：.claude/router/surfaces.json），
每个面 = 一组 glob + 轻重（heavy = 要编译 Rust，占重票名额）。
"""
import json
import os
from fnmatch import fnmatchcase
from pathlib import Path

import store


def load_surfaces(path: Path) -> dict:
    table = json.loads(Path(path).read_text(encoding="utf-8"))
    for name, spec in table.items():
        if spec.get("weight") not in ("heavy", "light"):
            raise ValueError(f"代码面 {name} 的 weight 必须是 heavy 或 light")
        spec.setdefault("globs", [])
    return table


def _match(path: str, glob: str) -> bool:
    # fnmatch 的 * 本来就跨 /，把 ** 收成 * 后整串匹配即可
    return fnmatchcase(os.path.expanduser(path), os.path.expanduser(glob).replace("**", "*"))


def surfaces_for_files(files, table) -> set:
    return {name for name, spec in table.items()
            for f in files if any(_match(f, g) for g in spec["globs"])}


def held_locks(state) -> dict:
    """surface → 持有者（票号，或 ext:<worktree>）。票优先于外部 session 登记。带未核实意图的票也持锁。"""
    locks = {}
    for tid, t in state["tickets"].items():
        if store.holds_lock(t):
            for s in t["surfaces"]:
                locks.setdefault(s, tid)
    for path, ext in state["external"].items():
        if ext.get("locking", True):
            for s in ext.get("surfaces", []):
                locks.setdefault(s, f"ext:{path}")
    return locks


def conflicts(state, tid, surfaces) -> dict:
    return {s: holder for s, holder in held_locks(state).items()
            if s in set(surfaces) and holder != tid}


def weight_for(surfaces, table) -> str:
    return "heavy" if any(table.get(s, {}).get("weight") == "heavy" for s in surfaces) else "light"
