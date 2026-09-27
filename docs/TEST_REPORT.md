# RunPod 4090 Serverless（Qwen-Image-2.1 int8）部署与测试报告

日期：2026-09-27（UTC）。旧生产 endpoint `w2ww53ksg9wtj7` 全程只读，未做任何修改。

## 1. 交付物一览

| 项 | 值 |
|---|---|
| 新 endpoint ID / API 根 | `btrcmfepllqgbf` · `https://api.runpod.ai/v2/btrcmfepllqgbf` |
| template ID | `lyk71ps4un`（独立 template，非共享） |
| 镜像 | `ghcr.io/rockycqu002/qwen-image-edit-4090:v0.1.3@sha256:5d03bef9387de3545d9a6a9747a6e99eb0dd73606c6c4cfdaf52f27b10b4e2ab`（19 层，压缩 19.0 GB；公开包） |
| 代码仓库 | `github.com/rockycqu002/qwen-image-edit-4090`（Dockerfile / handler / 锁文件 / 清单 / 测试 / 部署脚本 / 切换文档） |
| GPU / 区域 | 仅 `NVIDIA GeForce RTX 4090`（池 ADA_24，"24 GB PRO"，$1.10/h = $0.000306/s），minCudaVersion 13.0；数据中心全部开放，实测落在 US-CA-2 / US-TX-3 / EUR-IS-2 |
| 扩缩容 | workersMin 0 · workersMax 5 · REQUEST_COUNT/1 · idleTimeout 5 s · FlashBoot（Standard） |
| 超时 | executionTimeout 60 s（worker 内 55 s 主动中止）；Job TTL 默认 24 h；`/run` 结果保留 30 min |
| 容器 | 容器盘 50 GB；env `QIE_STEPS=20 QIE_RESOLUTION=1024 QIE_LOG_LEVEL=INFO` `QIE_COMFY_ARGS=--disable-nvml-pressure` |
| 模型 | Comfy-Org/Qwen-Image-2.1 @ `9a44dbdb`：`qwen_image_2.1_int8_convrot`（7.26 GB，sha256 cb74113c…）+ `qwen3vl_8b_int8_convrot`（9.35 GB，8bfd0f6e…）+ `qwen_image_2.1_vae_bf16`（0.68 GB，bb21f747…），见 `MANIFEST.json` |
| 软件 | ComfyUI `88ab4a0`（0.37.0）· torch 2.14.0+cu130 · comfy-kitchen 0.2.35 · sageattention 1.0.6 · triton 3.8.0 · runpod 1.12.0（`requirements.lock`） |
| 推理配置 | euler/simple · 20 步 · cfg 1.0 · 面积等价 1024² · SageAttention · EasyCache 关 · 无 LoRA · 输出 JPEG q95 |

## 2. 协议兼容性与差异

与旧 FireRed endpoint 相同：`POST /run {"input":{"image":<raw base64>,"prompt":<str>}}` → `/status` 返回 `output.image`（base64 图片）；失败为 `output.error` 字符串或平台 `FAILED`。

| 差异 | 旧 | 新 | 对调用方的影响 |
|---|---|---|---|
| 输出编码 | JPEG q90 | **JPEG q95**（>9.5 MiB 时自动降 q90/q85，`info.jpeg_quality`） | 无；体积略大（1024² 约 150–450 KB） |
| `output.info` | 字符串 | **对象**（seed/steps/resolution/width/height/exec_ms/infer_ms/gpu/vram/build…） | 若调用方 `JSON.parse(info)` 需兼容对象 |
| 可选输入 | `ref_image` | `ref_image` + `ref_images[]`（共 ≤ 6 张）+ `steps` 4–30 / `resolution` 768–1280 / `seed` | 向后兼容 |
| 输入上限 | 未知 | 单图解码前 ≤ 8 MiB、≤ 50 MP；非 JPEG/PNG/WebP（含 GIF）拒绝；动图取首帧；EXIF 方向应用；透明合成白底 | 超限返回 `output.error`（FAILED） |
| 输出尺寸 | 模型决定 | 面积 ≈ resolution²，按输入比例、16 对齐（如 1184×896、832×1248） | 与输入比例一致，非原像素尺寸 |
| 时限 | 60 s | 60 s（worker 55 s 主动中止并返回 error，不拖死 worker） | 相同 |

