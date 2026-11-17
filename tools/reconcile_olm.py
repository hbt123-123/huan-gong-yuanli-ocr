#!/usr/bin/env python3
"""多机 olm 结果回合一: 合并各节点断点 → 报告缺口 → (可选)补跑 → 合并最终稿。

用法(在主机/仓库根目录, 先 git pull 拿回设备2的产出):
  /mnt/workspace/ocr/venv/bin/python tools/reconcile_olm.py            # 只合并断点+报告
  /mnt/workspace/ocr/venv/bin/python tools/reconcile_olm.py --run      # 合并后自动补跑缺失页
  /mnt/workspace/ocr/venv/bin/python tools/reconcile_olm.py --run --merge

逻辑:
 1. 读 output/olm/state*.jsonl(含 state.jsonl 与各 state-node*.jsonl),
    把其他节点的 ok 记录并入主断点 state.jsonl(按页去重, 主记录优先)。
 2. 报告全书 1-529 中仍缺的页。
 3. --run: 调 pdf_ocr_pipeline.py olm 补跑缺失页(断点跳过已完成页)。
 4. --merge: 最后跑 merge_final.py 出最终交付稿。
"""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OLM = ROOT / "output" / "olm"
TOTAL = 529
PY = sys.executable


def load_state(path: Path) -> dict[int, dict]:
    recs: dict[int, dict] = {}
    if not path.exists():
        return recs
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rec = json.loads(line)
            recs[rec["page"]] = rec
        except Exception:
            continue
    return recs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="store_true", help="补跑缺失页")
    ap.add_argument("--merge", action="store_true", help="最后生成最终合并稿")
    ap.add_argument("--pages", default="1-529", help="范围(默认全书)")
    args = ap.parse_args()

    main_state = OLM / "state.jsonl"
    main = load_state(main_state)
    ok_main = {p for p, r in main.items() if r.get("status") == "ok"}

    merged_in = 0
    for extra in sorted(OLM.glob("state-*.jsonl")):
        recs = load_state(extra)
        for p, r in recs.items():
            if r.get("status") == "ok" and p not in ok_main:
                with open(main_state, "a", encoding="utf-8") as f:
                    f.write(json.dumps(r, ensure_ascii=False) + "\n")
                ok_main.add(p)
                merged_in += 1
        print(f"[reconcile] {extra.name}: ok {sum(1 for r in recs.values() if r.get('status')=='ok')} 页, "
              f"并入 {merged_in} 页(累计)")

    missing = []
    for part in args.pages.split(","):
        part = part.strip()
        if not part:
            continue
        a, _, b = part.partition("-")
        rng = range(int(a), int(b or a) + 1)
        missing += [p for p in rng if 1 <= p <= TOTAL and p not in ok_main]
    print(f"[reconcile] 全书 ok {len(ok_main)}/{TOTAL}; 缺 {len(missing)} 页")
    if missing:
        head = ", ".join(map(str, missing[:20]))
        print(f"[reconcile] 缺口示例: {head}{' ...' if len(missing) > 20 else ''}")

    if args.run and missing:
        import subprocess
        print(f"[reconcile] 补跑 {len(missing)} 页...", flush=True)
        rc = subprocess.call([PY, str(ROOT / "tools" / "pdf_ocr_pipeline.py"), "olm",
                              "--pages", ",".join(map(str, missing)),
                              "--skip-combine"])
        if rc != 0:
            print("[reconcile] 补跑退出码非 0, 重跑本脚本即可继续(断点续跑)")
            return

    if args.merge:
        import subprocess
        subprocess.call([PY, str(ROOT / "tools" / "merge_final.py")])


if __name__ == "__main__":
    main()
