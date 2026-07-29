"""관리자 웹 API (FastAPI) + Slack HTTP 엔드포인트 + Cron.

- 관리자 통계는 어느 컴퓨터에서든 브라우저로 접근(로그인/권한 필요, CORS 허용).
- 서버리스(Vercel 등)에서는 Slack을 HTTP Request URL 모드로, 스케줄러는 Cron으로 구동.
"""
from __future__ import annotations

import os
import pathlib

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from app.config import settings
from app import repo

api = FastAPI(title="크루유니언 근태봇 · 관리자 통계 API")

api.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.CORS_ORIGINS.split(",")],
    allow_methods=["*"], allow_headers=["*"], allow_credentials=True,
)

_DASHBOARD = pathlib.Path(__file__).parent / "dashboard.html"


@api.on_event("startup")
def _startup():
    """부팅 시 테이블이 없으면 생성(간단 배포용). 운영 마이그레이션은 Alembic 권장.
    DB 미설정이어도 대시보드 페이지는 뜨도록 실패를 삼킨다."""
    try:
        from app.db import init_db
        init_db()
    except Exception as e:  # DATABASE_URL 미설정 등
        print("startup init_db skipped:", e)


@api.get("/", response_class=HTMLResponse)
def dashboard():
    """루트에서 관리자 대시보드를 제공. API 주소는 배포된 origin으로 자동 설정.
    로컬 데모(DEMO_AUTOTOKEN 설정 시)에서는 토큰까지 자동 입력된다."""
    html = _DASHBOARD.read_text(encoding="utf-8")
    inject = "<script>window.DEMO_API=location.origin;"
    tok = os.getenv("DEMO_AUTOTOKEN")
    if tok:
        inject += f"window.DEMO_TOKEN='{tok}';"
    inject += "</script>"
    return html.replace("</head>", inject + "</head>")


@api.get("/healthz")
def healthz():
    return {"ok": True}


# ── 인증: 관리자(인사담당자/시스템관리자)만 통계 접근 ──
def require_admin(authorization: str = Header(default="")) -> dict:
    """JWT 검증. 토큰의 slack_user_id로 역할을 확인해 hr/sysadmin만 허용."""
    token = authorization.replace("Bearer ", "")
    if not token:
        raise HTTPException(401, "로그인이 필요합니다.")
    import jwt
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
    except Exception:
        raise HTTPException(401, "유효하지 않은 토큰입니다.")
    role = repo.role_of(payload.get("slack_user_id", ""))
    if role not in ("hr", "sysadmin"):
        raise HTTPException(403, "통계 접근 권한이 없습니다.")
    return {"role": role, "slack_user_id": payload.get("slack_user_id")}


# ── 통계 엔드포인트 (웹 대시보드가 호출) ───────────────
@api.get("/api/stats/overview")
def stats_overview(_: dict = Depends(require_admin)):
    return repo.stats_overview()


@api.get("/api/stats/monthly")
def stats_monthly(month: str, _: dict = Depends(require_admin)):
    return repo.stats_monthly(month)


@api.get("/api/stats/by-employee")
def stats_by_employee(month: str, _: dict = Depends(require_admin)):
    return repo.stats_by_employee(month)


@api.get("/api/live")
def live_status(status: str | None = None, dept: str | None = None,
               _: dict = Depends(require_admin)):
    return repo.live_status(status, dept)


@api.get("/api/approvals/pending")
def pending_approvals(_: dict = Depends(require_admin)):
    return repo.pending_approvals()


# ── Slack HTTP 모드 (서버리스 배포용) ──────────────────
# from slack_bolt.adapter.fastapi import SlackRequestHandler
# from app.slack.app import app as slack_app
# _handler = SlackRequestHandler(slack_app)
#
# @api.post("/slack/events")
# async def slack_events(req: Request):
#     return await _handler.handle(req)


# ── Cron: 예약 출퇴근 알림 스캔 (Celery beat 대체) ─────
@api.post("/cron/checkin-scan")
def cron_checkin_scan(x_cron_secret: str = Header(default="")):
    """Vercel Cron 등이 5~10분마다 호출. 상시 워커 없이 예약 알림 발송."""
    if x_cron_secret != settings.JWT_SECRET:   # 간단한 공유 시크릿 보호
        raise HTTPException(403, "forbidden")
    from app.scheduler.tasks import run_send_due_checkin_prompts
    return {"sent": run_send_due_checkin_prompts()}
