"""
Egress capacity probe — how much concurrency can this host actually push?

Every request leaves through the local proxy (HTTP(S)_PROXY, default
127.0.0.1:10808). Before blaming upstream for failures we must know the
local ceiling: this probe uses the cheap `GET /chat/categories` (no LLM
inference) and ramps concurrency.

    python probe_capacity.py --concurrency 5,10,20,30,40,60
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import aichat_client as ac  # noqa: E402

OUT = Path(__file__).resolve().parent / "report"


def probe(conc: int, token: str, rounds: int = 1) -> dict:
    res: list[tuple[int, float, str]] = []
    lock = threading.Lock()

    def hit() -> None:
        t0 = time.time()
        try:
            st, txt = ac._request(f"{ac.API_BASE}/chat/categories?lang=en&ver=2",  # noqa: SLF001
                                  None, token, "GET", 60.0)
            ok = st == 200 and '"success":true' in txt
            with lock:
                res.append((st, time.time() - t0, "" if ok else txt[:80]))
        except Exception as exc:  # noqa: BLE001
            with lock:
                res.append((-1, time.time() - t0, repr(exc)[:80]))

    threads = [threading.Thread(target=hit) for _ in range(conc * rounds)]
    t0 = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wall = time.time() - t0
    lat = sorted(r[1] for r in res)
    ok = sum(1 for r in res if r[0] == 200)
    errs: dict[str, int] = {}
    for r in res:
        if r[2]:
            errs[r[2]] = errs.get(r[2], 0) + 1
    return {
        "concurrency": conc,
        "requests": len(res),
        "ok": ok,
        "success_rate": round(100.0 * ok / max(1, len(res)), 1),
        "wall_s": round(wall, 1),
        "p50_ms": round(lat[len(lat) // 2] * 1000) if lat else 0,
        "p95_ms": round(lat[int(len(lat) * 0.95)] * 1000) if lat else 0,
        "max_ms": round(lat[-1] * 1000) if lat else 0,
        "rps": round(len(res) / wall, 2),
        "errors": dict(list(errs.items())[:3]),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--concurrency", default="5,10,20,30,40,60")
    ap.add_argument("--rounds", type=int, default=1)
    ap.add_argument("--gap", type=float, default=2.0, help="seconds between concurrency levels")
    args = ap.parse_args()
    concs = [int(x) for x in args.concurrency.split(",")]

    cred = ac.signup_anonymous()
    print(f"uid={cred.uid[:8]}… probing egress capacity on GET /chat/categories")
    rows = []
    for c in concs:
        r = probe(c, cred.id_token, args.rounds)
        rows.append(r)
        print(f"C={c:3d} -> {r['ok']}/{r['requests']} ok ({r['success_rate']}%) "
              f"p50 {r['p50_ms']}ms p95 {r['p95_ms']}ms wall {r['wall_s']}s {r['rps']} rps "
              f"{r['errors'] if r['errors'] else ''}")
        if r["success_rate"] < 50:
            print("  -> 本地出口开始崩溃，后续更高并发不再继续")
            break
        time.sleep(args.gap)

    OUT.mkdir(exist_ok=True)
    (OUT / "proxy_capacity.json").write_text(
        json.dumps({"when": time.strftime("%Y-%m-%dT%H:%M:%S"), "rows": rows},
                   ensure_ascii=False, indent=2), encoding="utf-8")
    print("-> report/proxy_capacity.json")


if __name__ == "__main__":
    main()
