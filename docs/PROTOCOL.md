# 协议全量还原 — AI Chat 1.4.28

样本：`AI Chat 1.4.28` · 包名 `com.appstation.chatgpt` · versionCode 31 · compileSdk 35
技术栈：Expo SDK 52 + React Native（newArch）+ **Hermes 字节码** + Firebase（Auth/Messaging/Analytics）
上游：`https://chatadmin.org/gd-api/v1`

---

## §0 自己提取凭证（仓库内不含密钥）

```bash
# 1) 解包（只看资源，不反编译 dex）
apktool d -s -o out "AI Chat_1.4.28.apk"

# 2) Firebase 三件套就在资源里
grep -E 'google_api_key|google_app_id|project_id|gcm_defaultSenderId|google_storage_bucket' \
     out/res/values/strings.xml
#   <string name="google_api_key">AIza...</string>
#   <string name="google_app_id">1:000000000000:android:xxxxxxxx</string>
#   <string name="project_id">xxxx-xxxx-droid</string>

# 3) 写进 config.local.json（git 忽略）
cat > config.local.json <<'JSON'
{
  "firebase_api_key": "AIza...",
  "firebase_app_id": "1:000000000000:android:xxxxxxxx",
  "firebase_project": "xxxx-xxxx-droid"
}
JSON
```

这些值是 **Firebase Web API Key 与 App ID**，`google_api_key` 是客户端公开标识（Firebase 设计上如此），
本仓库仍选择不内联，避免把别人的项目标识当成本仓库资产。

业务逻辑（端点、鉴权链、字段）全在 `assets/index.android.bundle`（6.85 MB Hermes HBC v96）。
反编译见 §6。

---

## §1 鉴权链

```
Firebase 匿名登录                        应用层
─────────────────                       ────────────────
POST identitytoolkit accounts:signUp    用 idToken 作 Bearer
  ?key=<firebase_api_key>               打 chatadmin.org
  {"returnSecureToken": true}
        ↓
  {idToken, refreshToken, localId(uid), expiresIn:"3600"}
```

- **没有任何自定义签名**：无 HMAC、无时间戳签名、无设备指纹、无 `X-Sign`、无证书绑定、无 SSL Pinning
- App 侧链路：`getAuth().currentUser.getIdToken()`，未登录则 `signInAnonymously()`（反编译 `L354630`、`L353905` 一带的 asyncThunk）

### 1.1 铸造

```http
POST https://identitytoolkit.googleapis.com/v1/accounts:signUp?key=<api_key>
Content-Type: application/json

{"returnSecureToken": true}
```

```json
{"kind":"identitytoolkit#SignupNewUserResponse",
 "idToken":"eyJhbGciOiJSUzI1NiIs...",
 "refreshToken":"AMf-...",
 "expiresIn":"3600",
 "localId":"<uid>"}
```

每次调用 = **新 uid** = **独立 5 条免费额度**。

### 1.2 续期（uid 不变，额度不重置）

```http
POST https://securetoken.googleapis.com/v1/token?key=<api_key>
Content-Type: application/x-www-form-urlencoded

grant_type=refresh_token&refresh_token=<refreshToken>
→ {"id_token":"...","refresh_token":"...","user_id":"<同一个 uid>","expires_in":"3600"}
```

---

## §2 端点全表（base = `https://chatadmin.org/gd-api/v1`）

| 方法 | 路径 | 请求 | 响应要点 |
|---|---|---|---|
| GET | `/chat/models?lang=en` | – | `data[]`: `{id, name, locked, image_day, image_night}` |
| GET | `/chat/categories?lang=en&ver=2` | – | 分类 + 内置提示词模板 |
| GET | `/chat/get-limit?special=` | – | `{"success":true,"limit":{"left":5,"full":5}}` |
| POST | `/chat/send` | `{messages, isPro, model, customer}` | 见 §3 |
| POST | `/chat/image` | `{isPro, model, prompt, customer}` | `message.embedded_img` = base64 PNG |
| POST | `/chat/reward` | `{user}` | 激励视频加次数；未接广告 SDK 时 `limits:{null,null}` |
| POST | `/chat/make-name` | `{language, userMessage, assistantMessage}` | `{"success":true,"name":"..."}` |
| POST | `/chat/report` | `{id}` | 举报消息 |
| POST | `/user/sync-data` | `{time_offset, timezone, pushToken, language, platform, customerInfo}` | 用户态同步（RevenueCat 透传） |

所有请求统一头：

```http
Content-Type: application/json
Authorization: Bearer <firebase idToken>
```

---

## §3 对话协议

### 3.1 请求

```http
POST /chat/send
{
  "messages": [
    {"role": "system",    "content": "You are ..."},
    {"role": "user",      "content": "hi"},
    {"role": "assistant", "content": "hello"},
    {"role": "user",      "content": "who are we"}
  ],
  "isPro":    true,
  "model":    "gpt-4o",
  "customer": null
}
```

