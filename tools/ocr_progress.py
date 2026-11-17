#!/usr/bin/env python3
"""OCR 批处理进度监控: 状态统计、失败页、ETA、抽样。"""
import json, sys, statistics
from pathlib import Path

OUTROOT = Path("/mnt/workspace/环工原理/output")
TOTAL = 529

def stat(engine: str):
    d = OUTROOT / engine
    state = d / "state.jsonl"
    ok, failed, secs, figs, chars = [], [], [], [], []
    if state.exists():
        for line in state.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if rec.get("status") == "ok":
                ok.append(rec["page"]); secs.append(rec.get("secs") or 0)
                figs.append(rec.get("figs") or 0); chars.append(rec.get("chars") or 0)
            elif rec.get("status") == "failed":
                failed.append(rec["page"])
    last_page = max(ok) if ok else 0
    med = statistics.median(secs) if secs else 0
    eta_h = (TOTAL - len(ok)) * med / 3600 if med else 0
    print(f"[{engine}] 完成 {len(ok)}/{TOTAL} (最后页 {last_page})  失败 {len(failed)}  "
          f"中位 {med:.0f}s/页  平均字符 {int(statistics.mean(chars)) if chars else 0}  "
          f"图共 {sum(figs)}  ETA ~{eta_h:.1f}h")
    if failed:
        print(f"  失败页: {sorted(failed)[:30]}{' ...' if len(failed) > 30 else ''}")

for eng in sys.argv[1:] or ["deepseek", "olm"]:
    stat(eng)
imgdir = OUTROOT / "images"
n_img = len(list(imgdir.glob("*.png"))) if imgdir.exists() else 0
print(f"images: {n_img} 张裁剪图")
