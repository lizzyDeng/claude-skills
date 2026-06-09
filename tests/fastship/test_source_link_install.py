import importlib.util
import subprocess
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "skills" / "fastship" / "scripts" / "install_source_link.py"


def _load_installer():
    spec = importlib.util.spec_from_file_location("install_source_link", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git_init(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
    return path


def test_source_link_install_replaces_copied_engine_files(tmp_path):
    installer = _load_installer()
    project = _git_init(tmp_path / "consumer")

    copied = project / ".claude" / "tools" / "fastship_state.py"
    copied.parent.mkdir(parents=True)
    copied.write_text("# stale copy\n", encoding="utf-8")

    results = installer.install(project, replace=True, with_forge=True, no_hooks=False, dry_run=False)

    assert any(".claude/tools/fastship_state.py" in line for line in results)
    assert (project / ".claude" / "commands" / "fastship.md").resolve() == (
        installer.ENGINE_DIR / "SKILL.md"
    ).resolve()
    assert (project / ".claude" / "tools" / "fastship_orchestrator.py").resolve() == (
        installer.ENGINE_DIR / "orchestrator.py"
    ).resolve()
    assert (project / ".claude" / "hooks" / "ship_verify_gate.py").resolve() == (
        installer.ENGINE_DIR / "hooks" / "ship_verify_gate.py"
    ).resolve()
    assert (project / ".claude" / "commands" / "forge.md").resolve() == (
        installer.SKILLS_DIR / "forge" / "SKILL.md"
    ).resolve()

    settings = (project / ".claude" / "settings.local.json").read_text(encoding="utf-8")
    assert "fastship_orchestrator.py" in settings
    assert "forge_gate.py" in settings
    gitignore = (project / ".gitignore").read_text(encoding="utf-8")
    assert ".claude/worktrees/" in gitignore
    assert ".claude/.fastship-codex-review.md" in gitignore

    proc = subprocess.run(
        [str(project / ".claude" / "tools" / "fastship"), "status"],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    assert "没有活跃 session" in proc.stdout


def test_source_link_install_refuses_to_overwrite_without_replace(tmp_path):
    installer = _load_installer()
    project = _git_init(tmp_path / "consumer")
    target = project / ".claude" / "commands" / "fastship.md"
    target.parent.mkdir(parents=True)
    target.write_text("stale", encoding="utf-8")

    try:
        installer.install(project, replace=False, with_forge=False, no_hooks=True, dry_run=False)
    except SystemExit as exc:
        assert "--replace" in str(exc)
    else:
        raise AssertionError("expected installer to refuse overwriting a copied command")
