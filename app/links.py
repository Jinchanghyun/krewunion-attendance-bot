"""Slack 홈 버튼용 짧은 매직 링크 코드 (무상태·서명형).

긴 JWT를 URL에 노출하지 않도록, slack_user_id·target·만료를 서명한 짧은 코드를 만든다.
/go/{code} 에서 검증 후 실제 페이지로 리다이렉트한다.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time

from app.config import settings


def _sign(msg: bytes) -> str:
    digest = hmac.new(settings.JWT_SECRET.encode(), msg, hashlib.sha256).digest()[:12]
    return base64.urlsafe_b64encode(digest).decode().rstrip("=")


def make_code(slack_user_id: str, target: str, hours: int = 12) -> str:
    exp = int(time.time()) + hours * 3600
    msg = f"{slack_user_id}|{target}|{exp}".encode()
    payload = base64.urlsafe_b64encode(msg).decode().rstrip("=")
    return f"{payload}.{_sign(msg)}"


def parse_code(code: str) -> dict | None:
    try:
        payload, sig = code.split(".", 1)
        msg = base64.urlsafe_b64decode(payload + "===")
        if not hmac.compare_digest(_sign(msg), sig):
            return None
        slack_user_id, target, exp = msg.decode().split("|")
        if int(exp) < time.time():
            return None
        return {"slack_user_id": slack_user_id, "target": target}
    except Exception:
        return None
