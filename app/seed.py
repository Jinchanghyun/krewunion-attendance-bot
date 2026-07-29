"""샘플 데이터 시드 — 배포된 DB가 비어 있을 때 한 번 채워 통계를 확인용으로 표시.

demo.py/manage.py와 별개로, 서버(/setup)에서도 재사용할 수 있는 순수 함수.
"""
from __future__ import annotations

from datetime import date, datetime, time

from sqlalchemy import func, select

from app import db, repo
from app.models import Employee, WorkConfig


def already_seeded() -> bool:
    with db.session_scope() as s:
        return (s.scalar(select(func.count()).select_from(Employee)) or 0) > 0


def seed_demo() -> bool:
    """비어 있으면 샘플을 넣고 True, 이미 데이터가 있으면 False."""
    db.init_db()
    if already_seeded():
        return False

    with db.session_scope() as s:
        s.add_all([
            Employee(id="K-9001", slack_user_id="U9001", team_id="T1", name="박팀장",
                     dept="개발", hire_date=date(2020, 1, 1), role="manager"),
            Employee(id="K-3001", slack_user_id="U3001", team_id="T1", name="김인사",
                     dept="경영지원", hire_date=date(2019, 1, 1), role="sysadmin"),
            Employee(id="K-2041", slack_user_id="U2041", team_id="T1", name="홍길동",
                     dept="개발", manager_id="K-9001", hire_date=date(2022, 3, 1), leave_balance=10.0),
            Employee(id="K-2044", slack_user_id="U2044", team_id="T1", name="박민수",
                     dept="개발", manager_id="K-9001", hire_date=date(2021, 6, 1)),
            Employee(id="K-3007", slack_user_id="U3007", team_id="T1", name="최동욱",
                     dept="영업", manager_id="K-9001", hire_date=date(2023, 2, 1)),
            Employee(id="K-2050", slack_user_id="U2050", team_id="T1", name="이영희",
                     dept="디자인", manager_id="K-9001", hire_date=date(2022, 9, 1)),
        ])
    with db.session_scope() as s:
        for eid in ("K-2041", "K-2044", "K-3007"):
            s.add(WorkConfig(employee_id=eid, work_type="normal",
                             checkin="09:00", checkout="18:00",
                             break_start="12:00", break_end="13:00",
                             recovery={"mode": "none"}, short_rules=[]))

    today = date.today()
    repo.record_checkin("K-2041", "office", datetime.combine(today, time(9, 1)))
    repo.record_checkin("K-2044", "remote", datetime.combine(today, time(9, 12)))
    repo.record_checkin("K-3007", "field", datetime.combine(today, time(8, 40)))
    repo.create_leave("K-2050", "annual", today, today, 1.0)
    repo.record_checkout("K-2041", datetime.combine(today, time(20, 0)))
    repo.create_approval("K-2041", "overtime", {"detail": "18:00~21:00 릴리스 대응"})
    repo.create_approval("K-3007", "holiday", {"detail": "8/2(일) 전시회 지원"})
    return True
