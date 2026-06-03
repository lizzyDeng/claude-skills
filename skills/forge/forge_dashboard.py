#!/usr/bin/env python3
"""Forge + Fastship progress dashboard. Read-only aggregation + local web UI.

Stdlib only. No external deps, no build step. See plan
docs/superpowers/plans/2026-06-02-forge-fastship-dashboard.md.

Data sources (all read-only):
  project-roadmap/roadmap.json                       north_star + objectives[] + features[]
  project-roadmap/features/<slug>/metric.json        baseline/target/metric_name
  project-roadmap/features/<slug>/harvest.json       actual/verdict/next_action
  .claude/forge-state/features/<slug>/state.json     forge gate phase + g1..g6
  <git-common-dir>/fastship/sessions/<id>/*.json     fastship session state (main)
  <git-common-dir>/worktrees/*/fastship/sessions/..  fastship session state (worktrees)
"""
import argparse
import glob
import json
import os
import re
import subprocess
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ALL_STEPS = ["1.0", "1.1", "1.2", "1.3", "1.3d", "1.4", "1.5", "1.5c", "1.6",
             "2.0", "2.5", "3.0", "3.1", "3.2", "3.3", "3.4", "3.5", "3.6"]
DONE_STATUSES = ("shipped", "measuring", "concluded")
TODO_STATUSES = ("draft", "planned", "in_progress")


def _read_json(path):
    """Read a JSON file; return {} on any error (missing / malformed / partial)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _git(args, cwd):
    try:
        r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    if r.returncode != 0:
        return None
    return (r.stdout.strip() or None)


def _now_iso():
    return datetime.now().isoformat(timespec="seconds")


def _as_list(v):
    """Coerce to a list of step ids; tolerate malformed (string/null/dict)."""
    return v if isinstance(v, list) else []


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except Exception:
        return 0.0


def _worktree_from_path(path):
    """Extract the linked-worktree name from a fastship state-home path, else None."""
    m = re.search(r"/worktrees/([^/]+)/fastship/", (path or "") + "/")
    return m.group(1) if m else None


def _parse_worktree_list(output):
    """Parse `git worktree list --porcelain` -> [{path, head, branch, is_bare}].
    Tolerant: detached -> branch None; bare -> is_bare True; missing fields ignored."""
    out, cur = [], None
    for line in (output or "").splitlines():
        if line.startswith("worktree "):
            if cur:
                out.append(cur)
            cur = {"path": line[9:].strip(), "head": None, "branch": None, "is_bare": False}
        elif cur is None:
            continue
        elif line.startswith("HEAD "):
            cur["head"] = line[5:].strip()
        elif line.startswith("branch "):
            b = line[7:].strip()
            cur["branch"] = b[11:] if b.startswith("refs/heads/") else b
        elif line.strip() == "bare":
            cur["is_bare"] = True
    if cur:
        out.append(cur)
    return out


def _branches(repo_root):
    out = _git(["for-each-ref", "--format=%(refname:short)", "refs/heads"], repo_root)
    return [b for b in (out or "").splitlines() if b]


def _branch_matches_slug(branch, slug):
    """A git branch 'belongs' to a feature when the slug is its trailing component."""
    if not slug or not branch:
        return False
    return (branch == "feat/" + slug or branch.split("/")[-1] == slug
            or branch.endswith("/" + slug) or branch.endswith("-" + slug))


def _git_context(repo_root):
    """Worktree + branch facts for resolving a feature's branch/worktree occupancy."""
    rows = _parse_worktree_list(_git(["worktree", "list", "--porcelain"], repo_root))
    by_name = {os.path.basename(r["path"].rstrip("/")): r for r in rows if r.get("path")}
    branch_to_wt = {r["branch"]: os.path.basename(r["path"].rstrip("/"))
                    for r in rows if r.get("branch") and r.get("path")}
    return {"by_name": by_name, "branch_to_wt": branch_to_wt, "branches": _branches(repo_root)}


# ---------------------------------------------------------------------------
# Fastship session scanning
# ---------------------------------------------------------------------------