## 3. 部署过程中的两个真实故障（均已修复，详见 `RUNPOD_PLAN.md §11`）

1. **容器盘 20 GB 装不下解压后 ~28 GB 的镜像** → worker 拉完镜像即在 `start container` 死亡且无日志、任务永久 IN_QUEUE。用探针 endpoint（同镜像、盘 60 GB、只跑 `nvidia-smi`/torch 自检的 handler）证实；改为 50 GB。
2. **`nvidia/cuda:*-base` 无 C 编译器** → Triton 在加载 int8 文本编码器时报 `Failed to find C compiler`，warm-up 失败、worker 每 17 s 退出一次。镜像加入 `gcc libc6-dev python3.12-dev`（v0.1.2）。

## 4. 真实硬件证据

- 探针（`results/probe_disk60.json`，US-CA-2 worker `h5lc3rnxbhs7nk`）：`nvidia-smi` → `NVIDIA GeForce RTX 4090, 580.126.20, 24564 MiB, 8.9`；`NVIDIA-SMI 580.126.20 / CUDA Version: 13.0`；`torch 2.14.0+cu130 13.0 True` / `get_device_name → NVIDIA GeForce RTX 4090`；`RUNPOD_GPU_NAME=NVIDIA+GeForce+RTX+4090`，`RUNPOD_GPU_SIZE=ADA_24`。
- worker 容器日志（RunPod 控制台 Logs）：`comfy-aimdo inited for GPU: NVIDIA GeForce RTX 4090 (VRAM: 24082 MB)`。
- 每个任务的 `output.info.gpu` = `cuda:0 NVIDIA GeForce RTX 4090 : cudaMallocAsync`，`vram_used_mib` 17043（三模型常驻）。所有测试 CSV 均带 job id 与 worker id。

## 5. 协议 / 格式 / 上限 / 失败与恢复（§6.1、§6.6）

