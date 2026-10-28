"""
AI Chat 1.4.28 — R2 large-scale load & upstream risk-control probe.

    python load_test.py --target gateway --base http://127.0.0.1:8960
    python load_test.py --target upstream            # 直打上游，单 uid，测上游真实容量
    python load_test.py --target idp                 # 只测 Firebase 铸造闸门
    python load_test.py --target gateway --stages A,B --concurrency 20,40,80 --per-worker 2

Stages
  A  baseline      1 rps × 30 s            低速率基线，抓限流/风控最低门槛
  B  sweep         C=20/40/80，各 C×N 次  找到并发拐点（成功率 < 95% 或 p95 陡增）
  C  soak          30 并发 × 120 s         持续负载下的稳定性与 rps
  D  idp flood     30 次连续 signUp         Firebase 铸造闸门
  E  uid fanout    单 uid 20 并发          上游对单身份的并发容忍度
  F  long context  10 轮对话，逐轮计时      上下文增长后的延迟/截断

安全阀：任一阶段连续 20 次里错误率 > 25% 立即中止该阶段（避免继续打上游）。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import aichat_client as ac  # noqa: E402

OUT = Path(__file__).resolve().parent / "report"
OUT.mkdir(exist_ok=True)


# --------------------------------------------------------------------------- #
@dataclass
class Sample:
    t: float
    status: int
    latency: float
    ok: bool
    reason: str = ""
    left: object = None
    err: str = ""


@dataclass
class Stage:
    name: str
    samples: list[Sample] = field(default_factory=list)
    aborted: bool = False
    note: str = ""
    wall: float = 0.0

    def add(self, s: Sample) -> None:
        self.samples.append(s)

    @property
    def total(self) -> int:
        return len(self.samples)

    @property
    def ok(self) -> int:
        return sum(1 for s in self.samples if s.ok)

    def lat(self) -> list[float]:
        return [s.latency for s in self.samples]

    def q(self, q: float) -> float:
        v = sorted(self.lat())
        return v[min(len(v) - 1, int(len(v) * q))] if v else 0.0

    def summary(self) -> dict:
        lat = self.lat()
        return {
            "stage": self.name,
            "total": self.total,
            "ok": self.ok,
            "fail": self.total - self.ok,
            "success_rate": round(100.0 * self.ok / self.total, 1) if self.total else 0.0,
            "p50_ms": round(self.q(0.5) * 1000),
            "p90_ms": round(self.q(0.9) * 1000),
            "p95_ms": round(self.q(0.95) * 1000),
            "p99_ms": round(self.q(0.99) * 1000),
            "max_ms": round(max(lat) * 1000) if lat else 0,
            "rps": round(self.total / self.wall, 2) if self.wall else 0.0,
            "wall_s": round(self.wall, 1),
            "status_codes": _counts(s.status for s in self.samples),
            "reasons": _counts(s.reason or "(none)" for s in self.samples),
            "errors": _counts(s.err for s in self.samples if s.err),
            "aborted": self.aborted,
            "note": self.note,
        }


def _counts(it) -> dict:
    out: dict[str, int] = {}
    for k in it:
        key = str(k)
        out[key] = out.get(key, 0) + 1
    return out


class Abort:
    """Stop a stage as soon as the local error window turns bad."""

    def __init__(self, window: int = 20, threshold: float = 0.25):
        self.window: list[bool] = []
        self.win = window
        self.thr = threshold
        self.tripped = False
        self._lock = threading.Lock()

    def push(self, ok: bool) -> bool:
        with self._lock:
            self.window.append(ok)
            if len(self.window) > self.win:
                self.window.pop(0)
            if len(self.window) == self.win and sum(1 for x in self.window if not x) / self.win > self.thr:
                self.tripped = True
            return self.tripped


# --------------------------------------------------------------------------- #
# request drivers
# --------------------------------------------------------------------------- #
class GatewayDriver:
    def __init__(self, base: str, key: str = "sk-load"):
        self.base = base.rstrip("/")
        self.h = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}

    def one(self, i: int) -> Sample:
        t0 = time.time()
        try:
            r = requests.post(f"{self.base}/v1/chat/completions", headers=self.h,
                              json={"model": "gpt-4o-mini", "stream": False,
                                    "messages": [{"role": "user", "content": f"Reply with exactly: L{i}"}]},
                              timeout=180)
            dt = time.time() - t0
            if r.status_code != 200:
                return Sample(t0, r.status_code, dt, False, err=r.text[:120])
            j = r.json()
            body = j["choices"][0]["message"]["content"]
            return Sample(t0, r.status_code, dt, body.strip() != "", left=None)
        except Exception as exc:  # noqa: BLE001
            return Sample(t0, -1, time.time() - t0, False, err=repr(exc)[:120])


class UpstreamDriver:
    """direct to chatadmin.org with one anonymous uid (isPro=true)"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._creds: list[ac.AnonCredential] = []
        self._i = 0

    def cred(self) -> ac.AnonCredential:
        with self._lock:
            if not self._creds:
                self._creds.append(ac.signup_anonymous())
            c = self._creds[self._i % len(self._creds)]
            self._i += 1
            return c

    def one(self, i: int) -> Sample:
        t0 = time.time()
        try:
            cred = self.cred()
        except Exception as exc:  # noqa: BLE001 - signup failure must not kill the worker
            return Sample(t0, -1, time.time() - t0, False, err=f"signup: {exc!r}"[:120])
        try:
            r = ac.send_chat(cred.id_token, [{"role": "user", "content": f"Reply with exactly: L{i}"}],
                             model="gpt-4o-mini", is_pro=True)
            return Sample(t0, 200, time.time() - t0, True, left=r.limits.get("left"))
        except ac.QuotaExhausted:
            with self._lock:
                self._creds.clear()
            return Sample(t0, 200, time.time() - t0, False, reason="LIMIT")
        except ac.UpstreamHTTPError as exc:
            return Sample(t0, exc.status, time.time() - t0, False, err=exc.body[:120])
        except ac.AIChatError as exc:
            return Sample(t0, -1, time.time() - t0, False, err=str(exc)[:120])