def _state_homes(repo_root):
    """All fastship state homes: main git-common-dir + every linked worktree,
    plus the non-git fallback. Verified layout: <common>/fastship and
    <common>/worktrees/<name>/fastship (see fastship_state.state_home())."""
    homes = []
    common = _git(["rev-parse", "--git-common-dir"], repo_root)
    if common:
        if not os.path.isabs(common):
            common = os.path.join(repo_root, common)
        common = os.path.realpath(common)
        homes.append(os.path.join(common, "fastship"))
        for wt in sorted(glob.glob(os.path.join(common, "worktrees", "*"))):
            homes.append(os.path.join(wt, "fastship"))
    homes.append(os.path.join(repo_root, ".claude", "state", "fastship"))  # fallback
    seen, out = set(), []
    for h in homes:
        if h not in seen:
            seen.add(h)
            out.append(h)
    return out


def _scan_sessions(repo_root):
    """Return {session_id: summary} across all state homes. When the same
    session id appears in multiple homes, keep the freshest by mtime."""
    sessions = {}
    for home in _state_homes(repo_root):
        for sdir in glob.glob(os.path.join(home, "sessions", "*")):
            if not os.path.isdir(sdir):
                continue
            opath = os.path.join(sdir, "orchestrator.json")
            gpath = os.path.join(sdir, "gate.json")
            orch = _read_json(opath)
            gate = _read_json(gpath)
            if not orch and not gate:
                continue
            sid = orch.get("session_id") or os.path.basename(sdir)
            # freshness = newest write across orchestrator + gate (gate can change later)
            summ = _session_summary(sid, sdir, orch, gate, max(_mtime(opath), _mtime(gpath)))
            prev = sessions.get(sid)
            if prev is None or summ["mtime"] >= prev["mtime"]:
                sessions[sid] = summ
    return sessions


def _session_summary(sid, sdir, orch, gate, mtime):
    skipped = _as_list(orch.get("skipped_steps"))
    completed = _as_list(orch.get("completed_steps"))
    applicable = [s for s in ALL_STEPS if s not in skipped]
    done = [s for s in completed if s in applicable]
    step_progress = 100.0 * len(done) / max(1, len(applicable))
    step = orch.get("current_step")
    status = step if step in ("done", "stopped") else ("active" if step else "unknown")
    return {
        "session_id": sid,
        "dir": sdir,
        "mtime": mtime,
        "requirement": orch.get("requirement"),
        "current_step": step,
        "phase": orch.get("phase"),
        "completed_steps": completed,
        "skipped_steps": skipped,
        "applicable_steps": len(applicable),
        "completed_count": len(done),
        "step_progress": round(step_progress, 1),
        "loop_count": orch.get("loop_count", 0),
        "status": status,
        "started_at": orch.get("started_at"),
        "updated_at": orch.get("updated_at"),
        "forge_feature": gate.get("forge_feature"),
        "request_type": gate.get("request_type") or orch.get("request_type"),
        "test_passed": bool(gate.get("test_passed")),
        "e2e_executed": bool(gate.get("e2e_executed")),
        "e2e_gate_passed": bool(gate.get("e2e_gate_passed")),
        "knowledge_acknowledged": bool(gate.get("knowledge_acknowledged")),
        "branch": orch.get("branch") or gate.get("branch"),
        "base_sha": orch.get("base_sha"),
        "worktree": _worktree_from_path(sdir),
    }


def _session_matches_slug(s, slug):
    """A session is LINKED to a feature if its forge_feature, exact id, or id-prefix matches."""
    return bool(slug) and (s.get("forge_feature") == slug
                           or s.get("session_id") == slug
                           or str(s.get("session_id", "")).startswith(slug + "-"))


def _link_session(slug, sessions):
    """Find the best fastship session for a feature slug, freshest first."""
    if not slug:
        return None
    cands = [s for s in sessions.values() if _session_matches_slug(s, slug)]
    if not cands:
        return None
    # mtime (filesystem truth) primary; updated_at only as deterministic tiebreak
    cands.sort(key=lambda s: (s["mtime"], s.get("updated_at") or ""), reverse=True)
    return cands[0]


