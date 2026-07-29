"""환경설정 — 환경변수에서 로드. 실제 값은 .env / 시크릿 매니저로 주입."""
from __future__ import annotations

import os


class Settings:
    # Slack
    SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
    SLACK_SIGNING_SECRET = os.getenv("SLACK_SIGNING_SECRET", "")
    SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN", "")        # Socket Mode용
    USE_SOCKET_MODE = os.getenv("USE_SOCKET_MODE", "true").lower() == "true"
    # 화이트리스트: 비우면 '등록된 직원 전부' 허용, 값이 있으면 그 slack_user_id만 허용
    SLACK_ALLOWLIST = [x.strip() for x in os.getenv("SLACK_ALLOWLIST", "").split(",") if x.strip()]

    # DB / 큐
    DATABASE_URL = os.getenv("DATABASE_URL", "postgresql+psycopg://sam:sam@localhost/sam")
    REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")

    # 관리자 웹
    JWT_SECRET = os.getenv("JWT_SECRET", "change-me")
    CORS_ORIGINS = os.getenv("CORS_ORIGINS", "*")            # 어느 컴퓨터에서든 접근
    WEB_BASE_URL = os.getenv("WEB_BASE_URL", "https://krewunion-attendance-bot.vercel.app")

    # Google Calendar
    GCAL_ENABLED = os.getenv("GCAL_ENABLED", "false").lower() == "true"
    GCAL_TEAM_CALENDAR_ID = os.getenv("GCAL_TEAM_CALENDAR_ID", "")
    GCAL_SERVICE_ACCOUNT_JSON = os.getenv("GCAL_SERVICE_ACCOUNT_JSON", "")

    # 근무 기본값
    DEFAULT_CHECKIN = os.getenv("DEFAULT_CHECKIN", "09:00")
    DEFAULT_CHECKOUT = os.getenv("DEFAULT_CHECKOUT", "18:00")
    REMIND_AFTER_MIN = int(os.getenv("REMIND_AFTER_MIN", "30"))


settings = Settings()
