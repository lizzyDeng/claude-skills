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


LS_REMOTE_S, FETCH_S, PUSH_S = 30, 180, 120     # 弱网上界（plan §4②）：超时 rc=124 一律按 unknown


def _git(ctx, repo, *args, timeout=600):
    r = ctx.run(["git", "-C", str(repo), *args], timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args[:2])} 失败：{(r.stderr or r.stdout).strip()[:300]}")
    return r.stdout


def _err(r) -> str:
    return (r.stderr or r.stdout or f"rc={r.returncode}").strip()[:300]


# ---- 远端 base（R1）----------------------------------------------------------

def remote_branch(ctx, repo, branch):
    """→ ("present", sha) | ("missing", "") | ("unknown", 错误)。用完整 ref 名：staging 不会撞上 staging-old。
    rc 0 = 在、2 = 不在；其它（128 不可达、124 超时……）= 不知道 —— 网络坏了绝不当成「不存在」。"""
    r = ctx.run(["git", "-C", str(repo), "ls-remote", "--exit-code", "origin", f"refs/heads/{branch}"],
                timeout=LS_REMOTE_S)
    if r.returncode == 0:
        for line in (r.stdout or "").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[1] == f"refs/heads/{branch}":
                return "present", parts[0]
        return "unknown", f"ls-remote 输出看不懂：{(r.stdout or '').strip()[:120]}"
    if r.returncode == 2:
        return "missing", ""
    return "unknown", _err(r)


def tracking_sha(ctx, repo, branch):
    """本地 refs/remotes/origin/<branch> 的 sha（「router 见过这个分支」的证据之一）；没有就 None。"""
    r = ctx.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"])
    out = (r.stdout or "").strip()
    return out if r.returncode == 0 and out else None


def rebuild_branch(ctx, repo, base, main, main_sha) -> dict:
    """远端 base 被删时从 main 的 sha 重建。只推远端已有的 commit：
    --force-with-lease=refs/heads/<base>:（空期望值）= 只在远端仍然没有这个分支时创建，绝不覆盖别人刚建的。
    --no-verify：推的是远端已有的 commit；aifriends 的 pre-push 只校验工作区，而主 checkout 常年是脏的。
    → {"ok": True, "sha", "by_other"?, "warn"?} | {"ok": False, "reason": "foreign_tip"|"push"|"fetch"|"same", …}"""
    if base == main:
        return {"ok": False, "reason": "same", "error": f"base 与 main 同为 {main}：不重建"}
    r = ctx.run(["git", "-C", str(repo), "fetch", "origin", main], timeout=FETCH_S)
    if r.returncode != 0:
        return {"ok": False, "reason": "fetch", "error": f"git fetch origin {main} 失败：{_err(r)}"}
    r = ctx.run(["git", "-C", str(repo), "push", "--no-verify", f"--force-with-lease=refs/heads/{base}:", "origin",
                 f"{main_sha}:refs/heads/{base}"], timeout=PUSH_S)
    if r.returncode != 0:
        push_err = _err(r)
        kind, sha = remote_branch(ctx, repo, base)      # 被拒 ≠ 别人替我们重建了：别人也可能建出任意 tip
        if kind == "present" and sha == main_sha:
            return {"ok": True, "by_other": True, "sha": sha}
        if kind == "present":
            return {"ok": False, "reason": "foreign_tip", "sha": sha, "error": push_err}
        return {"ok": False, "reason": "push", "error": f"git push 失败：{push_err}"}
    out = {"ok": True, "sha": main_sha}
    r = ctx.run(["git", "-C", str(repo), "fetch", "origin", base], timeout=FETCH_S)
    if r.returncode != 0:                                 # 推上去了就算成功；本地跟踪分支下次 fetch 再补
        out["warn"] = f"重建后 git fetch origin {base} 失败：{_err(r)}"
    return out


def is_ancestor(ctx, repo, base, a, b):
    """a 是不是 b 的祖先（b 是不是从 a 前进而来）→ True | False | None（取不到 / 判不了）。"""
    r = ctx.run(["git", "-C", str(repo), "fetch", "origin", base], timeout=FETCH_S)
    if r.returncode != 0:
        return None
    r = ctx.run(["git", "-C", str(repo), "merge-base", "--is-ancestor", a, b])
    return {0: True, 1: False}.get(r.returncode)


