#!/usr/bin/env python3
"""Guard: ported forge functions must not carry aifriends-specific hardcoding (AC7)."""
import os
import sys
import inspect
import importlib
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'skills', 'forge', 'hooks'))
import forge_gate


@pytest.fixture(autouse=True)
def reload_module():
    importlib.reload(forge_gate)
    yield


PORTED = ["cmd_doctor", "cmd_audit_month", "month_plan_slugs", "metric_slugs",
          "_compaction_log_paths", "_read_compaction_log_epoch", "_last_compaction_epoch"]

FORBIDDEN = ["/Users/", "aifriends", "/home/", os.path.expanduser("~")]


def test_ported_functions_have_no_repo_hardcoding():
    for name in PORTED:
        src = inspect.getsource(getattr(forge_gate, name))
        for bad in FORBIDDEN:
            assert bad not in src, f"{name} contains hardcoded path fragment: {bad!r}"
