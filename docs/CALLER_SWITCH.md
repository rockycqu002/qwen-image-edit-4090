# 调用方切换 / 回滚步骤（旧 5090 FireRed → 新 4090 Qwen-Image-2.1）

本文只描述**调用方**需要做的最小变更和运行步骤；不涉及对旧 endpoint 的任何修改。所有 ID 均为非敏感配置，RunPod API key 只保存在服务端。

| | 旧（生产，保持不动） | 新 |
|---|---|---|
| endpoint ID | `w2ww53ksg9wtj7` | `btrcmfepllqgbf` |
| API 根地址 | `https://api.runpod.ai/v2/w2ww53ksg9wtj7` | `https://api.runpod.ai/v2/btrcmfepllqgbf` |
| template | （旧共享 template，未读取、未改动） | `lyk71ps4un`（独立） |
| 模型 / GPU | FireRed-Image-Edit-1.1-fast / RTX 5090 32 GB | Qwen-Image-2.1 int8 convrot / RTX 4090 24 GB |
| 请求 | `POST /run {"input":{"image":<raw base64>,"prompt":<str>}}` | 相同（另支持可选 `ref_image` / `ref_images` / `steps` / `resolution` / `seed`） |
| 响应 | `output.image` = JPEG q90 base64，`output.info` = 字符串 | `output.image` = **JPEG q95** base64，`output.info` = **对象**（seed/steps/resolution/宽高/耗时/GPU 等） |
| 失败 | `output.error` 或平台 `FAILED` | 相同语义：`output.error` 字符串；worker 崩溃时平台 `FAILED` |

## 1. 代码变更（一次性，可先合并、不切流）

1. 全仓搜索旧 ID：`grep -rn w2ww53ksg9wtj7 .` —— 需求文档已知至少两处：`app/api/generate/route.ts`（提交 + 浏览器轮询）与 `worker/sync-generations.ts`（Cron 补拉）。
2. 两处都改为读取服务端环境变量：
   ```ts
   const RUNPOD_ENDPOINT_ID = env.RUNPOD_ENDPOINT_ID;            // 当前用于"提交 + 查询"的 endpoint
   const RUNPOD_ENDPOINT_ID_PREV = env.RUNPOD_ENDPOINT_ID_PREV;  // 上一版，仅用于查询在途/历史 job（可为空）
   const runpodBase = (id: string) => `https://api.runpod.ai/v2/${id}`;
   ```
   - 提交（`/run`）永远只用 `RUNPOD_ENDPOINT_ID`。
   - 查询（`/status/{jobId}`、Cron 补拉）：**job ID 只能到产生它的 endpoint 查询**。最小方案（见 §2）通过"先排空再切"保证切换时刻没有在途旧任务，因此查询也只用 `RUNPOD_ENDPOINT_ID`；`RUNPOD_ENDPOINT_ID_PREV` 仅作回滚期间的手工排查用。
   - 若将来要"不停机切换"，需要在任务记录里持久化 `endpoint_id`，查询按记录路由（历史无字段的默认旧 ID）——涉及 D1 变更，另行评审，不在本次范围。
3. 配置来源：本地 `.dev.vars`，生产用平台 secrets；浏览器端不能传 endpoint，`RUNPOD_API_KEY` 保持服务端专用。
4. 不删除任何现有检查（鉴权 / job token / 限流 / 配额 / D1 / R2 / 失败语义）。对已发出的 `/run` 请求不做盲目重试（平台可能已接收）。
5. 解析 `output.info` 时按"可能是字符串也可能是对象"处理（旧是字符串，新是对象），或者只当作透传日志字段，不要 `JSON.parse` 一个已经是对象的值。

## 2. 切换步骤（最小方案：排空 → 切 → 验 → 恢复）

前置：新 endpoint 已通过 §6 测试报告（`results/TEST_REPORT.md`），且 `workersMax` 已按报告设定（当前 5）。

1. **本地验证**（不碰生产）：本地 dev server 的 `.dev.vars` 设 `RUNPOD_ENDPOINT_ID=btrcmfepllqgbf`，回放三类请求——提交、浏览器 1 s 轮询直到终态、Cron 补拉——核对落库字段（job id、状态、图片写入 R2、`info` 解析）。
2. **暂停新提交**：把提交入口置为维护态（返回可重试的 503 或前端禁用按钮）。
3. **排空旧 endpoint**：等待所有旧待处理任务到达终态**并且结果已保存**（不能只等浏览器的 4 分钟窗口——Cron 补拉也要跑完一轮）。核对方法：本地数据库中"非终态且 endpoint=旧"的记录为 0，且 `GET https://api.runpod.ai/v2/w2ww53ksg9wtj7/health` 的 `jobs.inQueue == 0 && jobs.inProgress == 0`（只读请求，不影响旧服务）。
4. **切换**：生产环境变量 `RUNPOD_ENDPOINT_ID=btrcmfepllqgbf`、`RUNPOD_ENDPOINT_ID_PREV=w2ww53ksg9wtj7`，重新部署两处（API route + Cron worker）。
5. **验证**：用一张真实业务图提交一次，确认 `status` 轮询到 `COMPLETED`、`output.image` 能解码、落库/R2 正常；确认 Cron 一轮无报错。
6. **恢复提交**。
7. 观察期（建议 ≥ 24 h）内保留旧 endpoint 原样（`workersMin=0` 时不产生费用），不要删除。

## 3. 回滚（对称）

任何一步失败或观察期内质量/延迟不达标：

1. 暂停新提交 → 等新 endpoint 在途任务终态并落库（`GET .../btrcmfepllqgbf/health` 的 inQueue/inProgress 归零；已提交到 4090 的 job **仍到 4090 查询**，不要拿到旧 endpoint 查）。
2. 环境变量换回 `RUNPOD_ENDPOINT_ID=w2ww53ksg9wtj7`，重新部署两处。
3. 验证一次真实提交 → 恢复。

旧 endpoint 在整个过程中没有被修改，因此回滚只是配置回退，没有 RunPod 侧操作。

## 4. 调用方需要知道的行为差异

- 输入上限：单张图 base64 解码后 ≤ 8 MiB（RunPod `/run` 请求体上限 10 MB）；像素上限按解码前的头部尺寸校验；动图只取第一帧；EXIF 方向会被应用；PNG/WebP 透明区域合成到白底后再编辑。
- 输出：始终 JPEG q95；当 q95 超过 9.5 MiB 时自动降到 q90/q85 以保证能通过 `/run` 结果通道（`info.jpeg_quality` 记录实际值）。
- 参数默认：20 步、`resolution=1024`（面积等价边长，输出宽高按输入比例、16 对齐）、随机 seed（`info.seed` 返回）；`steps` 4–30、`resolution` 768–1280 可选。
- 时限：执行超时 60 s（worker 侧 55 s 主动中止并返回 `output.error`）；`/run` 结果保留 30 min，务必及时拉取（后台 24 h 清扫阈值 ≠ RunPod 保留承诺）。
- 冷启动：无 FlashBoot 命中时首个任务会多出镜像启动 + 模型加载 + Triton 编译时间（数值见测试报告）；REQUEST_COUNT/1 扩容下并发请求会各自触发 worker。