# --------------------------------------------------------------------------- #
# stages
# --------------------------------------------------------------------------- #
def run_parallel(stage: Stage, driver, concurrency: int, per_worker: int,
                 abort: Abort, deadline: float | None = None) -> None:
    lock = threading.Lock()

    def worker(wid: int) -> None:
        n = 0
        while n < per_worker:
            if abort.tripped or (deadline and time.time() > deadline):
                break
            s = driver.one(wid * 1000 + n)
            with lock:
                stage.add(s)
            if abort.push(s.ok):
                break
            n += 1

    threads = [threading.Thread(target=worker, args=(w,)) for w in range(concurrency)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    stage.wall = time.time() - t0
    stage.aborted = abort.tripped


def stage_a(driver) -> Stage:
    st = Stage("A baseline 1rps/30s")
    abort = Abort()
    t_start = time.time()
    for i in range(30):
        s = driver.one(i)
        st.add(s)
        if abort.push(s.ok):
            st.aborted = True
            break
        time.sleep(1.0)
    st.wall = time.time() - t_start
    return st


def stage_b(driver, concs: list[int], per_worker: int) -> list[Stage]:
    out = []
    for c in concs:
        st = Stage(f"B sweep C={c} x{per_worker}")
        run_parallel(st, driver, c, per_worker, Abort())
        out.append(st)
        if st.total and st.ok / st.total < 0.9:
            st.note = "成功率 <90%，后续更高并发不再继续抬升"
            break
    return out


def stage_c(driver, concurrency: int, seconds: int) -> Stage:
    st = Stage(f"C soak C={concurrency} {seconds}s")
    run_parallel(st, driver, concurrency, per_worker=10_000, abort=Abort(),
                 deadline=time.time() + seconds)
    return st


def stage_d(count: int = 30, gap: float = 0.0) -> Stage:
    st = Stage(f"D idp flood x{count}")
    t_start = time.time()
    abort = Abort(window=10, threshold=0.4)
    for i in range(count):
        t0 = time.time()
        try:
            cred = ac.signup_anonymous()
            st.add(Sample(t0, 200, time.time() - t0, True, reason="uid:" + cred.uid[:6]))
            abort.push(True)
        except ac.AIChatError as exc:
            msg = str(exc)
            st.add(Sample(t0, 429 if "429" in msg else -1, time.time() - t0, False, err=msg[:120]))
            if abort.push(False):
                st.aborted = True
                st.note = f"连续失败，疑似 IDP 限流：{msg[:100]}"
                break
        except Exception as exc:  # noqa: BLE001
            st.add(Sample(t0, -1, time.time() - t0, False, err=repr(exc)[:120]))
            abort.push(False)
        if gap:
            time.sleep(gap)
    st.wall = time.time() - t_start
    return st


def stage_e(concurrency: int = 5) -> Stage:
    """single uid, N parallel - upstream per-identity concurrency tolerance"""
    st = Stage(f"E uid fanout 1uid x{concurrency}")
    cred = ac.signup_anonymous()
    drv = _SingleUidDriver(cred)
    run_parallel(st, drv, concurrency, per_worker=1, abort=Abort())
    return st


class _SingleUidDriver:
    def __init__(self, cred: ac.AnonCredential):
        self.cred = cred

    def one(self, i: int) -> Sample:
        t0 = time.time()
        try:
            r = ac.send_chat(self.cred.id_token, [{"role": "user", "content": f"Reply with exactly: U{i}"}],
                             model="gpt-4o-mini", is_pro=True)
            return Sample(t0, 200, time.time() - t0, True, left=r.limits.get("left"))
        except ac.QuotaExhausted:
            return Sample(t0, 200, time.time() - t0, False, reason="LIMIT")
        except ac.UpstreamHTTPError as exc:
            return Sample(t0, exc.status, time.time() - t0, False, err=exc.body[:120])
        except ac.AIChatError as exc:
            return Sample(t0, -1, time.time() - t0, False, err=str(exc)[:120])


def stage_f(turns: int = 10) -> Stage:
    st = Stage(f"F long context {turns} turns")
    cred = ac.signup_anonymous()
    t_start = time.time()
    history: list[dict] = [{"role": "system", "content": "You are a concise assistant. Answer in <=8 words."}]
    for i in range(turns):
        history.append({"role": "user", "content": f"Turn {i}: remember token T{i}."})
        t0 = time.time()
        try:
            r = ac.send_chat(cred.id_token, history, model="gpt-4o-mini", is_pro=True)
            st.add(Sample(t0, 200, time.time() - t0, bool(r.content), left=r.limits.get("left")))
            history.append({"role": "assistant", "content": r.content})
        except ac.AIChatError as exc:
            st.add(Sample(t0, -1, time.time() - t0, False, err=str(exc)[:120]))
            break
    st.wall = time.time() - t_start
    return st


# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--target", choices=["gateway", "upstream", "idp"], default="gateway")
    ap.add_argument("--base", default="http://127.0.0.1:8960")
    ap.add_argument("--stages", default="A,B,C,D,E,F")
    ap.add_argument("--concurrency", default="20,40,80")
    ap.add_argument("--per-worker", type=int, default=2)
    ap.add_argument("--soak-seconds", type=int, default=120)
    ap.add_argument("--soak-concurrency", type=int, default=30)
    ap.add_argument("--idp-count", type=int, default=10)
    ap.add_argument("--idp-gap", type=float, default=1.0)
    args = ap.parse_args()

    wanted = [s.strip().upper() for s in args.stages.split(",") if s.strip()]
    concs = [int(x) for x in args.concurrency.split(",") if x.strip()]

    if args.target == "gateway":
        driver = GatewayDriver(args.base)
        try:
            health = requests.get(f"{args.base}/health", timeout=10).json()
            print(f"gateway health: {json.dumps(health, ensure_ascii=False)[:200]}\n")
        except Exception as exc:  # noqa: BLE001
            print(f"!! gateway 不可达 {exc}")
            sys.exit(2)
    elif args.target == "upstream":
        driver = UpstreamDriver()
    else:
        driver = None

    stages: list[Stage] = []
    t_all = time.time()
    print(f"=== R2 load test · target={args.target} · stages={wanted} · {time.strftime('%H:%M:%S')} ===")

    for name in wanted:
        t0 = time.time()
        if name == "A":
            stages.append(stage_a(driver))
        elif name == "B":
            stages.extend(stage_b(driver, concs, args.per_worker))
        elif name == "C":
            stages.append(stage_c(driver, args.soak_concurrency, args.soak_seconds))
        elif name == "D":
            stages.append(stage_d(args.idp_count, args.idp_gap))
        elif name == "E":
            stages.append(stage_e())
        elif name == "F":
            stages.append(stage_f())
        s = stages[-1]
        print(f"[{name}] {s.total} req · ok {s.ok} · p50 {s.q(0.5)*1000:.0f}ms "
              f"p99 {s.q(0.99)*1000:.0f}ms · {time.time()-t0:.1f}s"
              + (" · ABORTED" if s.aborted else ""))

    elapsed = time.time() - t_all
    sums = [s.summary() for s in stages]
    total = sum(s["total"] for s in sums)
    okc = sum(s["ok"] for s in sums)
    print("\n================ 汇总 ================")
    print(f"{'stage':34s} {'req':>5s} {'ok':>5s} {'rate':>6s} {'p50':>7s} {'p99':>7s}")
    for s in sums:
        print(f"{s['stage']:34s} {s['total']:5d} {s['ok']:5d} {s['success_rate']:5.1f}% "
              f"{s['p50_ms']:6d}ms {s['p99_ms']:6d}ms")
    print(f"TOTAL {total} req / {okc} ok / {100.0*okc/max(1,total):.1f}% · {elapsed:.1f}s")

    out = OUT / f"load_{args.target}_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(json.dumps({"when": time.strftime("%Y-%m-%dT%H:%M:%S"), "target": args.target,
                               "base": args.base if args.target == "gateway" else "chatadmin.org",
                               "elapsed_s": round(elapsed, 1), "summary": sums},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"-> {out}")


if __name__ == "__main__":
    main()
