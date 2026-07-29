"""로컬 데모 실행 — SQLite에 샘플 데이터를 넣고 관리자 통계 API를 띄운다.

    python demo.py

실행하면:
  1) sam_demo.db(SQLite)에 직원·출퇴근·연차·승인 샘플이 시드된다.
  2) http://127.0.0.1:8000 에 통계 API가 뜬다.
  3) 콘솔에 관리자(hr) 접속 토큰이 출력된다.
  4) app/web/dashboard.html 을 브라우저로 열고 상단에
     API = http://127.0.0.1:8000 , 토큰을 붙여넣으면 실데이터가 보인다.
"""
from __future__ import annotations

import os
import sys
from datetime import date, datetime, time

# Windows 콘솔에서도 한글이 깨지지 않도록 UTF-8 출력
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

os.environ.setdefault("DATABASE_URL", "sqlite:///sam_demo.db")
os.environ.setdefault("CORS_ORIGINS", "*")

from app import db, repo                       # noqa: E402
from app.config import settings                # noqa: E402
from app.models import Employee, WorkConfig    # noqa: E402


def seed() -> None:
    db.configure(os.environ["DATABASE_URL"])
    db.init_db()
    try:
        repo.employee_by_slack_id("U3001")     # 이미 시드되어 있으면 스킵
        return
    except Exception:
        pass

    with db.session_scope() as s:
        s.add_all([
            Employee(id="K-9001", slack_user_id="U9001", team_id="T1", name="박팀장",
                     dept="개발", hire_date=date(2020, 1, 1), role="manager"),
            Employee(id="K-3001", slack_user_id="U3001", team_id="T1", name="김인사",
                     dept="경영지원", hire_date=date(2019, 1, 1), role="hr"),
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
    # 오늘 현황
    repo.record_checkin("K-2041", "office", datetime.combine(today, time(9, 1)))
    repo.record_checkin("K-2044", "remote", datetime.combine(today, time(9, 12)))
    repo.record_checkin("K-3007", "field", datetime.combine(today, time(8, 40)))
    repo.create_leave("K-2050", "annual", today, today, 1.0)      # 오늘 연차 → 휴가
    # 이번 달 누적 예시(어제 퇴근 처리)
    repo.record_checkin("K-2041", "office", datetime.combine(today, time(9, 0)))
    repo.record_checkout("K-2041", datetime.combine(today, time(20, 0)))  # 연장 발생
    # 승인 대기
    repo.create_approval("K-2041", "overtime", {"detail": "18:00~21:00 릴리스 대응"})
    repo.create_approval("K-3007", "holiday", {"detail": "8/2(일) 전시회 지원"})


def main() -> None:
    seed()
    import jwt
    token = jwt.encode({"slack_user_id": "U3001"}, settings.JWT_SECRET, algorithm="HS256")
    # 루트(/) 대시보드가 토큰을 자동 입력하도록 환경변수로 전달 (admin.py가 읽음)
    os.environ["DEMO_AUTOTOKEN"] = token

    import uvicorn
    from app.web.admin import api

    print("=" * 64)
    print(" 크루유니언 근태봇 · 데모 서버")
    print(" 대시보드 : 브라우저에서  http://127.0.0.1:8000/  로 접속 (토큰 자동 입력)")
    print(" API 문서 : http://127.0.0.1:8000/docs")
    print(" 관리자 토큰(hr):")
    print("  ", token)
    print("=" * 64)
    uvicorn.run(api, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    main()
