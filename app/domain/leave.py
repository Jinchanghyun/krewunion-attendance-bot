"""연차 엔진 — 연차 · 오전반차(4h) · 오후반차(4h).

승인 불필요: 신청 즉시 잔여에서 차감된다.
"""
from __future__ import annotations

from datetime import date

from .schedule import working_days

# 종류별 소진 단위(일)
LEAVE_UNIT = {"annual": 1.0, "half_am": 0.5, "half_pm": 0.5}
LEAVE_LABEL = {"annual": "연차", "half_am": "오전반차", "half_pm": "오후반차"}


class LeaveError(Exception):
    pass


def deduct_days(config: dict, kind: str, start: date, end: date | None = None) -> float:
    """신청에 필요한 소진 일수 계산.

    - annual: 기간(주말·놀금 제외) 일수 × 1.0
    - half_am / half_pm: 0.5 (하루)
    """
    if kind not in LEAVE_UNIT:
        raise LeaveError(f"알 수 없는 연차 종류: {kind}")
    if kind == "annual":
        end = end or start
        return round(working_days(config, start, end) * 1.0, 2)
    return LEAVE_UNIT[kind]


def apply_leave(balance: float, need: float) -> float:
    """잔여에서 차감. 부족하면 예외."""
    if need > balance + 1e-9:
        raise LeaveError(f"잔여 연차 부족: 잔여 {balance}일, 필요 {need}일")
    return round(balance - need, 2)


def calendar_summary(kind: str, emp_name: str) -> str:
    """구글 캘린더 이벤트 제목."""
    return f"{emp_name} · {LEAVE_LABEL[kind]}"
