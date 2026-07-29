"""근태 엔진 — 실근무·소정근로·연장·야간·휴일근로 자동 계산.

놀금·단축근무·반차는 schedule.effective_window 를 통해 소정근로시간에 반영된다.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from .schedule import effective_window, is_recovery_day, hm_to_min

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
) -> dict:
    """하루 근태 요약. 반환 단위는 분."""
    w = effective_window(config, d, leave_kind)
    scheduled = w["scheduled_minutes"]

    if checkin is None or checkout is None:
        return {"scheduled": scheduled, "worked": 0, "overtime": 0,
                "night": 0, "holiday": 0, "note": w["note"]}

    break_min = 0
    if config.get("break_start") and config.get("break_end") and leave_kind not in ("half_am", "half_pm"):
        break_min = hm_to_min(config["break_end"]) - hm_to_min(config["break_start"])

    worked = worked_minutes(checkin, checkout, break_min)
    is_holiday = is_company_holiday or d.weekday() >= 5 or is_recovery_day(config, d)
    holiday = worked if is_holiday else 0
    overtime = 0 if is_holiday else max(0, worked - scheduled)
    night = night_minutes(checkin, checkout)

    return {"scheduled": scheduled, "worked": worked, "overtime": overtime,
            "night": night, "holiday": holiday, "note": w["note"]}


def summarize_period(daily: list[dict]) -> dict:
    """일별 요약 리스트를 월/기간 합계로 집계."""
    keys = ("scheduled", "worked", "overtime", "night", "holiday")
    total = {k: sum(x.get(k, 0) for x in daily) for k in keys}
    total["over_52h_weeks"] = 0  # 주 52시간 초과 판정은 주 단위 집계에서 별도 처리
    return total
