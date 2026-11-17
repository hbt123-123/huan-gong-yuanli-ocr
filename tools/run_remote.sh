#!/usr/bin/env bash
# ============================================================================
# 设备2(第二台机器)一键脚本: 环境自举 → 起 olmOCR 服务 → 跑分工页码 → push 回仓库
#
# 前置(设备2, Linux x86_64):
#   - 已 git clone 本仓库(私有), 并进入仓库根目录
#   - 能访问 GitHub(克隆/push) 和 HF 模型源(默认 hf-mirror.com)
#   - 磁盘 ≥ 12GB 空闲(模型 6.8GB + llama.cpp 构建 ~1GB + 渲染缓存)
#   - 内存 ≥ 12GB, CPU ≥ 8 核(线程数按 cgroup 配额自动探测)
#
# 用法(一条命令, 可反复执行=断点续跑):
#   nohup bash tools/run_remote.sh > output/log/remote-node2.log 2>&1 &
#   tail -f output/log/remote-node2.log        # 看进度
#
# 可选环境变量:
#   PAGES=360-529        本机负责的页码(必读, 与主机分工不重叠!)
#   STATE=node2          断点/日志标识 → output/olm/state-node2.jsonl
#   THREADS=8            推理线程数(默认按 cgroup 配额探测)
#   HF=https://hf-mirror.com  模型下载源; 海外设备2可改 https://huggingface.co
#   PIP_INDEX_URL=...    pip 镜像(国内可设 https://pypi.tuna.tsinghua.edu.cn/simple)
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"

PAGES="${PAGES:-360-529}"
STATE="${STATE:-node2}"
HF="${HF:-https://hf-mirror.com}"
PORT="${PORT:-8930}"
TAG="v1.0.0"                       # 与主机同版本 llama.cpp, 保证推理行为一致

mkdir -p output/log external

log() { echo "[$(date '+%m-%d %H:%M:%S')] $*"; }

# ---------- 0. 线程数探测(cgroup 配额优先, 与主机同口径) ----------
detect_threads() {
  if [ -f /sys/fs/cgroup/cpu.max ]; then
    local q; q=$(awk '{print $1}' /sys/fs/cgroup/cpu.max)
    [ "$q" = "max" ] && q=$(( $(nproc) * 100000 ))
    echo $(( q / 100000 ))
  elif [ -f /sys/fs/cgroup/cpu/cpu.cfs_quota_us ]; then
    local q; q=$(cat /sys/fs/cgroup/cpu/cpu.cfs_quota_us)
    [ "$q" -gt 0 ] 2>/dev/null && echo $(( q / 100000 )) || echo "$(nproc)"
  else
    nproc
  fi
}
THREADS="${THREADS:-$(detect_threads)}"
[ "$THREADS" -lt 1 ] 2>/dev/null && THREADS=8
log "分工页码=$PAGES 断点=state-$STATE.jsonl 线程=$THREADS"

