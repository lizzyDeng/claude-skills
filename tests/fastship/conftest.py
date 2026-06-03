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
    """
    home = tmp_path_factory.mktemp("fastship_state_home")
    monkeypatch.setenv("FASTSHIP_STATE_HOME", str(home))
    monkeypatch.delenv("FASTSHIP_SESSION", raising=False)
