"""worker 的起停：每张票一个 worktree，里面后台跑 `claude -p … --output-format json`。

spike（docs/superpowers/reports/2026-11-02-router-headless-spike.md）定下的事实：
- 进程结束时 stdout 写一个 type=result 的 JSON；中途死掉就没有 → read_result 返回 None
- `--session-id` 首发、`--resume` 续跑，同一 session；用户可 `claude --resume <id>` 接管
- 起进程要脱离 router 的进程组（start_new_session），并剔除 router 的会话变量（Ctx 负责）
"""
import json
import os
import signal
from pathlib import Path


def _git(ctx, repo, *args):
    r = ctx.run(["git", "-C", str(repo), *args])
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} 失败：{(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout


def create_worktree(ctx, repo: Path, base_branch: str, wt_dir: str, tid: str):
    branch = f"router/{tid}"
    wt = Path(repo) / wt_dir / f"router-{tid}"
    _git(ctx, repo, "fetch", "origin", base_branch)
    _git(ctx, repo, "worktree", "add", "-b", branch, str(wt), f"origin/{base_branch}")
    return wt, branch


def claude_cmd(cfg: dict, prompt: str, session_id: str, resume: bool) -> list:
    binary = cfg.get("claude_bin", "claude")
    cmd = [*([binary] if isinstance(binary, str) else binary), "-p", prompt,
           "--output-format", "json", "--model", cfg["model"],
           "--permission-mode", cfg["permission_mode"]]
    cmd += ["--resume", session_id] if resume else ["--session-id", session_id]
    if cfg.get("allowed_tools"):
        cmd += ["--allowedTools", ",".join(cfg["allowed_tools"])]
    if cfg.get("disallowed_tools"):
        cmd += ["--disallowedTools", *cfg["disallowed_tools"]]   # 变长参数，必须放最后
    return cmd


def spawn(ctx, wt: Path, cmd, log: Path, env=None) -> int:
    return ctx.spawn_detached(cmd, wt, log, env=env)


def read_result(log: Path):
    """日志里最后一个能解析的 JSON 对象（优先 type=result）；没有就 None。"""
    log = Path(log)
    if not log.exists():
        return None
    raw = log.read_text(encoding="utf-8", errors="replace")
    dec, i, objs = json.JSONDecoder(), 0, []
    while True:
        j = raw.find("{", i)
        if j < 0:
            break
        try:
            obj, i = dec.raw_decode(raw, j)
            if isinstance(obj, dict):
                objs.append(obj)
        except ValueError:
            i = j + 1
    results = [o for o in objs if o.get("type") == "result"]
    return (results or objs or [None])[-1]


def stop_now(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)          # pid 即进程组号（start_new_session）
    except (ProcessLookupError, PermissionError):
        pass


def remove_worktree(ctx, repo, wt) -> None:
    _git(ctx, repo, "worktree", "remove", "--force", str(wt))