- `messages` 三角色全支持；服务端**不存上下文**，多轮必须回放全量历史（实测 10 轮无截断）
- `customer` 可为 `null`；有 RevenueCat `customerInfo` 时才透传
- `isPro`：**客户端自报，服务端不校验**（详见 §4）

### 3.2 成功响应

```json
{"success": true,
 "message": {"id": "cade2c9b-...", "created": 1793226714960,
             "role": "assistant", "status": "success", "type": "text",
             "content": "Hello there, friend!"},
 "limits": {"left": 3, "max": 5}}
```

- `limits.left` 是**本次请求之后**的剩余条数
- `type` 取值：`text` / `image`
- 正文可能被追加营销尾巴（§3.4）

### 3.3 额度耗尽

```json
{"success": false, "reason": "LIMIT", "message": null}
```

**HTTP 状态码仍是 200**。只看状态码的客户端会把限流当成功。

### 3.4 营销尾巴（必须剥离）

```
<正文>

---
⚠️ You are using an outdated version of AI Chat. Update the app to get the newest AI models and a faster, more reliable chat: https://play.google.com/store/apps/details?id=com.appstation.chatgpt
```

正则（`lib/aichat_client.py: strip_promo`）：

```python
re.compile(r"\n*-{3,}\s*\n?\s*(?:⚠️|⚠)?\s*You are using an outdated version.*$", re.DOTALL | re.I)
```

---

## §4 授权缺陷：`isPro` 由客户端决定

| 同一 uid 的请求序列 | `isPro` | 结果 |
|---|---|---|
| 第 1–5 次 | `false` | 200，`left: 4→0` |
| 第 6 次 | `false` | 200，`reason: "LIMIT"` |
| 第 6–13 次 | `true` | **200 正常返回**，`left: -1 → -8` |
| 20 次连打 | `true` | 20/20 成功（`left` 至 -11） |

即：服务端把「是否付费」的授权判定完全托付给请求体；计数照减但**不拦截**。
`/chat/image` 同样成立。

附带结论：
- 未知 `model`（如 `not-a-model`）**不报错**，静默回落到默认模型 → 模型白名单校验也要自己做
- 单 uid 5 并发、单出口 4.29 rps 持续 60 s，全程无 429/封禁（详见 `LOAD-TEST-R2.md`）

---

## §5 文生图协议

```http
POST /chat/image
{"isPro": true, "model": "gpt-4o-mini", "prompt": "a red apple on a white table", "customer": null}
```

```json
{"success": true,
 "message": {"id": "...", "created": 1793227149764, "role": "assistant",
             "status": "success", "type": "image",
             "content": "a red apple on a white table",
             "embedded_img": "iVBORw0KGgoAAAANSUhEUgAABAAAAAQACAIAAADwf7zU..."}}
```

- `embedded_img` = 完整 base64 PNG（1024×1024，含 C2PA 元数据段），实测 **1.5–1.8 MB / 张、约 17 s**
- `content` 回显 prompt
- 模型名对生图基本无影响（服务端用自家图片管线）

---

## §6 Hermes 字节码逆向（关键技巧）

React Native 应用的业务逻辑不在 dex 里，而在 `assets/index.android.bundle`（Hermes HBC）。

```bash
pip install hermes-dec
# ⚠️ 常见失败：read_header_from_buffer 的 SHA-1 尾部校验 AssertionError
#    原因：mod 版 APK 的 bundle 构建后被改写过，尾哈希没跟着更新
python tools/run_decomp.py assets/index.android.bundle out.js   # 内置 SHA-1 跳过补丁
```

`run_decomp.py` 做的事：给 `hermes_dec.parsers.hbc_file_parser.sha1` 塞一个
`digest()` 返回「恒等对象」的假哈希，让 `assert sha1(...) == file_data[-20:]` 恒真。

反编译产物是**伪 JS**（33 MB），不要通读，用关键词定位：

```bash
grep -n "gd-api" out.js                 # 找 base url
grep -n "'Authorization'" out.js        # 找鉴权构造
grep -n "getIdToken\|signInAnonymously" out.js   # 找凭证来源
grep -n "createAsyncThunk" out.js       # Redux thunk = 一个端点
```

本次命中位置（可对照）：`L353400–355400`（chat 层 thunk 群）、`L859500`（sync-data）。

---

## §7 与「Starnest AI Chat」的区别（别搞混）

| | Starnest AI Chat v133/142/128 | **本样本 1.4.28** |
|---|---|---|
| 包名 | `starnest.aichat.aichatbot.assistant` | `com.appstation.chatgpt` |
| 域名 | `api-aichat.starnestsolution.com` | `chatadmin.org` |
| 鉴权 | `Signature kid=...&value=sha256(...)` + 自签 JWT | Firebase 匿名 idToken |
| 状态 | **全端点 401，死渠道** | 可用 |

两者只是名字都叫「AI Chat」，协议毫无关系。
