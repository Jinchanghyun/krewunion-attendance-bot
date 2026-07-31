"""저장소 계층 — SQLAlchemy 구현.

Slack 핸들러·스케줄러·웹 라우트는 이 인터페이스만 바라본다.
도메인 엔진(schedule/attendance/leave)과 연동해 파생값을 계산한다.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from sqlalchemy import select

from app.config import now_kst, today_kst
from app.db import session_scope
from app.models import (Approval, AttendanceRecord, Company, Employee, LeaveRequest,
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


def today_leave_kind(employee_id: str, d: date | None = None) -> str | None:
    """오늘(또는 지정일) 적용되는 휴가 종류. 없으면 None."""
    with session_scope() as s:
        return _today_leave_kind(s, employee_id, d or today_kst())


# 반일(부분 근무 가능) 종류 — 이 외 종일 휴가는 출근 불가
_HALF_LEAVE = ("half_am", "half_pm", "_am", "_pm")


def _is_recovery(s, employee_id: str, d: date) -> bool:
    """놀금(리커버리데이) 여부 — 개인 근무설정 기준."""
    wc = s.get(WorkConfig, employee_id)
    cfg = _config_dict(wc) if wc else work_config(employee_id)
    return is_recovery_day(cfg, d)


def _full_leave_kind(s, employee_id: str, d: date) -> str | None:
    """실제 신청된 '종일' 휴가 종류(연차 등). 반차·시간단위 커스텀·놀금 제외. 없으면 None.
    → 출근 차단 판정 기준(놀금은 출근 허용이라 여기 포함하지 않음)."""
    lk = _today_leave_kind(s, employee_id, d)
    if not lk or lk.endswith("_am") or lk.endswith("_pm"):
        return None
    if leave_engine.is_custom(lk):   # 커스텀은 '일 단위'만 종일로 취급
        wc = s.get(WorkConfig, employee_id)
        if not leave_engine.is_custom_full_day(lk, (wc.leave_config if wc else None)):
            return None
    return lk


def today_full_leave_kind(employee_id: str, d: date | None = None) -> str | None:
    """오늘 종일 휴가(연차 등, 놀금 제외). 출근 차단용. 없으면 None."""
    with session_scope() as s:
        return _full_leave_kind(s, employee_id, d or today_kst())


def _checked_in(s, employee_id: str, d: date) -> bool:
    rec = s.scalar(select(AttendanceRecord).where(
        AttendanceRecord.employee_id == employee_id, AttendanceRecord.date == d))
    return bool(rec and rec.checked_in_at)


def today_off_people(d: date | None = None) -> list[dict]:
    """그날 종일 휴무(연차·놀금 등)인 직원 목록. 반차·이미 출근한 놀금자는 제외.
    반환: [{name, display_name, kind, label}] — 직책 순 정렬."""
    d = d or today_kst()
    out = []
    with session_scope() as s:
        for e in _by_position(s.scalars(select(Employee)).all()):
            kind = _full_leave_kind(s, e.id, d)
            if not kind:
                rec = s.scalar(select(AttendanceRecord).where(
                    AttendanceRecord.employee_id == e.id, AttendanceRecord.date == d))
                if rec and rec.checked_in_at:
                    continue                       # 이미 출근 → 휴무 아님
                if rec and rec.type == "dayoff":
                    kind = "dayoff"                 # 데이오프
                elif _is_recovery(s, e.id, d):
                    kind = "recovery"               # 놀금(미출근)
            if not kind:
                continue
            lc = None
            if leave_engine.is_custom(kind):
                wc = s.get(WorkConfig, e.id)
                lc = wc.leave_config if wc else None
            out.append({"name": e.name,
                        "display_name": getattr(e, "display_name", None) or "",
                        "kind": kind,
                        "label": leave_engine.label_of(kind, lc)})
    return out


def is_on_full_leave(employee_id: str, d: date | None = None) -> bool:
    """오늘 종일 휴가(연차 등)면 True — 반차·놀금은 False(놀금은 출근 가능)."""
    with session_scope() as s:
        return _full_leave_kind(s, employee_id, d or today_kst()) is not None


def today_state(employee_id: str) -> dict:
    today = today_kst()
    with session_scope() as s:
        fl = _full_leave_kind(s, employee_id, today)
        if fl:   # 실제 종일 휴가(연차 등) → 출근 불가
            return {"status": "off", "worked": "-", "leave_kind": fl}
        rec = s.scalar(select(AttendanceRecord).where(
            AttendanceRecord.employee_id == employee_id, AttendanceRecord.date == today))
        if rec and rec.checked_in_at:   # 출근 기록 있으면 근무 처리(놀금이라도)
            status = _TYPE_TO_STATUS.get(rec.type, "work")
            end = rec.checked_out_at or now_kst()
            mins = max(0, int((end - rec.checked_in_at).total_seconds() // 60))
            return {"status": status,
                    "worked": f"{mins // 60}시간 {mins % 60:02d}분", "leave_kind": None}
        if rec and rec.type == "dayoff":   # 자동/수동 데이오프 → 휴무
            return {"status": "off", "worked": "-", "leave_kind": "dayoff"}
        # 미출근
        if _is_recovery(s, employee_id, today):   # 놀금(미출근) → 휴무 표시, 출근은 가능
            return {"status": "off", "worked": "-", "leave_kind": "recovery"}
        lk = _today_leave_kind(s, employee_id, today)   # 반차 표시용
        return {"status": "none", "worked": "-", "leave_kind": lk}


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
            if already and (already.checked_in_at or already.type == "dayoff"):
                continue   # 이미 출근했거나 데이오프면 출근 알림 안 보냄
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
                  checkout_hm: str | None = None, kind: str = "office",
                  away_min: int = 0) -> None:
    """누락·수동 출퇴근 등록/수정 — 과거 날짜 포함. away_min: 자리비움(분) 차감."""
    from datetime import time as _t
    ci = datetime.combine(d, _t(*map(int, checkin_hm.split(":"))))
    co = datetime.combine(d, _t(*map(int, checkout_hm.split(":")))) if checkout_hm else None
    away_min = max(0, int(away_min or 0))
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
            worked = max(0, summary["worked"] - away_min)   # 자리비움 차감
            rec.work_minutes = worked
            rec.night_minutes = summary["night"]
            if summary["holiday"]:
                rec.holiday_minutes = worked
                rec.overtime_minutes = 0
            else:
                rec.holiday_minutes = 0
                rec.overtime_minutes = max(0, worked - summary["scheduled"])


def set_dayoff(employee_id: str, d: date) -> None:
    """데이오프(선택적 근무: 그날 근무 안 함) — 실근로 0, 휴가 아님."""
    with session_scope() as s:
        rec = s.scalar(select(AttendanceRecord).where(
            AttendanceRecord.employee_id == employee_id, AttendanceRecord.date == d))
        if not rec:
            rec = AttendanceRecord(employee_id=employee_id, date=d)
            s.add(rec)
        rec.type = "dayoff"
        rec.checked_in_at = None
        rec.checked_out_at = None
        rec.work_minutes = 0
        rec.overtime_minutes = 0
        rec.night_minutes = 0
        rec.holiday_minutes = 0


def _set_dayoff_row(s, employee_id: str, d: date) -> None:
    """세션 내부용 데이오프 마킹."""
    rec = s.scalar(select(AttendanceRecord).where(
        AttendanceRecord.employee_id == employee_id, AttendanceRecord.date == d))
    if not rec:
        rec = AttendanceRecord(employee_id=employee_id, date=d)
        s.add(rec)
    rec.type = "dayoff"
    rec.checked_in_at = rec.checked_out_at = None
    rec.work_minutes = rec.overtime_minutes = rec.night_minutes = rec.holiday_minutes = 0


def apply_auto_dayoff(employee_id: str, on_date: date | None = None) -> list[str]:
    """선택근무자가 이번 달 소정근로를 충족(실근로+휴가 ≥ 소정)했으면,
    그날 포함 이후 남은 근무일을 자동 데이오프로 표시한다.
    새로 데이오프 처리된 날짜(ISO) 목록 반환 — 비어 있으면 변화 없음(=DM 불필요)."""
    from calendar import monthrange
    from app.domain import worktime as _wt
    from app.domain.schedule import FRI
    on_date = on_date or today_kst()
    yy, mm = on_date.year, on_date.month
    first = date(yy, mm, 1)
    last = date(yy, mm, monthrange(yy, mm)[1])
    with session_scope() as s:
        wc = s.get(WorkConfig, employee_id)
        cfg = _config_dict(wc) if wc else work_config(employee_id)
        if cfg.get("work_type") != "selective":
            return []
        recs = s.scalars(select(AttendanceRecord).where(
            AttendanceRecord.employee_id == employee_id,
            AttendanceRecord.date >= first, AttendanceRecord.date <= last)).all()
        records = [{"work": r.work_minutes or 0, "date": r.date.isoformat()} for r in recs]
        leaves = [{"kind": l.kind, "start": l.start.isoformat(), "end": l.end.isoformat()}
                  for l in s.scalars(select(LeaveRequest).where(
                      LeaveRequest.employee_id == employee_id,
                      LeaveRequest.start <= last, LeaveRequest.end >= first)).all()]
        summ = _wt.monthly_summary(cfg, records, leaves, yy, mm)
        # 소정 충족은 '실근로시간'만으로 판단(휴가 사용시간은 합산하지 않음)
        if summ["actual_min"] < summ["scheduled_min"] or summ["scheduled_min"] == 0:
            return []   # 아직 소정 미충족
        by_date = {r.date: r for r in recs}
        newly = []
        for day in range(on_date.day, last.day + 1):
            d = date(yy, mm, day)
            if d.weekday() > FRI or is_recovery_day(cfg, d):
                continue                                  # 주말·놀금 제외
            if _today_leave_kind(s, employee_id, d):
                continue                                  # 이미 휴가
            r = by_date.get(d)
            if r and (r.checked_in_at or r.type == "dayoff"):
                continue                                  # 이미 근무했거나 데이오프
            _set_dayoff_row(s, employee_id, d)
            newly.append(d.isoformat())
        return newly


def selective_employees() -> list[dict]:
    """선택근무자 목록(자동 데이오프 대상). [{id, slack_user_id, name}]"""
    with session_scope() as s:
        out = []
        for e in s.scalars(select(Employee)).all():
            c = s.get(WorkConfig, e.id)
            wt = c.work_type if c else "normal"
            if wt == "selective":
                out.append({"id": e.id, "slack_user_id": e.slack_user_id, "name": e.name})
        return out


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
        wc = s.get(WorkConfig, employee_id)
        lc = (wc.leave_config if wc else None) or {}
        lr = LeaveRequest(employee_id=employee_id, kind=kind, start=start,
                          end=end, days=days, reason=(reason or None), status="applied")
        s.add(lr)
        s.flush()
        return {"id": lr.id, "employee_id": employee_id, "kind": kind,
                "kind_label": leave_engine.label_of(kind, lc), "emp_name": e.name,
                "start": start, "end": end, "days": days,
                "balance_after": e.leave_balance}


# 일(day) 단위로 부여·차감하는 휴가 그룹(당해년도 사용가능 일수)
_DAY_LEAVE_GROUPS = {
    "annual": (["annual", "half_am", "half_pm"], "연차"),
    "family_care_paid": (["family_care_paid", "family_care"], "가족돌봄(유급)"),
    "family_care_unpaid": (["family_care_unpaid"], "가족돌봄(무급)"),
    "health": (["health"], "건강휴가"),
    "sabbatical": (["sabbatical"], "안식휴가"),
    "refresh": (["refresh"], "리프레쉬 휴가"),
    "special": (["special"], "특별휴가"),
}
_HALF_LEAVE_KINDS = {"half_am", "half_pm"}
# 워킹데이가 아니라 달력 일수(주말 포함)로 세는 그룹 — 안식휴가
_CALENDAR_DAY_GROUPS = {"sabbatical"}

# 시간(시간) 단위 휴가 그룹 — 사용 시간으로 표시
_HOUR_LEAVE_GROUPS = {
    "bd": (["bd"], "BD"),
    "seollal": (["seollal"], "설날"),
    "chuseok": (["chuseok"], "추석"),
    "birthday": (["birthday_full", "birthday_am", "birthday_pm"], "생일"),
    "health_check": (["health_check_full", "health_check_am", "health_check_pm"], "건강검진"),
}


def leave_used_hours(employee_id: str, group: str, year: int | None = None) -> float:
    """당해년도 시간 단위 휴가 사용(시간)."""
    if year is None:
        year = date.today().year
    ys, ye = date(year, 1, 1), date(year, 12, 31)
    lc = (work_config(employee_id).get("leave_config") or {})
    kinds = _HOUR_LEAVE_GROUPS[group][0]
    used = 0.0
    with session_scope() as s:
        rows = s.scalars(select(LeaveRequest).where(
            LeaveRequest.employee_id == employee_id, LeaveRequest.kind.in_(kinds),
            LeaveRequest.start <= ye, LeaveRequest.end >= ys)).all()
        for l in rows:
            if l.kind.endswith("_am") or l.kind.endswith("_pm"):
                used += 4
            elif group == "bd":
                used += lc.get("bd", {}).get("hours", 4)
            else:  # seollal·chuseok·*_full → 설정 시간(기본 8)
                used += lc.get(group, {}).get("hours", 8)
    return round(used, 1)


def _leave_group_of(kind: str) -> str | None:
    for g, (kinds, _) in _DAY_LEAVE_GROUPS.items():
        if kind in kinds:
            return g
    for g, (kinds, _) in _HOUR_LEAVE_GROUPS.items():
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
        out.append({"group": g, "label": label, "unit": "일", "granted": granted,
                    "used": used, "remaining": round(granted - used, 2)})
    for g, (_, label) in _HOUR_LEAVE_GROUPS.items():
        conf = lc.get(g) or {}
        if not conf.get("on", False):
            continue
        # 부여: 직접 입력한 사용가능(일)이 있으면 그 값, 없으면 설정 시간 기준(4h=0.5일, 8h=1일)
        granted = float(conf.get("quota") or 0)
        if not granted:
            hrs = conf.get("hours", 4 if g == "bd" else 8)
            granted = round(hrs / 8.0, 2)
        used_days = round(leave_used_hours(employee_id, g) / 8.0, 2)  # 4시간=0.5일
        out.append({"group": g, "label": label, "unit": "일", "granted": granted,
                    "used": used_days, "remaining": round(granted - used_days, 2)})
    return out


def stats_leave_types(year: int) -> dict:
    """연 휴가유형별 통계(전체) — 종류별 건수·일수(당해년도)."""
    from app.domain.leave import LEAVE_LABEL
    from app.domain.schedule import FRI
    start, end = date(year, 1, 1), date(year, 12, 31)
    half = {"half_am", "half_pm", "health_check_am", "health_check_pm",
            "birthday_am", "birthday_pm"}
    agg: dict = {}
    with session_scope() as s:
        # 커스텀 휴가 메타(전체 구성원 설정에서 수집)
        cust_meta: dict = {}
        for wc in s.scalars(select(WorkConfig)).all():
            for c in ((wc.leave_config or {}).get("custom") or []):
                if isinstance(c, dict) and c.get("key"):
                    cust_meta[c["key"]] = c
        cust_labels = {k: (c.get("name") or k) for k, c in cust_meta.items()}
        rows = s.scalars(select(LeaveRequest).where(
            LeaveRequest.start <= end, LeaveRequest.end >= start)).all()
        for l in rows:
            a = agg.setdefault(l.kind, [0, 0.0])
            a[0] += 1
            _cm = cust_meta.get(l.kind)
            if l.kind in half:
                a[1] += 0.5
            elif _cm is not None and _cm.get("unit") == "hour":
                a[1] += round((_cm.get("hours") or 8) / 8.0, 2)   # 시간단위 커스텀
            else:
                cur, e2, d = max(l.start, start), min(l.end, end), 0
                while cur <= e2:
                    if cur.weekday() <= FRI:
                        d += 1
                    cur += timedelta(days=1)
                a[1] += d
    out = [{"kind": k, "label": LEAVE_LABEL.get(k) or cust_labels.get(k, k), "count": v[0],
            "days": round(v[1], 1)} for k, v in agg.items()]
    out.sort(key=lambda x: -x["days"])
    return {"year": year, "rows": out}


def all_leave_balances() -> dict:
    """관리자용 — 전체 구성원의 휴가 부여/사용/잔여(일·시간 단위 전체)."""
    groups = [{"group": g, "label": lbl, "unit": "일"} for g, (_, lbl) in _DAY_LEAVE_GROUPS.items()]
    groups += [{"group": g, "label": lbl, "unit": "일"} for g, (_, lbl) in _HOUR_LEAVE_GROUPS.items()]
    rows = []
    for e in list_all_employees():
        bmap = {b["group"]: b for b in leave_balances(e["id"])}
        rows.append({"id": e["id"], "name": e["name"],
                     "display_name": e.get("display_name", ""),
                     "dept": e["dept"], "position": e.get("position", ""),
                     "balances": bmap})
    return {"groups": groups, "rows": rows}


def my_leaves(employee_id: str, limit: int = 30) -> list[dict]:
    """본인 연차 신청 내역(최근순)."""
    with session_scope() as s:
        wc = s.get(WorkConfig, employee_id)
        lc = (wc.leave_config if wc else None) or {}
        rows = s.scalars(select(LeaveRequest).where(
            LeaveRequest.employee_id == employee_id
        ).order_by(LeaveRequest.start.desc()).limit(limit)).all()
        return [{"id": l.id, "kind": l.kind,
                 "kind_label": leave_engine.label_of(l.kind, lc),
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
        wc = s.get(WorkConfig, lr.employee_id)
        lc = (wc.leave_config if wc else None) or {}
        return {"id": lr.id, "employee_id": lr.employee_id, "kind": lr.kind,
                "kind_label": leave_engine.label_of(lr.kind, lc), "emp_name": e.name,
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
            ref = str(p.get("month") or p.get("date", ""))[:7]
            if ref != month:
                continue
            if p.get("minutes") is not None:       # 월 단위(선택적) 신청
                approved_ot += int(p["minutes"])
            else:                                  # 일 단위(start~end)
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
    # 선택적 근무는 소정 충족 시 남은 날 자동 데이오프 + 초과근무는 사전 승인(시간외근무 탭)
    # 으로 처리하므로 월초 사후 승인 절차는 두지 않는다.
    wt["can_request_ot"] = False
    wt["ot_window_note"] = "초과근무는 시간외근무 탭에서 사전 승인 후 인정됩니다." \
        if wt["work_type"] in ("selective", "flex") else ""
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
                rec = s.scalar(select(AttendanceRecord).where(
                    AttendanceRecord.employee_id == e.id, AttendanceRecord.date == y))
                mins = rec.work_minutes if rec else 0
                # 토·일 근로는 전부 초과, 평일은 8h 초과분
                ot = mins if y.weekday() >= 5 else max(0, mins - _wt.STD_DAY_MIN)
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


def has_month_overtime_request(employee_id: str, month: str) -> bool:
    """해당 월 연장근로가 이미 신청/승인됐는지."""
    with session_scope() as s:
        for a in s.scalars(select(Approval).where(
                Approval.employee_id == employee_id, Approval.kind == "overtime",
                Approval.status.in_(["requested", "approved"]))).all():
            if str((a.payload or {}).get("month") or "")[:7] == month:
                return True
    return False


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

# 표시 정렬 순서: 지회장→수석부지회장→사무장→부지회장→부서 부장들→전임 스탭
_POSITION_SORT = ("지회장", "수석부지회장", "사무장", "부지회장",
                  "조직부장", "정책홍보부장", "재무운영부장", "대외협력부장", "전임 스탭")


def _pos_rank(pos: str) -> int:
    try:
        return _POSITION_SORT.index(pos)
    except ValueError:
        return len(_POSITION_SORT)


def _by_position(employees):
    """직책 순서 → 이름 순 정렬."""
    return sorted(employees, key=lambda e: (_pos_rank(getattr(e, "position", "") or ""),
                                            getattr(e, "name", "") or ""))


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
                    position: str = _DEFAULT_POSITION, display_name: str | None = None,
                    company: str | None = None) -> None:
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
        if company is not None:
            e.company = company or None
        e.dept = dept
        e.position = position
        e.role = role
        if not s.get(WorkConfig, emp_id):
            s.add(WorkConfig(employee_id=emp_id, work_type="normal",
                             checkin="09:00", checkout="18:00",
                             break_start="12:00", break_end="13:00",
                             recovery={"mode": "none"}, short_rules=[]))


_DEFAULT_COMPANIES = ["카카오", "카카오게임즈", "카카오페이", "카카오모빌리티", "카카오뱅크",
                      "카카오엔터테인먼트", "KEP", "카카오VX", "디케이테크인", "케이앤웍스", "엑스엘게임즈"]


def list_companies() -> list[str]:
    """소속회사 목록 — 기본 + 추가 등록분 + 실제 사용 중인 값(중복 제거, 순서 유지)."""
    with session_scope() as s:
        stored = [c.name for c in s.scalars(select(Company)).all()]
        used = [e.company for e in s.scalars(select(Employee)).all() if getattr(e, "company", None)]
    out = []
    for n in _DEFAULT_COMPANIES + sorted(stored) + used:
        if n and n not in out:
            out.append(n)
    return out


def add_company(name: str) -> None:
    name = (name or "").strip()
    if not name:
        return
    with session_scope() as s:
        if not s.get(Company, name):
            s.add(Company(name=name))


def update_employee_fields(emp_id: str, dept: str | None = None,
                           name: str | None = None,
                           display_name: str | None = None,
                           slack_user_id: str | None = None,
                           company: str | None = None) -> None:
    """구성원 정보(부서·이름·표시이름·Slack ID·소속회사) 수정 — 전달된 값만 변경.
    slack_user_id 변경 시 다른 구성원과 중복되면 예외(unique)."""
    with session_scope() as s:
        e = s.get(Employee, emp_id)
        if e is None:
            raise LookupError("대상 직원을 찾을 수 없습니다.")
        if company is not None:
            e.company = company.strip() or None
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
                 "company": getattr(e, "company", None) or "",
                 "dept": e.dept, "role": e.role,
                 "position": getattr(e, "position", _DEFAULT_POSITION), "slack": e.slack_user_id}
                for e in _by_position(s.scalars(select(Employee)).all())]


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
    today = today or today_kst()
    with session_scope() as s:
        emps = s.scalars(select(Employee)).all()
        counts = {"work": 0, "remote": 0, "field": 0, "off": 0, "none": 0}
        for e in emps:
            if _full_leave_kind(s, e.id, today):   # 실제 종일 휴가(연차 등)
                counts["off"] += 1
                continue
            rec = s.scalar(select(AttendanceRecord).where(
                AttendanceRecord.employee_id == e.id, AttendanceRecord.date == today))
            if rec and rec.checked_in_at and not rec.checked_out_at:
                counts[_TYPE_TO_STATUS.get(rec.type, "work")] += 1
            elif rec and rec.checked_out_at:
                counts["work"] += 1
            elif rec and rec.type == "dayoff":   # 데이오프 → 휴무
                counts["off"] += 1
            elif _is_recovery(s, e.id, today):   # 놀금 미출근 → 휴무
                counts["off"] += 1
            else:
                counts["none"] += 1
        present = counts["work"] + counts["remote"] + counts["field"]
        eligible = len(emps) - counts["off"]
        pending = s.scalar(select(Approval).where(Approval.status == "requested"))
        pending_n = len(s.scalars(select(Approval).where(Approval.status == "requested")).all())
        rate = round(present / eligible * 100) if eligible else 0
        return {"attendance_rate": rate, "present": present, "eligible": eligible,
                "total": len(emps),
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
    from app.domain import worktime as _wt
    start, end = _month_range(month)
    yy, mm = int(month[:4]), int(month[5:7])
    rows = []
    with session_scope() as s:
        for e in _by_position(s.scalars(select(Employee)).all()):
            recs = s.scalars(select(AttendanceRecord).where(
                AttendanceRecord.employee_id == e.id,
                AttendanceRecord.date >= start, AttendanceRecord.date <= end)).all()
            leaves = [{"kind": l.kind, "start": l.start.isoformat(), "end": l.end.isoformat()}
                      for l in s.scalars(select(LeaveRequest).where(
                          LeaveRequest.employee_id == e.id,
                          LeaveRequest.start <= end, LeaveRequest.end >= start)).all()]
            wc = s.get(WorkConfig, e.id)
            cfg = _config_dict(wc) if wc else work_config(e.id)
            leave_min = _wt.leave_used_minutes(cfg, leaves, yy, mm)
            rows.append({"employee_id": e.id, "name": e.name,
                         "display_name": getattr(e, "display_name", None) or "",
                         "company": getattr(e, "company", None) or "",
                         "dept": e.dept, "position": getattr(e, "position", ""),
                         "worked": sum(r.work_minutes for r in recs),
                         "overtime": sum(r.overtime_minutes for r in recs),
                         "night": sum(r.night_minutes for r in recs),
                         "leave": leave_min})
    return {"month": month, "rows": rows}


def live_status(status: str | None = None, dept: str | None = None) -> dict:
    today = today_kst()
    rows = []
    with session_scope() as s:
        for e in _by_position(s.scalars(select(Employee)).all()):
            if dept and e.dept != dept:
                continue
            st = today_state(e.id)["status"]
            if status and st != status:
                continue
            wc = s.get(WorkConfig, e.id)
            cfg = _config_dict(wc) if wc else None
            rows.append({"employee_id": e.id, "name": e.name,
                         "display_name": getattr(e, "display_name", None) or "",
                         "company": getattr(e, "company", None) or "",
                         "dept": e.dept, "status": st,
                         "work_type": (cfg or {}).get("work_type", "normal"),
                         "checkin": (cfg or {}).get("checkin", "09:00"),
                         "checkout": (cfg or {}).get("checkout", "18:00")})
    return {"filter": {"status": status, "dept": dept}, "rows": rows}


def pending_approvals() -> dict:
    rows = []
    with session_scope() as s:
        for a in s.scalars(select(Approval).where(Approval.status == "requested")).all():
            e = s.get(Employee, a.employee_id)
            rows.append({"id": a.id, "employee": e.name, "dept": e.dept,
                         "kind": a.kind, "detail": (a.payload or {}).get("detail", "")})
    return {"rows": rows}
