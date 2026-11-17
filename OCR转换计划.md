# 《环境工程原理》PDF → Markdown 转换计划

> 本文档是任务的完整恢复索引:设备关机/对话丢失后,按本文档可继续。
> 深度工作流状态另见 `.slim/deepwork/pdf2md-ocr/progress.md`(两者同步维护)。

## ⚡ 断电/关机恢复卡(最重要,照抄即可)

一切产出都在 NAS 持久卷 `/mnt/workspace`(1PB),关机不丢;根盘才清空。
断电时管线收到 SIGTERM 会完成当前页落盘退出;即使硬断电最多丢正在处理的 1 页。

**下次开机只需一条命令**:
```bash
cd /mnt/workspace/环工原理 && setsid nohup bash tools/resume.sh all >> output/log/resume.log 2>&1 < /dev/null &
```
它自动:恢复服务(deploy.sh + start.sh)→ 续跑 deepseek(断点跳过已完成页)→ 接 olm → 合并 → 最终合并稿。
**必须带 `setsid`**:2026-11-17 实测只用 `nohup ... &` 启动的进程会在发起它的会话结束后被整组杀掉(无 OOM、无日志),
`setsid` 独立 session 才不会(已在 17:50 复跑验证)。日志用 `>>` 追加,别覆盖历史。
看进度:`bash tools/watch_ocr.sh once`(或实时模式)。

## 0. 租用会话与计算预算(2026-11-16 现实约束)

单次租用约 1-2h,olm 全书需 ~24h 累计 → 跨会话断点续跑是既定方案,每次开机跑 resume.sh 即可。
olm 逐会话累计约需 20+ 次开机;若嫌多,备选方案:只对公式/表格密集页跑 olm(抽样 ~120 页 ≈ 5.4h),
deepseek 全书已保证版面与图片完整性。该取舍待全书 deepseek 完成后由用户定夺,resume.sh 默认仍全书 olm。

## 1. 任务

把 `环境工程原理.pdf`(529 页纯扫描件,101MB,无文本层)用两个本地 OCR 引擎转成 Markdown:
- **DeepSeek-OCR**(:8931)— 主源:版面结构 + 图表坐标裁剪
- **olmOCR-2**(:8930)— 校对源:公式转 LaTeX、表格转 HTML
- 图片图表无法识别重绘的 → 按坐标裁剪成 PNG,插入 markdown 原位
- 全程断点续跑(逐页落盘,重跑自动跳过已完成页)

## 2. 设备能力评估(实测,勿凭感觉)

| 项 | 实测值 | 结论 |
|---|---|---|
| CPU 配额 | cgroup 800000/100000 = **8 核**(宿主 64 核,监控里显示 12% 是口径差) | 真实算力 = 8 核 |
| 配额可改性 | `/sys/fs/cgroup/cpu/cpu.cfs_quota_us` 只读 | 不能调大 |
| 内存 | 28GB;27B Ollama 占 19GB 已卸载;OCR 单请求峰值 ~15GB | 可满足单 OCR |
| llama-server 实际占用 | 446-766% CPU(≈7.6 核满载) | 已打满,并行无收益 |
| deepseek 速度 | 12-32s/页(首次冷启 31.7s) | 529 页 ≈ 2.5-3.5h |
| olm 速度 | 162s/页 | 529 页 ≈ 24h |

**并行结论**:不要同时跑双 OCR(内存 15+15>28 会 OOM,CPU 抢同一 8 核配额互拖)。
真实优化 = 管线内渲染/裁剪与 OCR 重叠(预取线程,olm 批前实施,预期省 5-15%)。

## 3. 关键技术事实(踩坑记录)

1. deepseek grounding 坐标 = **0-1000 双轴归一化**(不是像素)。渲染页 852×1288px。
2. deepseek 每个段落都有 `text[[..]]` 坐标行 → 按 label 过滤,只裁 `image/table/chart` 类;
   `image_caption` 含 "image" 子串,必须排除,否则误裁。
3. HTTP 请求**图片在前、文本在后**(deepseek 顺序敏感)。
4. olm 在图页经常不出图引用(只抄图内文字)→ 图片裁剪以 deepseek 为主源。
5. 裁剪用 2× 渲染(2576px)+ 框外扩 4%,防切边;目检 图1.7/1.8 通过。
6. OCR 服务用 `pypdfium2` 渲染;venv = `/mnt/workspace/ocr/venv/bin/python`(系统 python 没有 PDF 库)。

