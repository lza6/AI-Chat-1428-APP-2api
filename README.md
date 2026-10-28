# AI Chat 1.4.28 → 2API

`com.appstation.chatgpt`（Expo + React Native + Hermes + Firebase）逆向 → OpenAI 兼容网关。
含 **匿名凭证自动铸造 + 号池自动轮换**、demo 面板、功能压测、大规模压力/风控探测、浏览器端到端验证。

> **纯协议实现**：不需要安装 App、不需要真机/模拟器、不需要 Frida、不需要抓包。
> 全部能力都是 HTTP 调用，凭证由 Firebase 匿名登录接口凭空铸造。

---

## 结论摘要

| 维度 | 结果 |
|---|---|
| 上游 | `https://chatadmin.org/gd-api/v1` |
| 鉴权 | Firebase 匿名登录 idToken（`Authorization: Bearer <idToken>`），**无签名 / 无设备指纹 / 无 SSL Pinning** |
| 凭证来源 | `identitytoolkit accounts:signUp` 空 body → 新 uid + idToken，**每次独立 5 条额度** |
| 免费额度 | 5 条 / uid，耗尽返回 `{"success":false,"reason":"LIMIT"}`（**HTTP 仍是 200**） |
| **授权缺陷** | `isPro` 由请求体自报、服务端不校验 → `isPro:true` 时计数转负仍继续服务 = **无限量** + 解锁全部 5 个模型 |
| 模型 | `gpt-4o-mini` / `gpt-4o` / `gpt-4.5-preview` / `deepseek-chat` / `gemini-2.0-flash` |
| 生图 | `POST /chat/image` → base64 PNG（1024×1024，约 17 s / 1.7 MB） |
| 传输 | 纯 JSON，**非 SSE**（流式由网关切块回放）；正文尾部带营销横幅需剥离 |
| 可用性 | 可用。R1 功能压测 10/10、浏览器 3 条真实路径全通；R2 上游 261/261（4.29 rps） |

## 与同名的「Starnest AI Chat」不是一回事

| | Starnest v133/142/128 | **本样本 1.4.28** |
|---|---|---|
| 包名 | `starnest.aichat.aichatbot.assistant` | `com.appstation.chatgpt` |
| 域名 | `api-aichat.starnestsolution.com` | `chatadmin.org` |
| 鉴权 | `Signature kid=...` + 自签 JWT | Firebase 匿名 idToken |
| 状态 | 全端点 401，死渠道 | 可用 |

---

## 快速开始

```bash
pip install -r requirements.txt

# 1) 从 APK 提取 Firebase 三件套，写 config.local.json（见 docs/PROTOCOL.md §0）
cp config.example.json config.local.json && vi config.local.json

# 2) 起网关（默认 is_pro=true 单身份无限量模式）
python demo_server.py --port 8960
#    -> http://127.0.0.1:8960   （demo 面板）
#    -> http://127.0.0.1:8960/health
#    -> http://127.0.0.1:8960/admin/pool

# 3) 无网/离线验证网关链路（本地回包，不打死上游）
AICHAT_MODE=mock python demo_server.py --port 8961

# 4) 自测
python test_account_pool.py          # 号池 10 项单测
python stress_test.py                # 10 个功能场景
python verify_browser.py             # Playwright 真实浏览器 3 条路径
python load_test.py --target upstream --stages C --soak-concurrency 5 --soak-seconds 60
```

## OpenAI 兼容接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/v1/models` | 上游实时模型表（5 min 缓存） |
| POST | `/v1/chat/completions` | `stream=false/true` 都支持（流式为切块回放） |
| POST | `/v1/images/generations` | 返回 `b64_json`（或 `url` = data URL） |
| GET | `/health` | 模式、池大小、成功/失败、p50 |
| GET | `/admin/pool` | 号池快照（脱敏，不含 token） |
| POST | `/admin/warm?n=3` | 预热账号 |

