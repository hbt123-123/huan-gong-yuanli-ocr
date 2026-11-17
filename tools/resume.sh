#!/usr/bin/env bash
# 一键恢复脚本: 恢复服务 → 顺序续跑 deepseek → olm → 合并
# 适应"租用会话切分计算"的场景: 每次开机只跑一条命令,断点自动续。
#
# 用法(下次开机执行):
#   cd /mnt/workspace/环工原理
#   nohup bash tools/resume.sh all > output/log/resume.log 2>&1 &
#   # 然后可断开终端; 用 bash tools/watch_ocr.sh once 看进度
#
# 参数: all(默认)=deepseek→olm 顺序; 也可单独 deepseek 或 olm
set -u
cd "$(dirname "$0")/.." || exit 1
PY=/mnt/workspace/ocr/venv/bin/python
TOTAL=529
mkdir -p output/log

log() { echo "[$(date '+%H:%M:%S')] $*"; }

ok_count() { # $1=engine → 已完成页数
  awk '/"status": "ok"/ { match($0,/"page": [0-9]+/); ok[substr($0,RSTART+8,RLENGTH-8)+0]=1 }
       END { n=0; for (p in ok) n++; print n+0 }' "output/$1/state.jsonl" 2>/dev/null || echo 0
}

# 1. 恢复服务(幂等)
if ! curl -sm 3 http://127.0.0.1:8930/health >/dev/null 2>&1 || \
   ! curl -sm 3 http://127.0.0.1:8931/health >/dev/null 2>&1; then
  log "恢复基础服务: bash /mnt/workspace/deploy.sh"
  bash /mnt/workspace/deploy.sh >/dev/null 2>&1
  log "启动 OCR 服务: bash /mnt/workspace/ocr/start.sh"
  bash /mnt/workspace/ocr/start.sh
  sleep 10
fi
if ! curl -sm 3 http://127.0.0.1:8930/health >/dev/null 2>&1; then log "olm :8930 未起,查 /mnt/workspace/ocr/logs/"; exit 1; fi
if ! curl -sm 3 http://127.0.0.1:8931/health >/dev/null 2>&1; then log "deepseek :8931 未起,查 /mnt/workspace/ocr/logs/"; exit 1; fi
log "两个 OCR 服务健康"

# 2. 顺序续跑(all = deepseek → olm; 同一时刻只跑一个,避免内存/CPU 配额冲突)
MODE=${1:-all}
ENGINES="deepseek olm"
[ "$MODE" = "deepseek" ] && ENGINES="deepseek"
[ "$MODE" = "olm" ] && ENGINES="olm"

for eng in $ENGINES; do
  # 若该引擎已有进程(例如上次断电前的残留在跑——重启后不会,但同会话重入会),等它结束
  while pgrep -f "pdf_ocr_pipeline.py $eng" >/dev/null 2>&1; do
    log "$eng 已有进程在跑,等待其完成(60s 后复查)"
    sleep 60
  done
  n=$(ok_count "$eng")
  if [ "$n" -ge "$TOTAL" ]; then
    log "$eng 已完成 $n/$TOTAL,跳过"
    continue
  fi
  log "续跑 $eng: 已完成 $n/$TOTAL,剩余约 $(( (TOTAL-n)*1 )) 页"
  "$PY" "tools/pdf_ocr_pipeline.py" "$eng" --pages 1-$TOTAL
  n=$(ok_count "$eng")
  log "$eng 批结束: $n/$TOTAL"
done

# 3. 合并已有产物(每引擎一份合并稿)
"$PY" tools/pdf_ocr_pipeline.py deepseek --mode combine || true
"$PY" tools/pdf_ocr_pipeline.py olm --mode combine || true
log "resume.sh 全部结束;合并稿在 output/环境工程原理.deepseek.md 与 .olm.md"

# 4. 最终合并稿(deepseek 骨架 + olm 表格修复 + 图片 Markdown 语法)
"$PY" tools/merge_final.py && log "最终合并稿: output/环境工程原理.md ; 校对清单: output/公式校对清单.md"
