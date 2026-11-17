#!/usr/bin/env python3
"""PDF 全书 OCR 管线: 断点续跑 + 双引擎 + 图表坐标裁剪。

用法:
  pdf_ocr_pipeline.py deepseek --pages 1-529          # 跑(默认续跑)
  pdf_ocr_pipeline.py olm      --pages 1-529
  pdf_ocr_pipeline.py deepseek --mode combine         # 只合并已有页

特性:
  - 断点: output/<engine>/state.jsonl 追加式日志;重跑自动跳过已完成页;失败页默认重试。
  - raw 保留: output/<engine>/raw/page-NNNN.raw.txt(deepseek 坐标、olm 图引用都在里面)。
  - 图裁剪: 解析 grounding 坐标,按 crop-scale 从原 PDF 重渲染高清页后裁剪,
            存 output/images/<eng>-pNNNN-fK.png,并把 markdown 里的引用替换成真实图片链接。
  - 优雅停机: SIGTERM/SIGINT 完成当前页后落盘退出。
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import queue
import re
import signal
import sys
import threading
import time
from pathlib import Path

import requests
from PIL import Image
import pypdfium2 as pdfium

# 仓库相对路径: 本机在 /mnt/workspace/环工原理, 设备2克隆到任意目录均可运行
ROOT = Path(__file__).resolve().parent.parent
PDF_PATH = ROOT / "环境工程原理.pdf"
OUTROOT = ROOT / "output"

OLM_PROMPT = (
    "Attached is one page of a document that you must process. "
    "Just return the plain text representation of this document as if you were reading it naturally. "
    "Convert equations to LateX and tables to HTML.\n"
    "If there are any figures or charts, label them with the following markdown syntax "
    "![Alt text describing the contents of the figure](page_startx_starty_width_height.png)\n"
    "Return your output as markdown, with a front matter section on top specifying values for the "
    "primary_language, is_rotation_valid, rotation_correction, is_table, and is_diagram parameters."
)
ENGINES = {
    "deepseek": {"model": "deepseek-ocr", "port": 8931, "prompt": "<|grounding|>Convert the document to markdown.",
                 "repeat_penalty": 1.05, "timeout": 2400, "tag": "ds", "coord_space": "norm1000"},
    "olm": {"model": "olmocr-2", "port": 8930, "prompt": OLM_PROMPT,
            "repeat_penalty": 1.0, "timeout": 3600, "tag": "olm", "coord_space": "pixel"},
}

STOP = False


def _on_signal(signum, frame):
    global STOP
    STOP = True
    print(f"[pipeline] 收到信号 {signum},当前页完成后退出", flush=True)


def log(msg: str, logfile: Path | None):
    line = f"[{time.strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    if logfile:
        with open(logfile, "a", encoding="utf-8") as f:
            f.write(line + "\n")


# ---------- 渲染 ----------

class Renderer:
    def __init__(self, pdf_path: Path):
        self.pdf = pdfium.PdfDocument(str(pdf_path))
        self.total = len(self.pdf)

    def render(self, page_no: int, target: int) -> Image.Image:
        page = self.pdf[page_no - 1]
        w, h = page.get_size()
        scale = target / max(w, h)
        bmp = page.render(scale=scale)
        img = bmp.to_pil()
        if img.mode in ("RGBA", "LA"):
            bg = Image.new("RGB", img.size, "white")
            bg.paste(img, mask=img.split()[-1])
            img = bg
        return img.convert("RGB")


def png_b64(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii")


class Prefetcher(threading.Thread):
    """预取线程: 渲染 + PNG 编码与 OCR 重叠,消除 llama-server 空泡。
    PDFium 非线程安全 → 本线程持有独立 PdfDocument 句柄。"""

    def __init__(self, pdf_path: Path, pages: list[int], target: int, png_dir: Path):
        super().__init__(daemon=True)
        self.pdf_path = pdf_path
        self.pages = pages
        self.target = target
        self.png_dir = png_dir
        self.q: queue.Queue = queue.Queue(maxsize=2)
        self.error: Exception | None = None

    def run(self):
        try:
            renderer = Renderer(self.pdf_path)  # 独立句柄,仅本线程使用
            for p in self.pages:
                img = renderer.render(p, self.target)
                f = self.png_dir / f"page-{p:04d}.png"
                if not f.exists():
                    img.save(f)
                self.q.put((p, png_b64(img)))
        except Exception as e:  # 渲染失败: 记录并停止供给(已入队页照常处理)
            self.error = e
        finally:
            self.q.put(None)


def call_ocr(eng: dict, img_b64: str, max_tokens: int, timeout: int) -> tuple[str, dict]:
    payload = {
        "model": eng["model"],
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + img_b64}},
            {"type": "text", "text": eng["prompt"]},
        ]}],
        "temperature": 0, "top_k": 1,
        "repeat_penalty": eng["repeat_penalty"],
        "max_tokens": max_tokens, "stream": False,
    }
    r = requests.post(f"http://127.0.0.1:{eng['port']}/v1/chat/completions",
                      json=payload, timeout=(10, timeout))
    if r.status_code != 200:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
    data = r.json()
    return data["choices"][0]["message"]["content"], data.get("usage") or {}


# ---------- 坐标解析与清洗 ----------

# deepseek 形式1: <|ref|>label<|det|>[[x1,y1,x2,y2]]<|/det|>
RE_REF_DET = re.compile(r"<\|ref\|>(.*?)<\|/ref\|>\s*<\|det\|>\s*(\[\[[0-9,\s.\-]+\]\])\s*<\|/det\|>", re.S)
# deepseek 形式2(服务端已剥 token): label[[x1,y1,x2,y2]]
RE_LINE_DET = re.compile(r"^([^\[\]\n]{0,120}?)\s*(\[\[[0-9,\s.\-]+\]\])\s*$", re.M)
# olm 形式: ![alt](page_startx_starty_width_height.png)
RE_OLM_FIG = re.compile(r"!\[([^\]]*)\]\(\s*page_(\d+)_(\d+)_(\d+)_(\d+)\.png\s*\)")

FIGISH = re.compile(r"image|figure|fig|chart|diagram|picture|photo|graph|table|表|图", re.I)


def parse_ds_boxes(raw: str) -> tuple[list[dict], str]:
    """返回 [(label, x1,y1,x2,y2)], 以及把每个框替换为 @@FIGK:i@@ 占位符后的文本。"""
    out: list[dict] = []
    def sub_refdet(m):
        label = m.group(1).strip()
        nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", m.group(2))]
        if len(nums) >= 4:
            out.append({"label": label, "box": nums[:4]})
            return f"@@FIGK:{len(out)-1}@@"
        return label
    text = RE_REF_DET.sub(sub_refdet, raw)
    def sub_line(m):
        label = m.group(1).strip()
        nums = [float(x) for x in re.findall(r"-?\d+(?:\.\d+)?", m.group(2))]
        if len(nums) >= 4:
            out.append({"label": label, "box": nums[:4]})
            return f"@@FIGK:{len(out)-1}@@"
        return m.group(0)
    text = RE_LINE_DET.sub(sub_line, text)
    # 清洗残余特殊 token
    text = re.sub(r"<\|/?grounding\|>", "", text)
    text = text.replace("<|ref|>", "").replace("<|/ref|>", "").replace("<|det|>", "").replace("<|/det|>", "")
    return out, text


def parse_olm_figs(raw: str) -> tuple[list[dict], str]:
    out: list[dict] = []
    def sub(m):
        alt, x, y, w, h = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))
        out.append({"label": alt, "box": [x, y, x + w, y + h]})
        return f"@@FIGK:{len(out)-1}@@"
    text = RE_OLM_FIG.sub(sub, raw)
    return out, text


def resolve_box(box, img_w: int, img_h: int, space: str) -> tuple[int, int, int, int] | None:
    """把坐标解释到渲染图像素空间并裁剪到边界;space=pixel|norm1000|auto。"""
    x1, y1, x2, y2 = box
    def scaled(space_choice):
        if space_choice == "norm1000":
            return (x1 / 1000 * img_w, y1 / 1000 * img_h, x2 / 1000 * img_w, y2 / 1000 * img_h)
        return (x1, y1, x2, y2)
    if space == "auto":
        if x2 > img_w * 1.02 or y2 > img_h * 1.02:
            space = "norm1000"
        else:
            space = "pixel"
    x1, y1, x2, y2 = scaled(space)
    # 规整顺序
    if x1 > x2: x1, x2 = x2, x1
    if y1 > y2: y1, y2 = y2, y1
    # 外扩 4%(grounding 框常偏紧,宁多勿切),再裁到边界
    px = max(8.0, (x2 - x1) * 0.04)
    py = max(8.0, (y2 - y1) * 0.04)
    x1, y1, x2, y2 = x1 - px, y1 - py, x2 + px, y2 + py
    x1, y1 = max(0, int(x1)), max(0, int(y1))
    x2, y2 = min(img_w, int(round(x2))), min(img_h, int(round(y2)))
    if x2 - x1 < 24 or y2 - y1 < 24:
        return None
    return (x1, y1, x2, y2)


# ---------- 断点状态 ----------

class State:
    def __init__(self, path: Path):
        self.path = path
        self.done: dict[int, dict] = {}
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    rec = json.loads(line)
                    self.done[rec["page"]] = rec
                except Exception:
                    continue

    def is_ok(self, page: int) -> bool:
        return self.done.get(page, {}).get("status") == "ok"

    def record(self, rec: dict):
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self.done[rec["page"]] = rec


# ---------- 主流程 ----------

def pending_pages(renderer: Renderer, state: State, pages_spec: str | None, skip_failed: bool) -> list[int]:
    total = renderer.total
    if pages_spec:
        wanted: set[int] = set()
        for part in pages_spec.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                a, b = part.split("-", 1)
                wanted.update(range(max(1, int(a)), min(total, int(b)) + 1))
            elif 1 <= int(part) <= total:
                wanted.add(int(part))
        pages = sorted(wanted)
    else:
        pages = list(range(1, total + 1))
    out = []
    for p in pages:
        if state.is_ok(p):
            continue
        rec = state.done.get(p)
        if rec and rec.get("status") == "failed" and skip_failed:
            continue
        out.append(p)
    return out


def crop_figures(renderer: Renderer, page_no: int, figs: list[dict], img_w: int, img_h: int,
                 crop_scale: float, space: str, imgdir: Path, eng_tag: str, logfile: Path | None) -> list[str]:
    """裁剪图,返回替换占位符用的相对链接列表(索引对应 figs)。"""
    rels: list[str] = []
    hi = None
    hi_w = hi_h = 0
    for i, fig in enumerate(figs):
        lbl = (fig.get("label") or "").strip()
        if lbl.lower().endswith("caption") or not FIGISH.search(lbl):
            rels.append("")  # 非图表引用(如 text/sub_title/image_caption): 只删坐标行,保留内容
            continue
        box = resolve_box(fig["box"], img_w, img_h, space)
        if not box:
            rels.append("")
            continue
        if crop_scale > 1.01 and hi is None:
            hi = renderer.render(page_no, int(1288 * crop_scale))
            hi_w, hi_h = hi.size
        src = hi if hi is not None else None
        if src is not None:
            fx = crop_scale
            cbox = (int(box[0] * fx), int(box[1] * fx),
                    min(int(round(box[2] * fx)), hi_w), min(int(round(box[3] * fx)), hi_h))
            cimg = src.crop(cbox)
        else:
            cimg = None
        if cimg is None or cimg.width < 24 or cimg.height < 24:
            rels.append("")
            continue
        name = f"{eng_tag}-p{page_no:04d}-f{i+1}.png"
        cimg.save(imgdir / name, format="PNG", optimize=True)
        rels.append(f"images/{name}")
    return rels


def clean_text(text: str, rels: list[str]) -> str:
    """把 @@FIGK:i@@ 占位符替换为真实图片链接;失败则删除占位符。"""
    def sub(m):
        i = int(m.group(1))
        return rels[i] if i < len(rels) and rels[i] else ""
    return re.sub(r"@@FIGK:(\d+)@@", sub, text)


def combine(engine: str, outdir: Path, state: State, total: int):
    md_files = sorted(outdir.glob("page-*.md"))
    parts = []
    for mf in md_files:
        page_no = int(mf.stem.split("-")[1])
        if not state.is_ok(page_no):
            continue
        body = mf.read_text(encoding="utf-8").strip()
        parts.append(f"<!-- ===== 第 {page_no} 页 ===== -->\n\n{body}")
    combined = "\n\n".join(parts) + "\n"
    outfile = OUTROOT / f"环境工程原理.{engine}.md"
    outfile.write_text(combined, encoding="utf-8")
    ok = sum(1 for p in range(1, total + 1) if state.is_ok(p))
    print(f"[combine] {engine}: {ok}/{total} 页 -> {outfile} ({len(combined)} chars)", flush=True)


def run(args):
    eng = ENGINES[args.engine]
    engdir = OUTROOT / args.engine
    rawdir = engdir / "raw"
    imgdir = OUTROOT / "images"
    pagesdir = OUTROOT / "pages"
    logdir = OUTROOT / "log"
    for d in (rawdir, imgdir, pagesdir, logdir):
        d.mkdir(parents=True, exist_ok=True)
    logfile = logdir / f"{args.engine}{args.state_tag}.log"

    # 服务健康检查
    try:
        r = requests.get(f"http://127.0.0.1:{eng['port']}/health", timeout=5)
        if r.status_code != 200:
            raise SystemExit(f"引擎 {args.engine} health={r.status_code},先运行 bash /mnt/workspace/ocr/start.sh")
    except requests.ConnectionError:
        raise SystemExit(f"引擎 {args.engine} 未运行,先执行 bash /mnt/workspace/ocr/start.sh")

    renderer = Renderer(PDF_PATH)
    state = State(engdir / args.state_file)
    todo = pending_pages(renderer, state, args.pages, args.skip_failed)
    ok_count = sum(1 for p in range(1, renderer.total + 1) if state.is_ok(p))
    log(f"engine={args.engine} 总页={renderer.total} 已完成={ok_count} 本次待跑={len(todo)}", logfile)

    if args.mode == "combine":
        combine(args.engine, engdir, state, renderer.total)
        return
    if not todo:
        if not args.skip_combine:
            combine(args.engine, engdir, state, renderer.total)
        return

    times: list[float] = []
    prefetcher = Prefetcher(PDF_PATH, todo, 1288, pagesdir)
    prefetcher.start()
    idx = 0
    while True:
        item = prefetcher.q.get()
        if item is None:
            break
        page_no, img_b64 = item
        if STOP:
            log(f"中断:已完成本批 {idx}/{len(todo)} 页,断点已保存", logfile)
            break
        idx += 1
        t0 = time.monotonic()
        try:
            raw, usage = call_ocr(eng, img_b64, args.max_tokens, eng["timeout"])
            rawdir.joinpath(f"page-{page_no:04d}.raw.txt").write_text(raw, encoding="utf-8")

            if args.engine == "deepseek":
                figs, text = parse_ds_boxes(raw)
                space = args.coord_space or eng["coord_space"]
            else:
                figs, text = parse_olm_figs(raw)
                space = eng["coord_space"]
            rels = []
            if figs and not args.no_crop:
                # 预取线程只把 img_b64 入队,img 对象不在本线程作用域;
                # 用缓存的 1288px 渲染页取尺寸(与 grounding 坐标空间一致)。
                page_png = pagesdir / f"page-{page_no:04d}.png"
                try:
                    with Image.open(page_png) as _im:
                        iw, ih = _im.size
                except Exception:
                    _r = renderer.render(page_no, 1288)
                    iw, ih = _r.size
                rels = crop_figures(renderer, page_no, figs, iw, ih,
                                    args.crop_scale, space, imgdir, eng["tag"], logfile)
            text = clean_text(text, rels)
            # front matter 剥离(olm)
            m = re.match(r"\A\s*---[ \t]*\n(.*?)\n---[ \t]*\n", text, re.S)
            if m:
                text = text[m.end():]
            engdir.joinpath(f"page-{page_no:04d}.md").write_text(text.strip() + "\n", encoding="utf-8")

            dt = time.monotonic() - t0
            times.append(dt)
            state.record({"page": page_no, "status": "ok", "secs": round(dt, 1),
                          "chars": len(text.strip()), "figs": sum(1 for r in rels if r),
                          "tokens": usage.get("completion_tokens"), "ts": time.time()})
            if idx % 10 == 0 or idx == len(todo):
                avg = sum(times[-20:]) / len(times[-20:])
                remain = (len(todo) - idx) * avg
                log(f"进度 {idx}/{len(todo)}(全局 {ok_count+idx}/{renderer.total}) "
                    f"均速 {avg:.0f}s/页 预计剩余 {remain/3600:.1f}h", logfile)
        except Exception as e:
            dt = time.monotonic() - t0
            state.record({"page": page_no, "status": "failed", "error": str(e)[:300],
                          "secs": round(dt, 1), "ts": time.time()})
            log(f"页 {page_no} 失败({dt:.0f}s): {str(e)[:200]}", logfile)
    if not STOP and idx == len(todo):
        if prefetcher.error:
            log(f"预取线程异常: {prefetcher.error}", logfile)
        log("本批页全部处理完毕", logfile)
        if not args.skip_combine:
            combine(args.engine, engdir, state, renderer.total)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("engine", choices=sorted(ENGINES))
    ap.add_argument("--mode", choices=["run", "combine"], default="run")
    ap.add_argument("--pages", help='页码如 "1-529" 或 "1-3,7"(默认全部)')
    ap.add_argument("--max-tokens", type=int, default=4096)
    ap.add_argument("--retries", type=int, default=2)
    ap.add_argument("--skip-failed", action="store_true", help="跳过历史失败页")
    ap.add_argument("--no-crop", action="store_true")
    ap.add_argument("--crop-scale", type=float, default=2.0)
    ap.add_argument("--coord-space", choices=["auto", "pixel", "norm1000"], default=None,
                    help="默认跟随引擎(deepseek=norm1000, olm=pixel)")
    ap.add_argument("--state-file", default="state.jsonl",
                    help="断点文件名(engdir 下);多机分工时各用各的,如 state-node2.jsonl")
    ap.add_argument("--state-tag", default="",
                    help="日志文件名后缀,配合 --state-file 区分多机日志")
    ap.add_argument("--skip-combine", action="store_true",
                    help="跑完不自动合并(多机分工时由主节点统一合并)")
    args = ap.parse_args()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    for attempt in range(args.retries + 1):
        run(args)
        if args.mode == "combine":
            break
        # run() 正常结束后若仍有待跑页(失败重试)且未中断,再来一轮
        renderer = Renderer(PDF_PATH)
        state = State(OUTROOT / args.engine / args.state_file)
        todo = pending_pages(renderer, state, args.pages, False)
        if not todo or STOP:
            break
        print(f"[pipeline] 第 {attempt+2} 轮重试 {len(todo)} 个失败页", flush=True)


if __name__ == "__main__":
    main()
