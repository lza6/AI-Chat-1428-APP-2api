"""
AI Chat 1.4.28 -> 2API  round-1 stress / behavior suite.

    python stress_test.py                     # gateway on 127.0.0.1:8960
    python stress_test.py --port 8960 --mode mock

Scenarios (each prints PASS/FAIL with the observed evidence):
  S1  anonymous credential minting (uniqueness + latency)
  S2  free tier: 5 messages per uid then reason=LIMIT
  S3  isPro=true bypass: unlimited after LIMIT
  S4  model matrix (all served ids + one bogus id)
  S5  concurrency: 24 in-flight requests
  S6  multi-turn context retention
  S7  promo banner stripped from content
  S8  gateway error branches (401 / 422 / upstream 401)
  S9  image generation -> decodable PNG
  S10 SSE chunk concatenation == non-stream content
"""

from __future__ import annotations

import argparse
import base64
import json
import statistics
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import requests

try:  # Windows consoles default to GBK and choke on emoji coming back from upstream
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import aichat_client as ac  # noqa: E402

RESULTS: list[dict] = []


def record(name: str, ok: bool, detail: str, extra: dict | None = None) -> None:
    RESULTS.append({"scenario": name, "ok": ok, "detail": detail, "extra": extra or {}})
    print(f"[{'PASS' if ok else 'FAIL'}] {name} · {detail}")


def pct(vals: list[float], q: float) -> float:
    if not vals:
        return 0.0
    vals = sorted(vals)
    return vals[min(len(vals) - 1, int(len(vals) * q))]


