"""
Browser end-to-end verification of the demo page (Playwright + Chromium).

    python verify_browser.py --base http://127.0.0.1:8960

Covers real user paths: type -> send -> streamed answer rendered, image tab ->
generate -> <img> appears, console/page errors collected, screenshots saved.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parent / "report"
OUT.mkdir(exist_ok=True)
LOG: list[str] = []


def log(msg: str) -> None:
    LOG.append(msg)
    print(msg)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8960")
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()

    ok_flags = {"chat": False, "stream": False, "image": False, "no_console_error": True}

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        page = browser.new_page(viewport={"width": 1360, "height": 900})
        errors: list[str] = []
        page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}") if m.type == "error" else None)
        page.on("pageerror", lambda e: errors.append(f"pageerror: {e}"))

        page.goto(args.base, wait_until="networkidle")
        page.wait_for_timeout(800)
        log(f"title={page.title()}")
        log(f"models in select: {page.eval_on_selector('#model', 'e => e.options.length')}")

        # ---- path 1: non-stream chat with pro model gpt-4o
        page.select_option("#model", "gpt-4o")
        page.select_option("#stream", "0")
        page.fill("#input", "Reply with exactly: BROWSER-OK")
        page.click("#sendBtn")
        page.wait_for_function(
            "() => { const m=[...document.querySelectorAll('.msg.assistant')]; "
            "return m.length && m[m.length-1].textContent.includes('BROWSER-OK'); }",
            timeout=150_000,
        )
        ok_flags["chat"] = True
        log("non-stream chat rendered")
        page.screenshot(path=str(OUT / "browser_chat_nonstream.png"), full_page=True)

        # ---- path 2: streaming
        page.select_option("#stream", "1")
        page.fill("#input", "List the numbers 1 to 5 separated by spaces, nothing else.")
        page.click("#sendBtn")
        page.wait_for_function(
            "() => { const m=[...document.querySelectorAll('.msg.assistant')]; "
            "return m.length >= 2 && m[m.length-1].textContent.includes('5'); }",
            timeout=150_000,
        )
        n_msgs = page.eval_on_selector_all(".msg.assistant", "els => els.length")
        ok_flags["stream"] = n_msgs >= 2
        log(f"stream chat rendered, assistant bubbles={n_msgs}")
        page.screenshot(path=str(OUT / "browser_chat_stream.png"), full_page=True)

        # ---- path 3: image generation
        page.click("#tabImage")
        page.fill("#input", "a single red apple on a plain white background, studio product shot")
        page.click("#sendBtn")
        page.wait_for_function(
            "() => document.querySelectorAll('#imgOut img').length > 0", timeout=300_000)
        w = page.eval_on_selector("#imgOut img", "e => e.naturalWidth")
        h = page.eval_on_selector("#imgOut img", "e => e.naturalHeight")
        ok_flags["image"] = w > 0 and h > 0
        log(f"image rendered {w}x{h}")
        page.screenshot(path=str(OUT / "browser_image.png"), full_page=True)

        # ---- health panel reflects traffic
        page.click("#refreshBtn")
        page.wait_for_timeout(600)
        log("panel: " + " | ".join(
            f"{k}={page.inner_text('#'+v)}" for k, v in
            [("mode", "stMode"), ("pro", "stPro"), ("req", "stReq"), ("up", "stUp"), ("lat", "stLat")]))

        if errors:
            ok_flags["no_console_error"] = False
            log("CONSOLE ERRORS: " + " ;; ".join(errors[:5]))
        else:
            log("no console/page errors")

        browser.close()

    passed = all(ok_flags.values())
    log(f"\nRESULT: {'PASS' if passed else 'FAIL'} {ok_flags}")
    (OUT / "browser_verify.log").write_text("\n".join(LOG), encoding="utf-8")
    sys.exit(0 if passed else 1)


if __name__ == "__main__":
    main()