def create_worktree(ctx, repo: Path, base_branch: str, wt_dir: str, tid: str):
    """幂等：上次派到一半崩了（worktree 或分支已在），这次接着用，不报错。"""
    branch = f"router/{tid}"
    wt = Path(repo) / wt_dir / f"router-{tid}"
    if wt.is_dir():
        return wt, branch
    _git(ctx, repo, "fetch", "origin", base_branch, timeout=FETCH_S)
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


def _registered_worktrees(ctx, repo) -> set:
    out = ctx.run(["git", "-C", str(repo), "worktree", "list", "--porcelain"]).stdout or ""
    return {line[len("worktree "):].strip() for line in out.splitlines() if line.startswith("worktree ")}


def cleanup_ticket_checkout(ctx, repo, wt, branch) -> dict:
    """票完成后清本地：worktree 干净才删，分支没有未推送的提交才删。幂等（已删的算 done）。
    每栏三种结果：
      "done"            —— 删了，或本来就没有；调用方清空 worker.worktree / worker.branch 指针
      "kept: 原因"      —— 有意保留（未提交改动 / 未推送提交 / worktree 保留），要人处理，不再重试；指针保留
      "error: 原因"     —— git 命令失败（锁、权限、瞬态），下一轮重试；指针保留"""
    res = {}
    p = Path(wt) if wt else None
    if p and p.exists():
        st = ctx.run(["git", "-C", str(p), "status", "--porcelain"])
        if st.returncode != 0:
            res["worktree"] = f"error: git status 失败：{_err(st)}"
        elif (st.stdout or "").strip():
            res["worktree"] = "kept: 有未提交改动"
        else:
            r = ctx.run(["git", "-C", str(repo), "worktree", "remove", str(p)])
            res["worktree"] = "done" if r.returncode == 0 else f"error: git worktree remove 失败：{_err(r)}"
    else:
        if p and str(p) in _registered_worktrees(ctx, repo):          # 路径没了、git 还登记着
            r = ctx.run(["git", "-C", str(repo), "worktree", "prune"])
            res["worktree"] = "done" if r.returncode == 0 else f"error: git worktree prune 失败：{_err(r)}"
        else:
            res["worktree"] = "done"
    if res["worktree"] != "done":
        res["branch"] = "kept: worktree 保留" if res["worktree"].startswith("kept") else "error: worktree 还没清"
        return res
    if not branch:
        res["branch"] = "done"
        return res
    exists = ctx.run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"])
    if exists.returncode != 0 or not (exists.stdout or "").strip():
        res["branch"] = "done"
        return res
    r = ctx.run(["git", "-C", str(repo), "rev-list", "--count", branch, "--not", "--remotes"])
    n = (r.stdout or "").strip()
    if r.returncode != 0 or not n.isdigit():
        res["branch"] = f"error: 数不出未推送提交：{_err(r)}"
    elif int(n) > 0:
        res["branch"] = f"kept: {n} 个未推送提交"
    else:
        d = ctx.run(["git", "-C", str(repo), "branch", "-D", branch])
        res["branch"] = "done" if d.returncode == 0 else f"error: git branch -D 失败：{_err(d)}"
    return res


# ---- 起 worker：准备（纯内存）/ 起进程 / 启动回执（R2 + H4）-----------------------

def log_path(home, tid, runs) -> Path:
    return Path(home) / "logs" / f"{tid}-{runs}.log"


def receipt_path(log) -> Path:
    """启动回执：与这一次运行的日志同名、后缀 .receipt.json。"""
    log = Path(log)
    return log.with_name(log.stem + ".receipt.json")


def prepare_run(w: dict, home, tid) -> Path:
    """纯内存：没有 session 就分配一个、运行次数 +1，返回这一次的日志路径。不做任何 IO。"""
    w["session_id"] = w.get("session_id") or str(uuid.uuid4())
    w["runs"] = w.get("runs", 0) + 1
    return log_path(home, tid, w["runs"])


def spawn_run(ctx, home, cfg, state, tid, prompt, resume, at, log=None, receipt=None) -> int:
    """起进程，写回 pid / 日志 / 启动时间。抛异常 ⇔ 子进程没起（ctx.spawn_detached 的语义）。
    receipt 给了：启动 wrapper 在 exec claude 之前把 {session_id, pid, pid_start} 原子写到这里。
    查启动时间失败只记 None（worker_alive 本来就容忍 None），不算没起。"""
    w = state["tickets"][tid]["worker"]
    log = Path(log) if log else log_path(home, tid, w.get("runs", 0))
    env = {"ROUTER_TICKET": tid, "ROUTER_HOME": str(home),
           "ROUTER_CLI": str(Path(__file__).resolve().with_name("router.py"))}
    if receipt:
        env.update(ROUTER_RECEIPT=str(receipt), ROUTER_RECEIPT_SID=w["session_id"])
    pid = spawn(ctx, Path(w["worktree"]), claude_cmd(cfg["worker"], prompt, w["session_id"], resume), log, env)
    try:
        started = ctx.proc_start(pid)
    except Exception:  # noqa: BLE001 — 进程已经起了：查不到启动时间不影响认领
        started = None
    w.update(pid=pid, log=str(log), started_at=at, pid_start=started)
    w.pop("exit", None)
    return pid


