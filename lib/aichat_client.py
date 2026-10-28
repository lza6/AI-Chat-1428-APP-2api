"""
AI Chat 1.4.28 (com.appstation.chatgpt / firebase project ai-chat-green-droid)
upstream client.

Protocol (reversed from assets/index.android.bundle, Hermes HBC):
  base   : https://chatadmin.org/gd-api/v1
  auth   : Authorization: Bearer <firebase anonymous idToken>
  idp    : https://identitytoolkit.googleapis.com/v1/accounts:signUp?key=<apiKey>

Endpoints:
  GET  /chat/models?lang=en
  GET  /chat/categories?lang=en&ver=2
  GET  /chat/get-limit?special=
  POST /chat/send            {messages, isPro, model, customer}
  POST /chat/image           {isPro, model, prompt, customer}
  POST /chat/reward          {user}
  POST /chat/report          {id}
  POST /chat/make-name       {language, userMessage, assistantMessage}
  POST /user/sync-data       {time_offset, timezone, pushToken, language, platform, customerInfo}

Quota model (measured):
  - isPro=false : 5 free messages per anonymous uid, then {"success":false,"reason":"LIMIT"}
  - isPro=true  : counter still decrements but is NOT enforced -> effectively unlimited
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

import config as cfg  # noqa: E402  (credentials live in config.local.json / env)

FIREBASE_API_KEY = cfg.FIREBASE_API_KEY
FIREBASE_APP_ID = cfg.FIREBASE_APP_ID
FIREBASE_PROJECT = cfg.FIREBASE_PROJECT
IDP_SIGNUP = cfg.IDP_SIGNUP
IDP_REFRESH = cfg.IDP_REFRESH
API_BASE = cfg.API_BASE

# server appends this promo/upgrade banner to message.content
_PROMO_RE = re.compile(
    r"\n*-{3,}\s*\n?\s*(?:⚠️|⚠)?\s*You are using an outdated version.*$",
    re.DOTALL | re.IGNORECASE,
)


class AIChatError(Exception):
    """transport / protocol level failure"""


class UpstreamHTTPError(AIChatError):
    def __init__(self, status: int, body: str, url: str = ""):
        super().__init__(f"upstream {status}: {body[:300]}")
        self.status = status
        self.body = body
        self.url = url


class QuotaExhausted(AIChatError):
    """{"success":false,"reason":"LIMIT"} - anonymous uid has used its 5 messages"""


@dataclass
class ChatResult:
    content: str
    model: str
    message_id: str = ""
    created: int = 0
    type: str = "text"
    limits: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


def strip_promo(text: str) -> str:
    if not text:
        return text
    return _PROMO_RE.sub("", text).strip()


_TLS = threading.local()


def _session() -> "requests.Session":
    """One keep-alive session per thread.

    Every request otherwise pays a fresh TLS handshake; behind a local egress
    proxy that is what saturates first at ~80 concurrent callers.
    """
    sess = getattr(_TLS, "sess", None)
    if sess is None:
        import requests
        from requests.adapters import HTTPAdapter

        sess = requests.Session()
        adapter = HTTPAdapter(pool_connections=8, pool_maxsize=8, max_retries=0)
        sess.mount("https://", adapter)
        sess.mount("http://", adapter)
        _TLS.sess = sess
    return sess


def _request(
    url: str,
    body: Any | None = None,
    token: str | None = None,
    method: str = "POST",
    timeout: float = 180.0,
    form: bool = False,
    retries: int = 2,
) -> tuple[int, str]:
    headers = {"Content-Type": "application/x-www-form-urlencoded" if form else "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data: bytes | None = None
    if body is not None:
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()

    last: Exception | None = None
    for attempt in range(retries + 1):
        try:
            resp = _session().request(method, url, data=data, headers=headers, timeout=timeout)
            return resp.status_code, resp.text
        except Exception as exc:  # noqa: BLE001 - transport level: retry then surface
            last = exc
            if attempt < retries:
                time.sleep(0.4 * (attempt + 1))
    raise AIChatError(f"transport failure: {last!r}") from last


# --------------------------------------------------------------------------- #
# anonymous firebase credential
# --------------------------------------------------------------------------- #
@dataclass
class AnonCredential:
    id_token: str
    refresh_token: str
    uid: str
    expires_at: float
    requests_used: int = 0

    @property
    def expired(self) -> bool:
        # refresh a bit early, firebase idToken lives 3600s
        return time.time() > self.expires_at - 300


def signup_anonymous(timeout: float = 40.0) -> AnonCredential:
    """Create a brand new anonymous firebase user. Each call = fresh 5-msg quota."""
    cfg.assert_configured()
    status, text = _request(
        IDP_SIGNUP.format(key=FIREBASE_API_KEY),
        {"returnSecureToken": True},
        timeout=timeout,
    )
    if status != 200:
        raise AIChatError(f"anonymous signup failed {status}: {text[:300]}")
    data = json.loads(text)
    return AnonCredential(
        id_token=data["idToken"],
        refresh_token=data["refreshToken"],
        uid=data["localId"],
        expires_at=time.time() + int(data.get("expiresIn", 3600)),
    )


def refresh_credential(cred: AnonCredential, timeout: float = 40.0) -> AnonCredential:
    status, text = _request(
        IDP_REFRESH.format(key=FIREBASE_API_KEY),
        f"grant_type=refresh_token&refresh_token={cred.refresh_token}",
        timeout=timeout,
        form=True,
    )
    if status != 200:
        raise AIChatError(f"token refresh failed {status}: {text[:300]}")
    data = json.loads(text)
    return AnonCredential(
        id_token=data["id_token"],
        refresh_token=data["refresh_token"],
        uid=data.get("user_id", cred.uid),
        expires_at=time.time() + int(data.get("expires_in", 3600)),
        requests_used=cred.requests_used,
    )


# --------------------------------------------------------------------------- #
# upstream calls
# --------------------------------------------------------------------------- #
def list_models(token: str, lang: str = "en", timeout: float = 40.0) -> list[dict[str, Any]]:
    status, text = _request(f"{API_BASE}/chat/models?lang={lang}", None, token, "GET", timeout)
    if status != 200:
        raise UpstreamHTTPError(status, text, "chat/models")
    data = json.loads(text)
    if not data.get("success"):
        raise AIChatError(f"models failed: {text[:200]}")
    return data.get("data") or []


def get_limit(token: str, special: str = "", timeout: float = 40.0) -> dict[str, Any]:
    status, text = _request(f"{API_BASE}/chat/get-limit?special={special}", None, token, "GET", timeout)
    if status != 200:
        raise UpstreamHTTPError(status, text, "chat/get-limit")
    return json.loads(text).get("limit") or {}


def send_chat(
    token: str,
    messages: list[dict[str, str]],
    model: str = "gpt-4o-mini",
    is_pro: bool = True,
    customer: Any = None,
    timeout: float = 180.0,
) -> ChatResult:
    payload = {"messages": messages, "isPro": bool(is_pro), "model": model, "customer": customer}
    status, text = _request(f"{API_BASE}/chat/send", payload, token, "POST", timeout)
    if status != 200:
        raise UpstreamHTTPError(status, text, "chat/send")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AIChatError(f"non-json response: {text[:200]}") from exc
    if not data.get("success"):
        if str(data.get("reason", "")).upper() == "LIMIT":
            raise QuotaExhausted(text[:200])
        raise AIChatError(f"send failed: {text[:300]}")
    msg = data.get("message") or {}
    return ChatResult(
        content=strip_promo(msg.get("content") or ""),
        model=model,
        message_id=str(msg.get("id") or ""),
        created=int(msg.get("created") or 0),
        type=msg.get("type") or "text",
        limits=data.get("limits") or {},
        raw=data,
    )


def generate_image(
    token: str,
    prompt: str,
    model: str = "gpt-4o-mini",
    is_pro: bool = True,
    customer: Any = None,
    timeout: float = 300.0,
) -> dict[str, Any]:
    payload = {"isPro": bool(is_pro), "model": model, "prompt": prompt, "customer": customer}
    status, text = _request(f"{API_BASE}/chat/image", payload, token, "POST", timeout)
    if status != 200:
        raise UpstreamHTTPError(status, text, "chat/image")
    data = json.loads(text)
    if not data.get("success"):
        raise AIChatError(f"image failed: {text[:300]}")
    msg = data.get("message") or {}
    return {
        "b64_json": msg.get("embedded_img") or "",
        "revised_prompt": strip_promo(msg.get("content") or ""),
        "id": str(msg.get("id") or ""),
        "created": int(msg.get("created") or 0),
        "limits": data.get("limits") or {},
    }
