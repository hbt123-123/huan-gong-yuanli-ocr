#!/usr/bin/env python3
"""Pass 3 合并: 以 deepseek 版面为骨架, 用 olm 修复表格, 规范图片语法, 输出公式校对清单。

用法:
  /mnt/workspace/ocr/venv/bin/python tools/merge_final.py
  /mnt/workspace/ocr/venv/bin/python tools/merge_final.py --report-only

规则(均有实测依据, 见 OCR转换计划.md §7):
 1. 骨架 = output/deepseek/page-NNNN.md (仅 state.jsonl 中 status=ok 的页)
    deepseek 有标题层级/行内公式 \(..\)/图裁剪坐标/image_caption, olm 没有。
 2. 裸图片路径行 `images/x.png` -> 标准 Markdown `![](images/x.png)`。
    (管线原来只写裸路径, 任何 Markdown 渲染器都不会显示成图片)
 3. deepseek 的 <table> 全部丢失行结构(82/82 页无 <tr>, 单元格文字挤成一行),
    olm 的 <table> 结构完整(实测 0 破损) -> 同页按顺序/相似度配对后整块替换。
 4. deepseek 偶发输出空行间公式 `\[\]`(全书仅 2 处, 均在第 64 页),
    用 olm 对照内容手工补丁 PATCHES。
 5. QC 报告: 公式块数量差异、公式文本相似度过低的页 -> output/公式校对清单.md
"""
import argparse
import difflib
import json
import re
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "output"
ENGDIRS = {"deepseek": OUT / "deepseek", "olm": OUT / "olm"}

IMG_REF = re.compile(r"(?<!\()images/([\w.\-]+\.png)(?!\))")
TABLE_BLOCK = re.compile(r"<table>.*?</table>", re.S)
TAG = re.compile(r"<[^>]+>")
DISP_BLOCK = re.compile(r"\\\[.*?\\\]", re.S)
REPEAT = re.compile(r"(.{6,40}?)\1{4,}")  # 模型复读退化(实测 p175/382/410)

# 第 64 页 ds 输出两个空 \[\], 公式内容由 olm 对照页(page-0064.md)恢复
PATCHES = {
    64: [
        r"\[\text{总能量输入率} = \frac{\text{电能输出率}}{\text{能量效率}} = \frac{1000}{1/3}\,\text{MW} = 3000\,\text{MW}\]",
        r"\[\text{废气携带的热量} = 0.15 \times 2000\,\text{MW} = 300\,\text{MW}\]",
    ],
}


def ok_pages(engdir: Path) -> set[int]:
    st = engdir / "state.jsonl"
    if not st.exists():
        return set()
    pages = set()
    for line in st.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("status") == "ok":
            pages.add(rec["page"])
    return pages


def read_page(engdir: Path, page: int) -> str:
    f = engdir / f"page-{page:04d}.md"
    return f.read_text(encoding="utf-8") if f.exists() else ""


def plain(block: str) -> str:
    return re.sub(r"\s+", "", TAG.sub("", block))


def pair_tables(ds_tables: list[str], olm_tables: list[str]) -> list[int | None]:
    """给每个 ds 表格配对 olm 表格下标; 先按顺序, 再用去标签文本相似度贪心配对。"""
    if len(ds_tables) == len(olm_tables):
        return list(range(len(olm_tables)))
    used: set[int] = set()
    pairs: list[int | None] = []
    for dt in ds_tables:
        best, best_r = None, 0.0
        dp = plain(dt)
        for j, ot in enumerate(olm_tables):
            if j in used:
                continue
            r = difflib.SequenceMatcher(None, dp, plain(ot)).ratio()
            if r > best_r:
                best, best_r = j, r
        if best is not None and best_r >= 0.35:
            used.add(best)
            pairs.append(best)
        else:
            pairs.append(None)
    return pairs


def repair_tables(body: str, olm_body: str, stats: dict) -> str:
    ds_tables = TABLE_BLOCK.findall(body)
    if not ds_tables:
        return body
    olm_tables = TABLE_BLOCK.findall(olm_body)
    pairs = pair_tables(ds_tables, olm_tables) if olm_tables else [None] * len(ds_tables)
    for dt, oi in zip(ds_tables, pairs):
        if "<tr" in dt.lower():
            stats["already_ok"] += 1
            continue
        if oi is None:
            stats["unrepaired"] += 1
            continue
        body = body.replace(dt, olm_tables[oi], 1)
        stats["repaired"] += 1
    return body


def normalize_images(body: str) -> str:
    return IMG_REF.sub(lambda m: f"![](images/{m.group(1)})", body)


def apply_patch(page: int, body: str) -> str:
    for formula in PATCHES.get(page, []):
        body = body.replace("\\[\\]", formula, 1)
    return body


def balanced(s: str) -> bool:
    return s.count("{") == s.count("}")