# ---------- 1. 基础依赖 ----------
MISSING=()
for c in curl git python3 pip3 cmake g++; do command -v "$c" >/dev/null 2>&1 || MISSING+=("$c"); done
if [ ${#MISSING[@]} -gt 0 ]; then
  log "缺依赖: ${MISSING[*]} → 尝试 apt 安装(需要 root/sudo)"
  if command -v apt-get >/dev/null 2>&1; then
    SUDO=""; [ "$(id -u)" != 0 ] && SUDO="sudo"
    $SUDO apt-get update -y && $SUDO apt-get install -y build-essential cmake curl git python3 python3-venv python3-pip
  else
    log "[error] 请手动安装: ${MISSING[*]}"; exit 1
  fi
fi

# ---------- 2. Python venv + 依赖(版本与主机一致) ----------
if [ ! -x ".venv-$STATE/bin/python" ]; then
  log "创建 venv: .venv-$STATE"
  python3 -m venv ".venv-$STATE"
fi
PY="$ROOT/.venv-$STATE/bin/python"
"$PY" -m pip install -q --upgrade pip
"$PY" -m pip install -q "pypdfium2==5.15.2" "pillow==13.0.0" "requests==2.34.2"
log "Python 依赖就绪: $("$PY" -c 'import pypdfium2, PIL, requests; print("ok")' 2>/dev/null || echo ok)"

# ---------- 3. llama.cpp v1.0.0(与主机同 tag; 有现成二进制则跳过) ----------
LLAMA_BIN="$ROOT/external/llama.cpp/build/bin/llama-server"
if [ ! -x "$LLAMA_BIN" ]; then
  log "构建 llama.cpp $TAG(一次性, 约 5-20 分钟)..."
  if [ ! -d external/llama.cpp ]; then
    git clone --depth 1 --branch "$TAG" https://github.com/ggml-org/llama.cpp.git external/llama.cpp
  fi
  cmake -S external/llama.cpp -B external/llama.cpp/build \
    -DCMAKE_BUILD_TYPE=Release -DLLAMA_CURL=OFF \
    -DLLAMA_BUILD_TESTS=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_BUILD_SERVER=ON
  cmake --build external/llama.cpp/build --config Release -j "$THREADS" --target llama-server
fi
log "llama-server: $LLAMA_BIN"

# ---------- 4. 模型下载(6.8GB, 已存在且大小正确则跳过) ----------
MDIR="$ROOT/external/models/olmocr-2"; mkdir -p "$MDIR"
fetch() { # $1=url $2=dst $3=min_bytes
  local sz; sz="$(stat -c%s "$2" 2>/dev/null || echo 0)"
  if [ "$sz" -ge "$3" ]; then log "已有 $(basename "$2") ($sz)"; return 0; fi
  log "下载 $(basename "$2") ..."; rm -f "$2" "$2.tmp"
  curl -fL --retry 3 -o "$2.tmp" "$1" && mv "$2.tmp" "$2"
  sz="$(stat -c%s "$2")"; [ "$sz" -ge "$3" ] || { log "[error] 模型不完整 $sz < $3"; exit 1; }
}
fetch "$HF/bartowski/allenai_olmOCR-2-7B-1025-GGUF/resolve/main/allenai_olmOCR-2-7B-1025-Q5_K_M.gguf" \
      "$MDIR/olmOCR-2-Q5_K_M.gguf" 5300000000
fetch "$HF/bartowski/allenai_olmOCR-2-7B-1025-GGUF/resolve/main/mmproj-allenai_olmOCR-2-7B-1025-f16.gguf" \
      "$MDIR/olmOCR-2-mmproj-f16.gguf" 1300000000

# ---------- 5. 启动服务(幂等) ----------
if curl -fsS --max-time 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
  log "olm 服务已在 :$PORT 运行"
else
  log "启动 olmOCR-2 服务 :$PORT ..."
  setsid "$LLAMA_BIN" \
    -m "$MDIR/olmOCR-2-Q5_K_M.gguf" \
    --mmproj "$MDIR/olmOCR-2-mmproj-f16.gguf" \
    --alias olmocr-2 --host 127.0.0.1 --port "$PORT" \
    -c 8192 -t "$THREADS" -tb "$THREADS" -np 1 \
    -fa off --no-mmproj-offload --no-warmup --log-colors off \
    > output/log/olm-$STATE-server.log 2>&1 < /dev/null &
  for i in $(seq 1 60); do
    sleep 10
    curl -fsS --max-time 2 "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
    [ "$i" = 60 ] && { log "[error] 服务 10 分钟未就绪, 查 output/log/olm-$STATE-server.log"; exit 1; }
  done
  log "服务就绪"
fi

# ---------- 6. 跑分工页码(断点续跑, 与主机互不重叠) ----------
log "开始 OCR: 页码 $PAGES"
"$PY" tools/pdf_ocr_pipeline.py olm \
  --pages "$PAGES" \
  --state-file "state-$STATE.jsonl" --state-tag "-$STATE" \
  --skip-combine
log "OCR 批结束: $(grep -c '"status": "ok"' "output/olm/state-$STATE.jsonl" 2>/dev/null || echo 0) 页 ok(state-$STATE)"

# ---------- 7. 提交并推送回仓库 ----------
if [ -z "$(git status --porcelain -- output/olm output/images output/log 2>/dev/null)" ]; then
  log "没有新产出需要提交"
else
  git config user.name  >/dev/null 2>&1 || git config user.name  "ocr-$STATE"
  git config user.email >/dev/null 2>&1 || git config user.email "ocr-$STATE@localhost"
  git add output/olm/page-*.md output/olm/raw/ "output/olm/state-$STATE.jsonl" \
          output/images/olm-*.png output/log/olm-"$STATE"* 2>/dev/null || true
  git commit -m "olm $STATE: pages $PAGES @ $(date '+%m-%d %H:%M')" || true
  git pull --rebase origin main 2>/dev/null || true
  if git push origin main 2>/dev/null; then
    log "已推送回仓库"
  else
    log "[warn] push 失败(无凭证/远端有更新)。请手动执行: git pull --rebase origin main && git push origin main"
  fi
fi

log "完成。可重复运行本脚本断点续跑; 两台机都跑完后, 在主机执行:"
log "  git pull origin main && /mnt/workspace/ocr/venv/bin/python tools/reconcile_olm.py --run"
