"""
Account pool tests.

    python test_account_pool.py            # offline (fake identity provider)
    python test_account_pool.py --live     # + real upstream: 8 calls in rotate mode
"""

from __future__ import annotations

import sys
import threading
import time
import traceback
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import aichat_client as ac  # noqa: E402
from account_pool import ACTIVE, DEAD, EXHAUSTED, AccountPool, PoolEmpty  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


class FakeIDP:
    """Deterministic stand-in for identitytoolkit; counts calls, no network."""

    def __init__(self) -> None:
        self.minted = 0
        self.refreshed = 0
        self.fail = False
        self._lock = threading.Lock()

    def signup(self) -> ac.AnonCredential:
        with self._lock:
            if self.fail:
                raise ac.AIChatError("transport failure: fake idp down")
            self.minted += 1
            n = self.minted
        return ac.AnonCredential(id_token=f"tok-{n}", refresh_token=f"ref-{n}",
                                 uid=f"uid-{n}", expires_at=time.time() + 3600)

    def refresh(self, cred: ac.AnonCredential) -> ac.AnonCredential:
        with self._lock:
            if self.fail:
                raise ac.AIChatError("refresh rejected")
            self.refreshed += 1
        return ac.AnonCredential(id_token=cred.id_token + "-r", refresh_token=cred.refresh_token,
                                 uid=cred.uid, expires_at=time.time() + 3600)


def new_pool(tmp: str, idp: FakeIDP, **kw) -> AccountPool:
    kw.setdefault("size", 1)
    kw.setdefault("is_pro", True)
    kw.setdefault("min_interval", 0.0)
    return AccountPool(store=Path(tmp), signup=idp.signup, refresh=idp.refresh,
                       autostart=False, **kw)


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, cond, detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" · {detail}" if detail else ""))


# --------------------------------------------------------------------------- #
def test_mint_on_demand(tmp: Path) -> None:
    idp = FakeIDP()
    pool = new_pool(str(tmp / "a.json"), idp)
    acc = pool.acquire()
    check("按需铸造", acc.uid == "uid-1" and idp.minted == 1 and pool.stats["minted"] == 1)


def test_limit_rotates(tmp: Path) -> None:
    idp = FakeIDP()
    pool = new_pool(str(tmp / "b.json"), idp, is_pro=False, size=2)
    a1 = pool.acquire()
    pool.report_ok(a1, 3)
    pool.report_limit(a1)
    a2 = pool.acquire()
    pool.report_ok(a2, 3)
    pool.report_limit(a2)
    a3 = pool.acquire()
    ok = a1.uid != a2.uid and a2.uid != a3.uid and pool.stats["rotations"] == 2
    check("额度耗尽自动换新号", ok,
          f"{a1.uid} -> {a2.uid} -> {a3.uid}, rotations={pool.stats['rotations']}, minted={idp.minted}")


def test_least_used_first(tmp: Path) -> None:
    idp = FakeIDP()
    pool = new_pool(str(tmp / "c.json"), idp, is_pro=False, size=3)
    pool.fill(3)
    a = pool.acquire()
    pool.report_ok(a, 4)          # uid-1 now used once
    b = pool.acquire()
    ok = b.uid != a.uid
    check("最少使用优先（均衡烧额度）", ok, f"首个={a.uid}, 第二个={b.uid}, minted={idp.minted}")


def test_is_pro_sticky(tmp: Path) -> None:
    idp = FakeIDP()
    pool = new_pool(str(tmp / "d.json"), idp, is_pro=True, size=3)
    pool.fill(3)
    uids = {pool.acquire().uid for _ in range(5)}
    check("isPro 模式复用单一身份", len(uids) == 1, f"distinct={len(uids)} minted={idp.minted}")


def test_persistence(tmp: Path) -> None:
    idp = FakeIDP()
    store = tmp / "e.json"
    pool = new_pool(str(store), idp)
    acc = pool.acquire()
    pool.report_ok(acc, 2)
    pool.stop()
    pool2 = new_pool(str(store), idp)
    snap = pool2.snapshot()
    ok = snap["usable"] == 1 and snap["accounts"][0]["quota_left"] == 2 and idp.minted == 1
    check("重启后账号不丢", ok, f"usable={snap['usable']} minted={idp.minted}")


def test_expiry_refresh(tmp: Path) -> None:
    idp = FakeIDP()
    pool = new_pool(str(tmp / "f.json"), idp)
    acc = pool.acquire()
    acc.expires_at = time.time() - 10  # force expiry
    again = pool.acquire()
    ok = again.uid == acc.uid and pool.stats["refreshed"] == 1 and idp.minted == 1
    check("token 过期走 refresh（uid 与额度不变）", ok,
          f"uid 相同={again.uid == acc.uid}, refreshed={pool.stats['refreshed']}, minted={idp.minted}")


