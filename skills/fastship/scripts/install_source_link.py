#!/usr/bin/env python3
"""Install fastship into a project as source links.

This is for local consumer repos such as aifriends where fastship should always
run from this claude-skills checkout. It replaces copied engine files with
symlinks, preserving the stable project-local paths that existing hook settings
already call.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


ENGINE_DIR = Path(__file__).resolve().parents[1]
SKILLS_DIR = ENGINE_DIR.parent


FASTSHIP_LINKS = {
    ".claude/commands/fastship.md": ENGINE_DIR / "SKILL.md",
    ".claude/hooks/ship_verify_gate.py": ENGINE_DIR / "hooks" / "ship_verify_gate.py",
    ".claude/tools/fastship": ENGINE_DIR / "fastship",
    ".claude/tools/fastship_orchestrator.py": ENGINE_DIR / "orchestrator.py",
    ".claude/tools/fastship_state.py": ENGINE_DIR / "fastship_state.py",
}

FORGE_LINKS = {
    ".claude/commands/forge.md": SKILLS_DIR / "forge" / "SKILL.md",
    ".claude/hooks/forge_gate.py": SKILLS_DIR / "forge" / "hooks" / "forge_gate.py",
    ".claude/tools/forge-dashboard": SKILLS_DIR / "forge" / "forge-dashboard",
    ".claude/tools/forge_dashboard.py": SKILLS_DIR / "forge" / "forge_dashboard.py",
}

FASTSHIP_GITIGNORE_LINES = [
    "# fastship runtime artifacts",
    ".claude/.ship-verify-state.json",
    ".claude/.fastship-orchestrator-state.json",
    ".claude/state/",
    ".claude/worktrees/",
    ".claude/fastship-e2e-result.json",
    ".claude/.fastship-brief.md",
    ".claude/.fastship-requirements.md",
    ".claude/.fastship-grill-result.md",
    ".claude/.fastship-codex-review.md",
    ".claude/.fastship-code-review.md",
    "docs/superpowers/plans/*.plan.html",
]


def _git_root(path: Path) -> Path:
    proc = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"not a git repo: {path}")
    return Path(proc.stdout.strip()).resolve()


def _same_link(target: Path, source: Path) -> bool:
    if not target.is_symlink():
        return False
    try:
        return target.resolve() == source.resolve()
    except OSError:
        return False


def _link(target: Path, source: Path, replace: bool, dry_run: bool) -> str:
    if not source.exists():
        raise SystemExit(f"missing source: {source}")
    if _same_link(target, source):
        return "exists"
    if target.exists() or target.is_symlink():
        if not replace:
            raise SystemExit(
                f"{target} already exists. Re-run with --replace to swap copied files for symlinks."
            )
        if dry_run:
            return "replace"
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink()
    if dry_run:
        return "link"
    target.parent.mkdir(parents=True, exist_ok=True)
    os.symlink(source, target)
    return "linked"


def _hook(command: str, timeout: int = 10) -> dict:
    return {"type": "command", "command": command, "timeout": timeout}


def _fastship_hooks(project: Path) -> dict:
    orch = project / ".claude" / "tools" / "fastship_orchestrator.py"
    return {
        "PreToolUse": [
            {"matcher": "Edit|Write", "hooks": [_hook(f'python3 "{orch}" pre_edit')]},
            {"matcher": "Bash", "hooks": [_hook(f'python3 "{orch}" pre_bash')]},
        ],
        "PostToolUse": [
            {"matcher": "Edit|Write", "hooks": [_hook(f'python3 "{orch}" post_edit')]},
            {"matcher": "Bash", "hooks": [_hook(f'python3 "{orch}" post_bash')]},
        ],
    }


def _forge_hooks(project: Path) -> dict:
    gate = project / ".claude" / "hooks" / "forge_gate.py"
    return {
        "PreToolUse": [
            {"matcher": "Edit|Write", "hooks": [_hook(f'python3 "{gate}" pre_edit')]},
        ],
        "PostToolUse": [
            {"matcher": "Edit|Write", "hooks": [_hook(f'python3 "{gate}" post_edit')]},
            {"matcher": "Bash", "hooks": [_hook(f'python3 "{gate}" post_bash')]},
        ],
    }


def _merge_hook_group(existing: list, additions: list) -> list:
    out = list(existing or [])
    commands = {
        hook.get("command")
        for group in out
        for hook in group.get("hooks", [])
        if isinstance(hook, dict)
    }
    for group in additions:
        hooks = [hook for hook in group.get("hooks", []) if hook.get("command") not in commands]
        if hooks:
            out.append({"matcher": group["matcher"], "hooks": hooks})
            commands.update(hook["command"] for hook in hooks)
    return out


def _merge_settings(project: Path, with_forge: bool, dry_run: bool) -> str:
    settings_path = project / ".claude" / "settings.local.json"
    if settings_path.exists():
        data = json.loads(settings_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise SystemExit(f"settings is not a JSON object: {settings_path}")
    else:
        data = {}

    hooks = data.setdefault("hooks", {})
    additions = _fastship_hooks(project)
    if with_forge:
        forge = _forge_hooks(project)
        for event, groups in forge.items():
            additions.setdefault(event, []).extend(groups)

    changed = False
    for event, groups in additions.items():
        before = json.dumps(hooks.get(event, []), sort_keys=True)
        hooks[event] = _merge_hook_group(hooks.get(event, []), groups)
        after = json.dumps(hooks.get(event, []), sort_keys=True)
        changed = changed or before != after

    if not changed:
        return "exists"
    if dry_run:
        return "update"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings_path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return "updated"


def _merge_gitignore(project: Path, dry_run: bool) -> str:
    path = project / ".gitignore"
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    present = set(line.strip() for line in existing)
    missing = [line for line in FASTSHIP_GITIGNORE_LINES if line.strip() and line.strip() not in present]
    if not missing:
        return "exists"
    if dry_run:
        return "update"
    out = list(existing)
    if out and out[-1].strip():
        out.append("")
    for line in FASTSHIP_GITIGNORE_LINES:
        if line.strip() and line.strip() not in present:
            out.append(line)
            present.add(line.strip())
    path.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    return "updated"


def install(project: Path, replace: bool, with_forge: bool, no_hooks: bool, dry_run: bool) -> list[str]:
    project = _git_root(project)
    links = dict(FASTSHIP_LINKS)
    if with_forge:
        links.update(FORGE_LINKS)

    results = []
    for rel, source in links.items():
        status = _link(project / rel, source.resolve(), replace=replace, dry_run=dry_run)
        results.append(f"{status:8} {rel} -> {source}")

    if not no_hooks:
        status = _merge_settings(project, with_forge=with_forge, dry_run=dry_run)
        results.append(f"{status:8} .claude/settings.local.json hooks")
    status = _merge_gitignore(project, dry_run=dry_run)
    results.append(f"{status:8} .gitignore fastship artifacts")
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install fastship as symlinks to this claude-skills checkout.")
    parser.add_argument("--project", default=".", help="consumer project path; defaults to cwd")
    parser.add_argument("--replace", action="store_true", help="replace existing copied files with symlinks")
    parser.add_argument("--with-forge", action="store_true", help="also source-link forge command/gate/dashboard")
    parser.add_argument("--no-hooks", action="store_true", help="do not merge Claude hook settings")
    parser.add_argument("--dry-run", action="store_true", help="print intended changes without writing")
    args = parser.parse_args(argv)

    for line in install(
        Path(args.project).resolve(),
        replace=args.replace,
        with_forge=args.with_forge,
        no_hooks=args.no_hooks,
        dry_run=args.dry_run,
    ):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
