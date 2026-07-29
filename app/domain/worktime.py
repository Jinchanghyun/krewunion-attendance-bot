"""근무제도별 월 근로시간 요약 — 소정 / 실근로 / 휴가사용 / 연장근로.

규칙(창현 요청):
- 선택적 근무(selective): 월 단위. 소정근로시간 대비 (실근로 + 휴가사용)으로 충족 판단.
  실근로시간이 월 소정근로시간을 초과하면 그 초과분이 연장근로.
- 시차 근무(flex): 일 단위. 하루 실근로 8시간 초과분이 연장근로(합산).
- 최대 연장근로 24시간/월.
- 휴가 사용 시간은 실근로에서 제외(휴가일에는 실근로 기록이 없음)하되, 소정 충족에는 포함.
"""
from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta

from .schedule import FRI, effective_window, is_recovery_day

STD_DAY_MIN = 480          # 소정 1일 = 8시간
HALF_MIN = 240             # 반일 = 4시간
MAX_OT_MIN = 24 * 60       # 월 최대 연장근로 24시간

# 반일(4시간) 휴가 종류
_HALF_KINDS = {"half_am", "half_pm", "health_check_am", "health_check_pm",
               "birthday_am", "birthday_pm"}
# 하루 단위(당일만) 휴가 종류
_SINGLE_DAY = _HALF_KINDS | {"health_check_full", "birthday_full", "seollal",
                             "chuseok", "health", "bd"}


def _leave_minutes_per_day(kind: str, leave_config: dict | None) -> int:
    """휴가 1일당 인정 시간(분)."""
    if kind in _HALF_KINDS:
        return HALF_MIN
    lc = leave_config or {}
    if kind == "bd":
        return int((lc.get("bd", {}).get("hours", 4)) * 60)
    if kind in ("seollal", "chuseok"):
        base = kind
        return int((lc.get(base, {}).get("hours", 8)) * 60)
    # annual · sabbatical · family_care · health · *_full → 종일(8h)
    return STD_DAY_MIN


def month_scheduled_minutes(config: dict, year: int, month: int) -> int:
    """그 달 소정근로시간(분). 주말·놀금·단축 반영, 휴가는 미반영(별도 충족)."""
    total = 0
    for day in range(1, monthrange(year, month)[1] + 1):
        d = date(year, month, day)
        if d.weekday() > FRI or is_recovery_day(config, d):
            continue
        total += effective_window(config, d, None)["scheduled_minutes"]
    return total


def leave_used_minutes(config: dict, leaves: list[dict], year: int, month: int) -> int:
    """그 달 휴가 사용 시간(분)."""
    lc = config.get("leave_config") or {}
    first = date(year, month, 1)
    last = date(year, month, monthrange(year, month)[1])
    total = 0
    for lv in leaves:
        kind = lv["kind"]
        s = date.fromisoformat(lv["start"]) if isinstance(lv["start"], str) else lv["start"]
        e = date.fromisoformat(lv["end"]) if isinstance(lv["end"], str) else lv["end"]
        s = max(s, first)
        e = min(e, last)
        if s > e:
            continue
        per = _leave_minutes_per_day(kind, lc)
        if kind in _SINGLE_DAY:
            total += per          # 당일 1회
        else:
            cur = s               # annual·sabbatical·family_care → 근무일마다
            while cur <= e:
                if cur.weekday() <= FRI and not is_recovery_day(config, cur):
                    total += per
                cur += timedelta(days=1)
    return total


def monthly_summary(config: dict, records: list[dict], leaves: list[dict],
                    year: int, month: int) -> dict:
    """월 근로시간 요약.

    records: [{"work": 분, ...}] (repo.my_month 형식)
    leaves:  [{"kind","start","end"}]
    """
    work_type = config.get("work_type", "normal")
    scheduled = month_scheduled_minutes(config, year, month)
    actual = sum(int(r.get("work") or 0) for r in records)
    lv_min = leave_used_minutes(config, leaves, year, month)

    if work_type == "flex":                       # 시차: 하루 8h 초과분 합
        overtime = sum(max(0, int(r.get("work") or 0) - STD_DAY_MIN) for r in records)
    else:                                         # 선택적 등: 월 소정 초과분
        overtime = max(0, actual - scheduled)
    overtime = min(overtime, MAX_OT_MIN)

    return {
        "work_type": work_type,
        "scheduled_min": scheduled,
        "actual_min": actual,
        "leave_min": lv_min,
        "fulfilled_min": actual + lv_min,          # 실근로 + 휴가
        "remaining_min": max(0, scheduled - (actual + lv_min)),
        "overtime_min": overtime,
        "overtime_capped": overtime >= MAX_OT_MIN,
    }
