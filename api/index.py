"""Vercel 서버리스 진입점.

Vercel의 Python 런타임은 `api/` 폴더의 파일에서 ASGI 앱 객체 `app`을 찾아 실행한다.
우리 FastAPI 앱(app.web.admin:api)을 그대로 노출한다.
"""
from app.web.admin import api as app  # noqa: F401
