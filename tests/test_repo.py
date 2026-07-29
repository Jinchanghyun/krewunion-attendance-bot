"""저장소·통계·권한 통합 테스트 (SQLite in-memory)."""
from datetime import date, datetime, time

import jwt
import pytest

from app import db, repo
from app.config import settings
from app.models import Employee, WorkConfig

TODAY = date.today()


def setup_module(_):
    db.configure("sqlite://")
    db.init_db()
    with db.session_scope() as s:
        s.add_all([
            Employee(id="K-9001", slack_user_id="U9001", team_id="T1", name="박팀장",
                     dept="개발", hire_date=date(2020, 1, 1), role="manager"),
            Employee(id="K-2041", slack_user_id="U2041", team_id="T1", name="홍길동",
                     dept="개발", manager_id="K-9001", hire_date=date(2022, 3, 1),
                     leave_balance=10.0),
            Employee(id="K-3001", slack_user_id="U3001", team_id="T1", name="김인사",
                     dept="경영지원", hire_date=date(2019, 1, 1), role="hr"),
        ])
    with db.session_scope() as s:
        s.add(WorkConfig(employee_id="K-2041", work_type="normal",
                         checkin="09:00", checkout="18:00",
                         break_start="12:00", break_end="13:00",
                         recovery={"mode": "none"}, short_rules=[]))


def test_checkin_state_and_overview():
    repo.record_checkin("K-2041", "office", datetime.combine(TODAY, time(9, 0)))
    assert repo.today_state("K-2041")["status"] == "work"
    ov = repo.stats_overview(TODAY)
    assert ov["present"] >= 1
    assert ov["attendance_rate"] > 0


def test_checkout_computes_overtime():
    repo.record_checkout("K-2041", datetime.combine(TODAY, time(20, 0)))
    month = TODAY.strftime("%Y-%m")
    rows = {r["employee_id"]: r for r in repo.stats_by_employee(month)["rows"]}
    # 09:00~20:00(11h) - 휴게 1h = 600분 근무, 소정 480 → 연장 120
    assert rows["K-2041"]["worked"] == 600
    assert rows["K-2041"]["overtime"] == 120


def test_leave_deducts_balance():
    req = repo.create_leave("K-2041", "half_am", TODAY, TODAY, 0.5)
    assert req["balance_after"] == 9.5
    assert req["kind_label"] == "오전반차"


def test_approval_flow_and_authority():
    # 승인권은 사무장에게 있다 → K-9001을 사무장으로 지정(hr가 지정)
    repo.assign_position("U3001", "K-9001", "사무장")
    a = repo.create_approval("K-2041", "overtime", {"detail": "18:00~21:00 릴리스"})
    assert a["status"] == "requested"
    assert repo.is_approver("U9001") is True                 # 사무장이 승인권자
    assert repo.is_manager_of("U9001", "K-2041") is True      # (동일 판정)
    assert repo.is_manager_of("U2041", "K-2041") is False     # 일반 직원은 불가
    done = repo.update_approval(a["id"], "approved", "U9001")
    assert done["status"] == "approved"
    assert repo.pending_approvals()["rows"] == []


def test_roles_and_assignment():
    assert repo.role_of("U3001") == "hr"
    assert repo.role_of("U2041") == "employee"
    # hr가 직원을 매니저로 승격
    repo.assign_role("U3001", "K-2041", "manager")
    assert repo.role_of("U2041") == "manager"
    # 일반 직원은 역할 지정 불가
    with pytest.raises(PermissionError):
        repo.assign_role("U9001", "K-3001", "sysadmin")


def test_admin_jwt_guard():
    from app.web.admin import require_admin
    hr_token = jwt.encode({"slack_user_id": "U3001"}, settings.JWT_SECRET, algorithm="HS256")
    assert require_admin(f"Bearer {hr_token}")["role"] == "hr"
    # 권한 없는 사용자(U2041은 manager로 승격됨 → 통계 접근 불가 403)
    emp_token = jwt.encode({"slack_user_id": "U2041"}, settings.JWT_SECRET, algorithm="HS256")
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        require_admin(f"Bearer {emp_token}")


def test_admin_handover_and_revoke():
    # 최초 설치자 부트스트랩 → K-3001(김인사)이 시스템 관리자
    repo.bootstrap_first_admin("K-3001")
    assert repo.role_of("U3001") == "sysadmin"

    # 마지막 시스템 관리자는 후임 없이 해제 불가
    with pytest.raises(PermissionError):
        repo.assign_role("U3001", "K-3001", "employee")

    # 시스템 관리자가 아니면 인수인계 불가
    with pytest.raises(PermissionError):
        repo.handover_admin("U2041", "K-9001")

    # 인수인계: 후임(K-9001) 승격 + 본인(K-3001) 강등을 원자적으로
    repo.handover_admin("U3001", "K-9001")
    assert repo.role_of("U9001") == "sysadmin"
    assert repo.role_of("U3001") == "employee"

    # 지정 해제(강등): 새 관리자가 다른 사람을 일반 직원으로
    repo.revoke_role("U9001", "K-2041")
    assert repo.role_of("U2041") == "employee"


def test_registration_guard():
    # 미등록 사용자는 None → Slack 핸들러에서 '미등록 홈'으로 안내
    assert repo.try_employee_by_slack_id("U-UNKNOWN") is None
    assert repo.try_employee_by_slack_id("U3001") is not None
