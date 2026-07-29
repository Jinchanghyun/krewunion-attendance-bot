"""SQLAlchemy 모델 — 설계 문서의 데이터 모델을 반영."""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (JSON, Boolean, Date, DateTime, Float, ForeignKey,
                        Integer, String, Time, UniqueConstraint)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Employee(Base):
    __tablename__ = "employees"
    id: Mapped[str] = mapped_column(String, primary_key=True)          # 사번 K-2041
    slack_user_id: Mapped[str] = mapped_column(String, index=True, unique=True)
    team_id: Mapped[str] = mapped_column(String)                        # Slack team
    name: Mapped[str] = mapped_column(String)
    dept: Mapped[str] = mapped_column(String)
    manager_id: Mapped[str | None] = mapped_column(ForeignKey("employees.id"), nullable=True)
    hire_date: Mapped[Date] = mapped_column(Date)
    leave_balance: Mapped[float] = mapped_column(Float, default=15.0)
    # 권한: employee < manager(승인) < hr(전체 조회·통계·설정) < sysadmin(회사설정·권한·API)
    role: Mapped[str] = mapped_column(String, default="employee")
    # 직책(표시용): 지회장·사무장·부장 등. 권한(role)은 직책에서 자동 매핑됨.
    position: Mapped[str] = mapped_column(String, default="일반")


class WorkConfig(Base):
    """Slack 모달과 웹 설정이 공유하는 근무 설정 원장."""
    __tablename__ = "work_configs"
    employee_id: Mapped[str] = mapped_column(ForeignKey("employees.id"), primary_key=True)
    work_type: Mapped[str] = mapped_column(String, default="normal")   # normal|flex|selective|elastic
    checkin: Mapped[str] = mapped_column(String, default="09:00")
    checkout: Mapped[str] = mapped_column(String, default="18:00")
    break_start: Mapped[str | None] = mapped_column(String, default="12:00")
    break_end: Mapped[str | None] = mapped_column(String, default="13:00")
    recovery: Mapped[dict] = mapped_column(JSON, default=dict)          # 놀금 설정
    short_rules: Mapped[list] = mapped_column(JSON, default=list)       # 단축근무 규칙 목록
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AttendanceRecord(Base):
    __tablename__ = "attendance_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[str] = mapped_column(ForeignKey("employees.id"), index=True)
    date: Mapped[Date] = mapped_column(Date, index=True)
    type: Mapped[str] = mapped_column(String, default="office")         # office|remote|field
    checked_in_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    checked_out_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    work_minutes: Mapped[int] = mapped_column(Integer, default=0)
    overtime_minutes: Mapped[int] = mapped_column(Integer, default=0)
    night_minutes: Mapped[int] = mapped_column(Integer, default=0)
    holiday_minutes: Mapped[int] = mapped_column(Integer, default=0)
    __table_args__ = (UniqueConstraint("employee_id", "date"),)


class LeaveRequest(Base):
    __tablename__ = "leave_requests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[str] = mapped_column(ForeignKey("employees.id"), index=True)
    kind: Mapped[str] = mapped_column(String)                           # annual|half_am|half_pm
    start: Mapped[Date] = mapped_column(Date)
    end: Mapped[Date] = mapped_column(Date)
    days: Mapped[float] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String, default="applied")      # 즉시 확정
    calendar_event_id: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Approval(Base):
    """연장·휴일근무 승인."""
    __tablename__ = "approvals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[str] = mapped_column(ForeignKey("employees.id"), index=True)
    kind: Mapped[str] = mapped_column(String)                          # overtime|holiday
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String, default="requested")
    approver_id: Mapped[str | None] = mapped_column(String, nullable=True)
    calendar_event_id: Mapped[str | None] = mapped_column(String, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class SlackReminder(Base):
    """예약 출퇴근 알림 중복 발송 가드."""
    __tablename__ = "slack_reminders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    employee_id: Mapped[str] = mapped_column(ForeignKey("employees.id"))
    date: Mapped[Date] = mapped_column(Date)
    kind: Mapped[str] = mapped_column(String)                          # checkin|checkout|remind
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    __table_args__ = (UniqueConstraint("employee_id", "date", "kind"),)


class SlackEvent(Base):
    """이벤트 멱등성 가드."""
    __tablename__ = "slack_events"
    event_id: Mapped[str] = mapped_column(String, primary_key=True)
    processed_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
