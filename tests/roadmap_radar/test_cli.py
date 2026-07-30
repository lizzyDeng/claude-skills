import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RADAR = ROOT / "skills" / "roadmap-radar" / "scripts" / "radar.py"
FIX = Path(__file__).resolve().parent / "fixtures"


def run(*args, cwd=None):
    return subprocess.run([sys.executable, str(RADAR), *args],
                          capture_output=True, text=True, cwd=cwd)


def test_from_graph_renders_without_touching_network(tmp_path):
    out = tmp_path / "radar.html"
    result = run("--from", str(FIX / "graph.json"), "--out", str(out), "--no-open")
    assert result.returncode == 0, result.stderr
    html = out.read_text(encoding="utf-8")
    assert html.startswith("<!DOCTYPE html>")
    assert "变现能力" in html


def test_config_discovery_reads_dot_radar_json(tmp_path):
    (tmp_path / "project-roadmap").mkdir()
    (tmp_path / "project-roadmap" / "roadmap.json").write_text(json.dumps({
        "north_star": "NS",
        "objectives": [{"id": "obj-1", "name": "G", "target_metric": "M"}],
        "features": [{"slug": "f", "name": "F", "objective_id": "obj-1",
                      "status": "concluded"}],
    }), encoding="utf-8")
    (tmp_path / ".radar.json").write_text(json.dumps({
        "sources": [{"type": "forge", "path": "project-roadmap/roadmap.json"}]
    }), encoding="utf-8")

    graph_out = tmp_path / "graph.json"
    result = run("--graph", str(graph_out), "--no-open", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    graph = json.loads(graph_out.read_text(encoding="utf-8"))
    by_id = {n["id"]: n for n in graph["nodes"]}
    assert by_id["goal:obj-1"]["progress"] == 1.0


def test_doctor_prints_report_and_writes_nothing(tmp_path):
    (tmp_path / "project-roadmap").mkdir()
    (tmp_path / "project-roadmap" / "roadmap.json").write_text(json.dumps({
        "north_star": "NS",
        "objectives": [{"id": "obj-1", "name": "G", "target_metric": "M"}],
        "features": [{"slug": "f", "name": "F", "objective": "obj-404",
                      "status": "weird-word"}],
    }), encoding="utf-8")
    (tmp_path / ".radar.json").write_text(json.dumps({
        "sources": [{"type": "forge", "path": "project-roadmap/roadmap.json"}]
    }), encoding="utf-8")

    result = run("--doctor", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "obj-404" in result.stdout
    assert "weird-word" in result.stdout
    assert not (tmp_path / "radar.html").exists()


def test_missing_config_exits_nonzero_with_actionable_message(tmp_path):
    result = run("--no-open", cwd=tmp_path)
    assert result.returncode != 0
    assert ".radar.json" in (result.stderr + result.stdout)
