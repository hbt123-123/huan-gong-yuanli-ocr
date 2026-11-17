#!/usr/bin/env bash
# OCR 批处理实时监控: 进度条 + 检测过程 + 失败页 + 引擎状态
# 用法:
#   bash tools/watch_ocr.sh          # 实时刷新(2s 一次, Ctrl+C 退出)
#   bash tools/watch_ocr.sh 5        # 5 秒刷新一次
#   bash tools/watch_ocr.sh once     # 只看一眼(不循环)
cd "$(dirname "$0")/.." || exit 1
TOTAL=529
REFRESH="${1:-2}"
ONCE=0
[ "$1" = "once" ] && { REFRESH=0; ONCE=1; }

G=$'\033[32m'; C=$'\033[36m'; Y=$'\033[33m'; R=$'\033[31m'; D=$'\033[2m'; N=$'\033[0m'

bar() { # $1=done $2=total $3=宽
  local w=${3:-40} fill empty
  fill=$(( $1 * w / ($2>0?$2:1) )); empty=$(( w - fill ))
  printf '%s%s%s%s' "$G" "$(printf '█%.0s' $(seq 1 $fill 2>/dev/null))" "$D" "$(printf '░%.0s' $(seq 1 $empty 2>/dev/null))"
}

engine_stat() { # $1=engine  →  打印一行状态
  local eng=$1
  local state="output/$eng/state.jsonl"
  local nok=0 nfail=0 last=0 avg=0 pid cpu
  if [ -f "$state" ]; then
    read -r nok nfail last avg <<< "$(awk '
      /"status": "ok"/ {
        match($0, /"page": [0-9]+/); p=substr($0, RSTART+8, RLENGTH-8)+0
        if (!(p in ok)) nok++
        ok[p]=1; delete fail[p]
        if (p>last) last=p
        match($0, /"secs": [0-9.]+/); sec[++k]=substr($0, RSTART+8, RLENGTH-8)+0
      }
      /"status": "failed"/ {
        match($0, /"page": [0-9]+/); p=substr($0, RSTART+8, RLENGTH-8)+0
        if (p in ok) { delete ok[p]; nok-- }
        if (!(p in fail)) nfail++
        fail[p]=1
      }
      END {
        m=(k<20?k:20); s=0
        for(i=k-m+1;i<=k;i++) s+=sec[i]
        printf "%d %d %d %.1f", nok+0, nfail+0, last+0, (m?s/m:0)
      }' "$state")"
  fi
  # 进程在吗
  pid=$(pgrep -f "pdf_ocr_pipeline.py $eng" | head -1)
  if [ -n "$pid" ]; then
    live="${G}● 运行中(pid $pid)$N"
  elif [ "$nok" -ge "$TOTAL" ]; then
    live="${G}✓ 已完成$N"
  else
    live="${Y}○ 已停止(断点在 output/$eng/state.jsonl)$N"
  fi
  # OCR 推理服务 CPU
  local spid
  spid=$(pgrep -f "alias $eng-ocr" | head -1)
  local activity="${D}服务未运行$N"
  if [ -n "$spid" ]; then
    cpu=$(top -bn1 -p "$spid" 2>/dev/null | awk '/llama|python/ {print $9; exit}')
    if [ -n "$cpu" ] && [ "${cpu%%.*}" -ge 400 ] 2>/dev/null; then
      activity="CPU ${cpu}% (${G}推理中$N)"
    else
      activity="CPU ${cpu:-0}% (${D}空泡/渲染期$N)"
    fi
  fi
  # ETA(不用 bc,纯 awk)
  local eta="-" remain=$(( TOTAL - nok ))
  if [ "$nok" -lt "$TOTAL" ]; then
    eta=$(awk -v r=$remain -v a=$avg 'BEGIN{printf "%.1fh", (a>0)?r*a/3600:0}' )
    [ "$eta" = "0.0h" ] && eta="-"
  else
    eta="0h"
  fi
  printf '\n%s[%s] %s\n' "$C" "$eng" "$live"
  local pct=$(( nok * 100 / TOTAL ))
  local info
  info=$(printf '%s%d/%d%s (%d%%)  %s失败 %d%s  %s%.0fs/页%s' \
    "$G" "$nok" "$TOTAL" "$N" "$pct" "$Y" "$nfail" "$N" "$D" "$avg" "$N")
  printf '  '; bar "$nok" "$TOTAL" 40; printf ' %s  剩余 %s  %s\n' "$info" "$eta" "$activity"
  [ "$nfail" -gt 0 ] && printf '  %s失败页: %s%s\n' "$R" "$(awk '/"status": "failed"/ {match($0,/"page": [0-9]+/); printf "%s ", substr($0,RSTART+8,RLENGTH-8)}' "$state" | head -c 200)" "$N"
  # 最新完成页
  [ -f "$state" ] && printf '  最新: %s\n' "$(tail -1 "$state" | sed 's/"ts": *[0-9.]*[ ,}]*/ /; s/[{}]//g')"
}

images_count=$(ls output/images/*.png 2>/dev/null | wc -l)

printf '%s《环境工程原理》OCR 转换监控%s  %s(共 %d 页)%s\n' "$C" "$N" "$D" "$TOTAL" "$N"
engine_stat deepseek
engine_stat olm
printf '\n%s图片: %s%d 张%s  %s\n' "$C" "$G" "$images_count" "$N" "$(du -sh output 2>/dev/null | awk '{print "output 占用 "$1}')"
printf '\n%s── 检测过程(最近日志)%s\n' "$C" "$N"
tail -n 6 output/log/deepseek.log 2>/dev/null | sed 's/^/  ds  /'
tail -n 3 output/log/olm.log 2>/dev/null | sed 's/^/  olm /'

if [ "$ONCE" = "1" ]; then exit 0; fi
while :; do
  sleep "$REFRESH"
  images_count=$(ls output/images/*.png 2>/dev/null | wc -l)
  clear
  printf '%s《环境工程原理》OCR 转换监控%s  %s刷新于 %s%s\n' "$C" "$N" "$D" "$(date '+%H:%M:%S')" "$N"
  engine_stat deepseek
  engine_stat olm
  printf '\n%s图片: %s%d 张%s  %s\n' "$C" "$G" "$images_count" "$N" "$(du -sh output 2>/dev/null | awk '{print "output 占用 "$1}')"
  printf '\n%s── 检测过程(最近日志)%s\n' "$C" "$N"
  tail -n 6 output/log/deepseek.log 2>/dev/null | sed 's/^/  ds  /'
  tail -n 3 output/log/olm.log 2>/dev/null | sed 's/^/  olm /'
  printf '\n%s(Ctrl+C 退出)%s' "$D" "$N"
done
