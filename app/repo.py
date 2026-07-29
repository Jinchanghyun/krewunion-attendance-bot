"""저장소 계층 — SQLAlchemy 구현.

Slack 핸들러·스케줄러·웹 라우트는 이 인터페이스만 바라본다.
도메인 엔진(schedule/attendance/leave)과 연동해 파생값을 계산한다.
"""
from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import select

from app.db import session_scope
from app.models import (Approval, AttendanceRecord, Employee, LeaveRequest,
                        SlackEvent, SlackReminder, WorkConfig)
from app.domain import attendance as att_engine
from app.domain import leave as leave_engine
from app.domain.schedule import prompt_times, is_recovery_day

_TYPE_TO_STATUS = {"office": "work", "remote": "remote", "field": "field"}


# ── 직렬화 헬퍼 ───────────────────────────────────────
def _emp_dict(e: Employee) -> dict:
    return {"id": e.id, "slack_user_id": e.slack_user_id, "name": e.name,
            "dept": e.dept, "manager_id": e.manager_id, "team_id": e.team_id,
            "leave_balance": e.leave_balance}


def _config_dict(c: WorkConfig) -> dict:
    return {"employee_id": c.employee_id, "work_type": c.work_type,
            "checkin": c.checkin, "checkout": c.checkout,
            "break_start": c.break_start, "break_end": c.break_end,
            "recovery": c.recovery or {}, "short_rules": c.short_rules or []}


# ── 직원 / 설정 ────────────────────────────────────────
def employee_by_slack_id(slack_user_id: str) -> dict:
    with session_scope() as s:
        e = s.scalar(select(Employee).where(Employee.slack_user_id == slack_user_id))
        if not e:
            raise LookupError(f"등록되지 않은 사용자: {slack_user_id}")
        return _emp_dict(e)


def try_employee_by_slack_id(slack_user_id: str) -> dict | None:
    """미등록이면 예외 대신 None. 가드용."""
    with session_scope() as s:
        e = s.scalar(select(Employee).where(Employee.slack_user_id == slack_user_id))
        return _emp_dict(e) if e else None


def work_config(employee_id: str) -> dict:
    with session_scope() as s:
        c = s.get(WorkConfig, employee_id)
        if not c:  # 없으면 기본값
            return {"employee_id": employee_id, "work_type": "normal",
                    "checkin": "09:00", "checkout": "18:00",
                    "break_start": "12:00", "break_end": "13:00",
                    "recovery": {"mode": "none"}, "short_rules": []}
        return _config_dict(c)


def save_work_config(employee_id: str, patch: dict) -> None:
    with session_scope() as s:
        c = s.get(WorkConfig, employee_id)
        if not c:
            c = WorkConfig(employee_id=employee_id)
            s.add(c)
        for k, v in patch.items():
            setattr(c, k, v)
        c.updated_at = datetime.utcnow()


# ── 출퇴근 ────────────────────────────────────────────
def _today_leave_kind(s, employee_id: str, d: date) -> str | None:
    lr = s.scalar(select(LeaveRequest).where(
        LeaveRequest.employee_id == employee_id,
        LeaveRequest.start <= d, LeaveRequest.end >= d))
    return lr.kind if lr else None