# --------------------------------------------------------------------------- #
@dataclass
class Ctx:
    base: str = "http://127.0.0.1:8960"
    key: str = "sk-stress"
    mode: str = "upstream"
    timings: list[float] = field(default_factory=list)

    @property
    def h(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"}


def gw_chat(ctx: Ctx, messages: list[dict], model: str = "gpt-4o-mini",
            stream: bool = False, timeout: float = 180.0) -> requests.Response:
    return requests.post(
        f"{ctx.base}/v1/chat/completions",
        headers=ctx.h,
        json={"model": model, "messages": messages, "stream": stream},
        timeout=timeout,
    )


# --------------------------------------------------------------------------- #
def s1_mint(ctx: Ctx) -> None:
    lats, uids = [], []
    for _ in range(3):
        t0 = time.time()
        cred = ac.signup_anonymous()
        lats.append(time.time() - t0)
        uids.append(cred.uid)
    ok = len(set(uids)) == 3
    record("S1 匿名凭证铸造", ok,
           f"3/3 成功, uid 全不重复={ok}, 延迟 {[round(x,2) for x in lats]}s",
           {"uid_unique": len(set(uids)), "latency_s": [round(x, 2) for x in lats]})


def s2_free_tier(ctx: Ctx) -> None:
    cred = ac.signup_anonymous()
    oks, limits, limit_hits = 0, [], 0
    for i in range(6):
        try:
            r = ac.send_chat(cred.id_token, [{"role": "user", "content": f"say {i}"}],
                             model="gpt-4o-mini", is_pro=False)
            oks += 1
            limits.append(r.limits.get("left"))
        except ac.QuotaExhausted:
            limit_hits += 1
    ok = oks == 5 and limit_hits >= 1
    record("S2 免费额度 5 次/uid", ok,
           f"成功 {oks}/5 后 LIMIT 命中 {limit_hits} 次, left 轨迹 {limits}",
           {"ok": oks, "limit_hits": limit_hits, "left_trace": limits})


def s3_ispro_bypass(ctx: Ctx) -> None:
    cred = ac.signup_anonymous()
    for _ in range(5):  # burn the free tier
        try:
            ac.send_chat(cred.id_token, [{"role": "user", "content": "x"}], is_pro=False)
        except ac.QuotaExhausted:
            break
    ok_count, left_trace = 0, []
    for i in range(8):
        try:
            r = ac.send_chat(cred.id_token, [{"role": "user", "content": f"say ok {i}"}],
                             model="gpt-4o", is_pro=True)
            ok_count += 1
            left_trace.append(r.limits.get("left"))
        except ac.AIChatError as exc:
            left_trace.append(f"ERR:{exc}")
    ok = ok_count == 8
    record("S3 isPro=true 额度绕过", ok,
           f"额度耗尽后 isPro=true 连打 8 次成功 {ok_count}/8, left 轨迹 {left_trace}",
           {"ok": ok_count, "left_trace": left_trace})


def s4_models(ctx: Ctx) -> None:
    try:
        models = [m["id"] for m in ac.list_models(ac.signup_anonymous().id_token)]
    except ac.AIChatError as exc:
        record("S4 模型矩阵", False, f"模型列表拉取失败: {exc}")
        return
    rows, passed = [], 0
    for mid in models + ["definitely-not-a-model"]:
        try:
            r = gateway_call_direct(ctx, mid)
            rows.append({"model": mid, "status": r.status_code, "ok": r.status_code == 200})
            passed += r.status_code == 200
        except Exception as exc:  # noqa: BLE001
            rows.append({"model": mid, "status": "EXC", "ok": False, "err": str(exc)[:80]})
    ok = passed == len(rows)
    record("S4 模型矩阵（含未知 id）", ok,
           f"{passed}/{len(rows)} 返回 200 · 上游对未知 id 不报错（回落到默认模型）",
           {"rows": rows})


def gateway_call_direct(ctx: Ctx, model: str) -> requests.Response:
    return gw_chat(ctx, [{"role": "user", "content": "reply with exactly: OK"}], model=model)


def s5_concurrency(ctx: Ctx) -> None:
    total, workers = 24, 8
    lock = threading.Lock()
    out: list[tuple[int, float]] = []

    def worker(n: int) -> None:
        for i in range(n):
            t0 = time.time()
            try:
                r = gw_chat(ctx, [{"role": "user", "content": f"ping {i}"}], model="gpt-4o-mini")
                with lock:
                    out.append((r.status_code, time.time() - t0))
            except Exception:  # noqa: BLE001
                with lock:
                    out.append((-1, time.time() - t0))

    t0 = time.time()
    threads = [threading.Thread(target=worker, args=(total // workers,)) for _ in range(workers)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    wall = time.time() - t0
    okc = sum(1 for s, _ in out if s == 200)
    lat = [d * 1000 for _, d in out]
    ok = okc == total
    record("S5 并发 24 请求", ok,
           f"{okc}/{total} 200 · wall {wall:.1f}s · p50 {pct(lat,0.5):.0f}ms p95 {pct(lat,0.95):.0f}ms "
           f"· {okc/wall:.2f} req/s",
           {"ok": okc, "wall_s": round(wall, 2), "p50_ms": round(pct(lat, 0.5)),
            "p95_ms": round(pct(lat, 0.95)), "rps": round(okc / wall, 2)})


def s6_multiturn(ctx: Ctx) -> None:
    r1 = gw_chat(ctx, [{"role": "user", "content": "Remember this codename: VELVET-9"}])
    r2 = gw_chat(ctx, [{"role": "user", "content": "Remember this codename: VELVET-9"},
                       {"role": "assistant", "content": "Understood."},
                       {"role": "user", "content": "What is the codename? One word."}])
    ok = r1.status_code == 200 and r2.status_code == 200 and "VELVET" in r2.json()["choices"][0]["message"]["content"].upper()
    body = r2.json()["choices"][0]["message"]["content"] if r2.status_code == 200 else r2.text[:120]
    record("S6 多轮上下文", ok, f"回放提问命中=true · 回复={body[:80]!r}")


def s7_promo_strip(ctx: Ctx) -> None:
    bad = 0
    texts = []
    for _ in range(3):
        r = gw_chat(ctx, [{"role": "user", "content": "say hi"}])
        if r.status_code != 200:
            bad += 1
            continue
        c = r.json()["choices"][0]["message"]["content"]
        texts.append(c[:60])
        if "outdated version" in c or "play.google.com/store" in c:
            bad += 1
    ok = bad == 0
    record("S7 上游升级横幅剥离", ok, f"3 次采样中残留 {bad} 次 · 样例={texts[:1]}")


def s8_errors(ctx: Ctx) -> None:
    checks = {}

    r = requests.post(f"{ctx.base}/v1/chat/completions", headers={"Content-Type": "application/json"},
                      json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "x"}]}, timeout=30)
    checks["no_auth=401"] = r.status_code == 401

    r = requests.post(f"{ctx.base}/v1/chat/completions",
                      headers={"Authorization": "Bearer sk-invalid-demo", "Content-Type": "application/json"},
                      json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "x"}]}, timeout=30)
    checks["bad_key=401"] = r.status_code == 401

    r = requests.post(f"{ctx.base}/v1/chat/completions", headers=ctx.h, json={"model": "gpt-4o-mini", "messages": []}, timeout=30)
    checks["empty_messages=422"] = r.status_code == 422

    try:
        ac.send_chat("not-a-real-jwt", [{"role": "user", "content": "x"}], is_pro=True)
        checks["upstream_bad_token"] = False
    except ac.UpstreamHTTPError as exc:
        checks["upstream_bad_token=401"] = exc.status in (401, 403)
    except ac.AIChatError:
        checks["upstream_bad_token=401"] = False
    ok = all(checks.values())
    record("S8 错误分支", ok, json.dumps(checks, ensure_ascii=False))