```bash
curl -s http://127.0.0.1:8960/v1/chat/completions \
  -H "Authorization: Bearer sk-any" -H "Content-Type: application/json" \
  -d '{"model":"gpt-4o","messages":[{"role":"user","content":"hi"}],"stream":false}'
```

## 号池（自动新号 / 自动轮换）

| 模式 | 行为 | 用途 |
|---|---|---|
| `AICHAT_IS_PRO=1`（默认） | 单身份无限量，池只需 1 个号 | 当前上游下最快最稳 |
| `AICHAT_IS_PRO=0` | 每号 5 条，耗尽自动换号 + 后台补足 | 上游一旦收紧 `isPro` 即切此模式 |

能力：按需铸造、后台补充、`idToken` 过期自动 refresh（uid 与额度不变）、额度耗尽即时淘汰、
401/403 判死补号、铸造指数退避、状态落盘 `data/accounts.json`。

实测：离线单测 **10/10**；实网 `--live` **11/11**（`is_pro=false` 连打 8 次 ok=8/8，自动轮换 1 次）；
网关 rotate 模式 **24/24 全部 200**（`minted=6 / exhausted=3 / rotations=3`）。

细节见 [`docs/ACCOUNT-POOL.md`](docs/ACCOUNT-POOL.md)。

---

## 文档

| 文件 | 内容 |
|---|---|
| [`docs/PROTOCOL.md`](docs/PROTOCOL.md) | 协议全量还原、端点表、Hermes 逆向技巧、凭证自提取 |
| [`docs/NEW-API-INTEGRATION.md`](docs/NEW-API-INTEGRATION.md) | **对接 new-api 的完整操作指南**（sidecar / 原生 Go 两种形态） |
| [`docs/LOAD-TEST-R2.md`](docs/LOAD-TEST-R2.md) | 六阶段压测数据、出口上限、风控结论 |
| [`docs/ACCOUNT-POOL.md`](docs/ACCOUNT-POOL.md) | 号池状态机、不变量、运维手册 |

## 目录

```
lib/aichat_client.py     上游协议客户端（铸造/刷新/chat/image/models/限额/横幅剥离）
lib/account_pool.py      匿名号池（铸造/轮换/淘汰/持久化/退避/后台补充）
lib/config.py            凭证配置（env 或 config.local.json；仓库内不含密钥）
demo_server.py           OpenAI 兼容网关（FastAPI）
static/demo.html         单页 demo：对话 / SSE / 生图 / 池状态
stress_test.py           10 个功能场景（S1–S10）
load_test.py             A–F 六阶段压力与风控探测（含错误率安全阀）
probe_capacity.py        本机出口并发上限探测
verify_browser.py        Playwright 真实浏览器验证 + 截图
test_account_pool.py     号池单测（10 offline + 1 live）
tools/run_decomp.py      Hermes HBC 反编译入口（内置 SHA-1 校验跳过补丁）
docs/                    协议 / 对接 / 压测 / 号池
report/                  压测结果 JSON、浏览器截图、逆向笔记
```

## 已知边界

- 上游 `chat/send` **非流式**，网关 SSE 是对已完成文本的切块回放，不是真 token 流
- 约 10% 请求有 18–20 s 长尾，客户端超时需 ≥ 180 s
- 测试环境出口是单代理（`127.0.0.1:10808`）：并发 ≤5 正常，8 劣化，10 即崩，80 打爆
  → **上游真实 QPS 上限未测到**，上量必须先池化出口
- 上游对模型自述不可信（问"你是谁"与 id 不一致），模型真伪未做语料甄别

## 声明

本项目为**协议兼容性研究与互操作实验**产物，不含 APK、不含上游密钥（凭证需自行从样本提取）。
`isPro` 绕过属于对上游服务条款的违反，仅供本机研究验证；请勿用于对外提供服务。
使用者需自行承担合规与法律风险。
