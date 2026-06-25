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
    cfg=Config(api_key="x")
    m=Manifest.load(args.manifest); errs=m.validate(max_assets=cfg.max_assets)
    for e in errs: print(f"[manifest] {e}")
    est=_estimate(m)
    print(f"[cost] estimated ~${est:.2f} for {len(m.assets)} assets (as-of 2026-06 prices; see run.log for actual)")
    if est>cfg.preview_cost_threshold or len(m.assets)>cfg.preview_asset_threshold:   # 非阻断:Stage 1.5 建议
        print(f"💡 PREVIEW SUGGESTED: 全套成本 ~${est:.2f} / {len(m.assets)} 个资产较高,建议先 preview 锁定艺术方向再 gen")
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
def _cmd_regen(args):
    # Stage 3.5 定向重生:清掉命名 asset 的 task_id+status → generate() 重新计费生成该张,其它 done 的复用不重复付费
    from transport import UrllibTransport
    from apimart import ApimartClient
    from engine import generate
    cfg=Config.from_env()
    if args.bg_resolution: cfg.default_bg_resolution=args.bg_resolution
    m=Manifest.load(args.manifest); by={a.id:a for a in m.assets}
    unknown=[i for i in args.asset if i not in by]
    if unknown:                                                  # 花钱前拦截未知 id
        print(f"[regen] asset(s) not found in manifest: {', '.join(unknown)} (have: {', '.join(by)})"); return 1
    if args.prompt is not None and len(args.asset)!=1:           # 一个 prompt 套多张几乎都是错的
        print(f"[regen] --prompt requires exactly a single --asset (got {len(args.asset)})"); return 1
    if args.prompt is not None: by[args.asset[0]].prompt=args.prompt
    for i in args.asset: by[i].task_id=None; by[i].status="pending"   # 清状态:强制重新 submit(重新计费)+进 todo
    errs=m.validate(max_assets=cfg.max_assets)                   # 改 prompt 后再校验
    if errs:
        for e in errs: print(f"[manifest] {e}")
        print("REGEN: FAIL (invalid manifest)"); return 1
    mp_out=os.path.join(args.project_dir,cfg.manifest_path)
    client=ApimartClient(UrllibTransport(),cfg.api_key,cfg.base_url)
    def persist(man): man.save_atomic(mp_out)
    persist(m)                                                   # 先落盘清掉的 task_id(提交前持久化,kill 不丢)
    named=[by[i] for i in args.asset]
    est=sum(_COST["cutout"] if a.kind=="cutout" else _COST["bg"].get(a.resolution or "2k",0.06) for a in named)
    _log(args.project_dir, f"{datetime.datetime.now().isoformat()} regen start: {[a.id for a in named]}, est ${est:.2f}")
    m=generate(m,args.project_dir,client,cfg,on_progress=persist); persist(m)
    for a in named: _log(args.project_dir, f"  {a.id} kind={a.kind} status={a.status} task_id={a.task_id}")
    print(f"[cost] regenerated ~${est:.2f} for {len(named)} asset(s) (as-of 2026-06 prices; see run.log for actual)")
    failed=[a.id for a in named if a.status!="done"]
    if failed:                                                   # 旧图被 .part→os.replace 原子保护,失败不白屏
        print(f"REGEN: PARTIAL — failed {failed} (旧图保留;rerun regen 重试)"); return 1
    print("REGEN: PASS"); return 0
def _load_variants(path):
    import json
    data=json.loads(open(path,encoding="utf-8").read())
    if not isinstance(data,list) or not data: raise ValueError("variants-file must be a non-empty JSON array")
    for v in data:
        if not isinstance(v,dict) or not v.get("label") or not v.get("prompt"):
            raise ValueError("each variant needs non-empty 'label' and 'prompt'")
    return data
