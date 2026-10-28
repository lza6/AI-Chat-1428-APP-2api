"""
AI Chat 1.4.28 -> OpenAI compatible gateway (round 1 demo).

    python demo_server.py            # http://127.0.0.1:8960
    python demo_server.py --port 8960

Routes:
    GET  /                          demo chat UI (static/demo.html)
    GET  /v1/models                 OpenAI model list
    POST /v1/chat/completions       OpenAI chat (stream=true -> simulated SSE)
    POST /v1/images/generations     upstream base64 png
    GET  /health                    pool / stats
    POST /admin/warm?n=3            pre-mint anonymous credentials

Env:
    AICHAT_IS_PRO=1   send isPro=true (quota bypass, default)
    AICHAT_MODE=upstream|mock
    AICHAT_PORT=8960
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent / "lib"))
import aichat_client as ac  # noqa: E402
from account_pool import AccountPool, PoolEmpty  # noqa: E402

MODE = os.environ.get("AICHAT_MODE", "upstream").lower()
IS_PRO = os.environ.get("AICHAT_IS_PRO", "1") not in ("0", "false", "False")
DEFAULT_MODEL = "gpt-4o-mini"

STATIC_DIR = Path(__file__).resolve().parent / "static"

# model ids served by the upstream /chat/models endpoint (verified live)
FALLBACK_MODELS = [
    {"id": "gpt-4o-mini", "owned_by": "openai", "locked": False},
    {"id": "gpt-4o", "owned_by": "openai", "locked": True},
    {"id": "gpt-4.5-preview", "owned_by": "openai", "locked": True},
    {"id": "deepseek-chat", "owned_by": "deepseek", "locked": True},
    {"id": "gemini-2.0-flash", "owned_by": "google", "locked": True},
]

PAGE = Path(__file__).resolve().parent
pool = AccountPool(
    store=PAGE / "data" / "accounts.json",
    size=int(os.environ.get("AICHAT_POOL_SIZE", "1" if IS_PRO else "5")),
    is_pro=IS_PRO,
    min_interval=float(os.environ.get("AICHAT_MINT_INTERVAL", "0.35")),
)
WORKER_THREADS = int(os.environ.get("AICHAT_THREADS", "256"))


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # sync endpoints run in anyio worker threads; default cap is 40 and would
    # bottleneck the gateway long before upstream does.
    from anyio import to_thread

    to_thread.current_default_thread_limiter().total_tokens = WORKER_THREADS
    yield


app = FastAPI(title="ai-chat-1.4.28-2api", version="1.0.0-r1", lifespan=_lifespan)

STATS = {
    "requests": 0,
    "upstream_ok": 0,
    "upstream_err": 0,
    "quota_rotations": 0,
    "latency_ms": [],
    "images": 0,
}
_MODEL_CACHE: dict[str, object] = {"ts": 0.0, "data": FALLBACK_MODELS}


# --------------------------------------------------------------------------- #
# auth: any bearer / api key is accepted, the gateway owns the upstream creds
# --------------------------------------------------------------------------- #
def _check_auth(request: Request) -> None:
    raw = request.headers.get("authorization") or ""
    token = raw[7:].strip() if raw.lower().startswith("bearer ") else raw.strip()
    if not token:
        token = (request.headers.get("x-api-key") or "").strip()
    if not token:
        raise HTTPException(status_code=401, detail={"error": {"message": "missing api key",
                                                             "type": "invalid_request_error"}})
    if token.startswith("sk-invalid"):
        raise HTTPException(status_code=401, detail={"error": {"message": "invalid api key",
                                                             "type": "invalid_request_error"}})


# --------------------------------------------------------------------------- #
# upstream call with automatic credential rotation on quota exhaustion
# --------------------------------------------------------------------------- #
def call_upstream(messages: list[dict], model: str, max_attempts: int = 3) -> ac.ChatResult:
    last: Exception | None = None
    for _ in range(max_attempts):
        try:
            acc = pool.acquire()
        except PoolEmpty as exc:
            last = exc
            break
        try:
            res = ac.send_chat(acc.id_token, messages, model=model, is_pro=IS_PRO)
            pool.report_ok(acc, res.limits.get("left"))
            STATS["upstream_ok"] += 1
            return res
        except ac.QuotaExhausted as exc:
            # this uid burned its 5 free messages -> drop it, next attempt gets a new one
            last = exc
            pool.report_limit(acc)
            STATS["quota_rotations"] += 1
            continue
        except ac.UpstreamHTTPError as exc:
            last = exc
            if exc.status in (401, 403):
                pool.report_dead(acc, exc.body[:120])
                continue
            if exc.status >= 500:
                time.sleep(0.4)
                continue
            raise
        except ac.AIChatError as exc:
            last = exc
            raise
    STATS["upstream_err"] += 1
    raise ac.AIChatError(f"upstream failed after {max_attempts} attempts: {last}")


def mock_result(messages: list[dict], model: str) -> ac.ChatResult:
    last_user = next((m["content"] for m in reversed(messages) if m.get("role") == "user"), "")
    text = (
        f"[mock:{model}] 收到 {len(messages)} 条消息，最后一条：{last_user[:120]}\n\n"
        "这是 AICHAT_MODE=mock 的本地回包，用于无网/离线验证网关链路（SSE、字段映射、错误分支）。"
    )
    return ac.ChatResult(content=text, model=model, message_id=str(uuid.uuid4()),
                         created=int(time.time() * 1000), limits={"left": 5, "max": 5})


# --------------------------------------------------------------------------- #
# schemas
# --------------------------------------------------------------------------- #
class ChatMessage(BaseModel):
    role: str
    content: str | None = None


class ChatCompletionRequest(BaseModel):
    model: str = DEFAULT_MODEL
    messages: list[ChatMessage] = Field(min_length=1)
    stream: bool = False
    temperature: float | None = None
    max_tokens: int | None = None
    user: str | None = None


class ImageRequest(BaseModel):
    model: str = DEFAULT_MODEL
    prompt: str = Field(min_length=1)
    n: int = 1
    size: str = "1024x1024"
    response_format: str = "b64_json"


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #
@app.get("/health")
def health() -> dict:
    lat = STATS["latency_ms"][-50:]
    return {
        "status": "ok",
        "mode": MODE,
        "is_pro": IS_PRO,
        "default_model": DEFAULT_MODEL,
        "pool": {"size": pool.snapshot()["usable"], "target": pool.size,
                 "strategy": pool.effective_mode(), "stats": pool.stats},
        "stats": {
            "requests": STATS["requests"],
            "upstream_ok": STATS["upstream_ok"],
            "upstream_err": STATS["upstream_err"],
            "quota_rotations": STATS["quota_rotations"],
            "images": STATS["images"],
            "latency_p50_ms": round(sorted(lat)[len(lat) // 2], 1) if lat else None,
        },
    }


@app.get("/v1/models")
def v1_models(request: Request) -> dict:
    _check_auth(request)
    now = time.time()
    if now - float(_MODEL_CACHE["ts"]) > 300:  # noqa: PLR2004
        try:
            acc = pool.acquire()
            live = ac.list_models(acc.id_token)
            if live:
                _MODEL_CACHE["data"] = [
                    {"id": m["id"], "owned_by": m.get("name", "ai-chat"), "locked": m.get("locked")}
                    for m in live
                ]
                _MODEL_CACHE["ts"] = now
        except ac.AIChatError:
            pass
    return {
        "object": "list",
        "data": [
            {"id": m["id"], "object": "model", "created": 0, "owned_by": m["owned_by"]}
            for m in _MODEL_CACHE["data"]  # type: ignore[union-attr]
        ],
    }


@app.post("/v1/chat/completions")
def chat_completions(req: ChatCompletionRequest, request: Request):
    _check_auth(request)
    STATS["requests"] += 1
    messages = [{"role": m.role, "content": m.content or ""} for m in req.messages]
    t0 = time.time()
    try:
        result = mock_result(messages, req.model) if MODE == "mock" else call_upstream(messages, req.model)
    except ac.AIChatError as exc:
        STATS["upstream_err"] += 1
        return JSONResponse(status_code=502, content={"error": {"message": str(exc),
                                                                "type": "upstream_error"}})
    elapsed_ms = (time.time() - t0) * 1000
    STATS["latency_ms"].append(elapsed_ms)
    STATS["latency_ms"] = STATS["latency_ms"][-200:]
    cid = f"chatcmpl-{result.message_id or uuid.uuid4().hex}"
    created = int(time.time())

    if not req.stream:
        return {
            "id": cid,
            "object": "chat.completion",
            "created": created,
            "model": result.model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": result.content},
                "finish_reason": "stop",
            }],
            "usage": {
                "prompt_tokens": max(1, sum(len(m["content"]) for m in messages) // 4),
                "completion_tokens": max(1, len(result.content) // 4),
                "total_tokens": max(2, (sum(len(m["content"]) for m in messages) + len(result.content)) // 4),
            },
        }

    def _sse() -> object:
        # upstream is non-streaming: chunk the finished text, do not claim incremental tokens
        base = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": result.model}
        yield f"data: {json.dumps({**base, 'choices': [{'index': 0, 'delta': {'role': 'assistant'}, 'finish_reason': None}]})}\n\n"
        text = result.content
        step = max(1, min(24, len(text) // 12 or 1))
        for i in range(0, len(text), step):
            chunk = {**base, "choices": [{"index": 0, "delta": {"content": text[i:i + step]}, "finish_reason": None}]}
            yield f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n"
            time.sleep(0.012)
        yield f"data: {json.dumps({**base, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]})}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(_sse(), media_type="text/event-stream")


@app.post("/v1/images/generations")
def images_generations(req: ImageRequest, request: Request):
    _check_auth(request)
    STATS["requests"] += 1
    STATS["images"] += 1
    out = None
    for _ in range(2):
        try:
            acc = pool.acquire()
        except PoolEmpty as exc:
            STATS["upstream_err"] += 1
            return JSONResponse(status_code=502, content={"error": {"message": str(exc),
                                                                    "type": "upstream_error"}})
        try:
            out = ac.generate_image(acc.id_token, req.prompt, model=req.model, is_pro=IS_PRO)
            pool.report_ok(acc, out["limits"].get("left"))
            break
        except ac.QuotaExhausted:
            pool.report_limit(acc)
            STATS["quota_rotations"] += 1
            continue
        except ac.UpstreamHTTPError as exc:
            if exc.status in (401, 403):
                pool.report_dead(acc, exc.body[:120])
                continue
            STATS["upstream_err"] += 1
            return JSONResponse(status_code=502, content={"error": {"message": str(exc),
                                                                    "type": "upstream_error"}})
        except ac.AIChatError as exc:
            STATS["upstream_err"] += 1
            return JSONResponse(status_code=502, content={"error": {"message": str(exc),
                                                                    "type": "upstream_error"}})
    if out is None:
        STATS["upstream_err"] += 1
        return JSONResponse(status_code=502, content={"error": {"message": "image generation failed",
                                                                "type": "upstream_error"}})
    b64 = out["b64_json"]
    item: dict[str, object] = {"revised_prompt": out["revised_prompt"]}
    if req.response_format == "b64_json":
        item["b64_json"] = b64
    else:
        # no public storage: expose inline data url so the field stays usable
        item["url"] = "data:image/png;base64," + b64
    return {"created": int(time.time()), "data": [item for _ in range(max(1, min(req.n, 2)))]}


@app.get("/admin/pool")
def admin_pool(request: Request) -> dict:
    _check_auth(request)
    return pool.snapshot()


@app.post("/admin/warm")
def admin_warm(request: Request, n: int = 1) -> dict:
    """Pre-mint accounts so the first user request does not pay the mint latency."""
    _check_auth(request)
    usable = pool.fill(max(1, min(n, 20)))
    return {"pool_size": usable, "stats": pool.stats}


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "demo.html")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=int(os.environ.get("AICHAT_PORT", "8960")))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--threads", type=int, default=WORKER_THREADS,
                        help="anyio worker threads for sync endpoints")
    args = parser.parse_args()
    import uvicorn

    globals()["WORKER_THREADS"] = args.threads
    print(f"[ai-chat-2api] mode={MODE} is_pro={IS_PRO} workers={args.threads} "
          f"-> http://{args.host}:{args.port}")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
