"""연차 엔진 — 연차 · 오전반차(4h) · 오후반차(4h).

승인 불필요: 신청 즉시 잔여에서 차감된다.
"""
from __future__ import annotations

from datetime import date

from .schedule import working_days

# 연차 잔여에서 차감하는 종류(일 단위)
LEAVE_UNIT = {"annual": 1.0, "half_am": 0.5, "half_pm": 0.5}

# 표시 라벨(연차 + 특수 휴가). 특수 휴가는 연차 잔여를 차감하지 않는다(별도 부여).
LEAVE_LABEL = {
    "annual": "연차", "half_am": "오전반차", "half_pm": "오후반차",
    "sabbatical": "안식휴가", "family_care": "가족돌봄 휴가",
    "family_care_paid": "가족돌봄(유급)", "family_care_unpaid": "가족돌봄(무급)",
    "refresh": "리프레쉬 휴가", "special": "특별휴가", "bd": "BD",
    "seollal": "설날 휴가", "chuseok": "추석 휴가",
    "health_check_am": "건강검진(오전 4h)", "health_check_pm": "건강검진(오후 4h)",
    "health_check_full": "건강검진(8h)", "health": "건강휴가",
    "birthday_am": "생일(오전 4h)", "birthday_pm": "생일(오후 4h)", "birthday_full": "생일(8h)",
    "recovery": "놀금(리커버리데이)",
    "dayoff": "데이오프",
}

# 연차 잔여 미차감 특수 휴가
SPECIAL_LEAVES = {
    "sabbatical", "family_care", "family_care_paid", "family_care_unpaid",
    "refresh", "special", "bd", "health", "seollal", "chuseok",
    "health_check_am", "health_check_pm", "health_check_full",
    "birthday_am", "birthday_pm", "birthday_full",
}

# 반일(하루) 처리 종류 — 기간 선택 없이 당일만
HALF_DAY_KINDS = {"half_am", "half_pm", "health_check_am", "health_check_pm",
                  "birthday_am", "birthday_pm"}


class LeaveError(Exception):
    pass


def deduct_days(config: dict, kind: str, start: date, end: date | None = None) -> float:
    """연차 잔여에서 차감할 일수. 특수 휴가는 0(별도 부여라 잔여 미차감)."""
    if kind == "annual":
        end = end or start
        return round(working_days(config, start, end) * 1.0, 2)
    if kind in ("half_am", "half_pm"):
        return 0.5
    if kind in SPECIAL_LEAVES:
        return 0.0
    raise LeaveError(f"알 수 없는 휴가 종류: {kind}")


def apply_leave(balance: float, need: float) -> float:
    """잔여에서 차감. 부족하면 예외."""
    if need > balance + 1e-9:
        raise LeaveError(f"잔여 연차 부족: 잔여 {balance}일, 필요 {need}일")
    return round(balance - need, 2)


def calendar_summary(kind: str, emp_name: str) -> str:
    """구글 캘린더 이벤트 제목."""
    return f"{emp_name} · {LEAVE_LABEL[kind]}"
