# 新 4090 图像编辑 endpoint 调用说明（给应用侧 / Codex）

目标：在现有应用里把图像编辑请求从旧 endpoint `w2ww53ksg9wtj7`（FireRed，5090）切到新 endpoint `btrcmfepllqgbf`（Qwen-Image-2.1 int8，4090）。**请求 / 响应协议与旧服务兼容**，应用侧最小改动是换 endpoint ID，并把 `output.info` 当作"可能是对象"处理。

所有 ID 为非敏感配置；RunPod API key 只能放在服务端（`RUNPOD_API_KEY`），不能下发到浏览器。旧 endpoint 保持原样，不要修改、不要删除。

---

## 1. 基本信息

| 项 | 值 |
|---|---|
| Endpoint ID | `btrcmfepllqgbf` |
| API 根地址 | `https://api.runpod.ai/v2/btrcmfepllqgbf` |
| 鉴权 | HTTP header `Authorization: Bearer <RUNPOD_API_KEY>`（与旧 endpoint 同一把 key） |
| 调用模式 | 队列式（Queue-based）：`POST /run` 提交 → `GET /status/{id}` 轮询；也支持 `POST /runsync` |
| GPU / 模型 | RTX 4090 24 GB · Qwen-Image-2.1（int8 convrot 量化）· 20 步 · 面积等价 1024² |
| 单张耗时（worker 已热） | 执行 7.4–9 s（中位 7.7 s） |
| 冷启动（无热 worker） | 首个任务多等 40–46 s（容器启动 + 模型加载）；FlashBoot 命中时 0.5–1 s |
| 执行超时 | 60 s（worker 在 55 s 主动中止并返回 `output.error`） |
| 结果保留 | `/run` 的结果在 RunPod 侧保留 **30 分钟**，超时未取则丢失 |
| 扩缩容 | 0–5 个 worker，每个 worker 同时只处理 1 个任务，并发请求会各自触发 worker |

---

## 2. 请求

### 2.1 提交任务 `POST /run`

```http
POST https://api.runpod.ai/v2/btrcmfepllqgbf/run
Authorization: Bearer <RUNPOD_API_KEY>
Content-Type: application/json

{
  "input": {
    "image":  "<base64 编码的 JPEG/PNG/WebP>",
    "prompt": "Change the mug to matte dark green with a black handle."
  }
}
```

| 字段 | 必填 | 类型 | 说明 |
|---|---|---|---|
| `input.image` | 是 | string | 待编辑图（Picture 1）。原始 base64；带 `data:image/...;base64,` 前缀也接受 |
| `input.prompt` | 是 | string | 编辑指令，中英文都可，原样传给模型 |
| `input.ref_image` | 否 | string | 第二张参考图（Picture 2），base64，与旧服务同名同义 |
| `input.ref_images` | 否 | string[] | 第 3–6 张参考图，base64 数组 |
| `input.steps` | 否 | int | 采样步数，允许 4–30，默认 20（越少越快、质量下降） |
| `input.resolution` | 否 | int | 输出面积等价边长，允许 768–1280，默认 1024（内部对齐到 32 的倍数） |
| `input.seed` | 否 | int | 0–2^53；不传则随机，实际值在 `output.info.seed` 返回；同 seed 同输入结果字节级可复现 |

未知字段被忽略。**默认调用只传 `image` + `prompt` 即可**，与旧服务完全一致。

图片输入约束（超出即返回 `FAILED` + `output.error`）：

- 单张图 base64 解码后 ≤ 8 MiB；整个 `/run` 请求体 ≤ 10 MB（RunPod 平台限制，超出直接 HTTP 400/413）
- 格式仅 JPEG / PNG / WebP（按文件头判断，GIF 拒绝）；动图取第一帧
- 像素数 ≤ 50 MP
- 图片总数（含 `image`）≤ 6
- 处理前会自动应用 EXIF 方向；PNG/WebP 透明区域合成到白底

