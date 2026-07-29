"""Vercel 서버리스 진입점 — 최상위 `app`(FastAPI)을 노출.

Vercel이 FastAPI 프레임워크로 자동 인식해 모든 경로를 이 앱으로 라우팅한다.
(별도 rewrite를 쓰면 Vercel의 새 정책상 앱이 원래 경로를 못 받으므로 rewrite는 두지 않는다.)
"""
from app.web.admin import api as app  # noqa: F401
