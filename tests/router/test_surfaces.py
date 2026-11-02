import os

from surfaces import conflicts, held_locks, surfaces_for_files, weight_for


def test_files_map_to_surfaces(table):
    assert surfaces_for_files(["services/api-server/src/services/her_chat/turn.rs"], table) == {"her-reply-prompt"}
    assert surfaces_for_files(["services/api-server/src/services/her_chat/a/b/c.rs"], table) == {"her-reply-prompt"}
    assert surfaces_for_files(["README.md"], table) == set()


def test_home_globs_expand(table):
    home_file = os.path.expanduser("~/.her-persona-daily/weekly.py")
    assert surfaces_for_files([home_file, "~/.her-persona-daily/state/releases.json"], table) == {"weekly-report"}


def test_locks_include_paused_and_external_but_not_merged(state_with):
    s = state_with(T1=("paused", ["her-reply-prompt"]), T2=("merged_staging", ["weekly-report"]),
                   external={"/wt/a": ["llm-config"]})
    assert held_locks(s) == {"her-reply-prompt": "T1", "llm-config": "ext:/wt/a"}


def test_idle_external_does_not_lock(state_with):
    s = state_with(external={"/wt/a": {"surfaces": ["llm-config"], "weight": "light",
                                       "active": False, "locking": False}})
    assert held_locks(s) == {}


def test_conflicts_ignore_own_locks(state_with):
    s = state_with(T1=("executing", ["her-reply-prompt", "weekly-report"]), T2=("await_card", ["llm-config"]))
    assert conflicts(s, "T1", {"her-reply-prompt", "llm-config"}) == {"llm-config": "T2"}
    assert conflicts(s, "T1", {"her-reply-prompt"}) == {}


def test_weight_is_heavy_if_any_surface_heavy(table):
    assert weight_for({"weekly-report", "her-reply-prompt"}, table) == "heavy"
    assert weight_for({"weekly-report"}, table) == "light"
    assert weight_for(set(), table) == "light"