## 4. 目录结构

```
/mnt/workspace/环工原理/
├── OCR转换计划.md              ← 本文档
├── 环境工程原理.pdf             ← 源文件
├── tools/
│   ├── pdf_ocr_pipeline.py     ← 主管线(断点+双引擎+图裁剪)
│   ├── merge_final.py          ← Pass 3 合并(deepseek 骨架 + olm 修表格 + 图片语法 + 校对清单)
│   ├── watch_ocr.sh            ← 实时监控: bash tools/watch_ocr.sh (进度条/失败页/引擎CPU)
│   ├── ocr_progress.py         ← 进度监控(一行式)
│   └── probe_ocr.py            ← 探针(计时+格式验证)
├── output/
│   ├── pages/page-NNNN.png     ← 1288px 渲染缓存(双引擎共用)
│   ├── deepseek/               ← page-NNNN.md + raw/ + state.jsonl(断点)
│   ├── olm/                    ← 同上
│   ├── images/                 ← 裁剪图 ds-pNNNN-fK.png / olm-pNNNN-fK.png
│   ├── log/                    ← 每引擎运行日志
│   ├── 环境工程原理.deepseek.md ← deepseek 合并稿(每批完成自动重写, 裸图片路径)
│   ├── 环境工程原理.md          ← ★最终交付稿(merge_final.py: 骨架+表格修复+![](images/..))
│   └── 公式校对清单.md          ← Pass 3 QC: 公式块数量/文本差异页, 供人工抽查
└── .slim/deepwork/             ← deepwork 恢复索引(不入库)
```

## 5. 断点与恢复(关机后照抄)

```bash
# 一键(推荐): 见顶部"断电/关机恢复卡"
cd /mnt/workspace/环工原理 && nohup bash tools/resume.sh all > output/log/resume.log 2>&1 &

# 分步(需要细控时):
bash /mnt/workspace/deploy.sh            # 1. 恢复服务
bash /mnt/workspace/ocr/status.sh        #    确认 :8930/:8931 health=200
/mnt/workspace/ocr/venv/bin/python tools/ocr_progress.py    # 2. 查进度
cd /mnt/workspace/环工原理                                                  # 3. 续跑(自动跳过已完成页)
/mnt/workspace/ocr/venv/bin/python tools/pdf_ocr_pipeline.py deepseek --pages 1-529
/mnt/workspace/ocr/venv/bin/python tools/pdf_ocr_pipeline.py olm      --pages 1-529
# 优雅停机: kill -TERM <PID>(完成当前页后落盘退出)

# 只重新合并不跑 OCR
/mnt/workspace/ocr/venv/bin/python tools/pdf_ocr_pipeline.py deepseek --mode combine
```

## 5.5 多机分工(GitHub 双机并行,2026-11-17 新增)

- **分工(页码不重叠 = 零 git 冲突)**:主机 olm `190-359`(170页,state.jsonl),设备2 olm `360-529`(170页,state-node2.jsonl)。
- **设备2 一条命令**(先 `git clone` 私有仓库并进入根目录):
  `nohup bash tools/run_remote.sh > output/log/remote-node2.log 2>&1 &`
  自动:装依赖 → 编译 llama.cpp v1.0.0(与主机同版本)→ 从 hf-mirror 下 olmOCR-2 模型 6.8GB → 起 :8930 → 跑分工页 → `git push` 回仓库。可反复执行=断点续跑。
- **回合**(两机都跑完后,主机执行):
  `git pull origin main` → `/mnt/workspace/ocr/venv/bin/python tools/reconcile_olm.py --run --merge`
  (把 state-node2.jsonl 并入主断点 → 自动补跑缺口/失败页 → 出最终稿+校对清单)
- **红线**:仓库必须 **Private**(教材版权);PDF 97MiB < GitHub 单文件 100MiB 上限,可入仓库;模型不走 git(设备2从 HF 直下);渲染缓存 `output/pages/`(241MB)不入仓库。
- **速度账**:单机跑剩余 340 页 ≈ 26h(实测后半本 ~280s/页);双机各 170 页 ≈ 13h + 设备2自举 ~0.5-1h。

## 6. Pass 计划

