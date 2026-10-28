# new-api 对接指南 — AI Chat 1.4.28（appstation.chatgpt）

本文给「另一个会话」直接照着做用。读完你应该能不问我任何问题就把渠道接上。

- 上游：`https://chatadmin.org/gd-api/v1`（AI Chat 1.4.28）
- 凭证：Firebase 匿名登录 idToken（**自动铸造，无需 App、无需真机、无需注册**）
- 本地网关：`demo_server.py`（OpenAI 兼容，`:8960`），已含号池与额度自愈
- 能力：5 个文本模型（含 `gpt-4o` / `gpt-4.5-preview` / `deepseek-chat` / `gemini-2.0-flash`）+ 文生图（base64 PNG）

---

## 0. 先做选择：两种接入形态

| | 形态 A：Sidecar（推荐先做） | 形态 B：原生 Go 渠道 |
|---|---|---|
| 做法 | 把 `demo_server.py` 跑在 new-api 同机，new-api 用**现有 OpenAI 渠道类型**指向它 | 在 new-api 里新增 `type` 渠道 + `relay/channel/aichat1428` 适配器 |
| 代码量 | **0 行 Go** | 约 500–800 行 Go + 单测 |
| 优点 | 10 分钟通；号池/重试/协议细节全在 Python 侧已实现并测过 | 进程少、可复用 new-api 计费/限流/日志/渠道监控；无跨进程调用 |
| 缺点 | 多一个进程；new-api 看不到上游真实错误码（只有我方映射后的） | 要吃透上游的非标行为（非 SSE、LIMIT 业务码、升级横幅） |
| 适用 | 先验证价值、快速上线 | 长期生产 |

**建议**：现在走 A；等号池/额度策略稳定后再把逻辑搬进 B（协议细节见 `PROTOCOL.md`，照抄不踩坑）。

---

## 1. 形态 A：Sidecar（0 行 Go）

### 1.1 起 sidecar

```bash
# 依赖
pip install fastapi uvicorn requests playwright   # playwright 仅浏览器验证需要

# 准备凭证（从 APK 提取，见 docs/PROTOCOL.md §0）
cp config.example.json config.local.json
# 编辑 config.local.json 填 firebase_api_key / firebase_app_id / firebase_project

# 起服务（生产建议用 is_pro=true 单身份模式）
AICHAT_IS_PRO=1 AICHAT_POOL_SIZE=1 python demo_server.py --host 0.0.0.0 --port 8960
```

健康检查：

```bash
curl -s http://127.0.0.1:8960/health
# {"status":"ok","mode":"upstream","is_pro":true,"pool":{"size":1,"target":1,"strategy":"single-identity",...}}
```

### 1.2 在 new-api 里加渠道

管理后台 → 渠道 → 新建：

| 字段 | 填什么 | 说明 |
|---|---|---|
| 类型 | **OpenAI**（或任意 OpenAI 兼容类型） | 走标准 `/v1/chat/completions` |
| 名称 | `AI Chat 1.4.28` | |
| 代理 / Base URL | `http://127.0.0.1:8960` | **不要**带 `/v1`，new-api 会自己拼 `/v1/chat/completions` |
| 密钥 | 任意非空字符串，如 `sk-aichat` | sidecar 只要求存在；用于错误分支测试可用 `sk-invalid-xxx` 触发 401 |
| 模型 | `gpt-4o-mini,gpt-4o,gpt-4.5-preview,deepseek-chat,gemini-2.0-flash` | 见下方映射建议 |
| 模型重定向 | 建议做，见 1.3 | |
| 超时 | **≥ 180 秒** | 上游长尾有 18–20 s，实测 p99 可到 112 s（重试叠加） |
| 重试 | 2 次以内 | 我方已做 2 次重试，重复重试会放大延迟 |

### 1.3 模型映射建议（重要）

new-api 里很多业务方按 `gpt-4o` / `gpt-4o-mini` 直接点名，而上游 5 个 id 语义不同。
建议用「模型重定向」把 new-api 对外模型名映射到上游 id：

```json
{
  "gpt-4o": "gpt-4o",
  "gpt-4o-mini": "gpt-4o-mini",
  "gpt-4.5-preview": "gpt-4.5-preview",
  "deepseek-chat": "deepseek-chat",
  "gemini-2.0-flash": "gemini-2.0-flash"
}
```

`is_pro=true` 时上游不校验 `locked`，5 个模型都可用（`locked` 只是 App 侧展示用）。

### 1.4 生图

sidecar 暴露 `POST /v1/images/generations`（OpenAI 格式，返回 `b64_json`）。
new-api 侧用「图片」路由挂到同一渠道即可；单张约 **1.5–1.8 MB base64 / 17 s**，
注意 `body` 体积上限与前端超时。返回里 `url` 字段是 `data:image/png;base64,...`（不落盘、无外链）。

