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


def test_branch_recovery_command_recognizes_packaged_orchestrator():
    """The recovery whitelist must accept the packaged python3 .../orchestrator.py
    form (plugin/source) AND the legacy fastship_orchestrator.py / wrapper forms,
    so plugin-mode recovery is never blocked during a branch mismatch."""
    f = fastship_state.is_branch_recovery_command
    # plugin / source canonical form (what branch_mismatch_lines now prints)
    assert f('python3 "/x/plugins/forge/skills/fastship/orchestrator.py" adopt-branch')
    assert f('python3 "/repo/skills/fastship/orchestrator.py" reset')
    assert f('python3 /repo/skills/fastship/orchestrator.py status')
    # legacy installed layouts
    assert f('python3 .claude/tools/fastship_orchestrator.py adopt-branch')
    assert f('.claude/tools/fastship reset')
    # still recognizes git escape hatches; rejects unrelated commands
    assert f('git switch feat/x')
    assert f('git status')
    assert not f('rm -rf /')


def test_branch_recovery_command_rejects_substring_and_comment_bypasses():
    """argv parsing (not substring) must reject commands that merely contain an
    engine-looking substring + a recovery word — the bypasses codex R4 found:
    a real non-recovery subcommand with a recovery word in a comment, and a
    look-alike path that is not the engine."""
    f = fastship_state.is_branch_recovery_command
    # runs `next`; `status` only appears in a trailing comment -> NOT recovery
    assert not f('python3 skills/fastship/orchestrator.py next # status')
    # look-alike basename (not-orchestrator.py) must not qualify via substring
    assert not f('python3 /tmp/not-orchestrator.py reset')
    # `git reset` is intentionally NOT an escape hatch (only status/branch/switch/checkout)
    assert not f('git reset --hard')


def test_branch_recovery_command_rejects_compound_and_misplaced(monkeypatch):
    """codex R5: must validate a SINGLE simple command. The engine/recovery pair
    must be the actual program+subcommand, not just an adjacent pair anywhere in
    argv, and any shell control operator / substitution disqualifies the command."""
    f = fastship_state.is_branch_recovery_command
    # adjacent pair but the program is `echo`/`touch`, not the engine/git
    assert not f('echo orchestrator.py reset')
    assert not f('touch blocked git status')
    # chaining a second command past a real recovery invocation
    assert not f('python3 skills/fastship/orchestrator.py reset && rm -rf /')
    assert not f('git switch saved; rm -rf /')
    assert not f('python3 skills/fastship/orchestrator.py reset $(rm -rf /)')
    assert not f('python3 skills/fastship/orchestrator.py reset | tee /etc/x')
    # backtick substitution
    assert not f('git switch `whoami`')


def test_branch_recovery_command_allows_env_and_sudo_prefixes():
    """Single simple recovery commands stay allowed even with common harmless
    prefixes (env assignments, sudo)."""
    f = fastship_state.is_branch_recovery_command
    assert f('FOO=bar python3 /repo/skills/fastship/orchestrator.py reset')
    assert f('sudo .claude/tools/fastship adopt-branch')
    # trailing flags after the subcommand are fine (e.g. reset --all)
    assert f('python3 /repo/skills/fastship/orchestrator.py reset --all')
    assert f('git switch feat/some-branch')
