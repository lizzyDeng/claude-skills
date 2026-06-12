#!/usr/bin/env python3
"""db-agent — 用自然语言查询任意数据库（datasette-agent 封装）。

把业务数据库导出为只读 SQLite 快照，再用 datasette + datasette-agent
提供 Web 聊天界面（/-/agent）和 CLI 一次性问答。

Subcommands:
  setup                      创建/刷新共享 venv（默认 ~/.cache/db-agent/venv）
  export DB_URL [-o OUT.db]  导出快照（postgres URL 或 sqlite 文件路径）
  serve SNAPSHOT.db          启动 Web 服务（前台进程，建议后台运行）
  chat SNAPSHOT.db -p "..."  CLI 一次性提问
  run DB_URL                 export + serve 一步到位

环境变量:
  ANTHROPIC_API_KEY   必需（serve/chat），或已配置 `llm keys set anthropic`
  DB_AGENT_MODEL      默认 anthropic/claude-haiku-4-5-20251001
  DB_AGENT_HOME       venv 安装位置，默认 ~/.cache/db-agent
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HOME = Path(os.environ.get("DB_AGENT_HOME", Path.home() / ".cache" / "db-agent"))
VENV = HOME / "venv"
VENV_PY = VENV / "bin" / "python"
DEFAULT_MODEL = os.environ.get("DB_AGENT_MODEL", "anthropic/claude-haiku-4-5-20251001")
PKGS = ["datasette", "datasette-agent", "llm-anthropic", "psycopg[binary]", "sqlite-utils"]


def in_venv() -> bool:
    return Path(sys.executable) == VENV_PY


def setup(force: bool = False) -> None:
    if force and VENV.exists():
        shutil.rmtree(VENV)
    HOME.mkdir(parents=True, exist_ok=True)
    uv = shutil.which("uv")
    if uv:
        if not VENV.exists():
            subprocess.run([uv, "venv", str(VENV)], check=True)
        subprocess.run(
            [uv, "pip", "install", "--python", str(VENV_PY), "--prerelease=allow", *PKGS],
            check=True,
        )
    else:
        if not VENV.exists():
            subprocess.run([sys.executable, "-m", "venv", str(VENV)], check=True)
        # datasette-agent 依赖 datasette 1.0 预发布版，必须 --pre
        subprocess.run([str(VENV / "bin" / "pip"), "install", "--pre", *PKGS], check=True)
    print(f"✓ venv ready: {VENV}")


def ensure_venv() -> None:
    """非 venv 解释器时自动建好环境并用 venv python 重新执行自己。"""
    if in_venv():
        return
    if not VENV_PY.exists():
        print("First run — setting up venv (one-time)...", file=sys.stderr)
        setup()
    os.execv(str(VENV_PY), [str(VENV_PY), os.path.abspath(__file__), *sys.argv[1:]])


def check_api_key(model: str) -> None:
    # 只对 anthropic/* 模型做前置检查；其他模型交给 llm 自己的 key 解析
    # （OpenAI 兼容模型在 extra-openai-models.yaml 里声明 api_key_name）
    if not model.startswith("anthropic/"):
        return
    if os.environ.get("ANTHROPIC_API_KEY"):
        return
    for keys_path in (
        Path.home() / "Library" / "Application Support" / "io.datasette.llm" / "keys.json",
        Path.home() / ".config" / "io.datasette.llm" / "keys.json",
    ):
        try:
            if "anthropic" in json.loads(keys_path.read_text()):
                return
        except (OSError, ValueError):
            continue
    sys.exit(
        "ERROR: 没有可用的 Anthropic API key。\n"
        "  export ANTHROPIC_API_KEY=sk-ant-...   # 或\n"
        f"  {VENV / 'bin' / 'llm'} keys set anthropic\n"
        "提示：项目 .env 里常见变量名 CLAUDE_API_KEY / ANTHROPIC_API_KEY。\n"
        "也可用 OpenAI 兼容模型（如 xAI Grok）：见 SKILL.md「换模型」。"
    )


def coerce(v):
    """Postgres 值 → SQLite 可存类型。JSONB/数组→JSON 字符串，UUID/Decimal/时间→str。"""
    import json as _json
    from decimal import Decimal
    from uuid import UUID

    if isinstance(v, (dict, list)):
        return _json.dumps(v, ensure_ascii=False, default=str)
    if isinstance(v, (UUID, Decimal)):
        return str(v)
    if isinstance(v, memoryview):
        return bytes(v)
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


def export(db_url: str, out: str, include: list, exclude: list) -> str:
    out_path = Path(out)
    # sqlite 源：直接拷贝
    if not db_url.startswith(("postgres://", "postgresql://")):
        src = Path(db_url.removeprefix("sqlite:///").removeprefix("sqlite:"))
        if not src.exists():
            sys.exit(f"ERROR: 不支持的 DB URL 或文件不存在: {db_url}\n"
                     "支持 postgres:// / postgresql:// URL 和 SQLite 文件路径。"
                     "MySQL 可先用 db-to-sqlite 转换。")
        if src.resolve() != out_path.resolve():
            shutil.copy(src, out_path)
        print(f"✓ snapshot: {out_path}")
        return str(out_path)

    import psycopg
    from psycopg.rows import dict_row
    import sqlite_utils

    db = sqlite_utils.Database(str(out_path), recreate=True)
    with psycopg.connect(db_url) as conn:
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename"
            )
        ]
        if include:
            tables = [t for t in tables if t in include]
        if exclude:
            tables = [t for t in tables if t not in exclude]
        print(f"exporting {len(tables)} tables -> {out_path}")
        for t in tables:
            total = 0
            with conn.cursor(row_factory=dict_row, name=f"dbagent_{t}") as cur:
                cur.itersize = 2000
                cur.execute(f'SELECT * FROM "{t}"')
                batch = []
                for r in cur:
                    batch.append({k: coerce(v) for k, v in r.items()})
                    if len(batch) >= 1000:
                        db[t].insert_all(batch, alter=True)
                        total += len(batch)
                        batch = []
                if batch:
                    db[t].insert_all(batch, alter=True)
                    total += len(batch)
            if total == 0:
                cols = conn.execute(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name=%s",
                    (t,),
                ).fetchall()
                if cols and t not in db.table_names():
                    db[t].create({c[0]: str for c in cols})
            print(f"  {t}: {total} rows")
    print(f"✓ snapshot: {out_path}")
    return str(out_path)


def write_config(snapshot: Path, model: str) -> Path:
    cfg = snapshot.with_suffix(".datasette.yml")
    cfg.write_text(
        f"""plugins:
  datasette-llm:
    default_model: {model}

