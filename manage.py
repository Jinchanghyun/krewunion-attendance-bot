"""배포 후 관리 CLI.

  python manage.py initdb
  python manage.py add-employee K-3001 U3001 김인사 경영지원 sysadmin
  python manage.py bootstrap-admin K-3001
  python manage.py issue-token U3001         # 대시보드 로그인 토큰 출력
  python manage.py list-employees
"""
from __future__ import annotations

import sys
from datetime import date

from app import db, repo
from app.config import settings
from app.models import Employee


def initdb(_args):
    db.configure()
    db.init_db()
    print("DB 초기화(테이블 생성) 완료")


def add_employee(args):
    if len(args) < 4:
        print("사용법: add-employee <사번> <slack_user_id> <이름> <부서> [role]")
        return
    eid, suid, name, dept = args[:4]
    role = args[4] if len(args) > 4 else "employee"
    db.configure()
    with db.session_scope() as s:
        s.merge(Employee(id=eid, slack_user_id=suid, name=name, dept=dept,
                         team_id="T1", hire_date=date.today(), role=role))
    print(f"직원 등록: {name} ({eid}) · slack={suid} · role={role}")


def bootstrap_admin(args):
    db.configure()
    repo.bootstrap_first_admin(args[0])
    print(f"최초 시스템 관리자 지정 시도: {args[0]}")


def issue_token(args):
    import jwt
    db.configure()
    token = jwt.encode({"slack_user_id": args[0]}, settings.JWT_SECRET, algorithm="HS256")
    print(token)


def list_employees(_args):
    from sqlalchemy import select
    db.configure()
    with db.session_scope() as s:
        for e in s.scalars(select(Employee)).all():
            print(f"{e.id}\t{e.slack_user_id}\t{e.name}\t{e.dept}\t{e.role}")


def seed_demo(_args):
    """샘플 직원·출퇴근·연차·승인 데이터를 넣어 대시보드 통계를 바로 확인.
    DATABASE_URL을 클라우드(Neon 등)로 설정하고 실행하면 그 DB에 저장된다."""
    import demo
    demo.seed()
    print("샘플 데이터 시드 완료 (직원·출퇴근·연차·승인). 대시보드에서 통계가 보입니다.")


CMDS = {"initdb": initdb, "add-employee": add_employee,
        "bootstrap-admin": bootstrap_admin, "issue-token": issue_token,
        "list-employees": list_employees, "seed-demo": seed_demo}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in CMDS:
        print("사용법: initdb | add-employee | bootstrap-admin | issue-token | list-employees | seed-demo")
        sys.exit(1)
    CMDS[sys.argv[1]](sys.argv[2:])
