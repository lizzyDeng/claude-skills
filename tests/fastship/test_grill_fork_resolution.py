"""Unit tests for the 1.5 grill fork-resolution contract.

When the grill runs (1B surfaced ≥1 open technical fork), it exists to arbitrate
those forks — so the grill summary must record a non-empty resolution for EVERY
open fork. This closes the 软裁 gap where a grill produced prose but left an open
fork dangling. Mirrors _check_plan_mapping's discipline (dangling / dup / missing).
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', 'skills', 'fastship'))


def check(open_ids, grill_gate):
    from orchestrator import _check_grill_fork_resolution
    return _check_grill_fork_resolution(set(open_ids), grill_gate)


def valid_gate():
    return {"fork_resolutions": [
        {"id": "tf-1", "resolution": "选 PG，因为已有连接池"},
        {"id": "tf-2", "resolution": "选 REST，团队更熟"},
    ]}


OPEN = ["tf-1", "tf-2"]


def test_valid_resolution_passes():
    ok, msg = check(OPEN, valid_gate())
    assert ok, msg


def test_non_dict_gate_fails():
    ok, _ = check(OPEN, ["nope"])
    assert ok is False


def test_missing_field_fails():
    ok, msg = check(OPEN, {"other": []})
    assert ok is False and "fork_resolutions" in msg


def test_empty_list_fails():
    ok, msg = check(OPEN, {"fork_resolutions": []})
    assert ok is False and "fork_resolutions" in msg


def test_entry_not_object_fails():
    ok, msg = check(OPEN, {"fork_resolutions": ["tf-1"]})
    assert ok is False and "object" in msg


def test_blank_id_fails():
    g = valid_gate()
    g["fork_resolutions"][0]["id"] = "  "
    ok, msg = check(OPEN, g)
    assert ok is False and "id" in msg


def test_dangling_resolution_fails():
    # Resolving a fork id the plan never declared open = fabricated arbitration.
    g = valid_gate()
    g["fork_resolutions"][0]["id"] = "tf-ghost"
    ok, msg = check(OPEN, g)
    assert ok is False and "tf-ghost" in msg and "dangling" in msg


def test_duplicate_id_fails():
    g = valid_gate()
    g["fork_resolutions"].append({"id": "tf-1", "resolution": "重复裁"})
    ok, msg = check(OPEN, g)
    assert ok is False and "重复" in msg


def test_blank_resolution_fails():
    g = valid_gate()
    g["fork_resolutions"][1]["resolution"] = "   "
    ok, msg = check(OPEN, g)
    assert ok is False and "resolution" in msg


def test_unresolved_open_fork_fails():
    # The core invariant: an open fork with no resolution entry → FAIL.
    g = {"fork_resolutions": [{"id": "tf-1", "resolution": "只裁了一个"}]}
    ok, msg = check(OPEN, g)
    assert ok is False and "tf-2" in msg and "未在 grill 裁决" in msg


# ── _extract_grill_resolution_gate picks the right block ────────────────────

def test_extract_picks_fork_resolutions_block():
    from orchestrator import _extract_grill_resolution_gate
    content = (
        "## 结论\nresolved\n"
        '```json\n{"unrelated": 1}\n```\n'
        '```json\n{"fork_resolutions": [{"id": "tf-1", "resolution": "选 A"}]}\n```\n'
    )
    gate = _extract_grill_resolution_gate(content)
    assert gate is not None and gate["fork_resolutions"][0]["id"] == "tf-1"


def test_extract_none_when_absent():
    from orchestrator import _extract_grill_resolution_gate
    assert _extract_grill_resolution_gate("## 结论\n纯文本无 json\n") is None