# ---------------------------------------------------------------------------
# Forge roadmap aggregation
# ---------------------------------------------------------------------------

def _feature_branch_worktree(slug, fs, ctx):
    by_name, branch_to_wt, branches = ctx["by_name"], ctx["branch_to_wt"], ctx["branches"]
    # 1. linked worktree-session: prefer LIVE porcelain branch (recorded may be stale)
    if fs and fs.get("worktree"):
        wt = by_name.get(fs["worktree"])
        return ((wt["branch"] if wt and wt.get("branch") else fs.get("branch")), fs["worktree"])
    # 2. main-checkout session branch (may itself be checked out in a worktree)
    if fs and fs.get("branch"):
        return fs["branch"], branch_to_wt.get(fs["branch"])
    # 3. a worktree whose basename == feature slug
    if slug and slug in by_name:
        return by_name[slug].get("branch"), slug
    # 4. a git branch named after the slug; worktree = where it's checked out (if any)
    if slug:
        cand = next((b for b in branches if _branch_matches_slug(b, slug)), None)
        if cand:
            return cand, branch_to_wt.get(cand)
    return None, None


def _feature_record(repo_root, feat, sessions, ctx):
    slug = feat.get("slug")
    fdir = os.path.join(repo_root, "project-roadmap", "features", slug or "")
    metric = _read_json(os.path.join(fdir, "metric.json")) or None
    harvest = _read_json(os.path.join(fdir, "harvest.json")) or None
    gates = _read_json(os.path.join(repo_root, ".claude", "forge-state", "features", slug or "", "state.json")) or None
    fs = _link_session(slug, sessions)
    branch, worktree = _feature_branch_worktree(slug, fs, ctx)
    return {
        "slug": slug, "name": feat.get("name"), "objective_id": feat.get("objective_id"),
        "status": feat.get("status"), "created_at": feat.get("created_at"),
        "shipped_at": feat.get("shipped_at"), "concluded_at": feat.get("concluded_at"),
        "harvest_due": feat.get("harvest_due"),
        "metric": metric, "harvest": harvest, "forge_gates": gates,
        "fastship": fs, "branch": branch, "worktree": worktree,
        "feature_progress": _feature_progress(feat.get("status"), fs),
    }


def _feature_progress(status, fs):
    if status == "concluded":
        return 100.0
    if status in ("shipped", "measuring"):
        return 90.0
    if status == "in_progress":
        return round(10.0 + 0.8 * fs["step_progress"], 1) if fs else 10.0
    if status == "planned":
        return 5.0
    return 0.0  # draft / unknown


def _rollup(features):
    by_status = {}
    for f in features:
        by_status[f["status"]] = by_status.get(f["status"], 0) + 1
    done = sum(by_status.get(s, 0) for s in DONE_STATUSES)
    todo = [{"slug": f["slug"], "name": f["name"], "status": f["status"],
             "feature_progress": f["feature_progress"]}
            for f in features if f["status"] in TODO_STATUSES]
    overall = round(sum(f["feature_progress"] for f in features) / len(features), 1) if features else 0.0
    return {
        "total": len(features), "by_status": by_status, "done": done,
        "in_progress": by_status.get("in_progress", 0), "todo": todo,
        "overall_progress": overall,
    }


