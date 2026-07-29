"""Vercel 서버리스 진입점.

Vercel의 Python 런타임은 `api/` 폴더의 파일에서 ASGI 앱 객체 `app`을 찾아 실행한다.
우리 FastAPI 앱(app.web.admin:api)을 노출한다. 임포트가 실패하면 그 traceback을
HTTP 응답으로 그대로 보여줘서 원인을 진단할 수 있게 한다.
"""
try:
    from app.web.admin import api as app  # noqa: F401
except Exception as _e:  # pragma: no cover
    import traceback
    _tb = traceback.format_exc()
    from fastapi import FastAPI

    app = FastAPI()

    @app.get("/{full_path:path}")
    def _import_error(full_path: str):
        return {"import_error": str(_e), "traceback": _tb.splitlines()[-25:]}