### 1.5 验证清单（逐条跑，别跳）

```bash
# 1) 模型列表
curl -s -H "Authorization: Bearer sk-aichat" http://127.0.0.1:8960/v1/models | jq '.data[].id'

# 2) 非流式
curl -s -X POST http://127.0.0.1:8960/v1/chat/completions \
  -H "Authorization: Bearer sk-aichat" -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"reply OK"}],"stream":false}' | jq '.choices[0].message'

# 3) 流式（SSE）
curl -sN -X POST http://127.0.0.1:8960/v1/chat/completions \
  -H "Authorization: Bearer sk-aichat" -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o-mini","messages":[{"role":"user","content":"count 1..5"}],"stream":true}' | head -5

# 4) 号池状态
curl -s -H "Authorization: Bearer sk-aichat" http://127.0.0.1:8960/admin/pool | jq '.stats,.accounts'

# 5) 生图（约 17 s）
curl -s -X POST http://127.0.0.1:8960/v1/images/generations \
  -H "Authorization: Bearer sk-aichat" -H "Content-Type: application/json" \
  -d '{"prompt":"a red apple","response_format":"b64_json"}' | jq '.data[0].b64_json' | head -c 80
```

验收标准：1) 5 个 id；2) `content` 非空且**不含** `outdated version` 字样；3) 能收到 `[DONE]`；
4) `stats.served` 递增、`minted ≥ 1`；5) 返回以 `iVBORw0KGgo` 开头。

---

## 2. 形态 B：原生 Go 渠道（长期）

### 2.1 渠道类型登记

new-api 现有常量集中在 `constant/channel.go`。挑一个未占用值（截止本仓库写作时 73 已被 duck.ai 占用）：

```go
ChannelTypeAIChat1428 = 74   // 名称建议：AI Chat 1.4.28
```

三处必改（照现有渠道抄）：

1. `constant/channel.go`：新增类型常量 + `ChannelType2String` 映射 + 默认 base url
2. `relay/channel/aichat1428/`：新建适配器（`adaptor.go` / `dto.go` / `constant.go`）
3. `relay/relay_adaptor.go`：`GetAdaptor()` 里把新类型接进 switch

### 2.2 适配器必须处理的 6 个上游特性

| # | 上游行为 | 适配器怎么写 |
|---|---|---|
| 1 | **非 SSE**：`POST /v1/chat/send` 一次性返回完整 JSON | 客户端要 `stream=true` 时，由适配器把全文按 20–40 字符切块转成 OpenAI SSE chunk，最后补 `finish_reason:"stop"` + `[DONE]` |
| 2 | **HTTP 200 里带业务失败**：`{"success":false,"reason":"LIMIT"}` | 必须解析 `success`；`LIMIT` → 映射成额度/限流错误（建议 429 + `insufficient_quota`），其余 → 500 `upstream_error` |
| 3 | 正文尾部追加升级横幅：`\n\n---\n⚠️ You are using an outdated version...` | 出参前正则剥离（见 `lib/aichat_client.py: strip_promo`） |
| 4 | 鉴权是 **Firebase idToken**，1 小时过期 | Go 侧实现「匿名 signUp + refresh + 池化」；或继续让 sidecar 负责凭证，Go 只做协议转换 |
| 5 | 请求体字段 `isPro` 决定额度是否生效 | 渠道配置里放 `is_pro` 开关，默认 true；设 false 时必须配号池轮换，否则 5 条就断 |
| 6 | 生图返回 base64（`embedded_img`），单张 ~1.7 MB | 图片路由直接回写 b64，别塞日志与数据库 |

### 2.3 请求/响应映射表

**文本**（OpenAI → 上游）

```
POST {base}/chat/send
Header: Authorization: Bearer <firebase idToken>
{
  "messages": [{"role":"user|assistant|system","content":"..."}],  // 原样透传
  "isPro":    true,                                                 // 渠道配置
  "model":    <映射后的上游 id>,
  "customer": null
}
```

响应映射：

```
上游 {"success":true,"message":{"id","created","role":"assistant","status":"success",
        "type":"text","content":"..."},"limits":{"left":N,"max":5}}
→  OpenAI {"id":"chatcmpl-<上游 id>","object":"chat.completion","created":<ms/1000>,
           "model":<请求模型>,"choices":[{"index":0,
           "message":{"role":"assistant","content":"<剥掉横幅的正文>"},
           "finish_reason":"stop"}],
           "usage":{"prompt_tokens":估算,"completion_tokens":估算,"total_tokens":估算}}
```

上游**不返回 usage**，只能估算（`len/4` 或接 tiktoken）。计费口径建议：按模型倍率 × 估算 token，或在 new-api 用「固定价格」模式。

**生图**