def build_snapshot(repo_root):
    repo_root = os.path.realpath(repo_root)
    roadmap = _read_json(os.path.join(repo_root, "project-roadmap", "roadmap.json"))
    sessions = _scan_sessions(repo_root)

    objectives_raw = roadmap.get("objectives")
    features_raw = roadmap.get("features")
    if not isinstance(objectives_raw, list):
        objectives_raw = []
    if not isinstance(features_raw, list):
        features_raw = []

    by_obj = {}
    objectives = []
    for obj in objectives_raw:
        if not isinstance(obj, dict):
            continue
        rec = {
            "id": obj.get("id"), "name": obj.get("name"),
            "description": obj.get("description"), "target_metric": obj.get("target_metric"),
            "features": [], "rollup": {},
        }
        objectives.append(rec)
        by_obj[obj.get("id")] = rec

    ctx = _git_context(repo_root)
    linked_ids = set()
    orphans = []
    total_features = 0
    for feat in features_raw:
        if not isinstance(feat, dict):
            continue
        total_features += 1
        rec = _feature_record(repo_root, feat, sessions, ctx)
        # EVERY session matching this slug is linked (not just the freshest _link_session pick)
        for s in sessions.values():
            if _session_matches_slug(s, feat.get("slug")):
                linked_ids.add(s["session_id"])
        target = by_obj.get(feat.get("objective_id"))
        (target["features"] if target else orphans).append(rec)

    for rec in objectives:
        rec["rollup"] = _rollup(rec["features"])

    other_sessions = sorted(
        [s for s in sessions.values()
         if s["session_id"] not in linked_ids and s["session_id"] != "default"],
        key=lambda s: (s.get("mtime", 0.0), s["session_id"]), reverse=True)

    return {
        "generated_at": _now_iso(),
        "repo_root": repo_root,
        "north_star": roadmap.get("north_star"),
        "objectives": objectives,
        "orphan_features": orphans,
        "other_sessions": other_sessions,
        "sessions": sorted(sessions.values(), key=lambda s: (s.get("started_at") or "", s["session_id"]), reverse=True),
        "counts": {"objectives": len(objectives), "features": total_features, "sessions": len(sessions)},
    }


# ---------------------------------------------------------------------------
# Web UI
# ---------------------------------------------------------------------------

