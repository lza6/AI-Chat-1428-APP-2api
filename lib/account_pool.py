"""
Anonymous account pool for the AI Chat 1.4.28 upstream.

Why a pool: every anonymous Firebase uid carries its own 5-message free quota
(`limits.max=5`). Whether the upstream enforces it depends on the client-supplied
`isPro` flag:

  * mode "unlimited" (is_pro=True)  -> one uid never hits the wall, pool size 1 is enough
  * mode "rotate"    (is_pro=False) -> 5 messages per uid, then the uid is dead weight
                                       and must be replaced by a freshly minted one

Design notes:
  * single process, thread safe (RLock); state persisted as JSON so a restart
    does not throw away live refresh tokens
  * idToken lives 1h -> refreshed through the refresh token, which keeps the
    uid (and therefore its remaining quota) intact
  * minting is rate limited (min_interval) with exponential backoff, because
    identitytoolkit is the one endpoint that punishes bursts
  * accounts are dropped on auth failure (dead) and on quota exhaustion
    (exhausted); a background thread tops the pool back up
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable

import aichat_client as ac

FRESH = "fresh"          # minted, never used
ACTIVE = "active"        # used at least once, quota left
EXHAUSTED = "exhausted"  # upstream said reason=LIMIT
DEAD = "dead"            # 401/403 or unusable token

_USABLE = (FRESH, ACTIVE)


@dataclass
class Account:
    uid: str
    id_token: str
    refresh_token: str
    expires_at: float
    state: str = FRESH
    used: int = 0
    quota_left: int | None = None
    created_at: float = field(default_factory=time.time)
    last_error: str = ""

    @property
    def expired(self) -> bool:
        return time.time() > self.expires_at - 300

    def public(self) -> dict:
        return {
            "uid": self.uid[:6] + "…",
            "state": self.state,
            "used": self.used,
            "quota_left": self.quota_left,
            "age_min": round((time.time() - self.created_at) / 60, 1),
            "expires_in_min": round((self.expires_at - time.time()) / 60, 1),
            "last_error": self.last_error,
        }


class PoolEmpty(ac.AIChatError):
    """no usable account could be produced"""


class AccountPool:
    def __init__(
        self,
        store: Path | str | None = Path("data/accounts.json"),
        size: int = 3,
        is_pro: bool = True,
        min_interval: float = 0.35,
        signup: Callable[[], ac.AnonCredential] = ac.signup_anonymous,
        refresh: Callable[[ac.AnonCredential], ac.AnonCredential] = ac.refresh_credential,
        autostart: bool = True,
    ) -> None:
        self._lock = threading.RLock()
        self._accounts: list[Account] = []
        self._store = Path(store) if store else None
        self.size = max(1, size)
        self.is_pro = is_pro
        self.min_interval = min_interval
        self._signup = signup
        self._refresh = refresh
        self._last_mint = 0.0
        self._mint_backoff = 0.0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._rr = 0
        self.stats = {
            "minted": 0,
            "refreshed": 0,
            "rotations": 0,
            "exhausted": 0,
            "dead": 0,
            "mint_fail": 0,
            "served": 0,
        }
        self._load()
        if autostart:
            self.start()

    # ------------------------------------------------------------------ #
    # persistence
    # ------------------------------------------------------------------ #
    def _load(self) -> None:
        if not self._store or not self._store.exists():
            return
        try:
            raw = json.loads(self._store.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        for item in raw.get("accounts", []):
            try:
                acc = Account(**item)
            except TypeError:
                continue
            if acc.state == EXHAUSTED:
                continue  # a restart means new demand; stale exhausted rows are noise
            self._accounts.append(acc)
        self.stats.update(raw.get("stats") or {})

    def _save(self) -> None:
        if not self._store:
            return
        self._store.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "saved_at": time.time(),
            "is_pro": self.is_pro,
            "accounts": [asdict(a) for a in self._accounts],
            "stats": self.stats,
        }
        tmp = self._store.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self._store)

    # ------------------------------------------------------------------ #
    # minting
    # ------------------------------------------------------------------ #
    def _mint_locked(self) -> Account:
        wait = self._mint_backoff - (time.time() - self._last_mint)
        if wait > 0:
            time.sleep(min(wait, 5.0))
        try:
            cred = self._signup()
        except Exception as exc:  # noqa: BLE001 - any transport failure -> backoff
            self.stats["mint_fail"] += 1
            self._mint_backoff = min(30.0, (self._mint_backoff or 1.0) * 2)
            raise PoolEmpty(f"mint failed: {exc!r}") from exc
        self._last_mint = time.time()
        self._mint_backoff = self.min_interval
        self.stats["minted"] += 1
        acc = Account(uid=cred.uid, id_token=cred.id_token, refresh_token=cred.refresh_token,
                      expires_at=cred.expires_at)
        self._accounts.append(acc)
        self._save()
        return acc

    def fill(self, n: int | None = None) -> int:
        """Mint until `n` usable accounts exist. Returns how many are usable."""
        target = n if n is not None else self.size
        with self._lock:
            attempts = 0
            while self._usable_locked() < target and attempts < target + 2:
                attempts += 1
                try:
                    self._mint_locked()
                except PoolEmpty:
                    break
            return self._usable_locked()

    def _usable_locked(self) -> int:
        return sum(1 for a in self._accounts if a.state in _USABLE)

    # ------------------------------------------------------------------ #
    # acquire / report
    # ------------------------------------------------------------------ #
    def acquire(self) -> Account:
        """Least-used usable account, minting when the pool is dry."""
        with self._lock:
            acc = self._pick_locked()
            if acc is None:
                acc = self._mint_locked()
            if acc.expired:
                acc = self._refresh_locked(acc)
            self.stats["served"] += 1
            return acc

    def _dry(self, acc: Account) -> bool:
        """Quota already known to be spent: skip it instead of paying a wasted call."""
        return (not self.is_pro) and acc.quota_left is not None and acc.quota_left <= 0

    def _pick_locked(self) -> Account | None:
        usable = [a for a in self._accounts
                  if a.state in _USABLE and not a.expired and not self._dry(a)]
        if not usable:
            # every known-good row is spent: drop them so the caller mints a fresh one
            spent = [a for a in self._accounts if a.state in _USABLE and self._dry(a)]
            for a in spent:
                self._accounts.remove(a)
                self.stats["exhausted"] += 1
                self.stats["rotations"] += 1
            if spent:
                self._save()
        if not usable:
            fresh = [a for a in self._accounts if a.state in _USABLE]
            if not fresh:
                return None
            usable = fresh
        if self.is_pro:
            # one identity is enough when the quota is not enforced: stick to it
            usable.sort(key=lambda a: a.created_at)
        else:
            # spread load so each uid burns its 5 messages before being dropped
            usable.sort(key=lambda a: (a.used, a.created_at))
        self._rr += 1
        return usable[0]

    def _refresh_locked(self, acc: Account) -> Account:
        try:
            cred = self._refresh(ac.AnonCredential(id_token=acc.id_token,
                                                   refresh_token=acc.refresh_token,
                                                   uid=acc.uid, expires_at=acc.expires_at))
        except Exception as exc:  # noqa: BLE001
            acc.state = DEAD
            acc.last_error = f"refresh failed: {exc!r}"[:160]
            self.stats["dead"] += 1
            self._save()
            self._accounts.remove(acc)
            return self._mint_locked()
        acc.id_token = cred.id_token
        acc.refresh_token = cred.refresh_token
        acc.expires_at = cred.expires_at
        self.stats["refreshed"] += 1
        self._save()
        return acc

    def report_ok(self, acc: Account, quota_left: int | None = None) -> None:
        with self._lock:
            acc.used += 1
            acc.state = ACTIVE
            if quota_left is not None:
                acc.quota_left = quota_left
            self._save()

    def report_limit(self, acc: Account) -> None:
        """Upstream answered reason=LIMIT: this uid is out of free messages."""
        with self._lock:
            acc.state = EXHAUSTED
            acc.quota_left = 0
            self.stats["exhausted"] += 1
            self.stats["rotations"] += 1
            if acc in self._accounts:
                self._accounts.remove(acc)
            self._save()

    def report_dead(self, acc: Account, reason: str = "") -> None:
        with self._lock:
            acc.state = DEAD
            acc.last_error = reason[:160]
            self.stats["dead"] += 1
            self.stats["rotations"] += 1
            if acc in self._accounts:
                self._accounts.remove(acc)
            self._save()

    # ------------------------------------------------------------------ #
    # introspection / lifecycle
    # ------------------------------------------------------------------ #
    def effective_mode(self) -> str:
        """How many accounts the current configuration actually needs."""
        return "single-identity" if self.is_pro else "rotate-per-quota"

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "mode": "is_pro=true / 不校验额度" if self.is_pro else "is_pro=false / 5 条每号",
                "strategy": self.effective_mode(),
                "size_target": self.size,
                "usable": self._usable_locked(),
                "accounts": [a.public() for a in self._accounts],
                "stats": dict(self.stats),
                "store": str(self._store) if self._store else None,
                "mint_backoff_s": round(self._mint_backoff, 1),
            }

    def _loop(self) -> None:
        while not self._stop.wait(20.0):
            try:
                with self._lock:
                    need = self.size - self._usable_locked()
                for _ in range(max(0, need)):
                    if self._stop.is_set():
                        return
                    try:
                        with self._lock:
                            self._mint_locked()
                    except PoolEmpty:
                        break
            except Exception:  # noqa: BLE001 - the refiller must never die
                continue

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="pool-refill", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)