- **Pass 1(进行中)**:deepseek 全书 529 页 → 初稿 markdown + 图裁剪。完成后立即启动 Pass 2。
- **Pass 2**:olm 全书 529 页。✅ 管线已加预取线程(渲染/PNG 编码与 OCR 重叠,独立 PdfDocument 句柄,消除空泡),olm 批直接受益。
- **Pass 3**:合并——以 deepseek 版面为骨架,olm 的 LaTeX 公式/HTML 表格择优;
  图统一用 deepseek 坐标裁剪。**已实现 `tools/merge_final.py`**(2026-11-17):
  - 骨架 deepseek 页;裸 `images/x.png` 行 → `![](images/x.png)`(原管线缺陷:Markdown 不渲染裸路径)。
  - **deepseek 表格 82/82 页全部丢失行结构**(`<table>` 内无 `<tr>`,单元格文字挤成一行);
    olm 表格 0 破损 → 按同页顺序/去标签相似度配对,整块用 olm `<table>` 替换。
  - 空行间公式 `\[\]` 全书 2 处(均 p64),已用 olm 对照内容人工补丁进 `merge_final.py` PATCHES。
  - 公式质量两引擎互有胜负,未做自动互换(证据:p99 deepseek `E_{动}` 正确 / olm 误作 `E_{ab}`;
    p100 olm `E_{sh}`、`H_f`、单位 `m/s` 正确 / deepseek 误作 `E_eff`、`H_t`、`m³/s`)。
    改为输出 `output/公式校对清单.md` 标注公式块数量差异/相似度低的页,人工抽查。
  - olm 全部完成后重跑: `/mnt/workspace/ocr/venv/bin/python tools/merge_final.py`
    (resume.sh 已在 olm 后自动调用)。
- **Gate**:oracle 评审合并质量;抽样人工检查图截取位置与 `公式校对清单.md` 中标出的页。
  2026-11-17 抽检 2 张:p175 图4.5.1(辐射吸收/反射/透过示意图,标注完整)、p109 表格条带(表头微裁入,可接受)。

## 7. 当前状态(每次 pass 转换时更新)

- 2026-11-17 20:33 **双机分工启动**:
  - 主机管线重启为 `--pages 190-359`(setsid, state.jsonl, --skip-combine);实测 ~285s/页(后半本更密),170 页 ≈ 13.5h。
  - **私有仓库已建并推送**: `github.com/hbt123-123/huan-gong-yuanli-ocr`(private, commit 含 PDF+全量产出+脚本)。
  - 设备2:clone → `nohup bash tools/run_remote.sh > output/log/remote-node2.log 2>&1 &`(默认 PAGES=360-529)。
  - 后期:两机完成后主机 `git pull` + `tools/reconcile_olm.py --run --merge` 回合。
  - 安全:推送用的 PAT 是用户在对话里提供的,全部完成后应到 GitHub → Settings → Developer settings → Tokens **撤销**。
- 2026-11-17 17:55 **Pass 2 续跑中:olm 155/529**(17:50 起用 `setsid` 独立会话,已很稳)。
  - 断点续跑正常:重启后从 154 页继续,单页 144-319s,近期均速 ~190s/页,剩余 ~374 页 ≈ 19.5h。
  - 17:40 首次续跑用了 `nohup ... &`,进程在 5 分钟后随发起会话被整组杀掉(无日志/无 OOM);17:50 改用 `setsid` 复跑,存活验证通过。**恢复卡已改**。
  - deepseek 529/529 保持完整;olm 至今 0 failed。
- 2026-11-17 17:55 **Pass 3 合并工具落地** `tools/merge_final.py`,已生成最终交付稿
  `output/环境工程原理.md`(529 页,816,060 chars,423 张图全部为标准 `![](images/..)` 语法)。
  - 实测缺陷:deepseek 82/82 表格页无 `<tr>`(olm 0 破损);已修 30 块(1-155 页范围),剩 89 块待 olm 完成。
  - `output/公式校对清单.md`:当前 14 页待人工抽查(olm 完成前另有 374 页无对照)。
  - **新发现 deepseek 复读退化**(同片段重复数百次):p175(900×`\mathbb{H}`)、p382(259×`space~volume`)、p410(568×`\mathrm{~t~}`);
    merge_final.py 自动截断/删块: p175 已还原正确公式,p382/p410 留 `<!-- OCR 公式重复故障 -->` 注释待 olm 补。
  - 终稿结构校验:529 页标记、423 图全存在、无未闭合 `\[`、无占位符/U+FFFD、最长行 1610(正常文本)。
  - 已把合并步骤追加到 resume.sh 末尾,以后续跑结束自动出最终稿。
