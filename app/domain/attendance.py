"""근태 엔진 — 실근무·소정근로·연장·야간·휴일근로 자동 계산.

놀금·단축근무·반차는 schedule.effective_window 를 통해 소정근로시간에 반영된다.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from .schedule import effective_window, is_recovery_day, hm_to_min
from .holidays import is_public_holiday

NIGHT_START = 22 * 60  # 22:00
NIGHT_END = 6 * 60     # 06:00


def worked_minutes(checkin: datetime, checkout: datetime, break_min: int) -> int:
    return max(0, int((checkout - checkin).total_seconds() // 60) - break_min)


def night_minutes(start: datetime, end: datetime) -> int:
    """22:00~06:00 사이 근무 분. 자정을 넘겨도 분 단위로 정확히 집계."""
    mins, cur = 0, start
    while cur < end:
        mm = cur.hour * 60 + cur.minute
        if mm >= NIGHT_START or mm < NIGHT_END:
            mins += 1
        cur += timedelta(minutes=1)
    return mins


def summarize_day(
    config: dict,
    d: date,
    checkin: datetime | None,
    checkout: datetime | None,
    leave_kind: str | None = None,
    is_company_holiday: bool = False,
    on_dayoff: bool = False,
) -> dict:
    """하루 근태 요약. 반환 단위는 분.
    on_dayoff=True: 데이오프(소정 근무일 아님)에 근무 → 소정 0, 근무시간 전부가 초과근로."""
    w = effective_window(config, d, leave_kind)
    scheduled = 0 if on_dayoff else w["scheduled_minutes"]

    if checkin is None or checkout is None:
        return {"scheduled": scheduled, "worked": 0, "overtime": 0,
                "night": 0, "holiday": 0, "note": ("데이오프 근무" if on_dayoff else w["note"])}

    break_min = 0
    if config.get("break_start") and config.get("break_end") and leave_kind not in ("half_am", "half_pm"):
        break_min = hm_to_min(config["break_end"]) - hm_to_min(config["break_start"])

    worked = worked_minutes(checkin, checkout, break_min)
    # 휴일근로: 일요일(6)·공휴일·놀금만. 토요일(5)은 '무급 휴무'이지 휴일근로가 아님.
    is_holiday = (is_company_holiday or d.weekday() == 6
                  or is_recovery_day(config, d) or is_public_holiday(d))
    work_type = config.get("work_type", "normal")
    nf = work_type in ("normal", "flex")   # 일 8h·주 40h 정산
    if on_dayoff:
        # 데이오프 근무: 소정근로일이 아니므로 전부 초과근로로 집계
        holiday = 0
        overtime = worked
    elif is_holiday:
        # 휴일근로. 일반·시차는 주 40h 초과이므로 휴일근로+초과근로 동시 발생.
        holiday = worked
        overtime = worked if nf else 0
    elif d.weekday() == 5:
        # 토요일(무급휴무) 근무: 주 40h 초과 → 일반·시차는 전부 초과근로.
        holiday = 0
        overtime = worked if nf else 0
    else:
        holiday = 0
        overtime = max(0, worked - scheduled)
    night = night_minutes(checkin, checkout)

    return {"scheduled": scheduled, "worked": worked, "overtime": overtime,
            "night": night, "holiday": holiday, "note": ("데이오프 근무" if on_dayoff else w["note"])}


def summarize_period(daily: list[dict]) -> dict:
    """일별 요약 리스트를 월/기간 합계로 집계."""
    keys = ("scheduled", "worked", "overtime", "night", "holiday")
    total = {k: sum(x.get(k, 0) for x in daily) for k in keys}
    total["over_52h_weeks"] = 0  # 주 52시간 초과 판정은 주 단위 집계에서 별도 처리
    return total