`/run` 响应（HTTP 200）：

```json
{"id": "870c8202-e680-4c2e-9d3a-0f1e2d3c4b5a-e1", "status": "IN_QUEUE"}
```

### 2.2 查询状态 `GET /status/{id}`

```http
GET https://api.runpod.ai/v2/btrcmfepllqgbf/status/870c8202-e680-4c2e-9d3a-0f1e2d3c4b5a-e1
Authorization: Bearer <RUNPOD_API_KEY>
```

进行中：

```json
{"id": "…", "status": "IN_PROGRESS", "delayTime": 512, "workerId": "ud65qmv9cw0clh"}
```

成功：

```json
{
  "id": "…",
  "status": "COMPLETED",
  "delayTime": 515,
  "executionTime": 7744,
  "workerId": "ud65qmv9cw0clh",
  "output": {
    "image": "<base64 JPEG>",
    "info": {
      "model": "qwen-image-2.1-int8-convrot",
      "seed": 1234567890,
      "steps": 20,
      "resolution": 1024,
      "width": 1184,
      "height": 896,
      "n_images": 1,
      "jpeg_quality": 95,
      "output_bytes": 168821,
      "exec_ms": 7744,
      "infer_ms": 7480,
      "gpu": "cuda:0 NVIDIA GeForce RTX 4090 : cudaMallocAsync",
      "vram_used_mib": 17043,
      "rss_mib": 726,
      "worker_jobs": 12,
      "init_s": 31.0,
      "build": "v0.1.3",
      "comfy_args": "--disable-nvml-pressure"
    }
  }
}
```

`status` 取值：`IN_QUEUE` → `IN_PROGRESS` → `COMPLETED` | `FAILED` | `CANCELLED` | `TIMED_OUT`。`delayTime`（排队 + 冷启动，ms）和 `executionTime`（worker 内执行，ms）由 RunPod 填写。

### 2.3 取消 `POST /cancel/{id}`

排队中或执行中均可取消，返回 `{"id": "...", "status": "CANCELLED"}`。执行中取消会让该 worker 重启，下一任务多等约 60 s，非必要不用。

### 2.4 健康 `GET /health`

```json
{"jobs":{"completed":241,"failed":16,"inProgress":0,"inQueue":0,"retried":0},
 "workers":{"idle":0,"initializing":0,"ready":0,"running":0,"throttled":5,"unhealthy":0}}
```

`workers.throttled` 表示 worker 所在宿主机的 4090 暂时被别的租户占用，任务会排队直到有 worker 可用；`ready > 0` 时新任务通常 1 s 内开始。可用于监控页，不影响调用逻辑。

### 2.5 同步模式 `POST /runsync`（可选）

同样的请求体发到 `/runsync`，HTTP 连接保持到任务结束（RunPod 默认最多等 90 s），直接返回 §2.2 的成功/失败结构；若超过等待时间返回 `IN_PROGRESS` + `id`，之后仍需 `/status` 轮询。请求体上限 20 MB。**现有应用已经是 `/run` + 轮询，不必改成 `/runsync`。**

---

## 3. 输出

- `output.image`：始终是 **JPEG（q95）** 的 base64，无 `data:` 前缀。当 q95 编码超过 9.5 MiB 时自动降到 q90 / q85，实际值见 `info.jpeg_quality`。1024² 一般 150–450 KB，约为旧服务的 2 倍。
- 输出尺寸：面积 ≈ `resolution²`，宽高按输入比例、对齐到 16 的倍数（例：1600×1200 输入 → 1184×896；竖图 → 832×1248；方图 → 1024×1024）。**不是原图像素尺寸**，与旧服务行为一致（旧服务也由模型决定尺寸）。
- `output.info`：**对象**（旧服务是字符串）。应用侧若做 `JSON.parse(info)` 必须先判断类型；建议只当作日志字段透传。有用的字段：`seed`（复现）、`exec_ms` / `infer_ms`（监控）、`width` / `height`。

