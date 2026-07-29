"""근무 스케줄 계산 — 놀금(리커버리데이) · 단축근무 · 반차 · 예약 출퇴근 시각.

순수 함수 모듈. DB나 외부 API에 의존하지 않으므로 단위 테스트가 쉽다.
work_config 예시::

    {
        "employee_id": "K-2041",
        "work_type": "normal",          # normal|flex|selective|elastic
        "checkin": "09:00", "checkout": "18:00",
        "break_start": "12:00", "break_end": "13:00",
        "recovery": {"mode": "biweekly", "anchor": "2026-07-31", "custom": []},
        "short_rules": [
            {"kind": "late",  "amount_min": 30, "repeat": "weekly", "weekdays": [1]},
            {"kind": "early", "amount_min": 30, "repeat": "weekly", "weekdays": [5]},
        ],
    }
"""
from __future__ import annotations

from datetime import date, timedelta

MON, FRI = 0, 4  # date.weekday(): 월=0 .. 일=6


def hm_to_min(hm: str) -> int:
    h, m = hm.split(":")
    return int(h) * 60 + int(m)


def min_to_hm(x: int) -> str:
    return f"{x // 60:02d}:{x % 60:02d}"


def _iso(d: date) -> str:
    return d.isoformat()


def last_friday(year: int, month: int) -> date:
    """해당 월의 마지막 금요일."""
    nxt_year = year + (1 if month == 12 else 0)
    nxt_month = 1 if month == 12 else month + 1
    d = date(nxt_year, nxt_month, 1) - timedelta(days=1)  # 말일
    while d.weekday() != FRI:
        d -= timedelta(days=1)
    return d


def is_recovery_day(config: dict, d: date) -> bool:
    """놀금(정기 휴무일) 여부."""
    rc = config.get("recovery") or {}
    mode = rc.get("mode", "none")
    if mode == "lastfri":
        return d == last_friday(d.year, d.month)
    if mode == "thirdfri":
        # 매월 셋째주 금요일(그 달의 3번째 금요일)
        if d.weekday() != FRI:
            return False
        return (d.day - 1) // 7 == 2
    if mode == "biweekly":
        anchor = rc.get("anchor")
        if not anchor or d.weekday() != FRI:
            return False
        diff = (d - date.fromisoformat(anchor)).days
        return diff >= 0 and diff % 14 == 0
    if mode == "custom":
        return _iso(d) in (rc.get("custom") or [])
    return False


def _rule_matches(rule: dict, d: date) -> bool:
    repeat = rule.get("repeat", "daily")
    if repeat == "dates":
        return _iso(d) in (rule.get("dates") or [])
    start, end = rule.get("start"), rule.get("end")
    if start and _iso(d) < start:
        return False
    if end and _iso(d) > end:
        return False
    if d.weekday() > FRI:  # 주말 제외
        return False
    if repeat == "weekly":
        # weekdays 규약: 1=월 .. 5=금
        return (d.weekday() + 1) in (rule.get("weekdays") or [])
    return True  # daily = 기간 내 월–금


def matched_short_rules(config: dict, d: date) -> list[dict]:
    return [r for r in (config.get("short_rules") or []) if _rule_matches(r, d)]


def effective_window(config: dict, d: date, leave_kind: str | None = None) -> dict:
    """그날의 실제 소정 출퇴근 시각과 소정근로(분).

    우선순위: 놀금(휴무) > 연차(종일) > 오전/오후 반차 > 단축근무.
    leave_kind: None | "annual" | "half_am" | "half_pm"
    """
    checkin = hm_to_min(config["checkin"])
    checkout = hm_to_min(config["checkout"])
    break_min = 0
    if config.get("break_start") and config.get("break_end"):
        break_min = hm_to_min(config["break_end"]) - hm_to_min(config["break_start"])

    if is_recovery_day(config, d) or leave_kind == "annual":
        note = "놀금(휴무)" if is_recovery_day(config, d) else "연차"
        return {"checkin": None, "checkout": None, "scheduled_minutes": 0, "note": note}

    # 반차: 각 4시간 근무. 오전반차 → 오후만 근무, 오후반차 → 오전만 근무.
    if leave_kind == "half_am":
        checkin = hm_to_min(config.get("half_pm_start", "14:00"))
        break_min = 0
    elif leave_kind == "half_pm":
        checkout = hm_to_min(config.get("half_am_end", "13:00"))
        break_min = 0

    # 단축근무 규칙: 출근 늦춤 / 퇴근 당김
    for r in matched_short_rules(config, d):
        amt = r.get("amount_min", 0)
        if r.get("kind") == "late":
            checkin += amt
        elif r.get("kind") == "early":
            checkout -= amt

    scheduled = max(0, checkout - checkin - break_min)
    return {
        "checkin": min_to_hm(checkin),
        "checkout": min_to_hm(checkout),
        "scheduled_minutes": scheduled,
        "note": {"half_am": "오전반차", "half_pm": "오후반차"}.get(leave_kind, "정상"),
    }


def prompt_times(config: dict, d: date, leave_kind: str | None = None) -> dict:
    """예약 출퇴근 알림을 보낼 시각. 휴무/연차면 None."""
    w = effective_window(config, d, leave_kind)
    return {"checkin": w["checkin"], "checkout": w["checkout"]}


def working_days(config: dict, start: date, end: date) -> int:
    """연차 종일 신청 기간의 실제 소진 일수(주말·놀금 제외)."""
    n, cur = 0, start
    while cur <= end:
        if cur.weekday() <= FRI and not is_recovery_day(config, cur):
            n += 1
        cur += timedelta(days=1)
    return n