```
POST {base}/chat/image   {"isPro":true,"model":"gpt-4o-mini","prompt":"...","customer":null}
→ {"success":true,"message":{"type":"image","content":"<原 prompt>",
     "embedded_img":"<base64 png>","id":"...","created":...}}
```

### 2.4 建议的目录结构

```
relay/channel/aichat1428/
├── adaptor.go        # ConvertRequest / DoRequest / DoResponse
├── dto.go            # 上游请求响应结构体
├── constant.go       # base url、模型表、is_pro 开关
└── adaptor_test.go   # 用 testdata/*.json 跑转换单测（不打网络）
```

单测至少覆盖：正常 200 / `success:false` LIMIT / 带横幅正文剥离 / SSE 切块拼接 / 未知 model 回落 / 上游 5xx。

### 2.5 其它可复用端点（可选增强）

| 端点 | 用途 |
|---|---|
| `GET /chat/models?lang=en` | 渠道模型列表动态同步（含 `locked` 标记） |
| `GET /chat/get-limit?special=` | 剩余额度查询，可做渠道健康度 |
| `POST /chat/make-name` | 会话自动命名（可做 `/v1/chat/title` 之类扩展） |

---

## 3. 额度与号池（两条形态通用）

| 模式 | 行为 | 何时用 |
|---|---|---|
| `is_pro=true`（默认） | 单身份无限量；号池只需 1 个号 | 当前上游实现下最快最稳 |
| `is_pro=false` | 每号 5 条，耗尽自动换号 | 上游一旦补上 `isPro` 校验，切这个 |

环境变量：

```bash
AICHAT_IS_PRO=1        # 1=无限量模式(默认) 0=按号轮换
AICHAT_POOL_SIZE=1     # 无限量模式 1 就够；轮换模式建议 5~10
AICHAT_MINT_INTERVAL=0.35   # 两次铸造最小间隔(秒)，防 IDP 限流
AICHAT_THREADS=256     # 网关 anyio 线程池
```

号池行为（`lib/account_pool.py`，已单测 10/10 + 实网 24/24 验证）：

- 按需铸造 / 后台补充（每 20 s 巡检一次）
- `token` 过期自动 refresh（uid 与剩余额度不变）
- 额度耗尽（`LIMIT` 或 `left<=0`）→ 立刻换号，不浪费一次调用
- 401/403 → 判死号并补新号
- 铸造失败指数退避（1→2→4…30 s 上限），不会把 IDP 打挂
- 状态落盘 `data/accounts.json`；**单进程独占**，不要两个进程共用同一个 store

---

## 4. 已知坑（照抄这段能省半天）

1. **上游不是流式**。任何"提升首字延迟"的期待都不成立，用户体感=等 1–3 s 后一次性吐字。
2. **`LIMIT` 也是 HTTP 200**。如果只看 HTTP 状态码，会把「额度耗尽」当成功。
3. **正文带营销尾巴**。不剥离会污染对话历史、污染计费 token 估算。
4. **长尾延迟**。观测到约 10% 请求 18–20 s；客户端超时必须 ≥180 s，否则误判失败。
5. **本机出口是单代理**。实验环境 `HTTP(S)_PROXY=127.0.0.1:10808`；并发 >8 代理先崩，
   报错是 `ConnectionResetError / SSLEOFError`，**不是上游风控**。生产上量要先上出口池。
6. **模型自述不可信**。上游对 "what model are you" 的回答与 id 不一致，别拿它做甄别依据。
7. **`customer` 字段可传 null**。传 RevenueCat `customerInfo` 时才透传，接渠道时不用管。
8. **未知 model 不报错**。上游会静默回落到默认模型，所以渠道侧模型名校验要自己做。

---

## 5. 交付物索引

| 文件 | 作用 |
|---|---|
| `lib/aichat_client.py` | 上游协议客户端（铸造/刷新/chat/image/models/限额/横幅剥离） |
| `lib/account_pool.py` | 号池（铸造、轮换、淘汰、持久化、退避、后台补充） |
| `lib/config.py` | 凭证配置（env / config.local.json，仓库内不含密钥） |
| `demo_server.py` | OpenAI 兼容网关（形态 A 的全部内容） |
| `static/demo.html` | 可视化 demo（对话/SSE/生图/池状态） |
| `stress_test.py` | 10 个功能场景压测 |
| `load_test.py` | A~F 六阶段压力与风控探测 |
| `probe_capacity.py` | 本机出口并发上限探测 |
| `verify_browser.py` | Playwright 真实浏览器验证 |
| `test_account_pool.py` | 号池单测（10 offline + 1 live） |
| `docs/PROTOCOL.md` | 协议全量还原 + 自己提取凭证的方法 |
| `docs/LOAD-TEST-R2.md` | 压测数据与风控结论 |
| `docs/ACCOUNT-POOL.md` | 号池设计与运维 |
