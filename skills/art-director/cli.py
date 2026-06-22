# cli.py
import os, sys
sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))   # realpath 跟随软链 → 真 skill 根(symlink/-m 健壮)
import argparse, datetime
from config import Config
from manifest import Manifest
from extractor import reconcile, UnsupportedMarkup
from wiring import verify, degrade, reset_failed
# 粗略成本(USD,as-of 2026-06 §6 文档区间;非权威,见 run.log 实际)
_COST={"bg":{"1k":0.02,"2k":0.06,"4k":0.21},"cutout":0.06}
def _estimate(m): return sum(_COST["cutout"] if a.kind=="cutout" else _COST["bg"].get(a.resolution or "2k",0.06) for a in m.assets)
def _log(project_dir, line):
    p=os.path.join(project_dir,".art-director","run.log"); os.makedirs(os.path.dirname(p),exist_ok=True)
    with open(p,"a",encoding="utf-8") as f: f.write(line+"\n")
def _cmd_validate(args):
    m=Manifest.load(args.manifest); errs=m.validate(max_assets=Config(api_key="x").max_assets)
    for e in errs: print(f"[manifest] {e}")
    print(f"[cost] estimated ~${_estimate(m):.2f} for {len(m.assets)} assets (as-of 2026-06 prices; see run.log for actual)")
    if args.code:
        try: r=reconcile(open(args.code,encoding="utf-8").read(), m)
        except UnsupportedMarkup as e: print(f"[reconcile] {e}"); errs.append("unsupported")
        else:
            for p in sorted(r.missing_in_manifest): print(f"[reconcile] code refs {p} without manifest entry"); errs.append("x")
            for p in sorted(r.not_wired): print(f"[reconcile] manifest {p} placeholder not wired in code"); errs.append("x")
    print("VALIDATE: FAIL" if errs else "VALIDATE: PASS"); return 1 if errs else 0
def _cmd_gen(args):
    from transport import UrllibTransport
    from apimart import ApimartClient
    from engine import generate
    cfg=Config.from_env()
    if args.bg_resolution: cfg.default_bg_resolution=args.bg_resolution
    m=Manifest.load(args.manifest); errs=m.validate(max_assets=cfg.max_assets)
    if errs:
        for e in errs: print(f"[manifest] {e}")
        print("GEN: FAIL (invalid manifest)"); return 1
    mp_out=os.path.join(args.project_dir,cfg.manifest_path)
    client=ApimartClient(UrllibTransport(),cfg.api_key,cfg.base_url)
    def persist(man): man.save_atomic(mp_out)
    _log(args.project_dir, f"{datetime.datetime.now().isoformat()} gen start: {len(m.assets)} assets, est ${_estimate(m):.2f}")
    m=generate(m,args.project_dir,client,cfg,on_progress=persist); persist(m)
    for a in m.assets: _log(args.project_dir, f"  {a.id} kind={a.kind} status={a.status} task_id={a.task_id}")
    failed=[a.id for a in m.assets if a.status!="done"]
    if failed:
        n=degrade(m,args.project_dir,args.page)
        print(f"GEN: PARTIAL — failed {failed} ({n} degraded in {args.page}; rerun to resume)"); return 1
    print("GEN: PASS"); return 0
def _cmd_gate(args):
    m=Manifest.load(args.manifest)
    code=open(args.code,encoding="utf-8").read() if args.code else None
    probs=verify(m,args.project_dir,code=code)
    for p in probs: print(f"[gate] {p}")
    if probs:
        reset_failed(m); m.save_atomic(os.path.join(args.project_dir,Config(api_key="x").manifest_path))
        print(f"GATE: FAIL ({len(probs)})"); return 1
    print("GATE: PASS"); return 0
def main(argv=None):
    ap=argparse.ArgumentParser(prog="art-director"); sub=ap.add_subparsers(dest="cmd",required=True)
    v=sub.add_parser("validate"); v.add_argument("--manifest",required=True); v.add_argument("--code"); v.set_defaults(fn=_cmd_validate)
    g=sub.add_parser("gen"); g.add_argument("--manifest",required=True); g.add_argument("--project-dir",required=True); g.add_argument("--bg-resolution"); g.add_argument("--page",default="index.html"); g.set_defaults(fn=_cmd_gen)
    t=sub.add_parser("gate"); t.add_argument("--manifest",required=True); t.add_argument("--project-dir",required=True); t.add_argument("--code"); t.set_defaults(fn=_cmd_gate)
    args=ap.parse_args(argv); return args.fn(args)
if __name__=="__main__": sys.exit(main())
