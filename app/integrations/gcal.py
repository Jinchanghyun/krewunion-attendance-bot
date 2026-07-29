"""구글 캘린더 연동 — 연차/부재 일정 종일 이벤트.

팀 공유 캘린더에 서비스 계정으로 등록하는 방식을 기본으로 한다.
Google Workspace 도메인 위임을 쓰면 개인 캘린더에도 등록 가능.
"""
from __future__ import annotations

from datetime import date, timedelta

from app.config import settings

_service = None


def enabled() -> bool:
    return settings.GCAL_ENABLED and bool(settings.GCAL_TEAM_CALENDAR_ID)


def _client():
    """서비스 계정으로 Calendar API 클라이언트 생성(지연 초기화)."""
    global _service
    if _service is not None:
        return _service
    import json
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    info = json.loads(settings.GCAL_SERVICE_ACCOUNT_JSON)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/calendar"])
    _service = build("calendar", "v3", credentials=creds, cache_discovery=False)
    return _service


def create_all_day_event(summary: str, start: date, end: date) -> str:
    """종일 이벤트 생성 → event_id 반환. end는 캘린더 규약상 배타적이라 +1일."""
    body = {
        "summary": summary,
        "start": {"date": start.isoformat()},
        "end": {"date": (end + timedelta(days=1)).isoformat()},
        "transparency": "opaque",
    }
    ev = _client().events().insert(calendarId=settings.GCAL_TEAM_CALENDAR_ID, body=body).execute()
    return ev["id"]


def update_event(event_id: str, start: date, end: date) -> None:
    body = {"start": {"date": start.isoformat()},
            "end": {"date": (end + timedelta(days=1)).isoformat()}}
    _client().events().patch(calendarId=settings.GCAL_TEAM_CALENDAR_ID,
                             eventId=event_id, body=body).execute()


def delete_event(event_id: str) -> None:
    _client().events().delete(calendarId=settings.GCAL_TEAM_CALENDAR_ID,
                              eventId=event_id).execute()


def sync_leave(leave_id: int) -> str | None:
    """연차 신청을 팀 캘린더에 종일 이벤트로 등록. 미설정이면 조용히 통과."""
    if not enabled():
        return None
    from app import repo
    lr = repo.get_leave(leave_id)
    summary = f"{lr['emp_name']} · {lr['kind_label']}"
    ev = create_all_day_event(summary, lr["start"], lr["end"])
    repo.set_leave_calendar_event(leave_id, ev)
    return ev
