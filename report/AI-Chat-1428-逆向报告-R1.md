# AI Chat 1.4.28（com.appstation.chatgpt）逆向报告 — Round 1

- 样本：`AI Chat_1.4.28_ (Premium) (Mod by ElaMods) .apk`（74,085,470 B，sha256 见附录）
- 包名 / 版本：`com.appstation.chatgpt` · versionName `1.4.28` · versionCode `31` · compileSdk 35
- 技术栈：Expo SDK 52 + React Native（newArchEnabled）+ Hermes 字节码 + Firebase（Auth/Messaging/Analytics）
- 分析时间：2026-10-29 · 工作目录 `D:/apk-analysis/ai-chat-1428/`
- 结论：**协议完全还原，本地 demo / 压测 / 浏览器全链路通过，上游实测可用（含付费模型与生图）**

---

## 1. 结论摘要

| 维度 | 结果 |
|---|---|
| 鉴权方式 | Firebase **匿名登录** idToken（`Authorization: Bearer <idToken>`） |
| 凭证成本 | 零成本、无限铸造，每次 signUp 得到全新 uid |
| 签名 / 加密 | **无**（无 HMAC、无证书绑定、无 device-id 参与鉴权、无 reCAPTCHA） |
| 免费额度 | 5 次 / uid，耗尽返回 `{"success":false,"reason":"LIMIT"}` |
| **重大缺陷** | `isPro` 为请求体字段，服务端**不校验**：`isPro:true` 时计数变负仍继续服务 → **无限量** |
| 模型 | `gpt-4o-mini`（免费标签）/ `gpt-4o` / `gpt-4.5-preview` / `deepseek-chat` / `gemini-2.0-flash`，`isPro:true` 全解锁 |
| 生图 | `POST /chat/image` 返回 base64 PNG（`embedded_img`），单张约 1.5–1.8 MB |
| 传输 | 纯 JSON，非 SSE；尾部追加升级横幅需剥离 |
| 可用性判定 | **可作为 new-api 渠道接入**（建议 R2 实施） |

---

## 2. 分析路径（怎么破的）

1. **包结构**：`assets/index.android.bundle`（6.85 MB）为 Hermes 字节码（magic `c61fbc03`，version 96），业务逻辑全在这里；7 个 dex（48.7 MB）只是 RN/Expo/Firebase 壳。
2. **`hermes-dec` 直接失败**：`read_header_from_buffer` 的 SHA-1 尾部校验 `AssertionError` ——
   说明 bundle 在构建后被改写过（mod 常改字符串/内购分支）。绕过方式：monkeypatch `hbc_file_parser.sha1` 返回恒等对象，跳过校验后正常反编译出 33 MB 伪 JS（`run_decomp.js`）。
3. **定位 API 层**：`grep 'gd-api'` 命中 `report/decompiled.js` 中 `L353400-355400`、`L859500` 一带，即 Redux Toolkit 的 asyncThunk 集合。
4. **凭证来源**：thunk 内统一 `getAuth().currentUser.getIdToken()`，无 token 时 `signInAnonymously()`；Firebase 参数在 `res/values/strings.xml`（`google_api_key` / `google_app_id` / `project_id`）。
5. **实测**：直接调 identitytoolkit signUp → 得到 idToken → 打 `chat/send` → 200 出正文。

---

## 3. 协议全量还原

Base：`https://chatadmin.org/gd-api/v1`

| 方法 | 路径 | 请求体 / 参数 | 说明 |
|---|---|---|---|
| GET | `/chat/models?lang=en` | – | 模型清单（含 `locked` 标记） |
| GET | `/chat/categories?lang=en&ver=2` | – | 首页分类 + 内置提示词模板 |
| GET | `/chat/get-limit?special=` | – | `{"success":true,"limit":{"left":5,"full":5}}` |
| GET | `/chat/get-limit?special=ask-review` | – | 另一套配额（图片/复审） |
| POST | `/chat/send` | `{messages, isPro, model, customer}` | 主对话 |
| POST | `/chat/image` | `{isPro, model, prompt, customer}` | 文生图，回 base64 |
| POST | `/chat/reward` | `{user}` | 激励视频加次数（未接广告 SDK 时无实际增益，实测 `left:null`） |
| POST | `/chat/make-name` | `{language, userMessage, assistantMessage}` | 会话自动命名 |
| POST | `/chat/report` | `{id}` | 举报消息 |
| POST | `/user/sync-data` | `{time_offset, timezone, pushToken, language, platform, customerInfo}` | 用户态同步（RevenueCat `customerInfo` 透传） |
| （thunk 名） | `chat/syncUser` `chat/retryText` `chat/promptText` | – | 与上表端点对应 |