- 2026-11-16 19:59 **Pass 1 完成**:deepseek 529/529 页全部 ok,合并稿 `output/环境工程原理.deepseek.md`(818,949 chars)。
  - 裁剪图 423 张(`output/images/ds-p*.png`),md 中 423 处图片引用,数量一致,无异常小图。
  - 修复 bug:预取线程重构后 `run()` 里残留 `img.width/img.height` 未定义 → 改为从缓存 1288px 渲染页取尺寸。
    **此 bug 会影响 olm 批每个含图页,已在 olm 启动前修复。**
  - 第 529 页(原批次 1-528 漏掉)已补跑成功,含 1 张裁剪图。
- 历史:2026-11-16 15:06 deepseek 批启动(PID 241340),均速 ~32s/页。
- 已完成:探针、坐标空间确认、管线单测、页 30 目检、`OCR转换计划.md`、预取线程、passed bug 修复。
- 2026-11-16 20:17 **Pass 2 启动**:用户选定「全书 olm 529 页」。已通过 `nohup bash tools/resume.sh all > output/log/resume.log 2>&1 &` 启动(deepseek 段自动跳过)。
  - olm 进程 PID 31994,resume PID 30946;日志 `output/log/olm.log`;断点 `output/olm/state.jsonl`。
  - 实测 ~100s/页(优于探针 162s),全书预计 ~15h 累计;跨会话用恢复卡一条命令续跑。
  - 注意:resume.sh 的 `pgrep -f "pdf_ocr_pipeline.py olm"` 会被包含该字串的命令行误触发等待(60s 后自恢复),勿在检查命令里写该完整字串,用 `[o]lm` 规避。

## 8. 决策记录

| 决策 | 理由 |
|---|---|
| 卸载 27B Ollama | 释放 19GB,OCR 需要 ~15GB 峰值;deploy.sh 可随时恢复 |
| deepseek 先跑 | 快(3h 出全书初稿),其坐标是图裁剪唯一可靠源 |
| olm 后跑、同管线 | 24h 长尾,断点续跑是硬需求;两者不并行(内存+CPU 配额) |
| 裁剪图放 output/images,md 里相对引用 | 最终 md 在 output/ 根,路径自洽 |
| 不开双 OCR 并行 | CPU 配额 8 核已满载,并行无收益还会 OOM |

## 9. Known issues

- ocr_progress.py 的"图共 N"统计口径 = 全部坐标标签(含 text),非实际裁剪数;olm 批前改为实际裁剪数。
- **olm 图引用格式(已在真实数据验证)**:管线写成裸路径 `images/olm-pNNNN-fK.png` 独立一行,不是 Markdown 图片语法;
  且 olm 绝大多数图页不出图引用(155 页仅 12 处),图内文字被当正文抄出 → 图片一律以 deepseek 为准。
- **olm 无标题层级**(不输出 `#`),正文行内公式多为纯文本 `^`/`_` → 不适合当骨架,只作公式/表格校对源。
- **deepseek 表格结构全损**(82/82 页 `<table>` 无 `<tr>`),必须用 olm 表格替换(merge_final.py 已处理)。
- **deepseek 偶发空行间公式** `\[\]`(全书 2 处,p64),已人工补丁;若后续发现新空块,merge_final.py 会留空并在校对清单提示。
- **deepseek 复读退化**(全书 3 页:p175/382/410,同片段重复 259-900 次,单行 6-9KB):merge_final.py 按重复起点截断;
  截断后 <60 字符或花括号不平衡的整块删除并留注释。p382/p410 的公式需 olm 完成后补回。
- deepseek/olm 公式符号各有误读(如 p99 `E_动` vs `E_ab`、p100 `E_sh` vs `E_eff`),无法自动判定,靠 `公式校对清单.md` 人工抽查。
- deepseek grounding 框偶有偏紧,已加 4% 外扩,极端情况仍可能微切。
- 后台进程必须 `setsid` 启动,否则会随发起会话被整组杀掉(2026-11-17 实测)。
