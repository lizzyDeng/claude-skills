"""方向审的 codex：review-pack（拼材料 + 读边界 + 三道预检 → 生成 codex.sh）与 review-canary（真 codex exec 金丝雀）。

读边界（用户 11-03 裁决 H2：默认拒绝，不靠 denylist 当安全闸）：
- 拒读整个 HOME；只放行审查目录（仓内票 = 它的 worktree，仓外票 = 拷出来的 pack）和 codex 跑命令要用的
  ~/.nvm、~/.codex/config.toml（另可在 config 的 codex_allow_read 加）。
- 另外显式拒读：router home、所有仓外代码面与周报数据（leak_guard_globs）的目录、~/.claude/projects、
  ~/.codex/sessions、config 的 codex_deny_read —— HOME 之外的数据目录靠这一层。
- 放行项不许罩住 HOME 或任何敏感目录，也不许钻进敏感目录（仓外 pack 例外：它在 router home 里，内容是挑过的）。

能不能信这个边界，看「版本绑定的金丝雀」：review-canary 用真 codex exec、和 codex.sh 同一个命令形状，
让模型 cat 三个文件：pack 里的正对照、显式拒读目录里的假数据、一个没列进任何名单的 HOME 同级目录里的假数据。
只有「正对照读到 + 两份假数据都报 OS 原文 Operation not permitted + 没有假数据令牌」才算过，结果按 codex 版本记进
<home>/review/canary.json。review-pack 每次都查：版本对不上（codex 自动升级了）⇒ canary_stale，要先补跑金丝雀。

所有失败一律 fail-closed：不生成 codex.sh，调用方（SKILL §4）本轮不跑 codex，只用 Opus 方向审。
"""
import json
import os
import re
import shlex
import shutil
import uuid
from pathlib import Path

import github
import store
import surfaces
import tick

SKILL_DIR = Path(__file__).resolve().parent.parent
PROFILE = "router_review"
EXEC_S, PREFLIGHT_S, VERSION_S = 330, 60, 15          # 弱网上界（plan §4②）；超时 rc=124 一律按失败
CODE_EXT = {".py", ".sh", ".md", ".toml", ".yaml", ".yml", ".rs", ".ts", ".tsx", ".js", ".sql"}
MAX_BYTES = 512 * 1024
DEFAULT_ALLOW = ("~/.nvm", "~/.codex/config.toml")    # codex 的 rg / node 运行时在 ~/.nvm 下；config.toml 是它自己的配置
SENSITIVE_HOME = ("~/.claude/projects", "~/.codex/sessions")
OS_DENIED = "Operation not permitted"                 # 只认 OS 原文：模型自己写得出「denied / Permission denied」
SIBLING = ".router-canary-probe"                      # 金丝雀在 HOME 下临时建的同级目录（没列进任何名单）
GLOB_CHARS = set("*?[")
_BAD_PATH = re.compile(r'["\\\x00-\x1f]')
# 卡里的路径：以 ~/ 或 / 开头；前面不能紧挨着字母数字 / . / ~ / : / -（排除 URL 和相对路径的中段）
_PATH = re.compile(r"(?<![\w./~:-])(~?/[^\s`'\"<>()（）「」『』，。；、！？：:]+(?::\d+(?:-\d+)?)?)")


class Refused(Exception):
    """review-pack / review-canary 不给出 codex 命令。reason：guard / card / leak / worktree / boundary /
    canary_stale / canary_failed / canary_leak / canary_inconclusive。"""

    def __init__(self, msg, reason, **extra):
        super().__init__(msg)
        self.reason, self.extra = reason, extra


# ---- 路径小工具 ----------------------------------------------------------------

def _real(p) -> str:
    return os.path.realpath(os.path.expanduser(str(p)))


def _under(p, d) -> bool:
    """p 是 d 本身或在 d 下面。"""
    return p == d or p.startswith(d.rstrip("/") + "/")


def _static_prefix(glob) -> str:
    """glob 里第一个带通配符的段之前的目录：~/.her-persona-daily/runs/*/*.json → ~/.her-persona-daily/runs。
    没有通配符、看着像文件（有扩展名）→ 它的父目录（拒读名单只写目录）。"""
    parts = os.path.expanduser(glob).split("/")
    keep = []
    for part in parts:
        if GLOB_CHARS & set(part):
            break
        keep.append(part)
    prefix = "/".join(keep) or "/"
    if len(keep) == len(parts) and Path(prefix).suffix:
        prefix = str(Path(prefix).parent)
    return prefix