def today_state(employee_id: str) -> dict:
    today = date.today()
    with session_scope() as s:
        if _today_leave_kind(s, employee_id, today) == "annual":
            return {"status": "off", "worked": "-"}
        rec = s.scalar(select(AttendanceRecord).where(
            AttendanceRecord.employee_id == employee_id, AttendanceRecord.date == today))
        if not rec or not rec.checked_in_at:
            return {"status": "none", "worked": "-"}
        status = _TYPE_TO_STATUS.get(rec.type, "work")
        end = rec.checked_out_at or datetime.now()
        mins = max(0, int((end - rec.checked_in_at).total_seconds() // 60))
        return {"status": status, "worked": f"{mins // 60}시간 {mins % 60:02d}분"}


def record_checkin(employee_id: str, kind: str, at: datetime) -> None:
    today = at.date()
    with session_scope() as s:
        rec = s.scalar(select(AttendanceRecord).where(
            AttendanceRecord.employee_id == employee_id, AttendanceRecord.date == today))
        if not rec:
            rec = AttendanceRecord(employee_id=employee_id, date=today)
            s.add(rec)
        rec.type = kind
        if not rec.checked_in_at:
            rec.checked_in_at = at


def record_checkout(employee_id: str, at: datetime) -> None:
    today = at.date()
    with session_scope() as s:
        rec = s.scalar(select(AttendanceRecord).where(
            AttendanceRecord.employee_id == employee_id, AttendanceRecord.date == today))
        if not rec or not rec.checked_in_at:
            return
        rec.checked_out_at = at
        c = s.get(WorkConfig, employee_id)
        cfg = _config_dict(c) if c else work_config(employee_id)
        summary = att_engine.summarize_day(
            cfg, today, rec.checked_in_at, at,
            leave_kind=_today_leave_kind(s, employee_id, today))
        rec.work_minutes = summary["worked"]
        rec.overtime_minutes = summary["overtime"]
        rec.night_minutes = summary["night"]
        rec.holiday_minutes = summary["holiday"]


def employees_due_for_checkin(now: datetime) -> list[dict]:
    today, hm = now.date(), now.strftime("%H:%M")
    due = []
    with session_scope() as s:
        for e in s.scalars(select(Employee)).all():
            c = s.get(WorkConfig, e.id)
            cfg = _config_dict(c) if c else work_config(e.id)
            leave_kind = _today_leave_kind(s, e.id, today)
            pt = prompt_times(cfg, today, leave_kind)
            if not pt["checkin"] or pt["checkin"] > hm:   # 휴무/연차/아직 시각 전
                continue
            already = s.scalar(select(AttendanceRecord).where(
                AttendanceRecord.employee_id == e.id, AttendanceRecord.date == today))
            if already and already.checked_in_at:
                continue
            reminded = s.scalar(select(SlackReminder).where(
                SlackReminder.employee_id == e.id, SlackReminder.date == today,
                SlackReminder.kind == "checkin"))
            if reminded:
                continue
            s.add(SlackReminder(employee_id=e.id, date=today, kind="checkin"))
            d = _emp_dict(e)
            d["checkin"] = pt["checkin"]
            due.append(d)
    return due


# ── 연차 ──────────────────────────────────────────────
def create_leave(employee_id: str, kind: str, start: date, end: date, days: float) -> dict:
    with session_scope() as s:
        e = s.get(Employee, employee_id)
        e.leave_balance = leave_engine.apply_leave(e.leave_balance, days)
        lr = LeaveRequest(employee_id=employee_id, kind=kind, start=start,
                          end=end, days=days, status="applied")
        s.add(lr)
        s.flush()
        return {"id": lr.id, "employee_id": employee_id, "kind": kind,
                "kind_label": leave_engine.LEAVE_LABEL[kind], "emp_name": e.name,
                "start": start, "end": end, "days": days,
                "balance_after": e.leave_balance}


def get_leave(leave_id: int) -> dict:
    with session_scope() as s:
        lr = s.get(LeaveRequest, leave_id)
        e = s.get(Employee, lr.employee_id)
        return {"id": lr.id, "employee_id": lr.employee_id, "kind": lr.kind,
                "kind_label": leave_engine.LEAVE_LABEL[lr.kind], "emp_name": e.name,
                "start": lr.start, "end": lr.end}


def set_leave_calendar_event(leave_id: int, event_id: str) -> None:
    with session_scope() as s:
        s.get(LeaveRequest, leave_id).calendar_event_id = event_id


# ── 승인 (연장·휴일) ──────────────────────────────────
def create_approval(employee_id: str, kind: str, payload: dict) -> dict:
    with session_scope() as s:
        a = Approval(employee_id=employee_id, kind=kind, payload=payload, status="requested")
        s.add(a)
        s.flush()
        e = s.get(Employee, employee_id)
        return {"id": a.id, "employee_id": employee_id, "kind": kind,
                "status": a.status, "emp_name": e.name, "detail": payload.get("detail", "")}


def get_approval(approval_id: int) -> dict:
    with session_scope() as s:
        a = s.get(Approval, approval_id)
        return {"id": a.id, "employee_id": a.employee_id, "kind": a.kind,
                "status": a.status, "payload": a.payload}


def update_approval(approval_id: int, status: str, approver_id: str) -> dict:
    with session_scope() as s:
        a = s.get(Approval, approval_id)
        a.status = status
        a.approver_id = approver_id
        a.decided_at = datetime.utcnow()
        e = s.get(Employee, a.employee_id)
        return {"id": a.id, "kind": a.kind, "status": status,
                "emp_name": e.name, "approver_name": approver_id,
                "decided_hm": a.decided_at.strftime("%H:%M")}


def is_manager_of(approver_slack_id: str, employee_id: str) -> bool:
    with session_scope() as s:
        approver = s.scalar(select(Employee).where(Employee.slack_user_id == approver_slack_id))
        emp = s.get(Employee, employee_id)
        return bool(approver and emp and emp.manager_id == approver.id)


# ── 권한(역할) ────────────────────────────────────────
ROLES = ("employee", "manager", "hr", "sysadmin")
_ASSIGNERS = {"hr", "sysadmin"}          # 역할을 부여할 수 있는 주체


def role_of(slack_user_id: str) -> str:
    with session_scope() as s:
        e = s.scalar(select(Employee).where(Employee.slack_user_id == slack_user_id))
        return e.role if e else "employee"


def bootstrap_first_admin(employee_id: str) -> None:
    """앱 최초 설치 시 호출 — 시스템 관리자가 한 명도 없으면 설치자를 sysadmin으로."""
    with session_scope() as s:
        exists = s.scalar(select(Employee).where(Employee.role == "sysadmin"))
        if not exists:
            emp = s.get(Employee, employee_id)
            if emp:
                emp.role = "sysadmin"


def _count_sysadmins(s) -> int:
    return len(s.scalars(select(Employee).where(Employee.role == "sysadmin")).all())


def assign_role(actor_slack_id: str, target_employee_id: str, role: str) -> None:
    """역할 지정/해제.

    규칙:
    - hr/sysadmin만 역할을 지정한다.
    - sysadmin 역할의 부여·회수는 sysadmin만 할 수 있다(hr는 불가).
    - 마지막 남은 sysadmin은 후임 없이 강등될 수 없다(handover_admin 사용).
    - 지정 해제는 role="employee"로 강등하는 것과 같다.
    """
    if role not in ROLES:
        raise ValueError(f"알 수 없는 역할: {role}")
    with session_scope() as s:
        actor = s.scalar(select(Employee).where(Employee.slack_user_id == actor_slack_id))
        if not actor or actor.role not in _ASSIGNERS:
            raise PermissionError("역할을 지정할 권한이 없습니다(hr/sysadmin 전용).")
        target = s.get(Employee, target_employee_id)
        if target is None:
            raise LookupError("대상 직원을 찾을 수 없습니다.")
        if (role == "sysadmin" or target.role == "sysadmin") and actor.role != "sysadmin":
            raise PermissionError("시스템 관리자 권한은 sysadmin만 지정/회수할 수 있습니다.")
        if target.role == "sysadmin" and role != "sysadmin" and _count_sysadmins(s) <= 1:
            raise PermissionError("마지막 시스템 관리자는 해제할 수 없습니다. 인수인계(handover)를 사용하세요.")
        target.role = role


def revoke_role(actor_slack_id: str, target_employee_id: str) -> None:
    """역할 해제 = 일반 직원으로 강등. (assign_role의 얇은 래퍼)"""
    assign_role(actor_slack_id, target_employee_id, "employee")


def handover_admin(actor_slack_id: str, successor_employee_id: str,
                   step_down_to: str = "employee") -> None:
    """시스템 관리자 인수인계 — 후임을 sysadmin으로 지정하고 본인은 내려온다.

    후임 승격과 본인 강등을 한 트랜잭션에서 처리하므로 '관리자 0명' 상태가 생기지 않는다.
    """
    if step_down_to not in ROLES or step_down_to == "sysadmin":
        raise ValueError("내려갈 역할이 올바르지 않습니다.")
    with session_scope() as s:
        actor = s.scalar(select(Employee).where(Employee.slack_user_id == actor_slack_id))
        if not actor or actor.role != "sysadmin":
            raise PermissionError("인수인계는 현재 시스템 관리자만 할 수 있습니다.")
        successor = s.get(Employee, successor_employee_id)
        if successor is None:
            raise LookupError("후임 직원을 찾을 수 없습니다.")
        if successor.id == actor.id:
            raise ValueError("본인에게 인수인계할 수 없습니다.")
        successor.role = "sysadmin"   # 먼저 후임 승격
        actor.role = step_down_to     # 그 다음 본인 강등 → 항상 sysadmin ≥ 1


# ── 멱등성 ────────────────────────────────────────────
def seen_event(event_id: str) -> bool:
    with session_scope() as s:
        if s.get(SlackEvent, event_id):
            return True
        s.add(SlackEvent(event_id=event_id))
        return False


# ── 관리자 통계 집계 ──────────────────────────────────
def _month_range(month: str) -> tuple[date, date]:
    y, m = (int(x) for x in month.split("-"))
    start = date(y, m, 1)
    end = date(y + (m // 12), (m % 12) + 1, 1)
    from datetime import timedelta
    return start, end - timedelta(days=1)


def stats_overview(today: date | None = None) -> dict:
    today = today or date.today()
    with session_scope() as s:
        emps = s.scalars(select(Employee)).all()
        counts = {"work": 0, "remote": 0, "field": 0, "off": 0, "none": 0}
        for e in emps:
            if _today_leave_kind(s, e.id, today) == "annual":
                counts["off"] += 1
                continue
            rec = s.scalar(select(AttendanceRecord).where(
                AttendanceRecord.employee_id == e.id, AttendanceRecord.date == today))
            if rec and rec.checked_in_at and not rec.checked_out_at:
                counts[_TYPE_TO_STATUS.get(rec.type, "work")] += 1
            elif rec and rec.checked_out_at:
                counts["work"] += 1
            else:
                counts["none"] += 1
        present = counts["work"] + counts["remote"] + counts["field"]
        eligible = len(emps) - counts["off"]
        pending = s.scalar(select(Approval).where(Approval.status == "requested"))
        pending_n = len(s.scalars(select(Approval).where(Approval.status == "requested")).all())
        rate = round(present / eligible * 100) if eligible else 0
        return {"attendance_rate": rate, "present": present, "eligible": eligible,
                "remote": counts["remote"], "field": counts["field"],
                "off": counts["off"], "pending_approvals": pending_n}


def stats_monthly(month: str) -> dict:
    start, end = _month_range(month)
    with session_scope() as s:
        recs = s.scalars(select(AttendanceRecord).where(
            AttendanceRecord.date >= start, AttendanceRecord.date <= end)).all()
        agg = {k: sum(getattr(r, f"{k}_minutes") for r in recs)
               for k in ("work", "overtime", "night", "holiday")}
        return {"month": month,
                "worked": agg["work"], "overtime": agg["overtime"],
                "night": agg["night"], "holiday": agg["holiday"],
                "records": len(recs)}


def stats_by_employee(month: str) -> dict:
    start, end = _month_range(month)
    rows = []
    with session_scope() as s:
        for e in s.scalars(select(Employee)).all():
            recs = s.scalars(select(AttendanceRecord).where(
                AttendanceRecord.employee_id == e.id,
                AttendanceRecord.date >= start, AttendanceRecord.date <= end)).all()
            rows.append({"employee_id": e.id, "name": e.name, "dept": e.dept,
                         "worked": sum(r.work_minutes for r in recs),
                         "overtime": sum(r.overtime_minutes for r in recs),
                         "night": sum(r.night_minutes for r in recs),
                         "holiday": sum(r.holiday_minutes for r in recs)})
    return {"month": month, "rows": rows}


def live_status(status: str | None = None, dept: str | None = None) -> dict:
    today = date.today()
    rows = []
    with session_scope() as s:
        for e in s.scalars(select(Employee)).all():
            if dept and e.dept != dept:
                continue
            st = today_state(e.id)["status"]
            if status and st != status:
                continue
            rows.append({"employee_id": e.id, "name": e.name, "dept": e.dept, "status": st})
    return {"filter": {"status": status, "dept": dept}, "rows": rows}


def pending_approvals() -> dict:
    rows = []
    with session_scope() as s:
        for a in s.scalars(select(Approval).where(Approval.status == "requested")).all():
            e = s.get(Employee, a.employee_id)
            rows.append({"id": a.id, "employee": e.name, "dept": e.dept,
                         "kind": a.kind, "detail": (a.payload or {}).get("detail", "")})
    return {"rows": rows}
