"""도메인 엔진 검증 — 놀금 · 단축근무 · 반차 · 연장 · 승인."""
from datetime import date, datetime

from app.domain import schedule, attendance, leave, approval

CONFIG = {
    "employee_id": "K-2041",
    "work_type": "normal",
    "checkin": "09:00", "checkout": "18:00",
    "break_start": "12:00", "break_end": "13:00",
    "recovery": {"mode": "biweekly", "anchor": "2026-07-31", "custom": []},
    "short_rules": [
        {"kind": "late", "amount_min": 30, "repeat": "weekly", "weekdays": [1]},   # 매주 월 출근 30분 늦춤
        {"kind": "early", "amount_min": 30, "repeat": "weekly", "weekdays": [5]},  # 매주 금 퇴근 30분 당김
    ],
}


# ── 놀금(리커버리데이) ────────────────────────────────
def test_last_friday():
    assert schedule.last_friday(2026, 7) == date(2026, 7, 31)
    assert schedule.last_friday(2026, 2) == date(2026, 2, 27)


def test_biweekly_recovery():
    # 기준 2026-07-31(금)부터 격주 금요일
    assert schedule.is_recovery_day(CONFIG, date(2026, 7, 31)) is True
    assert schedule.is_recovery_day(CONFIG, date(2026, 8, 14)) is True   # +14일
    assert schedule.is_recovery_day(CONFIG, date(2026, 8, 7)) is False   # 다음 주 금
    assert schedule.is_recovery_day(CONFIG, date(2026, 8, 13)) is False  # 목요일


def test_recovery_day_is_zero_scheduled():
    w = schedule.effective_window(CONFIG, date(2026, 7, 31))
    assert w["scheduled_minutes"] == 0


# ── 단축근무 ──────────────────────────────────────────
def test_short_rule_monday_late():
    # 2026-08-03은 월요일 → 출근 09:30, 소정 480-30=450분
    w = schedule.effective_window(CONFIG, date(2026, 8, 3))
    assert w["checkin"] == "09:30"
    assert w["scheduled_minutes"] == 450


def test_short_rule_friday_early():
    # 2026-08-07은 금요일 → 퇴근 17:30, 소정 450분 (놀금 아닌 금요일)
    assert schedule.is_recovery_day(CONFIG, date(2026, 8, 7)) is False
    w = schedule.effective_window(CONFIG, date(2026, 8, 7))
    assert w["checkout"] == "17:30"
    assert w["scheduled_minutes"] == 450


# ── 반차 ──────────────────────────────────────────────
def test_half_am_and_pm_scheduled():
    # 화요일(단축 없음)
    d = date(2026, 8, 4)
    assert schedule.effective_window(CONFIG, d, "half_am")["scheduled_minutes"] == 240
    assert schedule.effective_window(CONFIG, d, "half_pm")["scheduled_minutes"] == 240
    assert schedule.effective_window(CONFIG, d, "annual")["scheduled_minutes"] == 0


# ── 근태 집계: 연장·야간 ───────────────────────────────
def test_overtime_and_night():
    d = date(2026, 8, 4)  # 화, 소정 480분
    s = attendance.summarize_day(
        CONFIG, d,
        datetime(2026, 8, 4, 9, 0),
        datetime(2026, 8, 4, 23, 0),   # 09:00~23:00, 휴게 60분
    )
    assert s["worked"] == 13 * 60          # 780분
    assert s["overtime"] == 780 - 480      # 300분
    assert s["night"] == 60                # 22:00~23:00


def test_holiday_work_counts_as_holiday_not_overtime():
    d = date(2026, 8, 9)  # 일요일 → 휴일근로
    s = attendance.summarize_day(
        CONFIG, d,
        datetime(2026, 8, 9, 10, 0),
        datetime(2026, 8, 9, 15, 0),
    )
    # 10:00~15:00(5h)에서 휴게 12:00~13:00(60분) 제외 → 240분, 전부 휴일근로
    assert s["holiday"] == 4 * 60
    assert s["overtime"] == 0


def test_saturday_is_not_holiday():
    # 토요일은 '무급 휴무'이지 휴일근로가 아님
    d = date(2026, 8, 8)  # 토요일
    s = attendance.summarize_day(
        CONFIG, d,
        datetime(2026, 8, 8, 10, 0),
        datetime(2026, 8, 8, 15, 0),
    )
    assert s["holiday"] == 0


# ── 연차 소진 ──────────────────────────────────────────
def test_leave_deduction():
    assert leave.deduct_days(CONFIG, "half_am", date(2026, 8, 4)) == 0.5
    # 8/3(월)~8/7(금): 5일 중 놀금 없음 → 5일. (8/7은 놀금 아님)
    assert leave.deduct_days(CONFIG, "annual", date(2026, 8, 3), date(2026, 8, 7)) == 5.0
    assert leave.apply_leave(10.0, 0.5) == 9.5


# ── 승인 상태머신 ──────────────────────────────────────
def test_approval_flow():
    assert approval.requires_approval("overtime") is True
    assert approval.requires_approval("annual") is False
    st = approval.create("overtime")
    assert st == "requested"
    assert approval.decide(st, "approve", actor_is_manager=True) == "approved"


def test_approval_guards():
    import pytest
    st = approval.create("holiday")
    with pytest.raises(approval.ApprovalError):
        approval.decide(st, "approve", actor_is_manager=False)      # 권한 없음
    with pytest.raises(approval.ApprovalError):
        approval.decide("approved", "reject", actor_is_manager=True)  # 이미 처리됨
    with pytest.raises(approval.ApprovalError):
        approval.create("annual")                                    # 승인 대상 아님