def _cmd_preview(args):
    from transport import UrllibTransport
    from apimart import ApimartClient
    from engine import generate_previews, PREVIEW_DIR
    cfg=Config.from_env()
    m=Manifest.load(args.manifest)
    try:
        carrier=m.carrier_by_id(args.carrier) if args.carrier else m.style_carrier()
        variants=_load_variants(args.variants_file)
    except ValueError as e:
        print(f"[preview] {e}"); return 1                            # 干净错误行,非 traceback
    out_rel=args.out or PREVIEW_DIR
    client=ApimartClient(UrllibTransport(),cfg.api_key,cfg.base_url)
    probe_cost=len(variants)*_COST["bg"]["1k"]
    _log(args.project_dir, f"{datetime.datetime.now().isoformat()} preview start: carrier={carrier.id} {len(variants)} variants @1k, est ${probe_cost:.2f}")
    results=generate_previews(carrier,variants,args.project_dir,client,cfg,out_dir=out_rel)
    # 落盘 previews.json(含 label/style_suffix,供 lock-style 复用)
    import json
    pj=os.path.join(args.project_dir,out_rel,"previews.json"); os.makedirs(os.path.dirname(pj),exist_ok=True)
    with open(pj,"w",encoding="utf-8") as f: f.write(json.dumps({"carrier":carrier.id,"variants":results},ensure_ascii=False,indent=2))
    for r in results:
        print(f"[preview] {r['label']}: {r['status']} → {os.path.join(args.project_dir,r['path'])}")
        _log(args.project_dir, f"  preview {r['label']} status={r['status']} path={r['path']}")
    print(f"[cost] probe estimated ~${probe_cost:.2f} for {len(variants)} variants @1k (as-of 2026-06 prices)")
    failed=[r["label"] for r in results if r["status"]!="done"]
    if failed: print(f"PREVIEW: PARTIAL — failed {failed}"); return 1
    print(f"PREVIEW: PASS ({len(results)} variants → {pj})"); return 0
def _cmd_lock_style(args):
    import json
    m=Manifest.load(args.manifest)
    try:
        carrier=m.carrier_by_id(args.carrier) if args.carrier else m.style_carrier()
        # 选中变体:优先 --variants-file,否则 previews.json(后者须显式 --project-dir,不静默默认 cwd)
        if args.variants_file: variants=_load_variants(args.variants_file)
        else:
            if not args.project_dir:
                print("[lock-style] --project-dir required to locate previews.json (or pass --variants-file)"); return 1
            pj=os.path.join(args.project_dir,".art-director","preview","previews.json")
            if not os.path.exists(pj): print(f"[lock-style] no previews.json at {pj}; run preview first or pass --variants-file"); return 1
            variants=json.loads(open(pj,encoding="utf-8").read()).get("variants",[])
    except ValueError as e:
        print(f"[lock-style] {e}"); return 1                          # 干净错误行,非 traceback
    chosen=next((v for v in variants if v.get("label")==args.variant), None)
    if chosen is None: print(f"[lock-style] variant {args.variant!r} not found among {[v.get('label') for v in variants]}"); return 1
    carrier.prompt=chosen["prompt"]                                  # carrier prompt ← 选中变体完整生成提示
    changed=m.apply_style_suffix(chosen.get("style_suffix",""), skip_ids={carrier.id})
    m.save_atomic(args.manifest)                                     # 回写到加载的 manifest(单源)
    print(f"[lock-style] carrier {carrier.id}.prompt ← variant {args.variant!r}")
    if changed: print(f"[lock-style] style_suffix appended to: {', '.join(changed)}")
    else: print(f"[lock-style] no other asset prompts changed (already styled or empty suffix)")
    print("LOCK-STYLE: PASS"); return 0
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
    p=sub.add_parser("preview"); p.add_argument("--manifest",required=True); p.add_argument("--project-dir",required=True); p.add_argument("--variants-file",required=True); p.add_argument("--carrier"); p.add_argument("--out"); p.set_defaults(fn=_cmd_preview)
    l=sub.add_parser("lock-style"); l.add_argument("--manifest",required=True); l.add_argument("--variant",required=True); l.add_argument("--variants-file"); l.add_argument("--carrier"); l.add_argument("--project-dir"); l.set_defaults(fn=_cmd_lock_style)
    t=sub.add_parser("gate"); t.add_argument("--manifest",required=True); t.add_argument("--project-dir",required=True); t.add_argument("--code"); t.set_defaults(fn=_cmd_gate)
    rg=sub.add_parser("regen"); rg.add_argument("--manifest",required=True); rg.add_argument("--project-dir",required=True); rg.add_argument("--asset",action="append",required=True); rg.add_argument("--prompt"); rg.add_argument("--bg-resolution"); rg.set_defaults(fn=_cmd_regen)
    args=ap.parse_args(argv); return args.fn(args)
if __name__=="__main__": sys.exit(main())