# 本地 demo 全开；对外暴露前必须收紧
permissions:
  datasette-agent: true
  datasette-agent-explore: true
  datasette-agent-background: true
""",
        encoding="utf-8",
    )
    return cfg


def llm_config_dir() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "io.datasette.llm"
    return Path.home() / ".config" / "io.datasette.llm"


def add_model(model_id: str, api_base: str, key: str, key_name: str) -> None:
    """注册一个 OpenAI 兼容模型（如 xAI Grok），供 --model 使用。"""
    import yaml  # datasette 依赖里自带 pyyaml

    cfg_dir = llm_config_dir()
    cfg_dir.mkdir(parents=True, exist_ok=True)

    keys_path = cfg_dir / "keys.json"
    try:
        keys = json.loads(keys_path.read_text())
    except (OSError, ValueError):
        keys = {}
    keys[key_name] = key
    keys_path.write_text(json.dumps(keys, indent=2))

    models_path = cfg_dir / "extra-openai-models.yaml"
    try:
        models = yaml.safe_load(models_path.read_text()) or []
    except OSError:
        models = []
    models = [m for m in models if m.get("model_id") != model_id]
    models.append({
        "model_id": model_id,
        "model_name": model_id.split("/")[-1],
        "api_base": api_base,
        "api_key_name": key_name,
        "supports_tools": True,
    })
    models_path.write_text(yaml.safe_dump(models, sort_keys=False, allow_unicode=True))
    print(f"✓ model registered: {model_id} -> {api_base}")
    print(f"  use: --model {model_id}  或  export DB_AGENT_MODEL={model_id}")


def serve(snapshot: str, port: int, model: str) -> None:
    check_api_key(model)
    snap = Path(snapshot)
    if not snap.exists():
        sys.exit(f"ERROR: snapshot 不存在: {snap}")
    cfg = write_config(snap, model)
    print(f"Agent UI: http://localhost:{port}/-/agent")
    print(f"Data UI:  http://localhost:{port}/")
    datasette = str(VENV / "bin" / "datasette")
    os.execv(datasette, [datasette, str(snap), "-c", str(cfg), "-p", str(port), "-h", "127.0.0.1"])


def chat(snapshot: str, prompt: str, model: str) -> None:
    check_api_key(model)
    datasette = str(VENV / "bin" / "datasette")
    proc = subprocess.run(
        [datasette, "agent", "chat", snapshot, "-m", model, "-p", prompt],
        stdin=subprocess.DEVNULL,
    )
    sys.exit(proc.returncode)


def main() -> None:
    p = argparse.ArgumentParser(prog="dbagent", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("setup", help="创建/刷新 venv")
    sp.add_argument("--force", action="store_true", help="删掉重建")

    for name in ("export", "run"):
        sp = sub.add_parser(name, help=f"{name} 快照")
        sp.add_argument("db_url", help="postgres:// URL 或 SQLite 文件路径")
        sp.add_argument("-o", "--out", default="dbagent-snapshot.db")
        sp.add_argument("--include", nargs="*", default=[], help="只导这些表")
        sp.add_argument("--exclude", nargs="*", default=[], help="跳过这些表（如敏感表）")
        if name == "run":
            sp.add_argument("--port", type=int, default=8765)
            sp.add_argument("--model", default=DEFAULT_MODEL)

    sp = sub.add_parser("serve", help="启动 Web 服务")
    sp.add_argument("snapshot")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--model", default=DEFAULT_MODEL)

    sp = sub.add_parser("chat", help="CLI 一次性提问")
    sp.add_argument("snapshot")
    sp.add_argument("-p", "--prompt", required=True)
    sp.add_argument("--model", default=DEFAULT_MODEL)

    sp = sub.add_parser("add-model", help="注册 OpenAI 兼容模型（如 xAI Grok）")
    sp.add_argument("model_id", help="如 xai/grok-4.3")
    sp.add_argument("--api-base", required=True, help="如 https://api.x.ai/v1")
    sp.add_argument("--key", help="API key（或用 --key-env 从环境变量读）")
    sp.add_argument("--key-env", help="存有 key 的环境变量名，如 XAI_API_KEY")
    sp.add_argument("--key-name", default=None, help="keys.json 里的名字，默认取 model_id 前缀")

    args = p.parse_args()

    if args.cmd == "setup":
        setup(force=args.force)
        return

    ensure_venv()

    if args.cmd == "add-model":
        key = args.key or (os.environ.get(args.key_env, "") if args.key_env else "")
        if not key:
            sys.exit("ERROR: 需要 --key 或 --key-env（且对应环境变量已设置）")
        key_name = args.key_name or (args.model_id.split("/")[0] if "/" in args.model_id else args.model_id)
        add_model(args.model_id, args.api_base, key, key_name)
    elif args.cmd == "export":
        export(args.db_url, args.out, args.include, args.exclude)
    elif args.cmd == "serve":
        serve(args.snapshot, args.port, args.model)
    elif args.cmd == "chat":
        chat(args.snapshot, args.prompt, args.model)
    elif args.cmd == "run":
        out = export(args.db_url, args.out, args.include, args.exclude)
        serve(out, args.port, args.model)


if __name__ == "__main__":
    main()
