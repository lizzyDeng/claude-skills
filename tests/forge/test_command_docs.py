#!/usr/bin/env python3
"""Automated doc-coverage assertions (A5/A6) — no human-read reliance."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'skills', 'fastship'))
import orchestrator

ROOT = os.path.join(os.path.dirname(__file__), '..', '..')

EXPECTED_STEPS = [
    "1.0", "1.1", "1.2", "1.3", "1.3d", "1.4", "1.5", "1.5c", "1.6",
    "2.0", "2.5", "3.0", "3.1", "3.2", "3.3", "3.4", "3.5", "3.6",
]


def _read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def test_forge_md_documents_new_commands():
    md = _read(".claude/commands/forge.md")
    for cmd in ("/forge dashboard", "/forge doctor", "/forge audit-month"):
        assert cmd in md, f"forge.md missing section for {cmd}"


def test_fastship_md_covers_all_18_orchestrator_steps():
    md = _read(".claude/commands/fastship.md")
    # drift guard: doc must track the real STEPS list
    assert len(orchestrator.STEPS) == 18
    assert len(EXPECTED_STEPS) == 18
    for sid in EXPECTED_STEPS:
        assert sid in md, f"fastship.md missing step {sid}"