---

## 4. 失败语义

| 情形 | `status` | `output` | 应用侧处理 |
|---|---|---|---|
| 输入不合法（缺 prompt、非法 base64、格式不支持、超限、参数越界、图片 > 6 张） | `FAILED` | `{"error": "invalid input: …"}` | 展示给用户 / 记录，**不要重试** |
| 推理超时（> 55 s） | `FAILED` | `{"error": "推理超过 55 s，已中断"}` | 可重试一次（正常参数不会触发，30 步 × 1280² 最坏 18 s） |
| 推理后端异常 / worker 崩溃 | `FAILED` | `{"error": "…"}` 或无 `output`（平台级错误在顶层 `error` 字段） | 重试一次，新任务会落到其他 worker |
| 平台执行超时（60 s） | `TIMED_OUT` 或 `FAILED` | `error: "executionTimeout exceeded"` | 重试一次 |
| 请求体 > 10 MB | HTTP 400 / 413 | — | 前端压缩后再提交（现有前端已把最长边压到 ≤ 1280，一般不会触发） |
| 鉴权失败 | HTTP 401 | — | 检查 `RUNPOD_API_KEY` |

`output.error` 里 `invalid input:` 开头的错误信息含中文（如 `prompt 缺失`、`image 不是 JPEG / PNG / WebP`），可直接展示或映射成自己的文案。

对已经发出但没收到响应的 `/run` 请求不要盲目重试，平台可能已接收并计费。

---

## 5. 示例

### 5.1 curl

```bash
export RUNPOD_API_KEY=…   # 服务端环境变量
EP=https://api.runpod.ai/v2/btrcmfepllqgbf

# 提交
JOB=$(curl -s -X POST "$EP/run" \
  -H "Authorization: Bearer $RUNPOD_API_KEY" -H "Content-Type: application/json" \
  -d "{\"input\":{\"image\":\"$(base64 -w0 input.jpg)\",\"prompt\":\"Remove the umbrella. Keep the woman, her pose and the background unchanged.\"}}" \
  | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')

# 轮询
while :; do
  R=$(curl -s "$EP/status/$JOB" -H "Authorization: Bearer $RUNPOD_API_KEY")
  S=$(echo "$R" | python3 -c 'import sys,json;print(json.load(sys.stdin)["status"])')
  echo "$S"; [ "$S" = IN_QUEUE ] || [ "$S" = IN_PROGRESS ] || break; sleep 1
done
echo "$R" | python3 -c 'import sys,json,base64;d=json.load(sys.stdin);o=d.get("output",{});
open("out.jpg","wb").write(base64.b64decode(o["image"])) if "image" in o else print("ERROR",d.get("error") or o.get("error"));print(o.get("info"))'
```

### 5.2 TypeScript（服务端，与现有 `app/api/generate/route.ts` 结构一致）

```ts
const RUNPOD_ENDPOINT_ID = env.RUNPOD_ENDPOINT_ID;          // "btrcmfepllqgbf"
const base = `https://api.runpod.ai/v2/${RUNPOD_ENDPOINT_ID}`;
const headers = { Authorization: `Bearer ${env.RUNPOD_API_KEY}`, "Content-Type": "application/json" };

// 提交
const run = await fetch(`${base}/run`, {
  method: "POST", headers,
  body: JSON.stringify({ input: { image: imageBase64, prompt } }),   // 可选: ref_image, ref_images, steps, resolution, seed
});
if (!run.ok) throw new Error(`runpod /run ${run.status}`);          // 400/413 = 请求体超限, 401 = key 错
const { id } = await run.json() as { id: string; status: string };