def fix_degenerate(body: str) -> tuple[str, int]:
    """deepseek 偶发复读退化(同一片段重复数百次)。截断到首次重复; 截断后 LaTeX 不完整则删块并留注释。"""
    out, fixed = [], 0
    for line in body.splitlines():
        if len(line) > 500:
            m = REPEAT.search(line)
            if m:
                cut = m.start()
                prefix = line[:cut].rstrip(" ,;，；")
                if prefix.lstrip().startswith("\\["):
                    # 截断后过短 = 该行本来就是复读产物(如 p382), 整块删除
                    if len(prefix) < 60:
                        out.append("<!-- OCR 公式重复故障, 已删除; 内容待 olm 对照页补回 -->")
                    elif balanced(prefix):
                        out.append(prefix + " \\]  ")
                    else:
                        out.append("<!-- OCR 公式重复故障, 已删除; 内容待 olm 对照页补回 -->")
                else:
                    out.append(prefix + "  ")
                fixed += 1
                continue
        out.append(line)
    return "\n".join(out), fixed


def disp_blocks(body: str) -> list[str]:
    return DISP_BLOCK.findall(body)


def page_math_score(ds_body: str, olm_body: str) -> tuple[float | None, int, int]:
    db, ob = disp_blocks(ds_body), disp_blocks(olm_body)
    if not db or not ob:
        return None, len(db), len(ob)
    ratios = []
    for d in db:
        dp = re.sub(r"\s+", "", d)
        best = max(
            difflib.SequenceMatcher(None, dp, re.sub(r"\s+", "", o)).ratio()
            for o in ob
        )
        ratios.append(best)
    return sum(ratios) / len(ratios), len(db), len(ob)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--report-only", action="store_true", help="只重出校对清单, 不重写合并稿")
    args = ap.parse_args()

    ds_ok = ok_pages(ENGDIRS["deepseek"])
    olm_ok = ok_pages(ENGDIRS["olm"])
    stats = {"repaired": 0, "unrepaired": 0, "already_ok": 0, "pages": 0}
    qc: list[tuple[int, str, str]] = []
    repaired_pages: list[int] = []

    parts: list[str] = []
    for page in range(1, 530):
        if page not in ds_ok:
            continue
        body = read_page(ENGDIRS["deepseek"], page).strip()
        if not body:
            continue
        olm_body = read_page(ENGDIRS["olm"], page) if page in olm_ok else ""

        before = body
        body = repair_tables(body, olm_body, stats)
        if body != before:
            repaired_pages.append(page)
        body = normalize_images(body)
        body = apply_patch(page, body)
        body, ndegen = fix_degenerate(body)
        if ndegen:
            qc.append((page, "公式复读故障", f"{ndegen} 处已截断/删除, 需对照 olm"))

        if olm_body:
            score, nd, no = page_math_score(body, olm_body)
            if abs(nd - no) >= 2:
                qc.append((page, "公式块数量差异", f"deepseek {nd} 块 / olm {no} 块"))
            elif score is not None and score < 0.60:
                qc.append((page, "公式文本差异大", f"平均相似度 {score:.2f} ({nd} 块)"))
            if "<table" in body and "<tr" not in body.lower():
                qc.append((page, "表格未修复", "olm 无对应表格"))
        elif not olm_body:
            qc.append((page, "缺少 olm 对照", "该页 olm 尚未完成"))

        stats["pages"] += 1
        parts.append(f"<!-- ===== 第 {page} 页 ===== -->\n\n{body}")

    if not args.report_only:
        header = (
            "<!--\n"
            f"《环境工程原理》(第四版) OCR 合并稿\n"
            f"骨架: DeepSeek-OCR(529 页) | 表格修复: olmOCR-2({len(olm_ok)} 页) | "
            f"生成: {time.strftime('%Y-%m-%d %H:%M')}\n"
            "源 PDF 页码以 <!-- ===== 第 N 页 ===== --> 标注\n"
            "-->\n\n"
        )
        final = header + "\n\n".join(parts) + "\n"
        (OUT / "环境工程原理.md").write_text(final, encoding="utf-8")
        print(f"[merge] {stats['pages']} 页 -> output/环境工程原理.md ({len(final)} chars)")

    # 校对清单
    no_olm = [p for p, k, _ in qc if k == "缺少 olm 对照"]
    qc = [r for r in qc if r[1] != "缺少 olm 对照"]
    qc.sort(key=lambda x: (x[1] != "公式复读故障", x[1] != "表格未修复",
                           x[1] != "公式块数量差异", x[0]))
    lines = [
        "# 公式/结构校对清单",
        "",
        f"生成: {time.strftime('%Y-%m-%d %H:%M')} | 覆盖: deepseek {len(ds_ok)} 页, "
        f"olm {len(olm_ok)} 页",
        "",
        f"- 表格修复: {stats['repaired']} 块; 未修复: {stats['unrepaired']} 块; "
        f"原本有行结构: {stats['already_ok']} 块",
        f"- 表格修复涉及页: {len(repaired_pages)} 页",
        f"- 待人工抽查: {len(qc)} 页(olm 完成前另有 {len(no_olm)} 页无对照, 未列入)",
        "",
        "抽查方法: 打开 `output/环境工程原理.md` 定位 `第 N 页`, "
        "与 `output/olm/page-NNNN.md` 同页对照公式/表格。",
        "",
        "| 页 | 问题 | 说明 |",
        "|---|---|---|",
    ]
    for page, kind, note in qc[:200]:
        lines.append(f"| {page} | {kind} | {note} |")
    (OUT / "公式校对清单.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"[merge] 校对清单 {len(qc)} 页 -> output/公式校对清单.md")
    print(f"[merge] 表格: 修复 {stats['repaired']} / 未修复 {stats['unrepaired']}")


if __name__ == "__main__":
    main()
