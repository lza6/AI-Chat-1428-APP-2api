"""
Configuration for the AI Chat 1.4.28 client.

Credentials are NOT shipped in this repository:

  * locally: put them in `config.local.json` next to this file's parent dir
              (git-ignored), or export the env vars below
  * values come from the APK's resources — see docs/PROTOCOL.md §0

Env overrides (take precedence over config.local.json):
    AICHAT_FIREBASE_API_KEY
    AICHAT_FIREBASE_APP_ID
    AICHAT_FIREBASE_PROJECT
    AICHAT_API_BASE
"""

from __future__ import annotations

import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "config.local.json"

_DEFAULTS = {
    "firebase_api_key": "REPLACE_ME_FIREBASE_WEB_API_KEY",
    "firebase_app_id": "REPLACE_ME_FIREBASE_APP_ID",
    "firebase_project": "REPLACE_ME_FIREBASE_PROJECT_ID",
    "api_base": "https://chatadmin.org/gd-api/v1",
    "idp_signup": "https://identitytoolkit.googleapis.com/v1/accounts:signUp?key={key}",
    "idp_refresh": "https://securetoken.googleapis.com/v1/token?key={key}",
}

_ENV = {
    "firebase_api_key": "AICHAT_FIREBASE_API_KEY",
    "firebase_app_id": "AICHAT_FIREBASE_APP_ID",
    "firebase_project": "AICHAT_FIREBASE_PROJECT",
    "api_base": "AICHAT_API_BASE",
}


def load() -> dict:
    cfg = dict(_DEFAULTS)
    if CONFIG_FILE.exists():
        try:
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError) as exc:
            raise RuntimeError(f"config.local.json is not valid JSON: {exc}") from exc
    for key, env in _ENV.items():
        value = os.environ.get(env)
        if value:
            cfg[key] = value
    return cfg


CFG = load()

FIREBASE_API_KEY: str = CFG["firebase_api_key"]
FIREBASE_APP_ID: str = CFG["firebase_app_id"]
FIREBASE_PROJECT: str = CFG["firebase_project"]
API_BASE: str = CFG["api_base"]
IDP_SIGNUP: str = CFG["idp_signup"]
IDP_REFRESH: str = CFG["idp_refresh"]


def assert_configured() -> None:
    if FIREBASE_API_KEY.startswith("REPLACE_ME"):
        raise RuntimeError(
            "Firebase API key is not configured.\n"
            "  1) pull it out of the APK:\n"
            "       apktool d -s -o out app.apk\n"
            "       grep -E 'google_api_key|google_app_id|project_id' out/res/values/strings.xml\n"
            "  2) write config.local.json:\n"
            "       {\"firebase_api_key\": \"AIza...\", \"firebase_app_id\": \"1:...:android:...\",\n"
            "        \"firebase_project\": \"...\"}\n"
            "     or export AICHAT_FIREBASE_API_KEY=AIza...\n"
            "  See docs/PROTOCOL.md §0 for the exact commands."
        )