HTML = r"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Forge / Fastship Dashboard</title>
<style>
  :root{--bg:#0d1117;--card:#161b22;--bd:#30363d;--fg:#e6edf3;--mut:#8b949e;
        --ok:#3fb950;--run:#58a6ff;--todo:#d29922;--draft:#6e7681;--bar:#21262d}
  *{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
    font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif}
  header{padding:18px 24px;border-bottom:1px solid var(--bd);display:flex;
    align-items:baseline;gap:16px;flex-wrap:wrap;position:sticky;top:0;background:var(--bg);z-index:1}
  h1{font-size:18px;margin:0}.ns{color:var(--mut)}.counts{margin-left:auto;color:var(--mut);font-size:12px}
  main{padding:24px 40px}
  .clamp2{display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
  td.sx{max-width:60vw}
  .obj{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:18px;margin-bottom:20px}
  .obj h2{font-size:16px;margin:0 0 4px}.tm{color:var(--mut);font-size:12px;margin-bottom:12px}
  .bar{height:8px;background:var(--bar);border-radius:4px;overflow:hidden;margin:6px 0}
  .bar>i{display:block;height:100%;background:var(--run)}
  .chips{display:flex;gap:8px;flex-wrap:wrap;font-size:12px;margin:8px 0}
  .chip{padding:2px 8px;border-radius:10px;background:var(--bar);color:var(--mut)}
  table{width:100%;border-collapse:collapse;margin-top:10px;font-size:13px}
  th,td{text-align:left;padding:6px 8px;border-bottom:1px solid var(--bd);vertical-align:top}
  th{color:var(--mut);font-weight:500}
  .badge{padding:1px 7px;border-radius:8px;font-size:11px;white-space:nowrap}
  .b-concluded{background:#1a3a24;color:var(--ok)}.b-in_progress{background:#0d2c4d;color:var(--run)}
  .b-shipped,.b-measuring{background:#1a3a24;color:var(--ok)}
  .b-planned{background:#3a2f12;color:var(--todo)}.b-draft{background:#262b31;color:var(--draft)}
  .steps{display:flex;gap:2px;flex-wrap:wrap;margin-top:4px}
  .step{width:14px;height:6px;border-radius:2px;background:var(--bar)}
  .step.done{background:var(--ok)}.step.cur{background:var(--run)}.step.skip{background:#30240d}
  .todo{margin-top:12px;font-size:12px;color:var(--todo)}.todo b{color:var(--fg)}
  .mut{color:var(--mut)}.sess{font-size:12px;color:var(--mut)}
  .wt{font-size:12px;color:var(--mut);margin-top:2px}
  .b-active{background:#0d2c4d;color:var(--run)}.b-done{background:#1a3a24;color:var(--ok)}
  .b-stopped,.b-unknown{background:#262b31;color:var(--draft)}
  .stale{color:var(--todo);font-size:11px;margin-left:4px}
</style></head>
<body>
<header><h1>Forge / Fastship</h1><span class="ns" id="ns"></span>
  <span class="counts" id="counts"></span></header>
<main id="root">Loading...</main>
<script>
const ALL=["1.0","1.1","1.2","1.3","1.3d","1.4","1.5","1.5c","1.6","2.0","2.5","3.0","3.1","3.2","3.3","3.4","3.5","3.6"];
function esc(s){return (s==null?"":""+s).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));}
function bar(p){return `<div class="bar"><i style="width:${Math.max(0,Math.min(100,p||0))}%"></i></div>`;}
function steps(fs){if(!fs)return "";const done=new Set(fs.completed_steps||[]);const skip=new Set(fs.skipped_steps||[]);
  return '<div class="steps">'+ALL.map(s=>{let c="step";if(skip.has(s))c+=" skip";else if(s===fs.current_step)c+=" cur";else if(done.has(s))c+=" done";
  return `<span class="${c}" title="${s}"></span>`;}).join("")+'</div>';}
function ageDays(iso){if(!iso)return null;const t=Date.parse(iso);return isNaN(t)?null:(Date.now()-t)/864e5;}
function staleHint(s){const a=ageDays(s.started_at);if(a==null||s.status!=='active')return '';
  const t=`started ${esc((s.started_at||'').slice(0,10))}, still at ${esc(s.current_step)}`;
  // stuck in Phase-1 (brainstorm/plan) for days = typical abandonment (work shipped outside fastship)
  if((''+s.current_step).charAt(0)==='1'&&a>2) return `<span class="stale" title="${t}">⚠ stuck in planning ${Math.round(a)}d</span>`;
  if(a>14) return `<span class="stale" title="${t}">⚠ stale? ${Math.round(a)}d no progress</span>`;
  return '';}
function fsCell(fs){if(!fs)return '<span class="mut">--</span>';
  return `<div class="sess">step ${esc(fs.current_step)} / P${esc(fs.phase)} / ${fs.completed_count}/${fs.applicable_steps} / loop ${fs.loop_count}/3`
    +(fs.started_at?` / ${esc(fs.started_at.slice(0,10))}`:'')+(fs.test_passed?' / OK test':'')+(fs.e2e_gate_passed?' / OK e2e':'')+staleHint(fs)+`</div>`+steps(fs);}
function metricCell(f){if(!f.metric)return '<span class="mut">--</span>';
  const m=f.metric,h=f.harvest;let s=`${esc(m.baseline)} -> ${esc(m.target)}`;
  if(h&&h.actual!=null)s+=` / actual ${esc(h.actual)} (${esc(h.verdict)})`;return `<span class="mut">${s}</span>`;}
function wtLine(o){return `<div class="wt">⎇ ${esc(o.branch||"—")} · 🗂 ${esc(o.worktree||"—")}</div>`;}
function otherCard(list){if(!list||!list.length)return "";
  const rows=list.map(s=>`<tr>
    <td class="sx"><div class="clamp2"><b>${esc(s.requirement||s.session_id)}</b></div><div class="mut">${esc(s.session_id)}</div>${wtLine(s)}</td>
    <td><span class="badge b-${esc(s.status)}">${esc(s.status)}</span></td>
    <td style="min-width:120px">${bar(s.step_progress)}<span class="mut">${s.step_progress}%</span></td>
    <td>${fsCell(s)}</td><td><span class="mut">--</span></td></tr>`).join("");
  return `<section class="obj"><h2>Other <span class="mut">未归入 forge</span></h2>
    <div class="tm">fastship session 未关联任何 forge feature（无 metric）</div>
    <table><thead><tr><th>Session</th><th>status</th><th>progress</th><th>fastship</th><th>metric</th></tr></thead>
    <tbody>${rows}</tbody></table></section>`;}
function featRow(f){return `<tr>
  <td><b>${esc(f.name)}</b><div class="mut">${esc(f.slug)}</div>${wtLine(f)}</td>
  <td><span class="badge b-${esc(f.status)}">${esc(f.status)}</span></td>
  <td style="min-width:120px">${bar(f.feature_progress)}<span class="mut">${f.feature_progress}%</span></td>
  <td>${fsCell(f.fastship)}</td>
  <td>${metricCell(f)}</td></tr>`;}
function objCard(o){const r=o.rollup||{};
  const chips=Object.entries(r.by_status||{}).map(([k,v])=>`<span class="chip">${esc(k)}: ${v}</span>`).join("");
  const todo=(r.todo||[]).map(t=>`<b>${esc(t.name)}</b> <span class="mut">(${esc(t.status)})</span>`).join(" / ");
  return `<section class="obj"><h2>${esc(o.name)} <span class="mut">${esc(o.id)}</span></h2>
    <div class="tm">${esc(o.target_metric)||""}</div>
    ${bar(r.overall_progress)}<div class="mut">total ${r.overall_progress||0}% / ${r.done||0}/${r.total||0} done</div>
    <div class="chips">${chips}</div>
    <table><thead><tr><th>Feature</th><th>status</th><th>progress</th><th>fastship</th><th>metric</th></tr></thead>
    <tbody>${(o.features||[]).map(featRow).join("")||'<tr><td colspan=5 class="mut">no feature</td></tr>'}</tbody></table>
    ${todo?`<div class="todo">TODO (${(r.todo||[]).length}): ${todo}</div>`:''}</section>`;}
async function load(){try{const s=await (await fetch("/api/state")).json();
  document.getElementById("ns").textContent=(s.north_star||"");
  document.getElementById("counts").textContent=`${s.counts.objectives} objectives / ${s.counts.features} features / ${s.counts.sessions} sessions / ${esc(s.generated_at)}`;
  document.getElementById("root").innerHTML=(s.objectives||[]).map(objCard).join("")
    +((s.orphan_features||[]).length?`<section class="obj"><h2>Unassigned Features</h2><table><tbody>${s.orphan_features.map(featRow).join("")}</tbody></table></section>`:"")
    +otherCard(s.other_sessions);
}catch(e){document.getElementById("root").textContent="load failed: "+e;}}
load();setInterval(load,5000);
</script></body></html>"""


def render_html():
    return HTML


class _Handler(BaseHTTPRequestHandler):
    repo_root = "."

    def log_message(self, *a):  # silence default stderr logging
        pass

    def _send(self, code, body, ctype):
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/healthz":
            return self._send(200, "ok", "text/plain; charset=utf-8")
        if self.path.startswith("/api/state"):
            snap = build_snapshot(self.repo_root)
            return self._send(200, json.dumps(snap, ensure_ascii=False), "application/json; charset=utf-8")
        if self.path == "/" or self.path.startswith("/index"):
            return self._send(200, render_html(), "text/html; charset=utf-8")
        return self._send(404, "not found", "text/plain; charset=utf-8")


def serve(repo_root, port):
    _Handler.repo_root = os.path.realpath(repo_root)
    httpd = ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    print(f"Forge dashboard -> http://127.0.0.1:{httpd.server_address[1]}  (repo: {_Handler.repo_root})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="Forge + Fastship progress dashboard")
    ap.add_argument("--repo-root", default=os.getcwd())
    ap.add_argument("--port", type=int, default=7575)
    ap.add_argument("--once", action="store_true", help="print snapshot JSON and exit")
    args = ap.parse_args(argv)
    if args.once:
        print(json.dumps(build_snapshot(args.repo_root), ensure_ascii=False, indent=2))
        return 0
    return serve(args.repo_root, args.port)


if __name__ == "__main__":
    raise SystemExit(main())