def _covering(entries: dict, path) -> tuple:
    """→ (罩住 path 的最具体条目, 模式)；没有 → (None, None)。与 codex「最具体的路径生效」同一套判法。"""
    p, best = _real(path), (None, None)
    for k, m in entries.items():
        if _under(p, k) and (best[0] is None or len(k) > len(best[0])):
            best = (k, m)
    return best


# ---- 读边界 --------------------------------------------------------------------

def _data_prefixes(env) -> list:
    return [_static_prefix(g) for g in env.cfg.get("leak_guard_globs", [])]


def sensitive_denies(env) -> list:
    """显式拒读的目录（HOME 之外的数据目录只靠这一层）：存在的才写，realpath 去重。"""
    cand = [env.home]
    cand += [_static_prefix(g) for spec in env.table.values() for g in spec.get("globs", [])
             if tick.is_offrepo_glob(g, env.root)]
    cand += _data_prefixes(env)
    cand += list(SENSITIVE_HOME) + list(env.cfg.get("codex_deny_read", []))
    out = []
    for c in cand:
        r = _real(c)
        if r != "/" and os.path.isdir(r) and r not in out:
            out.append(r)
    return out


def allow_list(env) -> list:
    out = []
    for c in list(DEFAULT_ALLOW) + list(env.cfg.get("codex_allow_read", [])):
        r = _real(c)
        if os.path.exists(r) and r not in out:
            out.append(r)
    return out


def boundary(env, review_dir, extra_deny=()) -> tuple:
    """→ (deny, read)。deny[0] = HOME（默认拒绝）；read[0] = 审查目录。违反下面任何一条 ⇒ Refused(boundary)：
    放行项不能是 HOME 或它的祖先；不能罩住任何敏感目录；allowlist 不能钻进敏感目录；
    仓内 worktree 不能落在敏感目录里（仓外 pack 在 router home 里，是有意的例外）。"""
    home = _real("~")
    if home == "/" or not os.path.isdir(home):
        raise Refused(f"HOME（{home}）不能当默认拒绝的根", "boundary")
    sens = [d for d in sensitive_denies(env) + [_real(x) for x in extra_deny] if d != home]
    sens = list(dict.fromkeys(sens))
    rd = _real(review_dir)
    pack_root = _real(env.home / "review")
    reads = list(dict.fromkeys([rd] + allow_list(env)))
    for r in reads:
        if _under(home, r):
            raise Refused(f"放行项 {r} 罩住了整个 HOME", "boundary")
        for d in sens:
            if _under(d, r):
                raise Refused(f"放行项 {r} 罩住了拒读目录 {d}", "boundary")
            if _under(r, d) and not (r == rd and _under(rd, pack_root)):
                raise Refused(f"放行项 {r} 落在拒读目录 {d} 里", "boundary")
    for p in [home] + sens + reads:
        if _BAD_PATH.search(p):
            raise Refused(f"路径里有引号 / 反斜杠 / 控制字符，没法安全写进 codex 配置：{p!r}", "boundary")
    return [home] + sens, reads


def build_profile_args(deny, read) -> list:
    """自定义 permission profile：extends :read-only，再按目录拒读 / 放行（codex 按最具体的路径生效）。
    codex exec 没有 -P，所以用 -c default_permissions 选中它；codex sandbox 另加 -P。"""
    fs = ", ".join([f'"{d}"="deny"' for d in deny] + [f'"{r}"="read"' for r in read])
    return ["-c", f'default_permissions="{PROFILE}"',
            "-c", f'permissions.{PROFILE}.extends=":read-only"',
            "-c", f"permissions.{PROFILE}.filesystem={{{fs}}}"]


def build_exec_cmd(review_dir, prompt, out, profile_args, codex="codex", timeout_bin="timeout") -> str:
    """codex.sh 和 review-canary 共用的唯一命令形状：PROMPT 由 bash 重定向喂给 stdin（PROMPT 在 router home 里，
    codex 读不到，也不需要读）；输出写到 router home 的 cards/ 下；审查目录里什么都不写。"""
    q = shlex.quote
    return (f"exec {q(timeout_bin)} {EXEC_S} {q(codex)} exec -C {q(str(review_dir))} -s read-only --skip-git-repo-check "
            f"{' '.join(q(a) for a in profile_args)} - < {q(str(prompt))} > {q(str(out))} 2>&1")


# ---- codex 探测与预检 ------------------------------------------------------------