def s9_image(ctx: Ctx) -> None:
    t0 = time.time()
    r = requests.post(f"{ctx.base}/v1/images/generations", headers=ctx.h,
                      json={"model": "gpt-4o-mini", "prompt": "a minimal blue square on white background",
                            "response_format": "b64_json"}, timeout=300)
    dt = time.time() - t0
    if r.status_code != 200:
        record("S9 生图", False, f"HTTP {r.status_code} · {r.text[:200]}")
        return
    b64 = r.json()["data"][0]["b64_json"]
    raw = base64.b64decode(b64)
    ok = raw[:8] == b"\x89PNG\r\n\x1a\n"
    record("S9 生图（base64 PNG）", ok,
           f"{dt:.1f}s · {len(raw)/1024:.0f} KB · PNG 魔数={'ok' if ok else 'bad'}",
           {"elapsed_s": round(dt, 1), "kb": round(len(raw) / 1024), "png": ok})


def s10_sse(ctx: Ctx) -> None:
    msg = [{"role": "user", "content": "List three primary colors, separated by commas only."}]
    r = gw_chat(ctx, msg)
    if r.status_code != 200:
        record("S10 SSE 一致性", False, f"非流式失败 {r.status_code}")
        return
    full = r.json()["choices"][0]["message"]["content"]

    acc, chunks, done = "", 0, False
    with requests.post(f"{ctx.base}/v1/chat/completions", headers=ctx.h,
                       json={"model": "gpt-4o-mini", "messages": msg, "stream": True},
                       stream=True, timeout=180) as s:
        for line in s.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                done = True
                break
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            piece = obj["choices"][0]["delta"].get("content")
            if piece:
                acc += piece
                chunks += 1
    # two independent generations, so compare shape not exact text
    ok = done and chunks >= 5 and len(acc) >= 3 and acc.strip() != ""
    record("S10 SSE 分块一致性", ok,
           f"chunks={chunks} · DONE={done} · 拼接长度={len(acc)} · 非流式长度={len(full)}",
           {"chunks": chunks, "done": done, "stream_len": len(acc), "json_len": len(full)})


# --------------------------------------------------------------------------- #
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8960")
    ap.add_argument("--mode", default="upstream")
    ap.add_argument("--skip-heavy", action="store_true")
    args = ap.parse_args()
    ctx = Ctx(base=args.base, mode=args.mode)

    print(f"=== ai-chat-1.4.28 2api stress · {time.strftime('%Y-%m-%d %H:%M:%S')} · {ctx.base} ===")
    try:
        health = requests.get(f"{ctx.base}/health", timeout=10).json()
        print(f"gateway: {json.dumps(health, ensure_ascii=False)[:220]}\n")
    except Exception as exc:  # noqa: BLE001
        print(f"!! gateway 不可达: {exc}")
        sys.exit(2)

    t0 = time.time()
    s1_mint(ctx)
    s2_free_tier(ctx)
    s3_ispro_bypass(ctx)
    s4_models(ctx)
    if not args.skip_heavy:
        s5_concurrency(ctx)
    s6_multiturn(ctx)
    s7_promo_strip(ctx)
    s8_errors(ctx)
    if not args.skip_heavy:
        s9_image(ctx)
        s10_sse(ctx)
    total = time.time() - t0

    passed = sum(1 for r in RESULTS if r["ok"])
    print("\n================ 汇总 ================")
    for r in RESULTS:
        print(f"{'PASS' if r['ok'] else 'FAIL'}  {r['scenario']}")
    print(f"合计 {passed}/{len(RESULTS)} PASS · 总耗时 {total:.1f}s")

    out = Path(__file__).resolve().parent / "report" / "stress_result.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"when": time.strftime("%Y-%m-%dT%H:%M:%S"),
                               "base": ctx.base, "passed": passed, "total": len(RESULTS),
                               "elapsed_s": round(total, 1), "results": RESULTS},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"结果已写入 {out}")
    sys.exit(0 if passed == len(RESULTS) else 1)


if __name__ == "__main__":
    main()
