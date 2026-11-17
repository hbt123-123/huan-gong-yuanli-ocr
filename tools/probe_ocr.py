#!/usr/bin/env python3
"""Probe: time one page on each OCR engine, keep RAW output to inspect formats."""
import base64, io, json, sys, time
from pathlib import Path

import requests
from PIL import Image
import pypdfium2 as pdfium

PDF = Path("/mnt/workspace/环工原理/环境工程原理.pdf")
OUT = Path("/mnt/workspace/环工原理/output/probe")
OUT.mkdir(parents=True, exist_ok=True)

OLM_PROMPT = (
    "Attached is one page of a document that you must process. "
    "Just return the plain text representation of this document as if you were reading it naturally. "
    "Convert equations to LateX and tables to HTML.\n"
    "If there are any figures or charts, label them with the following markdown syntax "
    "![Alt text describing the contents of the figure](page_startx_starty_width_height.png)\n"
    "Return your output as markdown, with a front matter section on top specifying values for the "
    "primary_language, is_rotation_valid, rotation_correction, is_table, and is_diagram parameters."
)
DS_PROMPT = "<|grounding|>Convert the document to markdown."

def render(page_no: int, target: int = 1288) -> Image.Image:
    pdf = pdfium.PdfDocument(str(PDF))
    page = pdf[page_no - 1]
    w, h = page.get_size()
    scale = target / max(w, h)
    bmp = page.render(scale=scale)
    img = bmp.to_pil()
    if img.mode in ("RGBA", "LA"):
        bg = Image.new("RGB", img.size, "white")
        bg.paste(img, mask=img.split()[-1])
        img = bg
    return img.convert("RGB")

def png_b64(img):
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode()

def call(port, model, prompt, img, max_tokens=4096, rp=1.0, timeout=2400):
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + png_b64(img)}},
            {"type": "text", "text": prompt},
        ]}],
        "temperature": 0, "top_k": 1, "repeat_penalty": rp,
        "max_tokens": max_tokens, "stream": False,
    }
    t0 = time.monotonic()
    r = requests.post(f"http://127.0.0.1:{port}/v1/chat/completions", json=payload, timeout=(10, timeout))
    dt = time.monotonic() - t0
    r.raise_for_status()
    data = r.json()
    text = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})
    return text, dt, usage

if __name__ == "__main__":
    page_no = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    which = sys.argv[2] if len(sys.argv) > 2 else "both"
    img = render(page_no)
    img.save(OUT / f"page-{page_no:04d}.png")
    print(f"rendered page {page_no}: {img.size}", flush=True)

    if which in ("both", "deepseek"):
        raw, dt, usage = call(8931, "deepseek-ocr", DS_PROMPT, img, rp=1.05)
        (OUT / f"page-{page_no:04d}.ds.raw.txt").write_text(raw, encoding="utf-8")
        print(f"deepseek: {dt:.1f}s  completion_tokens={usage.get('completion_tokens')}  chars={len(raw)}", flush=True)

    if which in ("both", "olm"):
        raw, dt, usage = call(8930, "olmocr-2", OLM_PROMPT, img, rp=1.0, timeout=3600)
        (OUT / f"page-{page_no:04d}.olm.raw.txt").write_text(raw, encoding="utf-8")
        print(f"olm: {dt:.1f}s  completion_tokens={usage.get('completion_tokens')}  chars={len(raw)}", flush=True)
