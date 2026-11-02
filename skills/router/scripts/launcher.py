"""worker 的起停：每张票一个 worktree，里面后台跑 `claude -p … --output-format json`。

spike（docs/superpowers/reports/2026-11-02-router-headless-spike.md）定下的事实：
- 进程结束时 stdout 写一个 type=result 的 JSON；中途死掉就没有 → read_result 返回 None
- `--session-id` 首发、`--resume` 续跑，同一 session；用户可 `claude --resume <id>` 接管
- 起进程要脱离 router 的进程组（start_new_session），并剔除 router 的会话变量（Ctx 负责）
"""
import json
import os
import signal
import uuid
from pathlib import Path


def _git(ctx, repo, *args):
    r = ctx.run(["git", "-C", str(repo), *args])
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} 失败：{(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout


def create_worktree(ctx, repo: Path, base_branch: str, wt_dir: str, tid: str):
    """幂等：上次派到一半崩了（worktree 或分支已在），这次接着用，不报错。"""
    branch = f"router/{tid}"
    wt = Path(repo) / wt_dir / f"router-{tid}"
    if wt.is_dir():
        return wt, branch
    _git(ctx, repo, "fetch", "origin", base_branch)
    exists = ctx.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"]).stdout.strip()
    if exists:
        _git(ctx, repo, "worktree", "add", str(wt), branch)
    else:
        _git(ctx, repo, "worktree", "add", "-b", branch, str(wt), f"origin/{base_branch}")
    return wt, branch


def worker_alive(ctx, w: dict) -> bool:
    """pid 活着且启动时间和记录的一致（记录了的话）。pid 被别的进程复用 = 我们的 worker 已经死了。"""
    pid = w.get("pid")
    if not pid or not ctx.pid_alive(pid):
        return False
    return not w.get("pid_start") or ctx.proc_start(pid) == w["pid_start"]


def stop_worker(ctx, w: dict) -> None:
    """只停我们自己起的那个进程组；身份对不上就不发信号（免得误杀复用了 pid 的无关进程）。"""
    if worker_alive(ctx, w):
        stop_now(w["pid"])


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


def run_worker(ctx, home: Path, cfg: dict, state: dict, tid: str, prompt: str, resume: bool, at: str) -> int:
    """起（或续跑）一张票的 worker，并把 pid / 日志 / 次数记回 ticket。每次运行一个新日志文件，
    这样上一次的结果对象不会被误读成这一次的。"""
    w = state["tickets"][tid]["worker"]
    w["session_id"] = w.get("session_id") or str(uuid.uuid4())
    w["runs"] = w.get("runs", 0) + 1
    log = Path(home) / "logs" / f"{tid}-{w['runs']}.log"
    env = {"ROUTER_TICKET": tid, "ROUTER_HOME": str(home),
           "ROUTER_CLI": str(Path(__file__).resolve().with_name("router.py"))}
    w["pid"] = spawn(ctx, Path(w["worktree"]), claude_cmd(cfg["worker"], prompt, w["session_id"], resume), log, env)
    w.update(log=str(log), started_at=at, pid_start=ctx.proc_start(w["pid"]))
    w.pop("exit", None)
    return w["pid"]
