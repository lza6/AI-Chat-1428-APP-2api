# 压测与风控探测（R2）

工具：`load_test.py`（A–F 六阶段）、`probe_capacity.py`（出口上限）
原始数据：`report/load_*.json`、`report/proxy_capacity.json`

---

## 1. 上游直连（绕过我方网关，单 uid，`isPro=true`）

| 阶段 | 请求 | 成功 | p50 | p99 | 耗时 |
|---|---|---|---|---|---|
| A 基线 1 rps × 30 s | 30 | 30 (100%) | 3.14 s | 34.9 s | 238.5 s |
| B 扫点 C=20 × 2 | 40 | 40 (100%) | 3.25 s | 19.0 s | ~20 s |
| B 扫点 C=40 × 2 | 80 | 80 (100%) | 3.25 s | 19.0 s | 42.9 s |
| C soak C=5 × 60 s | **261** | **261 (100%)** | 1.06 s | 5.05 s | 60.9 s → **4.29 rps** |
| E 单 uid 5 并发 | 5 | 5 (100%) | 2.45 s | 3.38 s | 4.9 s |
| F 10 轮上下文 | 10 | 10 (100%) | 1.13 s | 2.90 s | 13.8 s |
| D Firebase 铸造 × 10（间隔 1.5 s） | 10 | 10 (100%) | 0.73 s | 1.57 s | 23.3 s |

观察：

- **零 429、零封禁、零异常 LIMIT**；单 uid 5 并发正常，计数穿透到负数仍 200
- 长上下文 10 轮无截断、无掉速
- 铸造闸门 10 连击 p50 仅 0.73 s —— 早期观测到的 18 s 抖动是**冷 TLS 握手 + 本地代理**，不是 IDP 限流
- 连接复用（keep-alive）把 p50 从 3.1 s 压到 1.06 s → 上游本身很快，首轮数字被握手拖累

## 2. 网关层（经 `demo_server.py`，OpenAI 协议）

| 阶段 | 请求 | 成功 | p50 | p99 | 吞吐 |
|---|---|---|---|---|---|
| C soak C=4 × 60 s | 125 | 125 (100%) | 1.92 s | 2.88 s | 2.03 rps |
| B 突发 C=20 | 20 | 20 (100%) | 1.98 s | 2.16 s | 9.27 rps |
| 另一轮 C=5（脏数据，含 5×502） | 48 | 43 (89.6%) | 1.98 s | 112.6 s | — |

- 网关开销 ≈ **+0.9 s**（p50 1.92 vs 1.06）：JSON 解析 + 字段映射 + anyio 线程调度，可接受
- 那 5 个 502 全部是 `ConnectionResetError(10054)`（**本地代理重置**），网关如实回传原因，
  没有掩盖成 200 —— 错误分级正确

## 3. 出口并发上限（`GET /chat/categories`，廉价请求，排除推理耗时）

| 并发 | 成功 | p50 | p95 | 判定 |
|---|---|---|---|---|
| 1 | 1/1 | 2088 ms | 2088 ms | ok |
| 2 | 2/2 | 2877 ms | 2877 ms | ok |
| 3 | 3/3 | 2050 ms | 2123 ms | ok |
| 5 | 5/5 | 1955 ms | 1994 ms | ok |
| 8 | 8/8 | 4436 ms | 6985 ms | 开始排队劣化 |
| 10 | **1/10** | 6932 ms | – | `SSLError MaxRetryError`，出口崩 |
| 80 | **0/56** | 49879 ms | – | 彻底打爆，恢复需数分钟 |

**本轮压测真正的天花板是本机单代理出口 `127.0.0.1:10808`，不是上游风控。**

---

## 4. 结论

1. 上游未见 IP 维度限流/封禁：单出口 4.29 rps × 60 s、累计约 600 请求无异常。
   **但没测到真正上限**，因为出口先崩 → 要拿真实阈值必须多出口。
2. 上游未见单身份并发限制：同 uid 5 并发、计数转负仍全 200。
3. `isPro` 绕过在大规模下依然成立：261 次持续请求，`reason` 从未变 LIMIT。
4. 失败模式单一：全部是本地代理 `ConnectionResetError / SSLEOFError`，上游 5xx/429 一次都没有。
5. 成本口径：单出口可持续 ≈ 2 rps（网关）/ 4.3 rps（直连）；**上量必须先池化出口**。

## 5. 复现

```bash
# 上游直连：基线 + 并发扫点
python load_test.py --target upstream --stages A,B --concurrency 20,40 --per-worker 2

# 上游直连：60 s soak + 单 uid 扇出 + 长上下文
python load_test.py --target upstream --stages C,E,F --soak-concurrency 5 --soak-seconds 60

# Firebase 铸造闸门
python load_test.py --target idp --stages D --idp-count 10 --idp-gap 1.5

# 网关层
python load_test.py --target gateway --base http://127.0.0.1:8960 --stages C,B \
    --soak-concurrency 4 --soak-seconds 60 --concurrency 20 --per-worker 1

# 出口上限
python probe_capacity.py --concurrency 1,2,3,5,8 --gap 12
```

> `load_test.py` 内置安全阀：任一阶段连续 20 次里错误率 > 25% 立即中止该阶段。