class IntentNotSaved(Exception):
    """起进程之前，意图没落盘：一个进程都没起（worker 字段已还原）。"""


def begin_intent(t: dict, home, tid, kind, at, **extra):
    """纯内存：分配这一次运行（prepare_run）并写下意图 → (log, receipt, before)。
    意图 = {phase: spawning, kind: dispatch | resume, session_id, log, receipt, at, …}：at 是写意图这一刻
    （起进程前一刻），回执宽限期从它算起。"""
    w = t["worker"]
    before = {k: w.get(k) for k in ("session_id", "runs")}
    log = prepare_run(w, home, tid)
    receipt = receipt_path(log)
    t["dispatch"] = {"phase": "spawning", "kind": kind, "session_id": w["session_id"], "log": str(log),
                     "receipt": str(receipt), "at": at, **extra}
    return log, receipt, before


def abort_intent(t: dict, before: dict) -> None:
    t.pop("dispatch", None)
    t["worker"].update(before)


def resume_with_intent(ctx, home, cfg, state, tid, prompt, status, waiting, at, save) -> int:
    """续跑与派单同一个事务（R2 + H4，RV I-1）：意图（含启动回执路径、续跑后的状态）落盘 → 起进程（带回执）。
    返回 pid 时意图已从内存撤掉，调用方要**立刻存盘**；那次存盘失败 ⇒ 盘上留着意图，下一轮按回执认领，不重起。
    save() 失败 ⇒ 抛 IntentNotSaved，一个进程都不起；起进程失败 ⇒ 撤意图、还原 worker 字段后原样抛出。"""
    t = state["tickets"][tid]
    log, receipt, before = begin_intent(t, home, tid, "resume", at, status=status, waiting=waiting)
    try:
        save()
    except Exception as e:  # noqa: BLE001
        abort_intent(t, before)
        raise IntentNotSaved(repr(e)) from e
    try:
        pid = spawn_run(ctx, home, cfg, state, tid, prompt, True, at, log=log, receipt=receipt)
    except Exception:
        abort_intent(t, before)
        raise
    t.pop("dispatch", None)
    return pid


def run_worker(ctx, home: Path, cfg: dict, state: dict, tid: str, prompt: str, resume: bool, at: str) -> int:
    """起（或续跑）一张票的 worker，并把 pid / 日志 / 次数记回 ticket。每次运行一个新日志文件，
    这样上一次的结果对象不会被误读成这一次的。"""
    log = prepare_run(state["tickets"][tid]["worker"], home, tid)
    return spawn_run(ctx, home, cfg, state, tid, prompt, resume, at, log=log)


def read_receipt(path):
    """→ dict | None（文件不在）。读得到但看不懂（半截 / 字段不对）抛 ValueError：调用方按「核实不了」处理。"""
    p = Path(path) if path else None
    if not p or not p.exists():
        return None
    r = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(r, dict) or not isinstance(r.get("pid"), int) or not r.get("session_id"):
        raise ValueError(f"启动回执字段不全：{p}")
    return r


def receipt_liveness(ctx, r: dict) -> str:
    """按回执判活 → "alive" | "dead" | "unknown"。复用 worker_alive（pid + 进程启动时间）：
    pid 不在 = dead；pid 在、启动时间对不上 = pid 被复用 = dead；
    pid 在、但查不到启动时间（或回执里没记）= unknown —— 认不出是不是我们的进程，不派也不杀。"""
    pid = r.get("pid")
    if not pid or not ctx.pid_alive(pid):
        return "dead"
    try:
        now = ctx.proc_start(pid)
    except Exception:  # noqa: BLE001
        return "unknown"
    if not now or not r.get("pid_start"):
        return "unknown"
    return "alive" if worker_alive(ctx, {"pid": pid, "pid_start": r["pid_start"]}) else "dead"
