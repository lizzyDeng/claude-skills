import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'skills', 'fastship'))


@pytest.fixture(autouse=True)
def isolate_fastship_state_home(tmp_path_factory, monkeypatch):
    """Isolate the fastship state home to an empty throwaway dir for every test.

    Root cause this guards against: ``state_home()`` falls back to the real
    ``<git-dir>/fastship`` when ``FASTSHIP_STATE_HOME`` is unset, and the
    session resolver follows the registry's ``current_session`` pointer. Any
    validator that reads gate/registry state without an explicit override —
    ``validate_tests`` (``_read_gate_state_file``) or ``validate_knowledge``
    (``load_hook_state`` reload) — would then pick up the developer's live
    fastship session and the test becomes coupled to on-disk state.

    Pointing the home at a fresh empty dir makes every such read return ``{}``
    unless the test injects state itself. Tests that need a specific home still
    override ``FASTSHIP_STATE_HOME`` after this fixture runs.

    It also pins ``FASTSHIP_REPO_ROOT`` to a fresh EMPTY dir so ``repo_root()``
    (and thus ``load_project_config()`` / ``state_home`` fallbacks) never resolve
    to the developer's real checkout. Without this, a real repo-root
    ``.claude/fastship.project.json`` (e.g. another skill's E2E config) silently
    overrides a test's monkeypatched ``E2E_RESULT_PATH`` and breaks E2E-report
    validation tests. Tests that need a specific repo root still override
    ``FASTSHIP_REPO_ROOT`` / ``CLAUDE_PROJECT_DIR`` or monkeypatch ``repo_root``.
    """
    home = tmp_path_factory.mktemp("fastship_state_home")
    monkeypatch.setenv("FASTSHIP_STATE_HOME", str(home))
    monkeypatch.delenv("FASTSHIP_SESSION", raising=False)
    # Strip ambient harness env so project-root resolution tests aren't polluted, then
    # pin repo_root() to a fresh EMPTY dir (no .claude/fastship.project.json) so no test
    # accidentally reads the real checkout's project config. Resolution tests delenv
    # FASTSHIP_REPO_ROOT themselves before asserting precedence.
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    empty_repo_root = tmp_path_factory.mktemp("fastship_empty_repo_root")
    monkeypatch.setenv("FASTSHIP_REPO_ROOT", str(empty_repo_root))
