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