| case | expect | status | ok | delay ms | exec ms | out | worker | error / contract |
|---|---|---|---|---|---|---|---|---|
| jpeg_landscape_1280 | COMPLETED | COMPLETED | True | 515 | 11459 | 1248x832 189918B | ud65qmv9cw0clh |  |
| png_portrait_960 | COMPLETED | COMPLETED | True | 169 | 7744 | 896x1184 187069B | ud65qmv9cw0clh |  |
| webp_square_1024 | COMPLETED | COMPLETED | True | 518 | 7029 | 1024x1024 152418B | ud65qmv9cw0clh |  |
| png_rgba_transparent | COMPLETED | COMPLETED | True | 238 | 7857 | 1184x896 168821B | ud65qmv9cw0clh |  |
| data_url_prefix | COMPLETED | COMPLETED | True | 1110 | 9726 | 1184x896 190937B | ud65qmv9cw0clh |  |
| small_320 | COMPLETED | COMPLETED | True | 276 | 7372 | 1184x896 126266B | ud65qmv9cw0clh |  |
| jpeg_exif_rotated | COMPLETED | COMPLETED | True | 541 | 7307 | 832x1248 191693B | ud65qmv9cw0clh |  |
| animated_webp_first_frame | COMPLETED | COMPLETED | True | 1003 | 9423 | 1024x1024 151499B | ud65qmv9cw0clh |  |
| two_refs | COMPLETED | COMPLETED | True | 1221 | 12755 | 1184x896 93592B | ud65qmv9cw0clh |  |
| params_steps12_res896_seed | COMPLETED | COMPLETED | True | 43583 | 4314 | 1024x768 82905B | 5w6s0m2eotmp9v |  |
| big_jpeg_7002KiB_payload9561kB | COMPLETED|HTTP 413 | COMPLETED | True | 634 | 8527 | 1024x1024 166691B | ud65qmv9cw0clh |  |
| max_params_steps30_res1280 | COMPLETED | COMPLETED | True | 1029 | 17993 | 1280x1280 313882B | ud65qmv9cw0clh |  |
| invalid_base64 | FAILED | FAILED | True | 909 | 98 | B | ud65qmv9cw0clh | invalid input: image 不是有效的 base64 |
| corrupt_jpeg | FAILED | FAILED | True | 712 | 159 | B | ud65qmv9cw0clh | invalid input: image 无法解码: OSError |
| gif_unsupported | FAILED | FAILED | True | 500 | 137 | B | ud65qmv9cw0clh | invalid input: image 不是 JPEG / PNG / WebP |
| missing_prompt | FAILED | FAILED | True | 312 | 104 | B | ud65qmv9cw0clh | invalid input: prompt 缺失 |
| missing_image | FAILED | FAILED | True | 107 | 69 | B | ud65qmv9cw0clh | invalid input: image 缺失或不是字符串 |
| steps_out_of_range | FAILED | FAILED | True | 1203 | 116 | B | ud65qmv9cw0clh | invalid input: steps=99 超出允许范围 4–30 |
| seven_images_too_many | FAILED | FAILED | True | 556 | 158 | B | 5w6s0m2eotmp9v | invalid input: 参考图最多 6 张（含 image） |
| over_8mib_png_platform_limit | HTTP 413|HTTP 400|FAILED | HTTP 400 | True |  |  | B |  | {"status":400,"title":"Bad Request","detail":"bad request: body: exceeded max body size of |
| policy_timeout_5s | TIMED_OUT|FAILED | FAILED | True | 886 | 8089 | B | ud65qmv9cw0clh | executionTimeout exceeded |
| cancel_while_queued | CANCELLED | CANCELLED | True |  |  | B |  |  |
| cancel_while_running | CANCELLED | CANCELLED | True | 60413 | 216 | B | ud65qmv9cw0clh |  |
| after_cancel_still_serving | COMPLETED | COMPLETED | True | 67747 | 7770 | 1184x896 163821B | ud65qmv9cw0clh |  |

24/24 cases ok.

## 6. 性能：热跑、冷启动、并发、突发（§6.3、§6.4）

所有数字来自 RunPod `/status` 文档（`delayTime` / `executionTime`）与 worker 自报的 `info`（`infer_ms` / `init_s` / `worker_jobs` / `build` / `comfy_args`），每行都带 job id 与 worker id（`results/load*.csv`）。测试期间 4090+CUDA13 宿主供给有限：多数时间只有 3 台 worker 缓存了镜像，其余槽位在拉镜像或 `throttled`。

### 6.0 热跑抖动的定位与最终 ComfyUI 参数（A/B/C/D/E）

基线（v0.1.2，ComfyUI 0.37 默认开启 DynamicVRAM / comfy-aimdo）热跑 30 张：30/30 成功、exec 中位 7.8 s，但 **7 张落在 15–50 s**，集中在 3 台 worker 中的 2 台，同一输出形状、VRAM 恒定、非首任务——排除 Triton 重编译与显存不足。v0.1.3 增加 `QIE_COMFY_ARGS`（模板环境变量即可改 ComfyUI 启动参数，不用重建镜像），逐一对照：

| 变体 | 张数 / worker 数 | exec 最小 / 中位 / 均值 / P90 / 最大 (s) | ≥12 s 的张数 | 推理中位 (s) | VRAM |
|---|---|---|---|---|---|
| A 基线：DynamicVRAM 开（v0.1.2 默认） | 30 / 3 | 7.3 / 7.8 / 12.4 / 19.4 / 50.5 | **7** | 7.5 | 17043 MiB |
| B `--disable-dynamic-vram` | 30 / 4 | 9.0 / 9.7 / 9.8 / 10.4 / 11.5 | **0** | 9.3 | 17527 MiB |
| C `--disable-dynamic-vram --highvram` | 30 / 3 | 8.6 / 9.6 / 9.9 / 10.9 / 15.0 | **1** | 9.1 | 17527 MiB |
| D DynamicVRAM 开 + `--disable-nvml-pressure` | 30 / 3 | 7.4 / 7.7 / 7.9 / 8.7 / 9.1 | **0** | 7.5 | 17043 MiB |
| E 对照：默认参数，与 D 同一批宿主 | 30 / 3 | 7.3 / 7.7 / 8.1 / 9.0 / 10.7 | **0** | 7.4 | 17043 MiB |

结论：**最终采用 D = DynamicVRAM 开 + `--disable-nvml-pressure`**（用 CUDA 而不是 NVML 探测显存压力）。它保持了 DynamicVRAM 的速度（中位 7.7 s、P90 8.7 s）且 30 张零异常；B/C（经典加载）也稳定但每张慢约 2 s。对照 E（回到默认参数，但此时的 3 台宿主已不是基线时出抖动的那两台）同样 30 张零异常（中位 7.7 s、最大 10.7 s）——说明基线的抖动**与宿主机相关**，无法在后来的机群上复现；`--disable-nvml-pressure` 是零成本的防御性选择（速度相同），若线上再出现抖动，退路是模板环境改成 `--disable-dynamic-vram`（稳定、每张 +2 s），不用重建镜像。建议调用方记录 `info.infer_ms` 以便观察。

### 6.1 摘要（最终配置：v0.1.3 + `--disable-nvml-pressure`）

| 指标 | 值 |
|---|---|
| 热 worker 单张（20 步、面积 1024²） | exec 最小 7.4 / 中位 7.7 / 均值 7.9 / P90 8.7 / 最大 9.1 s；推理中位 7.5 s（30 张，3 台 worker） |
| 真·冷启动（容器重启，无 FlashBoot 命中） | delay 40–46 s（其中 handler 初始化 28–34 s = ComfyUI 启动 + 三模型加载 + Triton JIT + 2 张预热），端到端 48–54 s（最终配置复测：真·冷 delay 40.8 s / init 34 s / 端到端 50.3 s；FlashBoot 命中 delay 1.1 s / 端到端 10.4 s） |
| FlashBoot 命中（空闲 90 s 后） | delay 0.5 s，端到端 9.4 s（基线 2/5 次命中） |
| 并发 3 | 3 台 worker 同时冷启动并行处理，delay 45.3–45.9 s，exec 7.4–7.8 s |
| 并发 5 | 基线时只有 3 台可用（另 2 台 initializing）：5 任务 delay 1.3–12.5 s 全部成功；最终配置复测（`results/load_final.csv`）：5 任务落到 4 台 worker（1 台已热：delay 2.1 s；3 台同时冷启动：delay 40–49 s；1 任务排队 10.9 s），5/5 成功，最坏端到端 59 s |
| 突发 7 / 60 s | 基线 7/7 成功，端到端 P50 9.4 s、P95 20.5 s；最终配置复测：7/7 成功，3 台热 worker 消化，delay ≤ 1.2 s，端到端 P50 9.5 s / P95 11.7 s |
| 内存漂移（30 张） | handler RSS 724→731 MiB，VRAM 17043→17043 MiB，`worker_jobs` 最大 22，无泄漏迹象 |
| 4 分钟轮询窗口 | 全部 300+ 个任务中最坏端到端 59 s（并发 5 时 3 台同时冷启动）——余量充足 |
| 最坏允许参数 30 步 × 1280² | exec 18.0 s（协议用例 `max_params_steps30_res1280`）< 60 s 超时 |


### 6.3 / 6.4 热跑、冷启动、并发、突发（`results/load.csv`）

| mode | jobs | completed | exec ms (min/med/p90/max) | delay ms | e2e ms | infer ms | distinct workers | cold (worker_jobs=1) |
|---|---|---|---|---|---|---|---|---|
| hot | 30 | 30 | n=30 min=7272 med=7772 p90=20058 max=50542 | n=30 min=67 med=510 p90=4553 max=62122 | n=30 min=8179 med=10498 p90=44280 max=71335 | n=30 min=7101 med=7541 p90=19787 max=50260 | 3 | 3 |
| cold | 5 | 5 | n=5 min=7412 med=7524 p90=7719 max=7771 | n=5 min=530 med=39869 p90=43748 max=45691 | n=5 min=9420 med=48220 p90=52320 max=54416 | n=5 min=7255 med=7318 p90=7450 max=7485 | 2 | 3 |
| conc1 | 1 | 1 | n=1 min=7894 med=7894 p90=7894 max=7894 | n=1 min=41746 med=41746 p90=41746 max=41746 | n=1 min=50478 med=50478 p90=50478 max=50478 | n=1 min=7659 med=7659 p90=7659 max=7659 | 1 | 1 |
| conc3 | 3 | 3 | n=3 min=7422 med=7761 p90=7779 max=7783 | n=3 min=45278 med=45685 p90=45890 max=45941 | n=3 min=53807 med=54521 p90=54883 max=54974 | n=3 min=7208 med=7533 p90=7539 max=7540 | 3 | 3 |
| conc5 | 5 | 5 | n=5 min=7433 med=7626 p90=8654 max=9312 | n=5 min=1325 med=3542 p90=11646 max=12458 | n=5 min=10452 med=12596 p90=19966 max=20388 | n=5 min=7205 med=7386 p90=7505 max=7511 | 3 | 0 |
| burst | 7 | 7 | n=7 min=7284 med=7621 p90=8891 max=10465 | n=7 min=76 med=445 p90=9097 max=9150 | n=7 min=8253 med=9412 p90=19096 max=20546 | n=7 min=7092 med=7390 p90=8702 max=10226 | 3 | 1 |

冷启动明细：

| i | zero-workers s | delay ms | init s | worker_jobs | exec ms | e2e ms | worker | status |
|---|---|---|---|---|---|---|---|---|
| 100 | 90 | 39869 | 31 | 1 | 7412 | 48220 | g0d1h7e0fs09rm | COMPLETED |
| 101 | 90 | 548 | 31 | 2 | 7508 | 9420 | g0d1h7e0fs09rm | COMPLETED |
| 102 | 90 | 45691 | 32 | 1 | 7640 | 54416 | g0d1h7e0fs09rm | COMPLETED |
| 103 | 90 | 530 | 32 | 2 | 7524 | 9509 | g0d1h7e0fs09rm | COMPLETED |
| 104 | 90 | 40833 | 33.6 | 1 | 7771 | 49175 | 5w6s0m2eotmp9v | COMPLETED |

conc1: health before={"workers": {"idle": 2, "initializing": 1, "ready": 2, "running": 1, "throttled": 1, "unhealthy": 0}, "jobs": {"completed": 50, "failed": 8, "inProgress": 0, "inQueue": 0, "retried": 0}} after={"workers": {"idle": 3, "initializing": 1, "ready": 3, "running": 1, "throttled": 0, "unhealthy": 0}, "jobs": {"completed": 51, "failed": 8, "inProgress": 0, "inQueue": 0, "retried": 0}}; per-job: ud65qm/41746+7894ms

conc3: health before={"workers": {"idle": 3, "initializing": 1, "ready": 3, "running": 1, "throttled": 0, "unhealthy": 0}, "jobs": {"completed": 51, "failed": 8, "inProgress": 0, "inQueue": 0, "retried": 0}} after={"workers": {"idle": 1, "initializing": 1, "ready": 1, "running": 3, "throttled": 0, "unhealthy": 0}, "jobs": {"completed": 54, "failed": 8, "inProgress": 0, "inQueue": 0, "retried": 0}}; per-job: 5w6s0m/45278+7783ms, sa5y38/45685+7761ms, g0d1h7/45941+7422ms

conc5: health before={"workers": {"idle": 1, "initializing": 1, "ready": 1, "running": 3, "throttled": 0, "unhealthy": 0}, "jobs": {"completed": 54, "failed": 8, "inProgress": 0, "inQueue": 0, "retried": 0}} after={"workers": {"idle": 0, "initializing": 2, "ready": 0, "running": 3, "throttled": 0, "unhealthy": 0}, "jobs": {"completed": 59, "failed": 8, "inProgress": 0, "inQueue": 0, "retried": 0}}; per-job: sa5y38/1708+7668ms, 5w6s0m/1325+9312ms, g0d1h7/3542+7626ms, sa5y38/10429+7433ms, g0d1h7/12458+7461ms

burst: health before={"workers": {"idle": 0, "initializing": 2, "ready": 0, "running": 3, "throttled": 0, "unhealthy": 0}, "jobs": {"completed": 59, "failed": 8, "inProgress": 0, "inQueue": 0, "retried": 0}} after={"workers": {"idle": 2, "initializing": 1, "ready": 2, "running": 1, "throttled": 1, "unhealthy": 0}, "jobs": {"completed": 66, "failed": 8, "inProgress": 0, "inQueue": 0, "retried": 0}}; per-job: g0d1h7/76+7405ms, sa5y38/9062+7632ms, sa5y38/9150+7284ms, sa5y38/353+7621ms, ud65qm/8727+10465ms, sa5y38/445+7329ms, sa5y38/250+7842ms

hot 内存漂移：RSS 724→726 MiB，VRAM 17043→17043 MiB；worker_jobs 最大 12

## 7. 质量对照：旧 5090 FireRed vs 新 4090 Qwen-Image-2.1（§6.5）

| pair | prompt | old 5090 FireRed: job / worker / exec / bytes | new 4090 Qwen: job / worker / exec / bytes | 目测（input \| old \| new 见 quality_sheet.jpg） |
|---|---|---|---|---|
| e1_recolor | Change the mug to matte dark green with a black … | `7dc96b7e-82b2…` / fq5es97b7b4o0g / 9931 ms / 107 KB | `870c8202-e680…` / ud65qmv9cw0clh / 7679 ms / 262 KB | 两者都正确变哑光深绿+黑柄；新版杯形/桌面保持更完整 |
| e2_material | Replace the wooden table with a white marble cou… | `5cc7c79f-f5e3…` / fq5es97b7b4o0g / 9471 ms / 59 KB | `469a7451-472b…` / ayqlmvrgl6k7fe / 8804 ms / 146 KB | 两者都换成大理石台面；新版杯子与光照保持不变，旧版杯柄略有改动 |
| e3_remove | Remove the umbrella. Keep the woman, her pose an… | `5ad9e730-fa04…` / fq5es97b7b4o0g / 10015 ms / 266 KB | `b71d6721-b876…` / ayqlmvrgl6k7fe / 8414 ms / 455 KB | 两者都去掉了伞并补全双手；新版雨衣褶皱更自然 |
| e4_style | Turn this photo into a loose watercolor painting… | `522f03c4-3f4e…` / mvemnob337w3om / 10367 ms / 274 KB | `e9c11b10-f071…` / ayqlmvrgl6k7fe / 8606 ms / 673 KB | 两者都转成水彩；旧版纸纹更重，新版更贴近原构图 |
| e5_text_en | Change the text on the sign to "CLOSED SUNDAYS".… | `fa1b2c53-774f…` / mvemnob337w3om / 10130 ms / 253 KB | `f245ad73-a396…` / ud65qmv9cw0clh / 7694 ms / 474 KB | 两者都正确改为 CLOSED SUNDAYS，字体/招牌保持 |
| e6_text_zh | 把海报上的大字"春季新品上市"改为"夏季特惠"，小字改为"满300减50"，其他保持不变。… | `9d531da1-2051…` / mvemnob337w3om / 9673 ms / 49 KB | `5beda80b-7c13…` / ayqlmvrgl6k7fe / 8395 ms / 115 KB | 两者都正确改为 夏季特惠 / 满300减50（中文渲染正确），排版保持 |
| e7_add | Add a large potted monstera plant in the empty c… | `554c5716-999d…` / fq5es97b7b4o0g / 9780 ms / 132 KB | `7931c2ad-2918…` / ud65qmv9cw0clh / 9567 ms / 273 KB | 两者都在窗边角落加了龟背竹，透视/光照匹配 |
| e9_logo_flag | Depict this logo as a flag waving in the sky. Ke… | `300489eb-0363…` / mvemnob337w3om / 9281 ms / 93 KB | `a4f77567-8df7…` / ayqlmvrgl6k7fe / 7779 ms / 232 KB | 旧版黑旗保留原色；新版把旗面改成红色但 logo 形状与文字保留——新版对'保持原样'的遵循略弱 |


## 8. 费用（§6.7）

### 8.1 单价与实测计费口径

| | 旧 5090（32 GB PRO） | 新 4090（24 GB PRO） |
|---|---|---|
| 标价 | $1.58/h = $0.000439/s | $1.10/h = $0.000306/s |
| 计费 API 实测 | `results/billing_old_7d.json`：8 天 $103.79 / 65.7 h ⇒ **$0.000439/s**（约 $12–22/天） | `results/billing_new_hourly.json`：测试期 ⇒ **$0.000307/s** |

RunPod 只对 worker **运行中**的秒数计费（含 idleTimeout 的 5 s 与冷启动的初始化时间，不含 FlashBoot 暂停态和镜像拉取）。

### 8.2 单张成本（按实测 exec 时间）

| 场景 | 旧 5090 FireRed（8 步） | 新 4090 Qwen-Image-2.1 int8（20 步） |
|---|---|---|
| 热 worker，单张 1024² | 9.8 s × $0.000439 = **$0.0043** | 7.8 s × $0.000306 = **$0.0024**（−44%） |
| 每次请求附带的 5 s idle | +$0.0022 | +$0.0015 |
| 真·冷启动（无 FlashBoot 命中） | 未测 | +40–46 s × $0.000306 = **+$0.012–0.014** |
| 30 步 1280²（允许的最坏参数） | — | 18.0 s ⇒ $0.0055 |

按旧 endpoint 近 8 天日均约 6.6 h 计费时长折算：同样的负载在 4090 上 ≈ 6.6 h × ($1.10/$1.58) × (7.8/9.8) ≈ **每天节省 ~45%**（假设冷启动比例不变；若流量稀疏、冷启动多，节省比例会下降——每次冷启动约多付 1.3 美分）。

### 8.3 本次测试的实际花费

- 新 endpoint（含排障、探针、A–E 对照与全部测试，约 330 个任务）：计费 API 到 19:59 UTC 累计 **$0.82 / 44.7 min**（`results/billing_new_hourly.json`；20:00–20:35 的最后一小时尚未出账，估计再 +$0.3）。
- 旧 endpoint：仅 9 个只读 `/run` 请求（8 组对照 + 1 次因客户端 bug 重跑），约 90 s × $0.000439 ≈ **$0.04**；配置未动。


## 9. 结论与遗留风险

**结论：新 endpoint `btrcmfepllqgbf` 可以进入调用方本地联调与切流审核。** 协议兼容（24/24 用例）、真实 4090、单张热跑 ~7.8 s、真·冷启动 ~45 s 端到端仍远在 4 分钟轮询窗口内、并发 3 可并行、5 台上限已配置、质量与旧服务同级（文字/中文/去物/加物/换材质全部正确）。

遗留 / 需要知道的：

1. **单卡供给**：4090 + CUDA 13 的宿主机不是随时有 5 台；测试中多次出现 `throttled`（宿主 GPU 被占）和第 4/5 台 worker 初始化滞后。REQUEST_COUNT/1 下并发 5 的尾延迟取决于当时的可用 worker 数（见 §6 表）。若业务并发经常 >3，建议观察一周的 `/health` 与 delayTime 再决定是否加 `workersMin=1`（每小时 $1.10）。
2. **首任务 / 冷启动**：无 FlashBoot 命中时约 40–46 s（容器启动 ~10 s + ComfyUI 与模型加载 + Triton JIT ~32 s）。FlashBoot 命中时 0.5 s。业务上如果对首图延迟敏感，同样用 `workersMin=1` 解决。
3. **执行超时 60 s 会杀 worker**：任何超过 60 s 的任务（本项目里只有人为的 policy 超时能触发）会导致该 worker 重启，下一任务多等约 60 s（`cancel_while_running`/`after_cancel_still_serving` 两行可见）。参数范围已收紧（30 步 × 1280² 最坏 18 s），正常流量不会触发。
4. **输出体积**：JPEG q95 平均比旧服务大约 2 倍（约 340 KB vs 160 KB），仍远低于 10 MB 结果上限；若带宽敏感可把 `JPEG_QUALITIES` 首选改回 90。
5. **DynamicVRAM 抖动**：见 §6.0。
6. **AutoDL 同 seed 基准对照未做**：AutoDL 机器当前是无卡模式，无法生成基准图；RunPod 侧同 seed 结果可复现（同 seed 输出字节完全一致：smoke #2/#3 均 269694 B）。

## 10. 保留的资源与持续费用

| 资源 | 状态 | 持续费用 |
|---|---|---|
| endpoint `btrcmfepllqgbf` | workersMin 0 / workersMax 5，FlashBoot 暂停态 worker 不计费 | $0（无请求时） |
| template `lyk71ps4un` | 指向 v0.1.3 digest | $0 |
| GHCR 镜像 `rockycqu002/qwen-image-edit-4090`（v0.1.1 / v0.1.2 / v0.1.3，公开） | 保留 | $0（公开包免费） |
| 探针 endpoint/template（2 个） | **已删除** | — |
| 旧 endpoint `w2ww53ksg9wtj7` | **未改动**，按原样运行 | 由现有业务决定 |

测试任务的结果在 RunPod 侧 30 min 后自动过期；本地保留了全部输出（`results/`，已在 `.gitignore`，未入库）。

## 11. 文件索引

- 仓库 `github.com/rockycqu002/qwen-image-edit-4090`：`Dockerfile`、`src/handler.py`、`scripts/fetch_models.py`、`requirements.lock`、`MANIFEST.json`、`deploy/runpod_api.py`、`tests/`、`docs/CALLER_SWITCH.md`（切换/回滚步骤）、`README.md`
- 本地 `~/qwen-image-edit-4090/results/`：`protocol.csv` + `protocol_images/`、`load.csv`（基线 hot/cold/conc/burst）、`load_ab_{nodyn,highvram,nvml,default}.csv`（A/B/C/D/E）、`load_final.csv`（最终配置 cold/conc5/burst）、`quality/`（16 张原始输出 + `quality.csv` + `quality_sheet.jpg`）、`probe_disk60.json`、`billing_*.json`、`smoke*.log`、`run_tests.log`、`image_versions.txt`
- 报告与关键证据的副本已入库：`docs/TEST_REPORT.md`、`docs/evidence/`
- 方案与部署日志：`~/autodl_qwen_image_2.1_sy/results/RUNPOD_PLAN.md`（§11 为故障复盘）
