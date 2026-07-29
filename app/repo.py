"""저장소 계층 — SQLAlchemy 구현.

Slack 핸들러·스케줄러·웹 라우트는 이 인터페이스만 바라본다.
도메인 엔진(schedule/attendance/leave)과 연동해 파생값을 계산한다.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

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
            "leave_balance": e.leave_balance, "role": e.role,
            "position": getattr(e, "position", _DEFAULT_POSITION)}


def _config_dict(c: WorkConfig) -> dict:
    return {"employee_id": c.employee_id, "work_type": c.work_type,
            "checkin": c.checkin, "checkout": c.checkout,
            "break_start": c.break_start, "break_end": c.break_end,
            "recovery": c.recovery or {}, "short_rules": c.short_rules or [],
            "leave_config": getattr(c, "leave_config", None) or {}}


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
                    "recovery": {"mode": "none"}, "short_rules": [], "leave_config": {}}
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


def record_checkin(employee_id: str, kind: str, at: datetime) -> dict:
    """출근 기록 후 확인 메시지용 정보 반환."""
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
        return {"date": today, "kind": kind,
                "checkin": rec.checked_in_at.strftime("%H:%M")}


def record_checkout(employee_id: str, at: datetime) -> dict | None:
    """퇴근 기록 후 근무시간 요약 반환."""
    from app.domain.schedule import hm_to_min
    today = at.date()
    with session_scope() as s:
        rec = s.scalar(select(AttendanceRecord).where(
            AttendanceRecord.employee_id == employee_id, AttendanceRecord.date == today))
        if not rec or not rec.checked_in_at:
            return None
        rec.checked_out_at = at
        c = s.get(WorkConfig, employee_id)
        cfg = _config_dict(c) if c else work_config(employee_id)
        leave_kind = _today_leave_kind(s, employee_id, today)
        summary = att_engine.summarize_day(cfg, today, rec.checked_in_at, at, leave_kind=leave_kind)
        rec.work_minutes = summary["worked"]
        rec.overtime_minutes = summary["overtime"]
        rec.night_minutes = summary["night"]
        rec.holiday_minutes = summary["holiday"]
        break_min = 0
        if cfg.get("break_start") and cfg.get("break_end") and leave_kind not in ("half_am", "half_pm"):
            break_min = hm_to_min(cfg["break_end"]) - hm_to_min(cfg["break_start"])
        return {"date": today,
                "checkin": rec.checked_in_at.strftime("%H:%M"),
                "checkout": at.strftime("%H:%M"),
                "work": summary["worked"], "break": break_min,
                "night": summary["night"]}


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


def record_manual(employee_id: str, d: date, checkin_hm: str,
                  checkout_hm: str | None = None, kind: str = "office") -> None:
    """누락(수동) 출퇴근 등록 — 과거 날짜 포함."""
    from datetime import time as _t
    ci = datetime.combine(d, _t(*map(int, checkin_hm.split(":"))))
    co = datetime.combine(d, _t(*map(int, checkout_hm.split(":")))) if checkout_hm else None
    with session_scope() as s:
        rec = s.scalar(select(AttendanceRecord).where(
            AttendanceRecord.employee_id == employee_id, AttendanceRecord.date == d))
        if not rec:
            rec = AttendanceRecord(employee_id=employee_id, date=d)
            s.add(rec)
        rec.type = kind
        rec.checked_in_at = ci
        if co:
            rec.checked_out_at = co
            c = s.get(WorkConfig, employee_id)
            cfg = _config_dict(c) if c else work_config(employee_id)
            summary = att_engine.summarize_day(cfg, d, ci, co,
                                               leave_kind=_today_leave_kind(s, employee_id, d))
            rec.work_minutes = summary["worked"]
            rec.overtime_minutes = summary["overtime"]
            rec.night_minutes = summary["night"]
            rec.holiday_minutes = summary["holiday"]


def team_status() -> list[dict]:
    """팀원 오늘 현황(간단)."""
    return live_status()["rows"]


def employees_due_for_checkout(now: datetime) -> list[dict]:
    """퇴근 시각이 됐는데 아직 퇴근 안 찍은 직원(출근한 사람만)."""
    today, hm = now.date(), now.strftime("%H:%M")
    due = []
    with session_scope() as s:
        for e in s.scalars(select(Employee)).all():
            rec = s.scalar(select(AttendanceRecord).where(
                AttendanceRecord.employee_id == e.id, AttendanceRecord.date == today))
            if not rec or not rec.checked_in_at or rec.checked_out_at:
                continue  # 미출근이거나 이미 퇴근
            c = s.get(WorkConfig, e.id)
            cfg = _config_dict(c) if c else work_config(e.id)
            pt = prompt_times(cfg, today, _today_leave_kind(s, e.id, today))
            if not pt["checkout"] or pt["checkout"] > hm:
                continue
            if s.scalar(select(SlackReminder).where(
                    SlackReminder.employee_id == e.id, SlackReminder.date == today,
                    SlackReminder.kind == "checkout")):
                continue
            s.add(SlackReminder(employee_id=e.id, date=today, kind="checkout"))
            d = _emp_dict(e)
            d["checkout"] = pt["checkout"]
            due.append(d)
    return due


# ── 연차 ──────────────────────────────────────────────
def create_leave(employee_id: str, kind: str, start: date, end: date, days: float,
                 reason: str | None = None) -> dict:
    with session_scope() as s:
        e = s.get(Employee, employee_id)
        e.leave_balance = leave_engine.apply_leave(e.leave_balance, days)
        lr = LeaveRequest(employee_id=employee_id, kind=kind, start=start,
                          end=end, days=days, reason=(reason or None), status="applied")
        s.add(lr)
        s.flush()
        return {"id": lr.id, "employee_id": employee_id, "kind": kind,
                "kind_label": leave_engine.LEAVE_LABEL[kind], "emp_name": e.name,
                "start": start, "end": end, "days": days,
                "balance_after": e.leave_balance}


# 일(day) 단위로 부여·차감하는 휴가 그룹(당해년도 사용가능 일수)
_DAY_LEAVE_GROUPS = {
    "annual": (["annual", "half_am", "half_pm"], "연차"),
    "family_care": (["family_care", "family_care_paid", "family_care_unpaid"], "가족돌봄 휴가"),
    "health": (["health"], "건강휴가"),
    "sabbatical": (["sabbatical"], "안식휴가"),
    "refresh": (["refresh"], "리프레쉬 휴가"),
}
_HALF_LEAVE_KINDS = {"half_am", "half_pm"}
# 워킹데이가 아니라 달력 일수(주말 포함)로 세는 그룹 — 안식휴가
_CALENDAR_DAY_GROUPS = {"sabbatical"}


def _leave_group_of(kind: str) -> str | None:
    for g, (kinds, _) in _DAY_LEAVE_GROUPS.items():
        if kind in kinds:
            return g
    return None


def leave_used_days(employee_id: str, group: str, year: int | None = None) -> float:
    """당해년도 특정 그룹의 사용 일수."""
    from app.domain.schedule import working_days
    if year is None:
        year = date.today().year
    ys, ye = date(year, 1, 1), date(year, 12, 31)
    cfg = work_config(employee_id)
    kinds = _DAY_LEAVE_GROUPS[group][0]
    used = 0.0
    with session_scope() as s:
        rows = s.scalars(select(LeaveRequest).where(
            LeaveRequest.employee_id == employee_id, LeaveRequest.kind.in_(kinds),
            LeaveRequest.start <= ye, LeaveRequest.end >= ys)).all()
        cal = group in _CALENDAR_DAY_GROUPS
        for l in rows:
            if l.kind in _HALF_LEAVE_KINDS:
                used += 0.5
            elif cal:  # 안식휴가 등: 주말 포함 달력 일수
                used += (min(l.end, ye) - max(l.start, ys)).days + 1
            else:
                used += working_days(cfg, max(l.start, ys), min(l.end, ye))
    return round(used, 2)


def leave_balances(employee_id: str) -> list[dict]:
    """휴가 관리에서 켠 '일 단위' 휴가의 부여/사용/잔여(일)."""
    cfg = work_config(employee_id)
    lc = cfg.get("leave_config") or {}
    out = []
    for g, (_, label) in _DAY_LEAVE_GROUPS.items():
        conf = lc.get(g) or {}
        on = conf.get("on", True) if g == "annual" else conf.get("on", False)
        if not on:
            continue
        granted = float(conf.get("quota") or 0)
        used = leave_used_days(employee_id, g)
        out.append({"group": g, "label": label, "granted": granted,
                    "used": used, "remaining": round(granted - used, 2)})
    return out


def my_leaves(employee_id: str, limit: int = 30) -> list[dict]:
    """본인 연차 신청 내역(최근순)."""
    with session_scope() as s:
        rows = s.scalars(select(LeaveRequest).where(
            LeaveRequest.employee_id == employee_id
        ).order_by(LeaveRequest.start.desc()).limit(limit)).all()
        return [{"id": l.id, "kind": l.kind,
                 "kind_label": leave_engine.LEAVE_LABEL.get(l.kind, l.kind),
                 "start": l.start.isoformat(), "end": l.end.isoformat(),
                 "days": l.days, "reason": getattr(l, "reason", None) or "",
                 "status": l.status} for l in rows]


def cancel_leave(employee_id: str, leave_id: int) -> bool:
    """본인 연차 취소 — 차감했던 연차일수 복원 후 삭제."""
    with session_scope() as s:
        lr = s.get(LeaveRequest, leave_id)
        if not lr or lr.employee_id != employee_id:
            return False
        e = s.get(Employee, employee_id)
        if e is not None:
            e.leave_balance = (e.leave_balance or 0) + (lr.days or 0)
        s.delete(lr)
        return True


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
def my_month(employee_id: str, month: str) -> dict:
    """직원 본인의 월 근태(달력·목록용) + 요약 + 설정."""
    start, end = _month_range(month)
    with session_scope() as s:
        recs = s.scalars(select(AttendanceRecord).where(
            AttendanceRecord.employee_id == employee_id,
            AttendanceRecord.date >= start, AttendanceRecord.date <= end
        ).order_by(AttendanceRecord.date)).all()
        records = [{"date": r.date.isoformat(), "type": r.type,
                    "checkin": r.checked_in_at.strftime("%H:%M") if r.checked_in_at else None,
                    "checkout": r.checked_out_at.strftime("%H:%M") if r.checked_out_at else None,
                    "work": r.work_minutes, "overtime": r.overtime_minutes,
                    "night": r.night_minutes, "holiday": r.holiday_minutes} for r in recs]
        leaves = [{"id": l.id, "kind": l.kind, "start": l.start.isoformat(),
                   "end": l.end.isoformat(), "days": l.days,
                   "reason": getattr(l, "reason", None) or ""}
                  for l in s.scalars(select(LeaveRequest).where(
                      LeaveRequest.employee_id == employee_id,
                      LeaveRequest.start <= end, LeaveRequest.end >= start)).all()]
        c = s.get(WorkConfig, employee_id)
        cfg = _config_dict(c) if c else work_config(employee_id)
        # 승인된 연장근로(분) — 연장근로는 승인분만 인정
        approved_ot = 0
        for a in s.scalars(select(Approval).where(
                Approval.employee_id == employee_id,
                Approval.kind == "overtime", Approval.status == "approved")).all():
            p = a.payload or {}
            if str(p.get("date", ""))[:7] == month:
                st, en = p.get("start"), p.get("end")
                try:
                    if st and en:
                        approved_ot += (int(en[:2]) * 60 + int(en[3:5])) - \
                                       (int(st[:2]) * 60 + int(st[3:5]))
                except Exception:
                    pass
    summary = {"worked": sum(r["work"] for r in records),
               "overtime": sum(r["overtime"] for r in records),
               "night": sum(r["night"] for r in records),
               "holiday": sum(r["holiday"] for r in records)}
    from app.domain import worktime as _wt
    yy, mm = int(month[:4]), int(month[5:7])
    wt = _wt.monthly_summary(cfg, records, leaves, yy, mm, approved_ot_min=approved_ot)
    return {"month": month, "records": records, "leaves": leaves,
            "config": cfg, "summary": summary, "worktime": wt}


def my_approvals(employee_id: str) -> list[dict]:
    with session_scope() as s:
        rows = s.scalars(select(Approval).where(Approval.employee_id == employee_id)
                         .order_by(Approval.id.desc())).all()
        return [{"id": a.id, "kind": a.kind, "status": a.status,
                 "detail": (a.payload or {}).get("detail", ""),
                 "payload": a.payload} for a in rows]


def cancel_approval(employee_id: str, approval_id: int) -> bool:
    """본인의 '대기중' 요청만 취소 가능."""
    with session_scope() as s:
        a = s.get(Approval, approval_id)
        if not a or a.employee_id != employee_id or a.status != "requested":
            return False
        a.status = "cancelled"
        return True


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


def mark_reminder_once(employee_id: str, d: date, kind: str) -> bool:
    """중복 발송 가드. 처음이면 기록하고 True, 이미 있으면 False."""
    with session_scope() as s:
        exists = s.scalar(select(SlackReminder).where(
            SlackReminder.employee_id == employee_id,
            SlackReminder.date == d, SlackReminder.kind == kind))
        if exists:
            return False
        s.add(SlackReminder(employee_id=employee_id, date=d, kind=kind))
        return True


def pending_overtime_notifications(today: date) -> list[dict]:
    """오늘 보낼 연장근로 신청 안내 대상.
    - 시차(flex): 어제(평일) 실근로 8h 초과 → 오늘 신청 안내
    - 선택적(selective): 매월 1~7일 → 지난달 월 소정 초과분 안내
    """
    from calendar import monthrange
    from app.domain import worktime as _wt
    from app.domain.schedule import FRI
    out: list[dict] = []
    with session_scope() as s:
        for e in s.scalars(select(Employee)).all():
            c = s.get(WorkConfig, e.id)
            cfg = _config_dict(c) if c else None
            if not cfg:
                continue
            wtype = cfg.get("work_type")
            if wtype == "flex":
                y = today - timedelta(days=1)
                if y.weekday() > FRI:
                    continue
                rec = s.scalar(select(AttendanceRecord).where(
                    AttendanceRecord.employee_id == e.id, AttendanceRecord.date == y))
                mins = rec.work_minutes if rec else 0
                ot = max(0, mins - _wt.STD_DAY_MIN)
                if ot > 0:
                    out.append({"emp_id": e.id, "slack": e.slack_user_id, "name": e.name,
                                "kind": "flex", "hours": round(ot / 60, 1), "ref": y.isoformat()})
            elif wtype == "selective":
                if not (1 <= today.day <= 7):
                    continue
                py = today.year - (1 if today.month == 1 else 0)
                pm = 12 if today.month == 1 else today.month - 1
                ms, me = date(py, pm, 1), date(py, pm, monthrange(py, pm)[1])
                recs = [{"work": r.work_minutes} for r in s.scalars(select(AttendanceRecord).where(
                    AttendanceRecord.employee_id == e.id,
                    AttendanceRecord.date >= ms, AttendanceRecord.date <= me)).all()]
                leaves = [{"kind": l.kind, "start": l.start.isoformat(), "end": l.end.isoformat()}
                          for l in s.scalars(select(LeaveRequest).where(
                              LeaveRequest.employee_id == e.id,
                              LeaveRequest.start <= me, LeaveRequest.end >= ms)).all()]
                summ = _wt.monthly_summary(cfg, recs, leaves, py, pm)
                if summ["overtime_min"] > 0:
                    out.append({"emp_id": e.id, "slack": e.slack_user_id, "name": e.name,
                                "kind": "selective", "hours": round(summ["overtime_min"] / 60, 1),
                                "ref": f"{py}-{pm:02d}"})
    return out


def is_manager_of(approver_slack_id: str, employee_id: str | None = None) -> bool:
    """연장·휴일근무 승인 권한 판정 — 승인권은 사무장(또는 지회장)에게 있다."""
    return is_approver(approver_slack_id)


# ── 권한(역할) ────────────────────────────────────────
ROLES = ("employee", "manager", "hr", "sysadmin")
_ASSIGNERS = {"hr", "sysadmin"}          # 역할을 부여할 수 있는 주체

# 슈퍼유저: 모든 권한 검사 예외(테스트·운영용). 창현(지회장/개발자)
SUPERUSER_SLACK_IDS = {"U02V1HKUJNA"}

# 단일 직책 — 각 1명만. 새로 임명하면 기존 보유자는 '일반'으로 자동 인수인계(강등).
SINGLE_POSITIONS = ("지회장", "수석부지회장", "사무장")

# 조합 직책(표시용) → 권한 등급 매핑
POSITIONS = ("지회장", "수석부지회장", "사무장", "부지회장", "전임 스탭",
             "조직부장", "정책홍보부장", "재무운영부장", "대외협력부장")
POSITION_ROLE = {
    # 관리자(통계·설정 접근): 지회장·수석부지회장·사무장
    "지회장": "sysadmin", "수석부지회장": "sysadmin", "사무장": "hr",
    # 그 외 직책은 일반 권한(직원)
    "부지회장": "employee", "전임 스탭": "employee",
    "조직부장": "employee", "정책홍보부장": "employee",
    "재무운영부장": "employee", "대외협력부장": "employee",
}
_DEFAULT_POSITION = "전임 스탭"   # '일반' 대체 — 기본/강등 시 직책
APPROVER_POSITIONS = ("사무장",)          # 연장·휴일근무 승인권자


def approvers() -> list[dict]:
    """승인권자(사무장) 목록 — 승인 요청 알림 대상."""
    with session_scope() as s:
        rows = s.scalars(select(Employee).where(
            Employee.position.in_(APPROVER_POSITIONS))).all()
        return [_emp_dict(e) for e in rows]


def is_approver(approver_slack_id: str) -> bool:
    if approver_slack_id in SUPERUSER_SLACK_IDS:
        return True
    with session_scope() as s:
        a = s.scalar(select(Employee).where(Employee.slack_user_id == approver_slack_id))
        return bool(a and (getattr(a, "position", "") in APPROVER_POSITIONS
                           or a.role == "sysadmin"))


def assign_position(actor_slack_id: str, target_employee_id: str, position: str) -> None:
    """직책 지정 — 직책에 매핑된 권한(role)도 함께 설정.

    규칙:
    - 임명 권한: 지회장·수석부지회장·사무장(hr/sysadmin). 창현(슈퍼유저)은 모든 예외.
    - 단일 직책(지회장·수석부지회장·사무장)은 각 1명만 → 새로 임명하면 기존 보유자를
      '일반'으로 자동 인수인계(강등).
    """
    if position not in POSITIONS:
        raise ValueError(f"알 수 없는 직책: {position}")
    new_role = POSITION_ROLE[position]
    is_super = actor_slack_id in SUPERUSER_SLACK_IDS
    with session_scope() as s:
        actor = s.scalar(select(Employee).where(Employee.slack_user_id == actor_slack_id))
        if not is_super and (not actor or actor.role not in _ASSIGNERS):
            raise PermissionError("직책을 지정할 권한이 없습니다(사무장·지회장급 전용).")
        target = s.get(Employee, target_employee_id)
        if target is None:
            raise LookupError("대상 직원을 찾을 수 없습니다.")
        if not is_super:
            if (new_role == "sysadmin" or target.role == "sysadmin") and actor.role != "sysadmin":
                raise PermissionError("시스템관리자급 직책은 지회장(sysadmin)만 지정할 수 있습니다.")
        if position in SINGLE_POSITIONS:
            # 단일 직책(지회장·수석부지회장·사무장): 각 1명만.
            # 새로 임명하면 기존 보유자는 '전임 스탭'으로 자동 인수인계(강등).
            for other in s.scalars(select(Employee).where(
                    Employee.position == position, Employee.id != target_employee_id)).all():
                other.position = _DEFAULT_POSITION
                other.role = "employee"
        elif not is_super and target.role == "sysadmin" and new_role != "sysadmin" \
                and _count_sysadmins(s) <= 1:
            raise PermissionError("마지막 시스템관리자는 강등할 수 없습니다. 인수인계를 사용하세요.")
        target.position = position
        target.role = new_role


def upsert_employee(emp_id: str, slack: str, name: str, dept: str,
                    position: str = _DEFAULT_POSITION, display_name: str | None = None) -> None:
    """구성원 등록/수정 — 직책에 맞는 권한(role)도 함께 설정.
    name=Slack Full name, display_name=Slack Display name(표시용)."""
    from datetime import date
    role = POSITION_ROLE.get(position, "employee")
    with session_scope() as s:
        e = s.get(Employee, emp_id)
        if not e:
            e = Employee(id=emp_id, hire_date=date.today(), team_id="T1")
            s.add(e)
        e.slack_user_id = slack
        e.name = name
        if display_name is not None:
            e.display_name = display_name
        e.dept = dept
        e.position = position
        e.role = role
        if not s.get(WorkConfig, emp_id):
            s.add(WorkConfig(employee_id=emp_id, work_type="normal",
                             checkin="09:00", checkout="18:00",
                             break_start="12:00", break_end="13:00",
                             recovery={"mode": "none"}, short_rules=[]))


def update_employee_fields(emp_id: str, dept: str | None = None,
                           name: str | None = None,
                           display_name: str | None = None,
                           slack_user_id: str | None = None) -> None:
    """구성원 정보(부서·이름·표시이름·Slack ID) 수정 — 전달된 값만 변경.
    slack_user_id 변경 시 다른 구성원과 중복되면 예외(unique)."""
    with session_scope() as s:
        e = s.get(Employee, emp_id)
        if e is None:
            raise LookupError("대상 직원을 찾을 수 없습니다.")
        if dept is not None:
            e.dept = dept.strip() or e.dept
        if name is not None and name.strip():
            e.name = name.strip()
        if display_name is not None:
            e.display_name = display_name.strip() or None
        if slack_user_id is not None and slack_user_id.strip():
            new_sid = slack_user_id.strip()
            if new_sid != e.slack_user_id:
                dup = s.scalar(select(Employee).where(
                    Employee.slack_user_id == new_sid, Employee.id != emp_id))
                if dup is not None:
                    raise ValueError(f"이미 다른 구성원({dup.name})이 쓰는 Slack ID입니다.")
                e.slack_user_id = new_sid


def delete_employee(emp_id: str) -> None:
    from sqlalchemy import delete as _delete, update as _update
    with session_scope() as s:
        # 이 사람을 팀장(manager_id)으로 참조하는 직원 해제 → FK 제약 회피
        s.execute(_update(Employee).where(Employee.manager_id == emp_id).values(manager_id=None))
        # employee_id를 참조하는 모든 자식 레코드 정리 → FK 위반(500) 방지
        for M in (AttendanceRecord, LeaveRequest, Approval, SlackReminder):
            s.execute(_delete(M).where(M.employee_id == emp_id))
        wc = s.get(WorkConfig, emp_id)
        if wc:
            s.delete(wc)
        e = s.get(Employee, emp_id)
        if e:
            s.delete(e)


def list_all_employees() -> list[dict]:
    with session_scope() as s:
        return [{"id": e.id, "name": e.name,
                 "display_name": getattr(e, "display_name", None) or "",
                 "dept": e.dept, "role": e.role,
                 "position": getattr(e, "position", _DEFAULT_POSITION), "slack": e.slack_user_id}
                for e in s.scalars(select(Employee).order_by(Employee.dept, Employee.name)).all()]


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
    is_super = actor_slack_id in SUPERUSER_SLACK_IDS
    with session_scope() as s:
        actor = s.scalar(select(Employee).where(Employee.slack_user_id == actor_slack_id))
        if not is_super and (not actor or actor.role not in _ASSIGNERS):
            raise PermissionError("역할을 지정할 권한이 없습니다(hr/sysadmin 전용).")
        target = s.get(Employee, target_employee_id)
        if target is None:
            raise LookupError("대상 직원을 찾을 수 없습니다.")
        if not is_super:
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