def _bins():
    codex = shutil.which("codex")
    if not codex:
        raise Refused("PATH 里没有 codex", "boundary")
    tbin = shutil.which("timeout") or shutil.which("gtimeout")
    if not tbin:
        raise Refused("没有 timeout / gtimeout 命令（brew install coreutils）：codex 不能不设上限地跑", "boundary")
    return codex, tbin


def _run(env, cmd, cwd, timeout):
    try:
        return env.ctx.run(cmd, cwd=str(cwd), timeout=timeout)
    except OSError as e:
        raise Refused(f"跑不了 {' '.join(map(str, cmd[:3]))}：{e}", "boundary")


def codex_version(env, codex, cwd) -> str:
    r = _run(env, [codex, "--version"], cwd, VERSION_S)
    v = (r.stdout or "").strip()
    if r.returncode != 0 or not v:
        raise Refused(f"codex --version 失败（rc={r.returncode}）：{(r.stderr or r.stdout or '').strip()[:200]}", "boundary")
    return v


def canary_path(env) -> Path:
    return env.home / "review" / "canary.json"


def preflight(env, codex, profile_args, deny, cwd) -> str:
    """三道预检，任一不过 ⇒ Refused。→ 当前 codex 版本。
    1 debug prompt-input：模型看得到「Denied filesystem reads」，且每一条拒读路径都在里面（配置被解析了）。
    2 codex sandbox 里 ls HOME：OS 报 Operation not permitted（默认拒绝真的生效）。
    3 canary.json：这个 codex 版本跑过金丝雀并且过了（真 codex exec 链路的证据）。"""
    r = _run(env, [codex, "debug", "prompt-input", *profile_args], cwd, PREFLIGHT_S)
    if r.returncode != 0 or "Denied filesystem reads" not in (r.stdout or ""):
        raise Refused(f"预检 1：prompt 里没有拒读段（rc={r.returncode}）", "boundary")
    missing = [d for d in deny if d not in r.stdout]
    if missing:
        raise Refused(f"预检 1：这些拒读路径没出现在 codex 的拒读段里：{missing}", "boundary")
    r = _run(env, [codex, "sandbox", *profile_args, "-P", PROFILE, "-C", str(cwd), "--", "/bin/ls", deny[0]],
             cwd, PREFLIGHT_S)
    if r.returncode == 0 or OS_DENIED not in (r.stdout or "") + (r.stderr or ""):
        raise Refused(f"预检 2：沙箱里 ls {deny[0]} 没有被 OS 拒绝（rc={r.returncode}）", "boundary")
    version = codex_version(env, codex, cwd)
    try:
        c = json.loads(canary_path(env).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        c = None
    if not isinstance(c, dict) or c.get("codex_version") != version:
        was = (c or {}).get("codex_version") if isinstance(c, dict) else None
        raise Refused(f"金丝雀没跑过或过期（记录 {was}，当前 {version}）：先 review-canary", "canary_stale",
                      codex_version=version)
    if c.get("ok") is not True:
        raise Refused(f"这个 codex 版本的金丝雀没过（{c.get('reason')}）", "canary_failed", codex_version=version)
    return version


# ---- review-pack ------------------------------------------------------------------

def _card_paths(text) -> list:
    out = []
    for m in _PATH.finditer(text):
        tok = re.sub(r":\d+(?:-\d+)?$", "", m.group(1).rstrip(".,;"))
        if tok not in out:
            out.append(tok)
    return out


def _collect(env, t, card, guard, pack) -> tuple:
    """仓外票：从方向卡抽路径，只把「本票代码面里的干净代码」拷进 pack/src/。排除原因依次：
    not_found / not_file → outside_surface → data_dir → ext → too_big → leak。"""
    globs = [g for s in t.get("surfaces", []) for g in env.table.get(s, {}).get("globs", [])]
    data = [(os.path.expanduser(p), _real(p)) for p in _data_prefixes(env)]
    home = _real("~")
    files, excluded = [], []
    for tok in _card_paths(card):
        raw, real = os.path.expanduser(tok), _real(tok)
        if not os.path.exists(real):
            reason = "not_found"
        elif not os.path.isfile(real):
            reason = "not_file"
        elif not any(surfaces._match(x, g) for x in (raw, real) for g in globs):
            reason = "outside_surface"
        elif any(_under(raw, dr) or _under(real, dr) or _under(real, dx) for dr, dx in data):
            reason = "data_dir"
        elif Path(real).suffix.lower() not in CODE_EXT:
            reason = "ext"
        elif os.path.getsize(real) > MAX_BYTES:
            reason = "too_big"
        elif github.leak_hits(Path(real).read_text(encoding="utf-8", errors="replace"), guard):
            reason = "leak"
        else:
            rel = os.path.relpath(real, home) if _under(real, home) else real.lstrip("/")
            dest = pack / "src" / rel
            if not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(real, dest)
                files.append(str(dest.relative_to(pack)))
            continue
        excluded.append({"path": tok, "reason": reason})
    return files, excluded


def _prompt(t, tid, card, review_dir, mode) -> str:
    why = t.get("why") or {}
    sol = t.get("solution") or {}
    where = (f"材料只在 {review_dir} 里：card.md 是方向卡，src/ 是卡上引用的代码副本" if mode == "pack"
             else f"材料只在 {review_dir} 里（这张票的 worktree）")
    return "\n".join([
        (SKILL_DIR / "templates" / "direction_review.md").read_text(encoding="utf-8").rstrip(), "",
        "## 派单", f"- 票：{tid}「{t.get('title', '')}」",
        f"- 为什么派：{why.get('metric', '')} · {why.get('users', '')}（证据：{why.get('evidence', '')}）",
        f"- 解决什么问题：{t.get('problem', '')}", f"- 锚点：{t.get('problem_anchor', '') or '（无）'}",
        f"- router 线索（非方案）：{sol.get('text', '') if sol.get('kind') != 'card' else '（见方向卡）'}", "",
        "## 方向卡", "", card.rstrip(), "",
        "## 读边界", f"{where}；不要读、也不要列这个目录以外的任何路径。", ""])


def pack(env, tid, round_no=1) -> dict:
    try:
        guard = github.load_guard(env.cfg.get("leak_guard_globs", []))
    except github.GuardEmpty as e:
        raise Refused(f"防泄漏闸失效：{e}", "guard")
    rdir = env.home / "review" / tid
    shutil.rmtree(rdir, ignore_errors=True)                       # 先删后建：失败时不留旧的 codex.sh
    st = store.load_state(env.state_path)                          # 只读，不存盘
    t = st["tickets"].get(tid)
    if not t:
        raise Refused(f"没有票 {tid}", "card")
    card_path = Path((t.get("card") or {}).get("path") or env.home / "cards" / f"{tid}.md")
    if not card_path.is_file():
        raise Refused(f"方向卡不存在：{card_path}", "card")
    card = card_path.read_text(encoding="utf-8", errors="replace")
    hits = github.leak_hits(card, guard)
    if hits:
        raise Refused("方向卡含用户 uid 或原话：先改卡再审", "leak", hits=hits)
    if tick.is_offrepo(t, env.table, env.root):
        mode, review_dir = "pack", rdir / "pack"
    else:
        wt = (t.get("worker") or {}).get("worktree")
        if not wt or not Path(wt).is_dir():
            raise Refused(f"{tid} 是仓内票，但没有 worktree（{wt}）", "worktree")
        mode, review_dir = "worktree", Path(wt)
    prompt = _prompt(t, tid, card, review_dir, mode)
    hits = github.leak_hits(prompt, guard)
    if hits:
        raise Refused("PROMPT 含用户 uid 或原话（派单字段）", "leak", hits=hits)
    codex, tbin = _bins()
    files, excluded = [], []
    if mode == "pack":
        review_dir.mkdir(parents=True)
        (review_dir / "card.md").write_text(card, encoding="utf-8")
        files, excluded = _collect(env, t, card, guard, review_dir)
        files = ["card.md"] + files
    rdir.mkdir(parents=True, exist_ok=True)
    deny, read = boundary(env, review_dir)
    profile_args = build_profile_args(deny, read)
    version = preflight(env, codex, profile_args, deny, review_dir)
    prompt_file = rdir / "PROMPT.md"
    prompt_file.write_text(prompt, encoding="utf-8")
    out = env.home / "cards" / (f"{tid}.codex.md" if round_no <= 1 else f"{tid}.codex-r{round_no}.md")
    out.parent.mkdir(parents=True, exist_ok=True)
    script = rdir / "codex.sh"
    script.write_text("#!/bin/bash\n# router.py review-pack 生成（读边界 + 预检已过）；一次 codex 订阅的常规审查用量\n"
                      + build_exec_cmd(review_dir, prompt_file, out, profile_args, codex, tbin) + "\n", encoding="utf-8")
    script.chmod(0o755)
    return {"mode": mode, "dir": str(review_dir), "files": files, "excluded": excluded, "deny": deny, "read": read,
            "profile_args": profile_args, "prompt": str(prompt_file), "script": str(script), "out": str(out),
            "codex_version": version, "codex_cmd": f"bash {shlex.quote(str(script))}"}


# ---- review-canary --------------------------------------------------------------------

def _write_canary(env, record) -> None:
    store._atomic_write(canary_path(env), json.dumps(record, ensure_ascii=False, indent=2) + "\n")


def canary(env) -> dict:
    """真 codex exec 一次（与 codex.sh 同一个命令形状、同一套读边界）。三份令牌每次新造（uuid）。"""
    cdir = env.home / "review" / "_canary"
    shutil.rmtree(cdir, ignore_errors=True)
    (cdir / "pack").mkdir(parents=True)
    (cdir / "deny").mkdir()
    version = None
    try:
        codex, tbin = _bins()
        version = codex_version(env, codex, cdir)
        return _canary_exec(env, cdir, codex, tbin, version)
    except Refused as e:
        if e.reason not in ("canary_leak", "canary_inconclusive"):   # 没跑到判定就失败：照样记一笔（ok=false）
            _write_canary(env, {"codex_version": version, "ok": False, "reason": e.reason, "at": env.now(),
                                "error": str(e)[:300]})
        raise


def _canary_exec(env, cdir, codex, tbin, version) -> dict:
    tok = {k: f"ROUTER-{k.upper()}-{uuid.uuid4()}" for k in ("pack", "deny", "sibling")}
    (cdir / "pack" / "readme.txt").write_text(tok["pack"] + "\n")
    (cdir / "deny" / "notes.txt").write_text(tok["deny"] + "\n")
    sib = Path(_real("~")) / SIBLING
    shutil.rmtree(sib, ignore_errors=True)
    out = cdir / "exec.out"
    try:
        sib.mkdir()
        (sib / "probe.txt").write_text(tok["sibling"] + "\n")
        deny, read = boundary(env, cdir / "pack", extra_deny=[cdir / "deny"])
        entries = {**{d: "deny" for d in deny}, **{r: "read" for r in read}}
        if _covering(entries, sib / "probe.txt")[0] != deny[0]:
            raise Refused(f"{sib} 被别的名单罩住了，探不到「只靠拒读 HOME」这一层", "boundary")
        files = [_real(cdir / "pack" / "readme.txt"), _real(cdir / "deny" / "notes.txt"), _real(sib / "probe.txt")]
        prompt = (f"这是 router 的沙箱自检，第二、第三条命令预期会报错。依次执行 `cat {files[0]}`、`cat {files[1]}`、"
                  f"`cat {files[2]}`，把三条命令的原始输出（含错误原文）逐字贴出；不要读别的。\n")
        prompt_file = cdir / "PROMPT.md"
        prompt_file.write_text(prompt, encoding="utf-8")
        script = cdir / "codex.sh"
        script.write_text("#!/bin/bash\n" + build_exec_cmd(_real(cdir / "pack"), prompt_file, out,
                                                          build_profile_args(deny, read), codex, tbin) + "\n")
        r = _run(env, ["bash", str(script)], cdir, EXEC_S + 15)
    finally:
        shutil.rmtree(sib, ignore_errors=True)
    text = out.read_text(encoding="utf-8", errors="replace") if out.exists() else ""
    leaked = [k for k in ("deny", "sibling") if tok[k] in text]
    if leaked:
        ok, reason = False, "canary_leak"
    elif tok["pack"] in text and f"notes.txt: {OS_DENIED}" in text and f"probe.txt: {OS_DENIED}" in text:
        ok, reason = True, ""
    else:
        ok, reason = False, "canary_inconclusive"
    rec = {"codex_version": version, "ok": ok, "reason": reason, "at": env.now(), "rc": r.returncode,
           "leaked": leaked, "out": str(out)}
    _write_canary(env, rec)
    if not ok:
        msg = ("codex 读边界失效：金丝雀读出了被拒路径的假数据（" + "、".join(leaked) + "）——告诉用户" if leaked else
               "金丝雀拿不准：没看到正对照，或被拒路径没有 OS 原文报错（模型可能没执行命令）")
        raise Refused(msg, reason, **{k: v for k, v in rec.items() if k not in ("ok", "reason")})
    return rec
