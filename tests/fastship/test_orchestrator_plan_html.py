import importlib
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "skills" / "fastship"))


@pytest.fixture
def orch_mod():
    import orchestrator
    importlib.reload(orchestrator)
    return orchestrator


PLAN_MD = """# Integration Plan

> **For agentic workers:** REQUIRED SUB-SKILL: subagent-driven-development.

**Goal:** integrate.

**Architecture:** wire it.

**Tech Stack:** python.

---

## File Structure
| File | Responsibility | Change |
|------|----------------|--------|
| `x.py` | core | Create |

## Task 1: t
- [ ] **Step 1: do**
"""


def _write_plan(repo: Path):
    d = repo / "docs" / "superpowers" / "plans"
    d.mkdir(parents=True, exist_ok=True)
    p = d / "2026-06-05-integration.md"
    p.write_text(PLAN_MD, encoding="utf-8")
    return p


def test_generate_plan_html_creates_sibling(orch_mod, tmp_path):
    p = _write_plan(tmp_path)
    out = orch_mod.generate_plan_html(str(p))
    assert out and out.endswith("2026-06-05-integration.plan.html")
    assert os.path.exists(out)


def test_generate_plan_html_failure_returns_none(orch_mod, tmp_path, monkeypatch):
    p = _write_plan(tmp_path)
    # force renderer load to a bogus path so it raises internally -> swallowed
    monkeypatch.setattr(orch_mod, "_PLAN_HTML_SCRIPT", str(tmp_path / "nope.py"))
    out = orch_mod.generate_plan_html(str(p))
    assert out is None  # swallowed, no raise


def test_plan_html_not_in_trusted_ledger(orch_mod, tmp_path, monkeypatch):
    monkeypatch.setenv("FASTSHIP_PLAN_HTML_OPEN", "never")  # don't pop a browser in tests
    p = _write_plan(tmp_path)
    st = orch_mod.empty_orchestrator_state("x")
    orch_mod.record_step_artifact(st, "1.4", str(p))
    orch_mod.attach_plan_html(st, str(p))
    trusted = st.get("artifacts", {}).get(orch_mod.TRUSTED_ARTIFACTS_KEY, {})
    assert "plan_html" not in trusted
    assert "1.4_html" not in trusted
    assert st["artifacts"]["plan_html_path"].endswith(".plan.html")
