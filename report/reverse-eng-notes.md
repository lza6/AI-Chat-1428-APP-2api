# 逆向笔记 — AI Chat 1.4.28

记录「怎么找到的」，方便以后同类 React Native / Hermes 应用复用。

## 1. 包结构判断（30 秒定性）

```bash
python - <<'PY'
import zipfile
z = zipfile.ZipFile('app.apk')
[print(n, z.getinfo(n).file_size) for n in z.namelist()
 if n.endswith('.dex') or n.startswith('assets/')]
PY
```

- 出现 `assets/index.android.bundle` → **React Native / Hermes**，业务逻辑不在 dex 里
- 本例 dex 合计 48.7 MB，但全是 RN/Expo/Firebase 壳（`libhermes.so`、`libreactnative.so`、`libpairipcore.so`）
- 判 Hermes：前 4 字节 == `c61fbc03`（HBC magic），版本字段 96

## 2. hermes-dec 的 SHA-1 坑

直接跑 `hbc-decompiler` 会挂：

```
AssertionError  (hbc_file_parser.py:649, read_header_from_buffer)
  assert sha1(file_data[:-20]).digest() == file_data[-20:]
```

原因：mod 版改写过 bundle，尾部哈希没跟着更新。修法见 `tools/run_decomp.py` ——
让 `sha1()` 返回一个 `digest()` 恒等的对象，断言即通过。

## 3. 定位 API 层

```bash
grep -n "gd-api" out.js
# 353754 / 353932 / 354114 : base = https://chatadmin.org/gd-api/
# 354323 : /v1/chat/make-name      354689 : /v1/chat/reward
# 354892 : /v1/chat/send           355343 : /v1/chat/image
# 859549 : /v1/user/sync-data
```

## 4. 关键反编译片段（伪 JS，含语义注释）

```js
// ---- 鉴权：Firebase 匿名登录，无任何自定义签名 ----
r1 = getAuth().currentUser;
if (currentUser == null) { idToken = signInAnonymously().user.getIdToken(); }
else                      { idToken = currentUser.getIdToken(); }

// ---- POST /v1/chat/send ----
headers = { 'Content-Type': 'application/json',
            'Authorization': 'Bearer ' + idToken };
body    = { messages: messages,   // [{role:'user'|'assistant'|'system', content}]
            isPro:    isPro,      // ← 客户端自报，服务端不校验
            model:    model,      // 'gpt-4o-mini' 是客户端硬编码的默认值
            customer: customerInfo };  // RevenueCat CustomerInfo，可为 null
fetch('https://chatadmin.org/gd-api/v1/chat/send',
      {method:'POST', headers, body: JSON.stringify(body)})
  .then(r => r.json())
  .then(r => {
     if (!r.success) {
       if (r.reason === 'LIMIT') throw new LimitError();   // 业务码，HTTP 仍是 200
       throw new Error('Error');
     }
     return r.message;  // {id, created, role:'assistant', status:'success', type:'text', content}
  });

// ---- GET /v1/chat/models?lang=<lang>            模型表（含 locked 标记）----
// ---- GET /v1/chat/get-limit?special=<''|'ask-review'>   剩余额度 ----
// ---- POST /v1/chat/image {isPro, model, prompt, customer}
//          -> message.embedded_img = base64 PNG ----
```

## 5. 为什么不用碰 dex / 模拟器 / Frida

Firebase 参数全在 `res/values/strings.xml`（`google_api_key` / `google_app_id` / `project_id`），
协议里没有任何 native 参与（无 HMAC、无 device-attestation、无 Play Integrity、无 SSL Pinning），
因此纯 HTTP 即可完整复现客户端行为。

## 6. 踩过的坑

| 现象 | 真因 |
|---|---|
| `{"success":false,"reason":"LIMIT"}` 但 HTTP 200 | 业务码在 body 里，必须解析 `success` |
| 正文尾部突然多出 `⚠️ outdated version` | 服务端注入的升级横幅，出参前剥离 |
| 铸造偶发 18 s | 不是 IDP 限流，是冷 TLS 握手 + 本地代理；keep-alive 复用后 p50 0.73 s |
| 80 并发全挂 `SSLEOFError` | 本地单代理被打爆，不是上游风控（5 正常 / 8 劣化 / 10 崩） |
| 问「你是什么模型」答案与 id 不一致 | 上游模型自述不可信，别拿来做甄别 |
