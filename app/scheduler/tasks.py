"""스케줄러 — 예약 출퇴근 알림 · 미출근 리마인드 · 캘린더 동기화.

핵심 로직은 plain 함수로 두고, Celery 태스크와 서버리스 Cron 엔드포인트가
둘 다 재사용한다(호스팅에 종속되지 않게).
"""
from __future__ import annotations

from datetime import datetime

from app import repo
from app.slack.app import app as slack_app
from app.slack import views
from app.integrations import gcal

# Celery는 상시 워커가 있을 때만. 서버리스(Vercel)면 run_* 함수를 Cron이 직접 호출.
try:
    from celery import Celery
    from app.config import settings
    celery = Celery("sam", broker=settings.REDIS_URL, backend=settings.REDIS_URL)
except Exception:  # pragma: no cover
    celery = None


# ── 순수 실행 로직 (Cron / Celery 공용) ───────────────
def _now():
    """설정 시간대(기본 Asia/Seoul) 기준 현재 시각."""
    from zoneinfo import ZoneInfo
    from app.config import settings
    return datetime.now(ZoneInfo(settings.TIMEZONE))


def run_send_due_checkin_prompts(now: datetime | None = None) -> int:
    """지금 출근 시각이 된 직원에게 출근 버튼 메시지 발송. 발송 건수 반환."""
    now = now or _now()
    sent = 0
    for emp in repo.employees_due_for_checkin(now):   # 놀금·휴가·기출근·중복 제외
        slack_app.client.chat_postMessage(
            channel=emp["slack_user_id"],
            blocks=views.checkin_prompt(emp, emp.get("checkin", "09:00")))
        sent += 1
    return sent


def run_send_due_checkout_prompts(now: datetime | None = None) -> int:
    """지금 퇴근 시각이 된 직원(출근했고 미퇴근)에게 퇴근 버튼 메시지 발송."""
    now = now or _now()
    sent = 0
    for emp in repo.employees_due_for_checkout(now):
        slack_app.client.chat_postMessage(
            channel=emp["slack_user_id"],
            blocks=views.checkout_prompt(emp, emp.get("checkout", "18:00")))
        sent += 1
    return sent


def run_send_daily_off_announcement(now: datetime | None = None) -> int:
    """그날 휴무자(연차·놀금 등)를 한 채널에 요약 발송. 발송 인원 수 반환.
    휴무자가 없으면 발송하지 않음(채널 소음 방지)."""
    from app.config import settings
    people = repo.today_off_people()
    if not people:
        return 0
    lines = []
    for p in people:
        who = f"{p['name']} ({p['display_name']})" if p["display_name"] else p["name"]
        lines.append(f"• {who}님 — {p['label']}")
    text = ":palm_tree: *오늘 휴무 안내*\n" + "\n".join(lines)
    slack_app.client.chat_postMessage(channel=settings.OFF_ANNOUNCE_CHANNEL, text=text)
    return len(people)


def _dayoff_dm_text(name: str, days: list[str]) -> str:
    rng = f"{days[0]} ~ {days[-1]}" if len(days) > 1 else days[0]
    return (f":beach_with_umbrella: *{name}님, 이번 달 소정근로시간을 모두 채우셨어요!*\n"
            f"남은 근무일({rng}, {len(days)}일)은 자동으로 *데이오프*로 처리됩니다. "
            f"출근하지 않으셔도 됩니다. :tada:")


def run_apply_auto_dayoffs(now: datetime | None = None) -> int:
    """선택근무자 중 이번 달 소정근로를 방금 충족한 사람에게 자동 데이오프 처리 + DM.
    새로 데이오프가 생성된(=처음 충족한) 인원 수 반환."""
    now = now or _now()
    d = now.date()
    triggered = 0
    for emp in repo.selective_employees():
        newly = repo.apply_auto_dayoff(emp["id"], d)
        if not newly:
            continue
        try:
            slack_app.client.chat_postMessage(
                channel=emp["slack_user_id"], text=_dayoff_dm_text(emp["name"], newly))
            triggered += 1
        except Exception as ex:
            print("auto-dayoff DM failed:", ex)
    return triggered


def run_sync_leave_calendar(leave_id: int) -> str | None:
    """연차를 구글 캘린더에 종일 이벤트로 등록하고 event_id 저장."""
    if not gcal.enabled():
        return None
    req = repo.get_leave(leave_id) if hasattr(repo, "get_leave") else None
    if req is None:
        return None
    event_id = gcal.create_all_day_event(
        summary=f"{req['emp_name']} · {req['kind_label']}",
        start=req["start"], end=req["end"])
    repo.set_leave_calendar_event(leave_id, event_id)
    return event_id


# ── Celery 래퍼 (상시 워커 배포용) ────────────────────
if celery is not None:
    @celery.task
    def send_due_checkin_prompts():
        return run_send_due_checkin_prompts()

    @celery.task(bind=True, max_retries=3)
    def sync_leave_calendar(self, leave_id: int):
        try:
            return run_sync_leave_calendar(leave_id)
        except Exception as exc:  # 캘린더 실패는 근태 기록에 영향 없음 → 재시도
            raise self.retry(exc=exc, countdown=60)

    # Celery beat: 5분마다 출근 알림 대상 스캔
    celery.conf.beat_schedule = {
        "checkin-scan": {"task": "app.scheduler.tasks.send_due_checkin_prompts",
                         "schedule": 300.0},
    }
else:  # 서버리스: 데코레이터 없는 호출용 shim (tasks.sync_leave_calendar.delay(...) 호환)
    class _Shim:
        def __init__(self, fn):
            self._fn = fn

        def delay(self, *a, **k):
            return self._fn(*a, **k)

        __call__ = lambda self, *a, **k: self._fn(*a, **k)

    send_due_checkin_prompts = _Shim(run_send_due_checkin_prompts)
    sync_leave_calendar = _Shim(run_sync_leave_calendar)
