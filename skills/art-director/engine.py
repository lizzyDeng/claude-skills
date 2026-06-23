# engine.py
import os, time, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from registry import build_request, build_probe_request
from transport import RetryableError
from pngutil import is_png, png_has_alpha
from manifest import Asset
def _abs(d,r): return os.path.join(d,r)
def _safe_unlink(p):
    try: os.unlink(p)
    except OSError: pass
def validate_download(asset,dest):
    if not os.path.exists(dest) or os.path.getsize(dest)==0: raise ValueError(f"{asset.id}: empty download")
    if not is_png(dest): raise ValueError(f"{asset.id}: download not a PNG (likely error page)")
    if asset.kind=="cutout" and not png_has_alpha(dest): raise ValueError(f"{asset.id}: cutout without alpha")
def _sleep(clock,cfg,attempt,retry_after):
    if attempt>=cfg.retries: return
    delay=retry_after if retry_after else cfg.backoff_base*(2**attempt)
    if delay: clock.sleep(min(delay,30))
def _gen_one(asset,project_dir,client,cfg,clock,persist):
    body=build_request(asset,cfg)             # ValueError → 不重试
    last=None
    for attempt in range(cfg.retries+1):
        try:
            if not asset.task_id:
                asset.task_id=client.submit(body); persist()      # 提交即持久化(kill 不丢付费任务)
            result=client.poll(asset.task_id,cfg.poll_timeout,cfg.poll_interval,clock=clock)
            dest=_abs(project_dir,asset.path); os.makedirs(os.path.dirname(dest),exist_ok=True)
            tmp=dest+".part"; client.fetch_image(result,tmp); validate_download(asset,tmp); os.replace(tmp,dest)
            return "done"
        except RetryableError as e:
            last=e; _sleep(clock,cfg,attempt,e.retry_after)       # task_id 若已有则保留 → 续轮询;submit 期则 None → 重试 submit
        except TimeoutError as e:
            last=e; _sleep(clock,cfg,attempt,None)                # poll 超时:任务可能仍在跑 → 续轮询同一 task
        except Exception:                                         # FatalError / 校验失败 → 终态
            asset.task_id=None; _safe_unlink(_abs(project_dir,asset.path)+".part"); raise
    asset.task_id=None; _safe_unlink(_abs(project_dir,asset.path)+".part"); raise last  # 重试用尽:也清 task_id 以便重生
PREVIEW_DIR=os.path.join(".art-director","preview")
def _probe_one(asset,project_dir,client,cfg,clock):
    """Stage 1.5:单个变体的 1k 探针。复用 _gen_one 的 submit/poll/download/校验语义,
    但用 build_probe_request 强制 1k,且不持久化进 manifest(预览是一次性的)。"""
    body=build_probe_request(asset,cfg)            # 强制 resolution=1k；ValueError → 不重试
    last=None
    for attempt in range(cfg.retries+1):
        try:
            if not asset.task_id:
                asset.task_id=client.submit(body)
            result=client.poll(asset.task_id,cfg.poll_timeout,cfg.poll_interval,clock=clock)
            dest=_abs(project_dir,asset.path); os.makedirs(os.path.dirname(dest),exist_ok=True)
            tmp=dest+".part"; client.fetch_image(result,tmp); validate_download(asset,tmp); os.replace(tmp,dest)
            return "done"
        except RetryableError as e:
            last=e; _sleep(clock,cfg,attempt,e.retry_after)
        except TimeoutError as e:
            last=e; _sleep(clock,cfg,attempt,None)
        except Exception:
            asset.task_id=None; _safe_unlink(_abs(project_dir,asset.path)+".part"); raise
    asset.task_id=None; _safe_unlink(_abs(project_dir,asset.path)+".part"); raise last
def generate_previews(carrier_asset,variants,project_dir,client,cfg,out_dir=None,clock=time):
    """对一个 bg 风格载体跑 N 个变体的 1k 探针,输出到 .art-director/preview/。
    variants: [{"label","prompt","style_suffix"}...]。每个变体用其 prompt 构造强制 1k 的 bg 请求,
    并发提交→轮询→下载→PNG 校验,文件名 <carrier_id>__<label>.png。不写进 assets/gen/、不入 manifest。
    返回 [{"label","prompt","style_suffix","path"(相对 project_dir),"status"}...](保序)。"""
    if carrier_asset.kind!="bg":
        raise ValueError(f"preview only supports a bg style carrier (carrier {carrier_asset.id} kind={carrier_asset.kind!r})")
    out_rel=out_dir or PREVIEW_DIR
    os.makedirs(_abs(project_dir,out_rel),exist_ok=True)
    probes=[]                                       # (variant, Asset)
    for v in variants:
        label=v["label"]; rel=os.path.join(out_rel,f"{carrier_asset.id}__{label}.png")
        a=Asset(id=f"preview__{carrier_asset.id}__{label}",kind="bg",prompt=v["prompt"],
                aspect=carrier_asset.aspect,path=rel,placeholder="preview",
                transparent=False,resolution="1k",status="pending")
        probes.append((v,a))
    results={}
    with ThreadPoolExecutor(max_workers=cfg.concurrency) as ex:
        futs={ex.submit(_probe_one,a,project_dir,client,cfg,clock):(v,a) for v,a in probes}
        for fut in as_completed(futs):
            v,a=futs[fut]
            try: results[a.id]=fut.result()
            except Exception: results[a.id]="failed"
    out=[]
    for v,a in probes:
        out.append({"label":v["label"],"prompt":v["prompt"],"style_suffix":v.get("style_suffix",""),
                    "path":a.path,"status":results.get(a.id,"failed")})
    return out
def generate(manifest,project_dir,client,cfg,on_progress=None,clock=time):
    todo=[a for a in manifest.assets if not (a.status=="done" and os.path.exists(_abs(project_dir,a.path)))]
    lock=threading.Lock()
    def persist():
        if on_progress:
            with lock: on_progress(manifest)
    with ThreadPoolExecutor(max_workers=cfg.concurrency) as ex:
        futs={ex.submit(_gen_one,a,project_dir,client,cfg,clock,persist):a for a in todo}
        for fut in as_completed(futs):
            a=futs[fut]
            try: a.status=fut.result()
            except Exception: a.status="failed"
            persist()
    return manifest