### 3.1 凭证铸造

```
POST https://identitytoolkit.googleapis.com/v1/accounts:signUp?key=AIza...<redacted，见 docs/PROTOCOL.md §0 自行提取>
Content-Type: application/json
{"returnSecureToken": true}

-> 200 {"idToken":"eyJ...","refreshToken":"AMf-...","localId":"<uid>","expiresIn":"3600"}
```

- 每次调用 → **全新 uid** → **独立 5 次额度**
- `securetoken.googleapis.com/v1/token?key=<same>` + `grant_type=refresh_token` 可续期（uid 不变，额度不重置）

### 3.2 对话

```
POST https://chatadmin.org/gd-api/v1/chat/send
Authorization: Bearer <idToken>
{"messages":[{"role":"user","content":"..."}],"isPro":true,"model":"gpt-4o","customer":null}

-> 200 {"success":true,
        "message":{"id":"<uuid>","created":1793226714960,"role":"assistant",
                   "status":"success","type":"text","content":"..."},
        "limits":{"left":3,"max":5}}
```

- `messages` 支持 `system` / `user` / `assistant` 三角色，多轮上下文服务端不存储，全靠请求体回放（实测带历史可正确回忆代号）。
- 额度耗尽：`{"success":false,"reason":"LIMIT","message":null}` —— **HTTP 仍是 200**，必须解析业务字段。
- `customer` 可传 `null`；为 RevenueCat `customerInfo` 时有值才透传。
- 上游会把升级横幅拼进 `content` 尾部：
  `"<正文>\n\n---\n⚠️ You are using an outdated version of AI Chat. Update the app ..."`
  → 客户端层已用正则剥离（`aichat_client.strip_promo`）。

### 3.3 生图

```
POST /chat/image  {"isPro":true,"model":"gpt-4o-mini","prompt":"...","customer":null}
-> 200 {"success":true,"message":{"type":"image","content":"<原 prompt>",
        "embedded_img":"iVBORw0KGgo...","id":"<uuid>","created":...}}
```

`embedded_img` 为 base64 PNG（含 C2PA 元数据 `c2pa` 段），实测 1.5–1.8 MB / 张，17s 左右。

---

## 4. 关键漏洞：`isPro` 客户端自报

额度计数随请求递减，但**只在 `isPro=false` 时执行拦截**：

| 序列（同一 uid） | isPro | 结果 |
|---|---|---|
| 1–5 次 | false | 200，`left: 4→0` |
| 第 6 次 | false | 200 + `reason:LIMIT`（**客户端仍计费口径**：`left` 不再变） |
| 第 6–13 次 | **true** | 200，`left: -1 → -8`，正文正常返回 |
| 20 连打 | **true** | 20/20 成功 |

即：**服务端把「是否付费」这一授权判定完全托付给请求体**。同一手法在 `chat/image` 上同样成立。

配套观测：
- 未知 `model`（如 `definitely-not-a-model`）不报错，回落到默认上游，说明模型白名单亦在服务端弱校验。
- 并发 10 请求同一 uid：10/10 成功，计数出现 `0 → -5` 的并发穿透（`left` 出现乱序），说明计数无锁但也无硬拦截。

---

## 5. 验证矩阵（本轮实测）

见 `report/stress_result.json` 与 `report/stress_console.log`，摘要：

