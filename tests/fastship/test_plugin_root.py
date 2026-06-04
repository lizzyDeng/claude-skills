"""Plugin-mode project-root resolution tests (P1 seam).

Covers fastship_state.repo_root() reading CLAUDE_PROJECT_DIR, the precedence
order vs FASTSHIP_REPO_ROOT / installed-tool / cwd, the retired-/tmp e2e default
(orchestrator + ship_verify_gate), and the engine-relative gate_script_path().
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'skills', 'fastship'))
import fastship_state


def _mk_git_repo(path):
    os.makedirs(path, exist_ok=True)
    subprocess.run(["git", "-C", path, "init", "-q"], check=True)
    return os.path.realpath(path)


def test_claude_project_dir_wins_over_cwd(tmp_path, monkeypatch):
    project = _mk_git_repo(str(tmp_path / "project"))
    elsewhere = _mk_git_repo(str(tmp_path / "elsewhere"))
    monkeypatch.chdir(elsewhere)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", project)
    monkeypatch.delenv("FASTSHIP_REPO_ROOT", raising=False)
    assert fastship_state.repo_root() == project


def test_fastship_repo_root_beats_claude_project_dir(tmp_path, monkeypatch):
    project = _mk_git_repo(str(tmp_path / "project"))
    override = _mk_git_repo(str(tmp_path / "override"))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", project)
    monkeypatch.setenv("FASTSHIP_REPO_ROOT", override)
    assert fastship_state.repo_root() == override


def test_nonexistent_claude_project_dir_falls_through(tmp_path, monkeypatch):
    cwd_repo = _mk_git_repo(str(tmp_path / "cwd_repo"))
    monkeypatch.chdir(cwd_repo)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path / "does_not_exist"))
    monkeypatch.delenv("FASTSHIP_REPO_ROOT", raising=False)
    assert fastship_state.repo_root() == cwd_repo


def test_claude_project_dir_beats_installed_tool_fallback(tmp_path, monkeypatch):
    """AC1: CLAUDE_PROJECT_DIR must win even when the engine looks installed
    (_is_installed_tool_dir True) — proves the new tier sits above the
    installed-tool branch, not just above cwd."""
    project = _mk_git_repo(str(tmp_path / "project"))
    monkeypatch.setattr(fastship_state, "_is_installed_tool_dir", lambda: True)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", project)
    monkeypatch.delenv("FASTSHIP_REPO_ROOT", raising=False)
    assert fastship_state.repo_root() == project


def test_e2e_result_default_is_repo_relative(tmp_path, monkeypatch):
    """AC4: orchestrator retires the /tmp default → repo-relative .claude/ path."""
    import importlib
    proj = _mk_git_repo(str(tmp_path / "proj"))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", proj)
    monkeypatch.delenv("FASTSHIP_REPO_ROOT", raising=False)
    import orchestrator
    importlib.reload(orchestrator)
    path = orchestrator._e2e_result_path()
    assert not path.startswith("/tmp/")
    assert path == os.path.join(proj, ".claude", "fastship-e2e-result.json")


def test_ship_verify_gate_e2e_result_default_repo_relative(tmp_path, monkeypatch):
    """AC4: the OTHER engine (ship_verify_gate) must also retire /tmp."""
    import importlib
    proj = _mk_git_repo(str(tmp_path / "proj2"))
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", proj)
    monkeypatch.delenv("FASTSHIP_REPO_ROOT", raising=False)
    svg_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'skills', 'fastship', 'hooks')
    sys.path.insert(0, os.path.abspath(svg_dir))
    import ship_verify_gate
    importlib.reload(ship_verify_gate)
    path = ship_verify_gate.e2e_result_path()
    assert not path.startswith("/tmp/")
    assert path == os.path.join(proj, ".claude", "fastship-e2e-result.json")


def test_gate_script_path_resolves_engine_relative():
    """AC6: gate_script_path resolves to the real skills/fastship/hooks copy
    (engine-relative), so deleting the stale .claude/hooks duplicate is safe."""
    p = fastship_state.gate_script_path()
    assert p.endswith(os.path.join("skills", "fastship", "hooks", "ship_verify_gate.py")), p
    assert os.path.exists(p), p


def test_orchestrator_script_path_resolves_engine_relative():
    """Branch-recovery hints must point at the engine's own orchestrator
    (source/plugin: orchestrator.py beside fastship_state.py), not the legacy
    .claude/tools/fastship wrapper that does not exist in those layouts."""
    p = fastship_state.orchestrator_script_path()
    assert p.endswith(os.path.join("skills", "fastship", "orchestrator.py")), p
    assert os.path.exists(p), p


def test_branch_mismatch_lines_emit_engine_relative_python_invocation():
    """branch_mismatch_lines must print `python3 "<orch>" adopt-branch/reset`
    using the resolved engine path — never the non-existent .claude/tools/fastship."""
    lines = fastship_state.branch_mismatch_lines({"branch": "feat/x"})
    orch = fastship_state.orchestrator_script_path()
    joined = "\n".join(lines)
    assert f'python3 "{orch}" adopt-branch' in joined, joined
    assert f'python3 "{orch}" reset' in joined, joined
    assert ".claude/tools/fastship\"" not in joined, joined


def test_branch_recovery_command_recognizes_real_engine_invocation():
    """The recovery whitelist must accept the canonical hint branch_mismatch_lines
    prints — `python3 "<real orchestrator path>" <sub>` — and the git escape hatches,
    so plugin-mode recovery is never blocked during a branch mismatch."""
    f = fastship_state.is_branch_recovery_command
    orch = fastship_state.orchestrator_script_path()
    gate = fastship_state.gate_script_path()
    # plugin / source canonical form (exactly what branch_mismatch_lines emits)
    assert f(f'python3 "{orch}" adopt-branch')
    assert f(f'python3 "{orch}" reset')
    assert f(f'python3 {orch} status')
    # the gate script is also a valid recovery target
    assert f(f'python3 "{gate}" status')
    # git escape hatches; rejects unrelated commands
    assert f('git switch feat/x')
    assert f('git status')
    assert not f('rm -rf /')


def test_branch_recovery_command_rejects_engine_trailing_args():
    """codex R11: trailing options after the subcommand broaden the hatch beyond the
    printed current-session recovery commands (reset --all wipes all sessions;
    --session <other> targets a different session). The engine forms must be EXACTLY
    `python3 <engine> <sub>` / `<engine> <sub>` with no trailing args."""
    f = fastship_state.is_branch_recovery_command
    orch = fastship_state.orchestrator_script_path()
    assert not f(f'python3 "{orch}" reset --all')
    assert not f(f'python3 "{orch}" reset --session victim')
    assert not f(f'python3 "{orch}" adopt-branch --session victim')
    assert not f(f'python3 "{orch}" status --session victim')
    assert not f(f'"{orch}" reset --all')         # direct form, trailing arg
    # the bare canonical forms still pass
    assert f(f'python3 "{orch}" reset')
    assert f(f'python3 "{orch}" adopt-branch')


def test_branch_recovery_command_requires_real_engine_path_not_basename():
    """codex R6: basename-only matching let an attacker-named /tmp/orchestrator.py
    pass. The script must RESOLVE to the real engine path."""
    f = fastship_state.is_branch_recovery_command
    # look-alike basename at an arbitrary path -> NOT the engine
    assert not f('python3 /tmp/orchestrator.py reset')
    assert not f('python3 /tmp/not-orchestrator.py reset')
    # real engine basename but runs `next`; `status` only in a comment -> NOT recovery
    orch = fastship_state.orchestrator_script_path()
    assert not f(f'python3 {orch} next # status')
    # `git reset` is intentionally NOT an escape hatch
    assert not f('git reset --hard')


def test_branch_recovery_command_rejects_compound_and_misplaced():
    """codex R5: must validate a SINGLE simple command — any shell control operator /
    substitution / redirection disqualifies it, and the program must be git/python/the
    engine, not merely an adjacent pair anywhere in argv."""
    f = fastship_state.is_branch_recovery_command
    orch = fastship_state.orchestrator_script_path()
    # adjacent pair but the program is `echo`/`touch`, not the engine/git
    assert not f('echo orchestrator.py reset')
    assert not f('touch blocked git status')
    # chaining / substitution / redirection past a real recovery invocation
    assert not f(f'python3 {orch} reset && rm -rf /')
    assert not f('git switch saved; rm -rf /')
    assert not f(f'python3 {orch} reset $(rm -rf /)')
    assert not f(f'python3 {orch} reset | tee /etc/x')
    assert not f('git switch `whoami`')


def test_branch_recovery_command_requires_bare_interpreter_and_git():
    """codex R7: the program token (python3 / git) was matched by basename, so a local
    attacker-controlled `./python3` / `/tmp/python3` / `bin/python` / `./git` would be
    whitelisted. The interpreter/git must be a BARE PATH-resolved command."""
    f = fastship_state.is_branch_recovery_command
    orch = fastship_state.orchestrator_script_path()
    gate = fastship_state.gate_script_path()
    assert not f(f'./python3 {orch} reset')
    assert not f(f'/tmp/python3 {orch} reset')
    assert not f(f'bin/python {gate} status')
    assert not f('./git switch saved')
    assert not f('/tmp/git status')
    # bare interpreter/git still accepted
    assert f(f'python3 {orch} reset')
    assert f('git switch saved')


def test_branch_recovery_command_restricts_git_to_safe_shapes():
    """codex R7/R9: the git hatch was too broad — file-discarding / branch-creating /
    branch-deleting / pathspec-ambiguous shapes are NOT recovery and must be rejected.
    `git checkout` is excluded entirely because its operand is ambiguous (branch vs
    pathspec): `git checkout .` / `git checkout <file>` discard working-tree changes."""
    f = fastship_state.is_branch_recovery_command
    # mutating / destructive -> rejected
    assert not f('git switch -c newbranch')
    assert not f('git branch -D main')
    assert not f('git switch --detach')
    # checkout is dropped wholesale — every form rejected (R9)
    assert not f('git checkout saved')
    assert not f('git checkout .')
    assert not f('git checkout skills/fastship/fastship_state.py')
    assert not f('git checkout -b newbranch')
    assert not f('git checkout -- skills/fastship/orchestrator.py')
    # safe recovery shapes -> allowed
    assert f('git status')
    assert f('git status --porcelain')
    assert f('git branch')          # bare list, read-only
    assert f('git switch saved')


def test_branch_recovery_command_allows_valid_branch_name_chars():
    """codex R10: valid git branch names contain shell-inert chars (@ + , =) that the
    earlier whitelist wrongly rejected, blocking the canonical recovery hint."""
    f = fastship_state.is_branch_recovery_command
    assert f('git switch feature/foo@bar')
    assert f('git switch feature/foo+bar')
    assert f('git switch feature/foo,bar')
    assert f('git switch release/v1.0=rc1')
    assert f('git switch feat/foo-bar_baz.1')


def test_branch_recovery_command_allows_unicode_branch_names():
    """codex R12: unicode branch names (valid git refs) must not be blocked — every
    shell metachar is ASCII, so non-ASCII chars are shell-inert and allowed unquoted.
    Especially relevant in a Chinese-language project."""
    f = fastship_state.is_branch_recovery_command
    assert f('git switch 分支')
    assert f('git switch feat/é-accent')
    assert f("git switch '分支'")  # shlex.quote form also accepted
    # the printed hint for a unicode branch round-trips through the hatch
    lines = fastship_state.branch_mismatch_lines({"branch": "功能/登录"})
    switch_line = next(l for l in lines if "git switch" in l).strip()
    assert f(switch_line), switch_line
    # injection still rejected even adjacent to unicode
    assert not f('git switch 分支; rm -rf /')


def test_branch_recovery_command_allows_quoted_special_branch_but_not_unquoted():
    """A branch with shell-special chars recovers via a single-quoted hint (literal,
    injection-safe), while the same chars UNQUOTED stay rejected."""
    f = fastship_state.is_branch_recovery_command
    assert f("git switch 'feature/foo#bar'")
    assert f("git switch 'weird;name'")        # single-quoted -> shell-literal -> safe
    assert not f('git switch feature/foo#bar')  # unquoted '#' is a shell comment
    assert not f('git switch weird;name')       # unquoted ';' chains


def test_branch_mismatch_lines_quote_special_branch():
    """The printed `git switch` hint must shlex.quote the branch so a shell-special
    branch name yields a copy-pasteable, injection-safe command the hatch also accepts."""
    lines = fastship_state.branch_mismatch_lines({"branch": "feat/foo#bar"})
    switch_line = next(l for l in lines if "git switch" in l)
    assert "git switch 'feat/foo#bar'" in switch_line, switch_line
    # and that exact emitted command is accepted by the recovery whitelist
    assert fastship_state.is_branch_recovery_command(switch_line.strip())


def test_branch_recovery_command_rejects_glued_comment_and_expansions():
    """codex R8: shlex(comments=True) stripped a glued '#' — `git checkout HEAD# -- file`
    parsed as `git checkout HEAD` (looks safe) but the shell runs `git checkout HEAD#
    -- file`, discarding working-tree changes. A literal-safe character whitelist rejects
    '#' and all shell-expansion metacharacters so the parse can never diverge."""
    f = fastship_state.is_branch_recovery_command
    assert not f('git checkout HEAD# -- skills/fastship/fastship_state.py')
    assert not f('git switch saved#')              # glued comment char
    assert not f('git switch {saved,--detach}')    # brace expansion -> flag injection
    assert not f('git switch sav*')                # glob
    assert not f('git switch ~root')               # tilde expansion
    assert not f('git status\t&& rm -rf /')        # tab + control operators
    # the canonical literal hints still pass
    orch = fastship_state.orchestrator_script_path()
    assert f(f'python3 "{orch}" adopt-branch')
    assert f('git switch feat/recovery-branch')


def test_branch_recovery_command_rejects_env_and_sudo_prefixes():
    """codex R6: leading env-assignment / sudo prefixes can redirect command lookup
    (PATH=.) or run startup scripts (BASH_ENV=.x), so they must NOT be stripped — the
    program has to be the recovery program directly."""
    f = fastship_state.is_branch_recovery_command
    orch = fastship_state.orchestrator_script_path()
    assert not f(f'PATH=. python3 {orch} reset')
    assert not f('PATH=. git status')
    assert not f(f'BASH_ENV=.x python3 {orch} reset')
    assert not f(f'sudo python3 {orch} reset')
    assert not f(f'FOO=bar python3 {orch} reset')
