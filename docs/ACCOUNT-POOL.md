# 匿名号池（Account Pool）

文件：`lib/account_pool.py` · 测试：`test_account_pool.py`

上游每个 Firebase 匿名 uid 自带 **5 条**免费额度（`limits.max=5`）。
是否需要号池，取决于客户端自报的 `isPro`：

| 模式 | 上游行为 | 号池需求 |
|---|---|---|
| `is_pro=true`（默认） | 计数照减但**不拦截** | 1 个号即可，永久复用 |
| `is_pro=false` | 5 条后 `reason:"LIMIT"` | 必须轮换：每号 5 条，用完即换 |

---

## 1. 状态机

```
                ┌────────── mint ──────────┐
                ↓                          │
            ┌───────┐   第 1 次成功    ┌────────┐
            │ fresh │ ───────────────→ │ active │
            └───────┘                  └────────┘
                │                        │    │
                │  LIMIT / left<=0       │    │ 401/403
                ↓                        ↓    ↓
          ┌───────────┐            ┌──────────┐
          │ exhausted │            │   dead   │
          └───────────┘            └──────────┘
                └────── 被移除，补新号 ──────┘
```

- `fresh` 刚铸造未用；`active` 用过且仍有额度；`exhausted` 额度耗尽；`dead` 凭证不可用
- 只有 `fresh/active` 会被分配（`_USABLE`）

## 2. 关键不变量

1. **过期自动续**：`idToken` 寿命 1 h，`expires_at - 300` 时走 refresh；refresh 保持 uid，
   因此**剩余额度不丢**。refresh 失败 → 判死号并铸新号。
2. **不浪费调用**：`report_ok` 把 `limits.left` 记进账号；下次分配直接跳过 `left<=0` 的号，
   不会再用一个必然 `LIMIT` 的号去撞墙（这是实网从 7/8 提到 8/8 的那一处修复）。
3. **均衡烧额度**：`is_pro=false` 时按 `(used, created_at)` 升序挑，让多个号同步耗尽；
   `is_pro=true` 时固定复用最早的号（单身份策略）。
4. **铸造限速 + 退避**：两次铸造间隔 ≥ `min_interval`；连续失败按 1→2→4→…→30 s 指数退避，
   不会把 identitytoolkit 打挂。
5. **持久化**：状态写 `data/accounts.json`（原子替换）。重启后复用未耗尽的号，不重新铸造。
   **单进程独占**该文件，不要多进程共用。

## 3. 对外接口

```python
pool = AccountPool(store=Path("data/accounts.json"), size=3, is_pro=True)

acc   = pool.acquire()              # 取号（池空则现铸）
pool.report_ok(acc, left)           # 成功，回写剩余额度
pool.report_limit(acc)              # LIMIT -> 淘汰 + 计数轮换
pool.report_dead(acc, err)          # 401/403 -> 判死
pool.fill(5)                        # 预热到 5 个可用
pool.snapshot()                     # 运维快照（不含明文 token）
```

网关侧对应 HTTP 端点：

| 端点 | 作用 |
|---|---|
| `GET /admin/pool` | 快照：模式、可用数、每号 `used/quota_left/age/expires`、累计统计 |
| `POST /admin/warm?n=3` | 预热 3 个号（把铸造延迟提前到用户请求之前） |

`snapshot()` 只输出 `uid` 前 6 位 + 状态 + 计数，**不吐明文 idToken / refreshToken**。

## 4. 配置

| 环境变量 | 默认 | 说明 |
|---|---|---|
| `AICHAT_IS_PRO` | `1` | `1`=无限量模式；`0`=按号轮换 |
| `AICHAT_POOL_SIZE` | `1`（is_pro=1）/ `5`（is_pro=0） | 目标可用号数 |
| `AICHAT_MINT_INTERVAL` | `0.35` | 两次铸造最小间隔（秒） |

## 5. 验证证据

**离线单测（假 IDP，不联网）10/10 通过**

```
[PASS] 按需铸造
[PASS] 额度耗尽自动换新号 · uid-1 -> uid-2 -> uid-3, rotations=2, minted=3
[PASS] 最少使用优先（均衡烧额度） · 首个=uid-1, 第二个=uid-2
[PASS] isPro 模式复用单一身份 · distinct=1
[PASS] 重启后账号不丢 · usable=1 minted=1
[PASS] token 过期走 refresh（uid 与额度不变） · uid 相同=True, refreshed=1
[PASS] refresh 失败自动补新号 · 死号=uid-1 -> 新号=uid-2
[PASS] 铸造失败抛 PoolEmpty + 记录失败次数 · mint_fail=1, backoff=2.0
[PASS] 并发 8×20 无崩、无死锁 · served=160
[PASS] 快照字段完整（无明文 token）
```

**实网（真上游）**

- `python test_account_pool.py --live` → **11/11 PASS**，其中
  `LIVE is_pro=false 自动换号连打 8 次 · ok=8/8 rotated=1 distinct_uid=2`
- 网关 rotate 模式（`AICHAT_IS_PRO=0 AICHAT_POOL_SIZE=3`）：
  **24/24 请求全部 200**，累计 `minted=6 / exhausted=3 / rotations=3`，池始终维持 3 个可用号

## 6. 运维建议

- **生产先用 `is_pro=true` + `POOL_SIZE=1`**：最少铸造、最低延迟、最少暴露面
- 一旦上游补上 `isPro` 校验（表现为 `reason:"LIMIT"` 频繁出现），切 `AICHAT_IS_PRO=0`，
  `POOL_SIZE` 提到 5–10，并预热：`POST /admin/warm?n=10`
- 监控 `stats.dead` 突增 → 多半是上游换了 Firebase 项目/禁用了匿名登录
- 监控 `stats.mint_fail` 突增 → identitytoolkit 被限流，需要换出口或降速