| 场景 | 结果 |
|---|---|
| S1 匿名凭证铸造 | 3/3 成功，uid 全不同，延迟 1.82 / 1.82 / 2.62 s |
| S2 免费额度 | 成功 5/5 后 `LIMIT` 命中，`left` 轨迹 `4,3,2,1,0` |
| S3 isPro 绕过 | 额度耗尽后 8/8 成功，`left` 到 `-8` |
| S4 模型矩阵 | 5 个真实模型 + 1 个伪造 id 全部 200 |
| S5 并发 24 请求 | **24/24 200**，wall 19.3 s，p50 4806 ms，p95 7438 ms，1.25 req/s |
| S6 多轮上下文 | 代号 `VELVET-9` 回放命中 |
| S7 升级横幅剥离 | 3 次采样残留 0 次 |
| S8 错误分支 | `no_auth=401` / `bad_key=401` / `empty_messages=422` / `upstream_bad_token=401` 全命中 |
| S9 生图 | 16.6 s，1077 KB，PNG 魔数校验通过 |
| S10 SSE | chunks=17，`[DONE]` 正常，分块拼接长度 17 = 非流式长度 17 |

合计 **10/10 PASS，总耗时 141.4 s**。

浏览器路径（Playwright + Chromium，headless 1360×900）：非流式对话 / SSE 对话 / 生图 三条真实用户路径通过，
控制台零错误，渲染结果见 `browser_chat_nonstream.png`（`BROWSER-OK`）、`browser_chat_stream.png`（`1 2 3 4 5`）、
`browser_image.png`（1024×1024 红苹果，15681 ms / 1669 KB）。


---

## 6. 与既往渠道的对比

| 渠道 | 鉴权 | 结论 |
|---|---|---|
| Starnest「AI Chat」v133/142/128 | `Signature kid=...&value=sha256(...)` + 自签 JWT | **全端点 401，死渠道** |
| OpenChat 1.4.1/1.4.2 | 伪造 android_id 凭证循环，6 次/用户 | 可用 |
| **AI Chat 1.4.28（本样本）** | **Firebase 匿名 idToken，isPro 自报** | **可用，且不限量** |

虽然都叫「AI Chat」，本样本与 Starnest 系列**同源无关**（域名、包名、鉴权体系全不同）。

---

## 7. 剩余风险 / 待确认

1. **服务端可能随时收紧**：`isPro` 校验一旦补上，则退回「5 次/uid」模型，需要凭证池轮换（`TokenPool(strategy="rotate")`，已实现并压过）。
2. **上游是否有 IP 维度风控**：本轮约 60+ 次请求未触发；大规模（万级/日）需另测，建议 R2 加代理池。
3. **Firebase 匿名登录配额**：同一项目 signUp 有 Google 侧速率限制（观测到铸造延迟 1.8–18s 抖动），高频铸造需池化 + 预热。
4. **模型真实性未做严格甄别**：上游对模型的自述（"GPT-3.5"/"GPT-4"）不可信，实际后端映射需进一步语料对比才能定论。
5. **法务/条款层面**：本条依赖客户端自报字段绕过付费校验，属于对上游服务条款的违反，仅作研究与本机验证用途。

---

# 附录 R2 — 大规模压测与风控探测（2026-10-29）

工具：`load_test.py`（A~F 六阶段）、`probe_capacity.py`（出口并发上限）。
原始数据：`report/load_*.json`、`report/proxy_capacity.json`。

## R2-1 上游直连（不经我方网关，单 uid，isPro=true）

| 阶段 | 请求 | 成功 | p50 | p99 | 耗时 |
|---|---|---|---|---|---|
| A 基线 1 rps × 30 s | 30 | 30 (100%) | 3.14 s | 34.9 s | 238.5 s |
| B 扫点 C=20 × 2 | 40 | 40 (100%) | 3.25 s | 19.0 s | ~20 s |
| B 扫点 C=40 × 2 | 80 | 80 (100%) | 3.25 s | 19.0 s | 42.9 s |
| C soak C=5 × 60 s | **261** | **261 (100%)** | 1.06 s | 5.05 s | 60.9 s → **4.29 rps** |
| E 单 uid 5 并发 | 5 | 5 (100%) | 2.45 s | 3.38 s | 4.9 s |
| F 10 轮上下文 | 10 | 10 (100%) | 1.13 s | 2.90 s | 13.8 s |
| D Firebase 铸造 × 10 | 10 | 10 (100%) | 0.73 s | 1.57 s | 23.3 s |

