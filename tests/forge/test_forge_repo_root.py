"""forge_gate.get_repo_root() env-precedence tests (P1 seam).

FORGE_REPO_ROOT / CLAUDE_PROJECT_DIR must win over git-from-cwd so the engine
works when installed as a plugin (cwd may be elsewhere; project dir is signalled).
"""
import os
import subprocess
import sys
import importlib
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'skills', 'forge', 'hooks'))
import forge_gate


def _mk_git_repo(path):
    os.makedirs(path, exist_ok=True)
    subprocess.run(["git", "-C", path, "init", "-q"], check=True)
    return os.path.realpath(path)


def test_claude_project_dir_used(tmp_path):
    importlib.reload(forge_gate)
    project = _mk_git_repo(str(tmp_path / "project"))
    with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": project}, clear=False):
        os.environ.pop("FORGE_REPO_ROOT", None)
        assert forge_gate.get_repo_root() == project


def test_forge_repo_root_beats_claude_project_dir(tmp_path):
    importlib.reload(forge_gate)
    project = _mk_git_repo(str(tmp_path / "project"))
    override = _mk_git_repo(str(tmp_path / "override"))
    with mock.patch.dict(os.environ, {"CLAUDE_PROJECT_DIR": project, "FORGE_REPO_ROOT": override}, clear=False):
        assert forge_gate.get_repo_root() == override


def test_dashboard_launcher_resolves_payload_engine_relative(tmp_path):
    """codex R5: the forge-dashboard launcher must exec its python payload
    relative to the launcher (engine-relative), and read the consumer PROJECT via
    --repo-root. Invoke it by abspath from an unrelated cwd with CLAUDE_PROJECT_DIR
    set — the snapshot's repo_root must be the project, not the engine."""
    import json
    launcher = os.path.realpath(os.path.join(
        os.path.dirname(__file__), '..', '..', 'skills', 'forge', 'forge-dashboard'))
    assert os.path.exists(launcher), launcher
    project = _mk_git_repo(str(tmp_path / "consumer"))
    env = dict(os.environ)
    env.pop("FORGE_REPO_ROOT", None)
    env["CLAUDE_PROJECT_DIR"] = project
    # run from a cwd that is NOT the engine and NOT the project
    r = subprocess.run([launcher, "--once"], cwd=str(tmp_path), env=env,
                       capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stderr
    snap = json.loads(r.stdout)
    assert snap["repo_root"] == project, snap["repo_root"]