// 轮询（浏览器 1 s 一次 / Cron 补拉 均可）
type Status = {
  id: string; status: "IN_QUEUE" | "IN_PROGRESS" | "COMPLETED" | "FAILED" | "CANCELLED" | "TIMED_OUT";
  delayTime?: number; executionTime?: number; workerId?: string; error?: string;
  output?: { image?: string; info?: Record<string, unknown> | string; error?: string };
};
const st = await (await fetch(`${base}/status/${id}`, { headers })).json() as Status;

if (st.status === "COMPLETED" && st.output?.image) {
  const jpegBytes = Buffer.from(st.output.image, "base64");       // 总是 JPEG
  const info = typeof st.output.info === "string" ? st.output.info : JSON.stringify(st.output.info); // 旧: 字符串, 新: 对象
  // 写 R2 / 落库 …
} else if (st.status === "FAILED" || st.status === "TIMED_OUT" || st.status === "CANCELLED") {
  const msg = st.output?.error ?? st.error ?? st.status;
  const retryable = !(msg.startsWith("invalid input:"));
  // …
}
```

---

## 6. 现有应用的最小改动清单

1. 全仓搜索 `w2ww53ksg9wtj7`（已知至少 `app/api/generate/route.ts` 与 `worker/sync-generations.ts`），改为读环境变量 `RUNPOD_ENDPOINT_ID`；本地 `.dev.vars` 设 `RUNPOD_ENDPOINT_ID=btrcmfepllqgbf`。
2. `output.info` 解析兼容字符串 / 对象。
3. 其他逻辑（鉴权、job token、限流、配额、D1、R2、失败语义、4 分钟轮询窗口）全部不动。
4. **job ID 只能到产生它的 endpoint 查询**：切换期间若还有旧 endpoint 的在途任务，`/status` 必须仍打到旧 ID。最简单的做法是切换前先排空旧任务（见 `CALLER_SWITCH.md` §2）。
5. 不要把 endpoint ID 或 API key 暴露到浏览器。

---

## 7. 建议 Codex 在本地跑的验收用例

用本地 dev server 指向新 endpoint，逐条核对：

| # | 用例 | 预期 |
|---|---|---|
| 1 | 普通 JPEG 横图 + 英文 prompt | `COMPLETED`，`output.image` 可解码为 JPEG，尺寸约 1184×896（按比例），落库 / R2 正常 |
| 2 | PNG 竖图 | `COMPLETED`，尺寸约 896×1184 |
| 3 | 带 `data:image/png;base64,` 前缀的 base64 | `COMPLETED`（前缀被接受） |
| 4 | 中文 prompt（如"把海报大字改为'夏季特惠'"） | `COMPLETED`，中文渲染正确 |
| 5 | 带 `ref_image` 的双图请求 | `COMPLETED`，`info.n_images = 2` |
| 6 | 缺 `prompt` | `FAILED`，`output.error = "invalid input: prompt 缺失"`，应用侧不重试、UI 有提示 |
| 7 | GIF 文件 | `FAILED`，`output.error` 含"不是 JPEG / PNG / WebP" |
| 8 | 无 worker 时（`/health` 的 ready=0）提交 1 张 | `IN_QUEUE` 停留 40–60 s 后 `COMPLETED`，仍在轮询窗口内；应用不误报超时 |
| 9 | 连续提交 3 张 | 3 个任务各自完成，浏览器轮询与 Cron 补拉都不重复落库 |
| 10 | `info` 字段 | 落库时按对象处理不报错；旧数据（字符串）读取也不报错 |

每个用例记录 `id`、`status`、`delayTime`、`executionTime`、`info.seed`，便于对照。

---

## 8. 参考

- 切换 / 回滚步骤：`docs/CALLER_SWITCH.md`
- 部署与压测报告（性能、冷启动、并发、质量对照、费用）：`docs/TEST_REPORT.md`
- Worker 源码（协议以此为准）：`src/handler.py`
- 镜像：`ghcr.io/rockycqu002/qwen-image-edit-4090:v0.1.3`
