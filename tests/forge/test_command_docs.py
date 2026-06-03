#!/usr/bin/env python3
"""Automated doc-coverage assertions (A5/A6) — no human-read reliance."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'skills', 'fastship'))
import orchestrator

ROOT = os.path.join(os.path.dirname(__file__), '..', '..')


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_forge_md_documents_new_commands():
    md = _read(".claude/commands/forge.md")
    for cmd in ("/forge dashboard", "/forge doctor", "/forge audit-month"):
        assert cmd in md, f"forge.md missing section for {cmd}"


def test_fastship_docs_cover_all_orchestrator_steps():
    # ids derived from the live source of truth, not a hardcoded list
    ids = [s.id for s in orchestrator.STEPS]
    assert len(ids) == 18, f"expected 18 steps, got {len(ids)}"
    # both the command doc and the skill doc must reflect every step
    for rel in (".claude/commands/fastship.md", "skills/fastship/SKILL.md"):
        md = _read(rel)
        for sid in ids:
            assert sid in md, f"{rel} missing step {sid}"
