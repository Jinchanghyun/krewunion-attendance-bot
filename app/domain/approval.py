"""승인 엔진 — 초과(연장)근무 · 휴일근무 전용 상태머신.

휴가/출퇴근/외근/재택은 승인 대상이 아니다(즉시 확정).
"""
from __future__ import annotations

APPROVAL_KINDS = {"overtime", "holiday"}   # 승인이 필요한 종류
STATES = {"requested", "approved", "rejected"}


class ApprovalError(Exception):
    pass


def requires_approval(kind: str) -> bool:
    return kind in APPROVAL_KINDS


def create(kind: str) -> str:
    if not requires_approval(kind):
        raise ApprovalError(f"'{kind}'는 승인 대상이 아닙니다(즉시 확정).")
    return "requested"


def decide(current_state: str, decision: str, actor_is_manager: bool) -> str:
    """팀장의 승인/반려 처리.

    - 이미 처리된(approved/rejected) 요청은 재처리 불가 → 중복 승인 방지.
    - 승인 권한이 없는 사용자의 결정은 거부.
    """
    if not actor_is_manager:
        raise ApprovalError("승인 권한이 없습니다.")
    if current_state != "requested":
        raise ApprovalError("이미 처리된 요청입니다.")
    if decision == "approve":
        return "approved"
    if decision == "reject":
        return "rejected"
    raise ApprovalError(f"알 수 없는 결정: {decision}")