def test_refresh_failure_handled(tmp: Path) -> None:
    idp = FakeIDP()
    pool = new_pool(str(tmp / "g2.json"), idp, size=2)
    acc = pool.acquire()
    acc.expires_at = time.time() - 10
    # only refresh fails, minting still works
    real_refresh = idp.refresh

    def boom(cred):
        raise ac.AIChatError("refresh rejected")

    pool._refresh = boom  # noqa: SLF001
    nxt = pool.acquire()
    ok = nxt.uid != acc.uid and pool.stats["dead"] == 1
    check("refresh 失败自动补新号", ok, f"死号={acc.uid} -> 新号={nxt.uid}, dead={pool.stats['dead']}")
    pool._refresh = real_refresh  # noqa: SLF001


def test_mint_failure_backoff(tmp: Path) -> None:
    idp = FakeIDP()
    idp.fail = True
    pool = new_pool(str(tmp / "h.json"), idp)
    raised = False
    try:
        pool.acquire()
    except PoolEmpty:
        raised = True
    check("铸造失败抛 PoolEmpty + 记录失败次数", raised and pool.stats["mint_fail"] == 1,
          f"mint_fail={pool.stats['mint_fail']}, backoff={pool._mint_backoff}")  # noqa: SLF001


def test_concurrency(tmp: Path) -> None:
    idp = FakeIDP()
    pool = new_pool(str(tmp / "i.json"), idp, is_pro=False, size=4)
    errors: list[str] = []

    def worker() -> None:
        for _ in range(20):
            try:
                acc = pool.acquire()
                pool.report_ok(acc, 3)
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    snap = pool.snapshot()
    ok = not errors and pool.stats["served"] == 160 and snap["usable"] >= 1
    check("并发 8×20 无崩、无死锁", ok,
          f"served={pool.stats['served']} usable={snap['usable']} minted={idp.minted} errors={errors[:2]}")


def test_snapshot_shape(tmp: Path) -> None:
    idp = FakeIDP()
    pool = new_pool(str(tmp / "j.json"), idp)
    pool.acquire()
    snap = pool.snapshot()
    need = {"mode", "strategy", "size_target", "usable", "accounts", "stats", "store"}
    ok = need <= set(snap) and set(snap["accounts"][0]) >= {"uid", "state", "used", "quota_left"}
    check("快照字段完整（无明文 token）", ok, f"keys={sorted(snap)[:7]}")


# --------------------------------------------------------------------------- #
def live_rotation() -> None:
    """Real upstream: is_pro=false, 8 calls, the pool must rotate past uid 1."""
    store = Path("data/_tests/live.json")
    store.unlink(missing_ok=True)
    pool = AccountPool(store=store, size=2, is_pro=False, autostart=False)
    uids, ok_count, failures = [], 0, []
    for i in range(8):
        try:
            acc = pool.acquire()
            uids.append(acc.uid)
            res = ac.send_chat(acc.id_token, [{"role": "user", "content": f"say {i}"}],
                               model="gpt-4o-mini", is_pro=False)
            pool.report_ok(acc, res.limits.get("left"))
            ok_count += 1
        except ac.QuotaExhausted:
            pool.report_limit(acc)
            uids.append("*rotated*")
        except Exception as exc:  # noqa: BLE001
            failures.append(repr(exc)[:80])
    snap = pool.snapshot()
    ok = ok_count >= 6 and len(set(u for u in uids if u != "*rotated*")) >= 2 and not failures
    check("LIVE is_pro=false 自动换号连打 8 次", ok,
          f"ok={ok_count}/8 rotated={snap['stats']['rotations']} distinct_uid="
          f"{len(set(u for u in uids if u != '*rotated*'))} failures={failures[:1]}")
    store.unlink(missing_ok=True)


def main() -> None:
    import shutil
    tmp = Path("data/_tests")
    shutil.rmtree(tmp, ignore_errors=True)   # every run starts from an empty store
    tmp.mkdir(parents=True, exist_ok=True)
    tests = [
        test_mint_on_demand, test_limit_rotates, test_least_used_first, test_is_pro_sticky,
        test_persistence, test_expiry_refresh, test_refresh_failure_handled,
        test_mint_failure_backoff, test_concurrency, test_snapshot_shape,
    ]
    for t in tests:
        try:
            t(tmp)
        except Exception:  # noqa: BLE001
            RESULTS.append((t.__name__, False, "EXCEPTION"))
            print(f"[FAIL] {t.__name__} · EXCEPTION\n{traceback.format_exc()}")
    if "--live" in sys.argv:
        try:
            live_rotation()
        except Exception:  # noqa: BLE001
            RESULTS.append(("live", False, "EXCEPTION"))
            print(f"[FAIL] live · EXCEPTION\n{traceback.format_exc()}")
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{passed}/{len(RESULTS)} PASS")
    sys.exit(0 if passed == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
