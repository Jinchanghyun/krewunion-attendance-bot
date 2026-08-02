"""대한민국 공휴일 판정.

우선순위:
1) `holidays` 라이브러리(설치 시) — 음력·대체공휴일까지 정확.
2) 미설치 시 하드코딩 폴백(양력 고정일 + 2025~2027 음력/대체 목록).
"""
from __future__ import annotations

from datetime import date

# 양력 고정 공휴일 (월, 일) — 연도 무관
_FIXED = {(1, 1), (3, 1), (5, 5), (6, 6), (8, 15), (10, 3), (10, 9), (12, 25)}

# 음력·대체공휴일 등 변동 공휴일 (2025~2027, ISO 문자열)
_VARIABLE = {
    # 2025
    "2025-01-27", "2025-01-28", "2025-01-29", "2025-01-30",   # 설날+대체
    "2025-03-03",                                              # 삼일절 대체
    "2025-05-06",                                              # 어린이날/부처님 대체
    "2025-06-03",                                              # 대선
    "2025-10-05", "2025-10-06", "2025-10-07", "2025-10-08",   # 추석+대체
    # 2026
    "2026-02-16", "2026-02-17", "2026-02-18",                 # 설날
    "2026-03-02",                                              # 삼일절 대체
    "2026-05-24", "2026-05-25",                               # 부처님오신날+대체
    "2026-06-03", "2026-07-17",                               # 지방선거·임시공휴일 등
    "2026-08-17",                                              # 광복절 대체
    "2026-09-24", "2026-09-25", "2026-09-26",                 # 추석
    "2026-10-05",                                              # 개천절 대체
    # 2027
    "2027-02-06", "2027-02-07", "2027-02-08", "2027-02-09",   # 설날+대체
    "2027-05-13",                                              # 부처님오신날
    "2027-08-16",                                              # 광복절 대체
    "2027-09-14", "2027-09-15", "2027-09-16",                 # 추석
    "2027-10-04", "2027-10-11",                               # 대체공휴일
    "2027-12-27",                                              # 성탄절 대체
}

_kr_lib = None
try:  # 정확한 최신 공휴일 — 있으면 사용
    import holidays as _holidays_lib

    def _kr(year: int):
        global _kr_lib
        if _kr_lib is None:
            try:
                _kr_lib = _holidays_lib.SouthKorea(language="ko")   # 한국어 공휴일명
            except Exception:
                _kr_lib = _holidays_lib.SouthKorea()
        return _kr_lib
    _HAS_LIB = True
except Exception:  # pragma: no cover
    _HAS_LIB = False


def is_public_holiday(d: date) -> bool:
    """그 날짜가 대한민국 공휴일이면 True (주말은 제외 — 별도 판정)."""
    if _HAS_LIB:
        try:
            return d in _kr(d.year)
        except Exception:
            pass
    if (d.month, d.day) in _FIXED:
        return True
    return d.isoformat() in _VARIABLE


def public_holiday_name(d: date) -> str | None:
    """공휴일이면 이름, 아니면 None."""
    if _HAS_LIB:
        try:
            return _kr(d.year).get(d)
        except Exception:
            pass
    if (d.month, d.day) in _FIXED or d.isoformat() in _VARIABLE:
        return "공휴일"
    return None


def is_day_off(d: date) -> bool:
    """근무일이 아닌 날 = 주말(토·일) 또는 공휴일."""
    return d.weekday() >= 5 or is_public_holiday(d)


def needs_work_approval(d: date) -> bool:
    """휴일(일요일·공휴일) 근무는 승인이 있어야 기록 가능.
    (토요일=무급휴무, 놀금·데이오프는 여기서 제외 — 각자 별도 규칙)"""
    return d.weekday() == 6 or is_public_holiday(d)