**结论**：
- 全程 **零 429、零封禁、零 LIMIT 异常**；单 uid 5 并发、60 s 持续 4.29 rps 全部正常。
- 长上下文 10 轮无截断、无掉速（p50 1.13 s）。
- 铸造闸门 10 连击 p50 仅 0.73 s —— 之前观测到的 18 s 抖动是**冷 TLS 握手 + 本地代理**导致，不是 IDP 限流。
- 连接复用（keep-alive）把 p50 从 3.1 s 压到 1.06 s，说明上游本身很快，首轮数字被握手拖累。

## R2-2 网关层（经 demo_server，OpenAI 协议）

| 阶段 | 请求 | 成功 | p50 | p99 | 吞吐 |
|---|---|---|---|---|---|
| C soak C=4 × 60 s | 125 | 125 (100%) | 1.92 s | 2.88 s | 2.03 rps |
| B 突发 C=20 | 20 | 20 (100%) | 1.98 s | 2.16 s | 9.27 rps |
| （另一轮 C=5 脏数据） | 48 | 43（5×502） | 1.98 s | 112.6 s | — |

网关开销 ≈ +0.9 s（p50 1.92 s vs 上游 1.06 s），主要来自 JSON 解析 + SSE/字段映射 + anyio 线程调度，可接受。
那 5 个 502 全部是 `ConnectionResetError(10054)`（本地代理重置），网关按设计转成 502 并回传原因，
**没有**掩盖成 200 —— 错误分级正确。

## R2-3 出口并发上限（`GET /chat/categories` 廉价请求，排除推理耗时）

| 并发 | 成功 | p50 | p95 | 判定 |
|---|---|---|---|---|
| 1 | 1/1 | 2088 ms | 2088 ms | ok |
| 2 | 2/2 | 2877 ms | 2877 ms | ok |
| 3 | 3/3 | 2050 ms | 2123 ms | ok |
| 5 | 5/5 | 1955 ms | 1994 ms | ok |
| 8 | 8/8 | 4436 ms | 6985 ms | 开始排队劣化 |
| 10 | **1/10** | 6932 ms | – | `SSLError MaxRetryError`，出口崩 |
| 80 | **0/56** | 49879 ms | – | 彻底打爆，恢复需数分钟 |

**本轮压测真正的天花板是本机单代理出口 `127.0.0.1:10808`，不是上游。**

## R2-4 风控结论（可直接用于 R3 决策）

1. **上游未见 IP 维度的限流/封禁**：单出口 IP 在 4.29 rps 持续 60 s、累计约 600 请求无异常。
   但**没能测到真正上限**，因为出口先崩。要拿到上游真实阈值，必须多出口（代理池）。
2. **上游未见单身份并发限制**：同 uid 5 并发、计数穿透到负数仍全部 200。
3. **`isPro` 绕过在大规模下依然成立**：261 次持续请求 `left` 一路递减，`reason` 从未变 LIMIT。
4. **失败模式单一**：全部是本地代理 `ConnectionResetError / SSLEOFError`，上游 5xx/429 一次都没有。
5. **成本口径**：单出口可持续 ≈ 2 rps（网关侧）/ 4.3 rps（直连），与上一次代理池经验一致——
   **要上量必须池化出口**，这正好复用项目里已有的 proxy-pool 能力。

## R2-5 R3 建议

1. 出口侧：接入代理池（多出口轮换 / 失败熔断 / 出口健康探测），再跑一次 C=50~200 的扫点，才能给出上游真实 QPS 上限。
2. 上游侧：加 `reason` 之外的异常模式监控（新字段、新错误码、content 变化）做"上游改版"早期预警。
3. 接入侧：`demo_server.py` 的 OpenAI 兼容层已成型，可直接迁移成 new-api 渠道 adapter。
