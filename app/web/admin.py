"""관리자 웹 API (FastAPI) + Slack HTTP 엔드포인트 + Cron.

- 관리자 통계는 어느 컴퓨터에서든 브라우저로 접근(로그인/권한 필요, CORS 허용).
- 서버리스(Vercel 등)에서는 Slack을 HTTP Request URL 모드로, 스케줄러는 Cron으로 구동.
"""
from __future__ import annotations

import os
import pathlib

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import settings
from app import repo

api = FastAPI(title="크루유니언 근태봇 · 관리자 통계 API")

api.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in settings.CORS_ORIGINS.split(",")],
    allow_methods=["*"], allow_headers=["*"], allow_credentials=True,
)

_DASHBOARD = pathlib.Path(__file__).parent / "dashboard.html"


@api.on_event("startup")
def _startup():
    """부팅 시 테이블이 없으면 생성(간단 배포용). 운영 마이그레이션은 Alembic 권장.
    DB 미설정이어도 대시보드 페이지는 뜨도록 실패를 삼킨다."""
    try:
        from app.db import init_db
        init_db()
    except Exception as e:  # DATABASE_URL 미설정 등
        print("startup init_db skipped:", e)


@api.get("/", response_class=HTMLResponse)
def dashboard():
    """루트에서 관리자 대시보드를 제공. API 주소는 배포된 origin으로 자동 설정.
    로컬 데모(DEMO_AUTOTOKEN 설정 시)에서는 토큰까지 자동 입력된다."""
    html = _DASHBOARD.read_text(encoding="utf-8")
    inject = "<script>window.DEMO_API=location.origin;"
    tok = os.getenv("DEMO_AUTOTOKEN")
    if tok:
        inject += f"window.DEMO_TOKEN='{tok}';"
    inject += "</script>"
    return html.replace("</head>", inject + "</head>")


@api.get("/healthz")
def healthz():
    return {"ok": True}


@api.get("/go/{code}")
def go(code: str):
    """홈 버튼용 짧은 코드 → 검증 후 실제 페이지로 리다이렉트(토큰 부여)."""
    import jwt
    from datetime import datetime, timedelta, timezone
    from app.links import parse_code
    d = parse_code(code)
    if not d:
        raise HTTPException(403, "링크가 만료되었거나 올바르지 않습니다. Slack에서 다시 열어주세요.")
    token = jwt.encode({"slack_user_id": d["slack_user_id"],
                        "exp": datetime.now(timezone.utc) + timedelta(hours=12)},
                       settings.JWT_SECRET, algorithm="HS256")
    dest = {"me": "/me", "settings": "/settings", "dashboard": "/"}.get(d["target"], "/me")
    return RedirectResponse(url=f"{dest}?token={token}", status_code=302)


_SETTINGS_PAGE = pathlib.Path(__file__).parent / "work-settings.html"


# ── 개인 근무설정 (본인만) — 토큰의 slack_user_id로 식별 ──
def require_employee(authorization: str = Header(default="")) -> dict:
    token = authorization.replace("Bearer ", "")
    if not token:
        raise HTTPException(401, "로그인이 필요합니다.")
    import jwt
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
    except Exception:
        raise HTTPException(401, "유효하지 않은 토큰입니다.")
    emp = repo.try_employee_by_slack_id(payload.get("slack_user_id", ""))
    if not emp:
        raise HTTPException(403, "등록되지 않은 사용자입니다.")
    return emp


@api.get("/settings", response_class=HTMLResponse)
def settings_page():
    """개인 근무설정 웹페이지. ?token=<jwt> 로 본인 인증."""
    html = _SETTINGS_PAGE.read_text(encoding="utf-8")
    return html.replace("</head>", "<script>window.SAM_API=location.origin;</script></head>")


@api.get("/api/my/work-config")
def get_my_work_config(emp: dict = Depends(require_employee)):
    return {"employee": {"id": emp["id"], "name": emp["name"], "dept": emp["dept"]},
            "config": repo.work_config(emp["id"])}


@api.post("/api/my/work-config")
async def save_my_work_config(req: Request, emp: dict = Depends(require_employee)):
    body = await req.json()
    keys = ("work_type", "checkin", "checkout", "break_start", "break_end",
            "recovery", "short_rules")
    patch = {k: body[k] for k in keys if k in body}
    repo.save_work_config(emp["id"], patch)
    return {"ok": True}


# ── 직원 대시보드용 ────────────────────────────────────
_MY_PAGE = pathlib.Path(__file__).parent / "my.html"


@api.get("/me", response_class=HTMLResponse)
def my_page():
    html = _MY_PAGE.read_text(encoding="utf-8")
    return html.replace("</head>", "<script>window.SAM_API=location.origin;</script></head>")


@api.get("/api/my/summary")
def my_summary(emp: dict = Depends(require_employee)):
    """직원 헤더 정보(이름·부서·근무제도·역할)."""
    cfg = repo.work_config(emp["id"])
    return {"id": emp["id"], "name": emp["name"], "dept": emp["dept"],
            "role": emp["role"], "work_type": cfg.get("work_type", "normal")}


@api.get("/api/my/month")
def my_month(month: str, emp: dict = Depends(require_employee)):
    return repo.my_month(emp["id"], month)


@api.post("/api/my/attendance/edit")
async def edit_my_attendance(req: Request, emp: dict = Depends(require_employee)):
    """달력에서 특정 날짜의 출퇴근을 편집(30분 단위) + 자리비움 차감."""
    from datetime import date as _date
    body = await req.json()
    try:
        d = _date.fromisoformat(body.get("date"))
    except Exception:
        raise HTTPException(400, "날짜가 올바르지 않습니다.")
    kind = body.get("kind") or "office"
    if kind == "dayoff":     # 데이오프: 그날 근무 안 함
        repo.set_dayoff(emp["id"], d)
        return {"ok": True}
    checkin = (body.get("checkin") or "").strip()
    if not checkin:
        raise HTTPException(400, "출근 시간을 입력하세요.")
    # 휴일(일요일·공휴일) 근무는 승인이 있어야 근무시간 입력 가능
    from app.domain.holidays import needs_work_approval
    if needs_work_approval(d) and not repo.has_approved_holiday_work(emp["id"], d):
        raise HTTPException(400, "휴일 근무는 ‘시간외근무’ 탭에서 휴일근무 승인을 받은 뒤에 입력할 수 있습니다.")
    checkout = (body.get("checkout") or "").strip() or None
    if kind not in ("office", "remote", "field"):
        kind = "office"
    away = int(body.get("away_min") or 0)
    repo.record_manual(emp["id"], d, checkin, checkout, kind=kind, away_min=away)
    return {"ok": True}


@api.get("/api/my/overtime")
def my_overtime(emp: dict = Depends(require_employee)):
    return {"rows": repo.my_approvals(emp["id"])}


@api.post("/api/my/overtime")
async def create_my_overtime(req: Request, emp: dict = Depends(require_employee)):
    body = await req.json()
    kind = body.get("kind", "overtime")   # overtime | holiday
    if kind not in ("overtime", "holiday"):
        raise HTTPException(400, "kind는 overtime 또는 holiday")
    detail = body.get("detail", "")
    a = repo.create_approval(emp["id"], kind, {"detail": detail, **body})
    return {"ok": True, "id": a["id"]}


@api.post("/api/my/overtime/request-month")
async def request_month_overtime(req: Request, emp: dict = Depends(require_employee)):
    """근무시스템에서 계산된 월 연장근로를 승인 요청(선택적 근무: 월초·지난달분)."""
    body = await req.json()
    month = (body.get("month") or "").strip()
    if len(month) != 7:
        raise HTTPException(400, "month(YYYY-MM)가 필요합니다.")
    wt = repo.my_month(emp["id"], month).get("worktime", {})
    if not wt.get("can_request_ot"):
        raise HTTPException(400, wt.get("ot_window_note") or "지금은 연장근로를 신청할 수 없습니다.")
    if repo.has_month_overtime_request(emp["id"], month):
        raise HTTPException(400, "이미 해당 월 연장근로를 신청했습니다.")
    minutes = int(wt.get("pending_ot_min") or 0)
    if minutes <= 0:
        raise HTTPException(400, "신청할 연장근로가 없습니다.")
    hours = round(minutes / 60, 1)
    a = repo.create_approval(emp["id"], "overtime",
                             {"month": month, "minutes": minutes,
                              "detail": f"{month} 월 연장근로 {hours}시간"})
    return {"ok": True, "id": a["id"], "minutes": minutes, "hours": hours}


@api.post("/api/my/overtime/cancel")
async def cancel_my_overtime(req: Request, emp: dict = Depends(require_employee)):
    body = await req.json()
    ok = repo.cancel_approval(emp["id"], int(body.get("id")))
    if not ok:
        raise HTTPException(400, "취소할 수 없는 요청입니다(대기중 상태만 취소 가능).")
    return {"ok": True}


# ── 연차(무승인 즉시 반영) : 웹에서 신청/취소 ──────────
@api.get("/api/my/leave")
def my_leave_list(emp: dict = Depends(require_employee)):
    return {"rows": repo.my_leaves(emp["id"]), "balance": emp.get("leave_balance"),
            "balances": repo.leave_balances(emp["id"])}


@api.post("/api/my/leave")
async def create_my_leave(req: Request, emp: dict = Depends(require_employee)):
    """연차/오전반차/오후반차 신청 — 승인 없이 즉시 확정. 사유는 선택."""
    from datetime import date as _date
    from app.domain import leave as leave_engine
    body = await req.json()
    kind = body.get("kind", "annual")
    if kind not in leave_engine.LEAVE_LABEL:
        raise HTTPException(400, "알 수 없는 휴가 종류입니다.")
    try:
        start = _date.fromisoformat(body.get("start"))
    except Exception:
        raise HTTPException(400, "시작일이 올바르지 않습니다.")
    end_raw = body.get("end")
    end = _date.fromisoformat(end_raw) if end_raw else start
    if kind in leave_engine.HALF_DAY_KINDS:
        end = start   # 반일 종류는 하루
    if end < start:
        raise HTTPException(400, "종료일이 시작일보다 빠를 수 없습니다.")
    reason = (body.get("reason") or "").strip()   # 선택
    cfg = repo.work_config(emp["id"])
    # 부여 일수 초과 방지(일 단위 그룹만) — 안식휴가는 달력일 기준
    grp = repo._leave_group_of(kind)
    if grp:
        bals = {b["group"]: b for b in repo.leave_balances(emp["id"])}
        if grp in bals and "remaining" in bals[grp]:
            from app.domain.schedule import working_days as _wd
            lc = cfg.get("leave_config") or {}
            if kind.endswith("_am") or kind.endswith("_pm"):
                req_days = 0.5
            elif grp in repo._HOUR_LEAVE_GROUPS:   # 시간형 종일(설날·추석·생일8h·건강검진8h·BD)
                hrs = lc.get(grp, {}).get("hours", 4 if grp == "bd" else 8)
                req_days = round(hrs / 8.0, 2)
            elif grp in repo._CALENDAR_DAY_GROUPS:
                req_days = (end - start).days + 1
            else:
                req_days = _wd(cfg, start, end)
            rem = bals[grp]["remaining"]
            if req_days > rem + 1e-9:
                raise HTTPException(400, f"{bals[grp]['label']} 잔여 부족: 잔여 {rem}일, 필요 {req_days}일")
    days = leave_engine.deduct_days(cfg, kind, start, end)
    r = repo.create_leave(emp["id"], kind, start, end, days, reason=reason)
    # 구글 캘린더 동기화(설정된 경우에만) — 실패해도 신청은 유지
    try:
        from app.integrations import gcal
        gcal.sync_leave(r["id"])
    except Exception as e:
        print("gcal sync skipped:", e)
    return {"ok": True, "id": r["id"], "days": days, "balance_after": r.get("balance_after")}


@api.post("/api/my/leave/cancel")
async def cancel_my_leave(req: Request, emp: dict = Depends(require_employee)):
    body = await req.json()
    ok = repo.cancel_leave(emp["id"], int(body.get("id")))
    if not ok:
        raise HTTPException(400, "취소할 수 없는 연차입니다.")
    return {"ok": True}


# ── 개인 휴가 부여 설정(휴가 관리 탭) ──────────────────
@api.get("/api/my/leave-config")
def get_my_leave_config(emp: dict = Depends(require_employee)):
    return {"leave_config": repo.work_config(emp["id"]).get("leave_config", {})}


@api.post("/api/my/leave-config")
async def save_my_leave_config(req: Request, emp: dict = Depends(require_employee)):
    body = await req.json()
    cfg = body.get("leave_config")
    if not isinstance(cfg, dict):
        raise HTTPException(400, "leave_config는 객체여야 합니다.")
    repo.save_work_config(emp["id"], {"leave_config": cfg})
    return {"ok": True}


# ── 공휴일 조회(달력 표시용) ──────────────────────────
@api.get("/api/holidays")
def api_holidays(months: str = "", _: dict = Depends(require_employee)):
    """월별 공휴일 {ISO: 이름}. months=YYYY-MM,YYYY-MM (미지정 시 이번 달+다음 달)."""
    from datetime import date as _date
    from calendar import monthrange
    from app.config import today_kst
    from app.domain.holidays import public_holiday_name
    keys = [m.strip() for m in months.split(",") if m.strip()]
    if not keys:
        t = today_kst()
        ny, nm = (t.year + 1, 1) if t.month == 12 else (t.year, t.month + 1)
        keys = [f"{t.year:04d}-{t.month:02d}", f"{ny:04d}-{nm:02d}"]
    out = {}
    for k in keys:
        try:
            y, mm = int(k[:4]), int(k[5:7])
        except Exception:
            continue
        for day in range(1, monthrange(y, mm)[1] + 1):
            dd = _date(y, mm, day)
            nm2 = public_holiday_name(dd)
            if nm2:
                out[dd.isoformat()] = nm2
    return {"holidays": out}


# ── 공유 휴가 카탈로그(누구나 추가/각자 사용) ──────────
@api.get("/api/leave-types")
def api_leave_types(emp: dict = Depends(require_employee)):
    return {"types": repo.list_leave_types(emp["id"])}


@api.post("/api/leave-types")
async def api_add_leave_type(req: Request, emp: dict = Depends(require_employee)):
    body = await req.json()
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "휴가 이름을 입력하세요.")
    unit = body.get("unit") if body.get("unit") in ("day", "hour") else "day"
    hours = int(body.get("hours") or 8)
    deduct = bool(body.get("deduct"))
    t = repo.add_leave_type(name, unit, hours, deduct, created_by=emp["id"])
    return {"ok": True, "type": t, "types": repo.list_leave_types(emp["id"])}


@api.post("/api/leave-types/delete")
async def api_del_leave_type(req: Request, emp: dict = Depends(require_employee)):
    body = await req.json()
    key = (body.get("key") or "").strip()
    if not repo.delete_leave_type(key, requester_id=emp["id"]):
        raise HTTPException(403, "삭제 권한이 없거나 존재하지 않는 항목입니다.")
    return {"ok": True, "types": repo.list_leave_types(emp["id"])}


@api.get("/setup")
def setup(key: str = ""):
    """일회성 초기 설정 — DB 테이블 생성 + 샘플 데이터 + 관리자 토큰 발급.
    보안: JWT_SECRET을 key로 요구하고, 이미 데이터가 있으면 다시 넣지 않는다.
    실제 운영 데이터가 쌓이기 시작하면 이 엔드포인트는 재시드하지 않는다."""
    if key != settings.JWT_SECRET:
        raise HTTPException(403, "잘못된 key 입니다.")
    import jwt
    from app import seed
    seeded = seed.seed_demo()
    token = jwt.encode({"slack_user_id": "U3001"}, settings.JWT_SECRET, algorithm="HS256")
    return {"seeded": seeded, "token": token,
            "다음": "위 token 값을 복사해 대시보드 상단 토큰 칸에 붙여넣고 '불러오기'를 누르세요."}


# ── 인증: 관리자(인사담당자/시스템관리자)만 통계 접근 ──
def require_admin(authorization: str = Header(default="")) -> dict:
    """JWT 검증. 토큰의 slack_user_id로 역할을 확인해 hr/sysadmin만 허용."""
    token = authorization.replace("Bearer ", "")
    if not token:
        raise HTTPException(401, "로그인이 필요합니다.")
    import jwt
    try:
        payload = jwt.decode(token, settings.JWT_SECRET, algorithms=["HS256"])
    except Exception:
        raise HTTPException(401, "유효하지 않은 토큰입니다.")
    uid = payload.get("slack_user_id", "")
    # 창현(슈퍼유저)은 항상 최상위 권한으로 통과 — 테스트·운영용 예외
    if uid in repo.SUPERUSER_SLACK_IDS:
        return {"role": "sysadmin", "slack_user_id": uid}
    role = repo.role_of(uid)
    if role not in ("hr", "sysadmin"):
        raise HTTPException(403, "통계 접근 권한이 없습니다.")
    return {"role": role, "slack_user_id": uid}


# ── 통계 엔드포인트 (웹 대시보드가 호출) ───────────────
@api.get("/api/stats/overview")
def stats_overview(_: dict = Depends(require_admin)):
    return repo.stats_overview()


@api.get("/api/leave-stats")
def leave_stats(_: dict = Depends(require_admin)):
    """전체 구성원 휴가 부여/사용/잔여(관리자)."""
    return repo.all_leave_balances()


@api.get("/api/stats/leave-types")
def stats_leave_types(year: int = 0, _: dict = Depends(require_admin)):
    """연 휴가유형별 통계(전체)."""
    from datetime import date as _d
    return repo.stats_leave_types(year or _d.today().year)


@api.get("/api/stats/monthly")
def stats_monthly(month: str, _: dict = Depends(require_admin)):
    return repo.stats_monthly(month)


@api.get("/api/stats/by-employee")
def stats_by_employee(month: str, _: dict = Depends(require_admin)):
    return repo.stats_by_employee(month)


@api.get("/api/live")
def live_status(status: str | None = None, dept: str | None = None,
               _: dict = Depends(require_admin)):
    return repo.live_status(status, dept)


@api.get("/api/approvals/pending")
def pending_approvals(_: dict = Depends(require_admin)):
    return repo.pending_approvals()


@api.post("/api/approvals/decide")
async def decide_approval(req: Request, admin: dict = Depends(require_admin)):
    """연장·휴일근무 승인/반려 — 승인권은 사무장(또는 지회장)."""
    body = await req.json()
    actor = admin.get("slack_user_id", "")
    if not repo.is_approver(actor):
        raise HTTPException(403, "승인 권한은 사무장에게 있습니다.")
    decision = body.get("decision")
    if decision not in ("approve", "reject"):
        raise HTTPException(400, "decision은 approve 또는 reject")
    status = "approved" if decision == "approve" else "rejected"
    repo.update_approval(int(body.get("id")), status, actor)
    return {"ok": True, "status": status}


# ── 구성원/권한 관리 (대시보드에서 관리자 지정) ──────────
@api.get("/api/employees")
def api_employees(admin: dict = Depends(require_admin)):
    return {"rows": repo.list_all_employees(), "me": admin.get("slack_user_id")}


@api.post("/api/employees/role")
async def api_set_role(req: Request, admin: dict = Depends(require_admin)):
    body = await req.json()
    target, role = body.get("employee_id"), body.get("role")
    actor = admin.get("slack_user_id")
    try:
        repo.assign_role(actor, target, role)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except (ValueError, LookupError) as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "employee_id": target, "role": role}


def _invite_dm(slack: str, name: str) -> bool:
    """새 구성원에게 Slack 초대(환영) DM 발송. 성공 True."""
    try:
        from app.slack.app import app as slack_app
        from app.links import make_code
        link = f"{settings.WEB_BASE_URL}/go/{make_code(slack, 'me')}"
        slack_app.client.chat_postMessage(channel=slack, text=(
            f":wave: *{name}님, 크루유니언 근태봇에 등록되었습니다!*\n"
            f"이제 이 봇의 *홈* 탭에서 출근·퇴근·재택·연차를 기록할 수 있어요.\n"
            f"• 출근 `/attend in`   • 퇴근 `/attend out`\n"
            f"• 내 근태 보기: {link}"))
        return True
    except Exception as e:  # 미설치·잘못된 ID 등
        print("invite DM failed:", e)
        return False


def _resolve_slack_id(value: str) -> str:
    """이메일을 입력하면 Slack users.lookupByEmail로 실제 멤버 ID(U...)를 찾아 반환.
    이미 ID면 그대로. 조회 실패 시 원본 반환(필요 권한: users:read.email)."""
    v = (value or "").strip()
    if "@" not in v:
        return v
    try:
        from app.slack.app import app as slack_app
        r = slack_app.client.users_lookupByEmail(email=v)
        uid = (r.get("user") or {}).get("id")
        return uid or v
    except Exception as e:
        print("email->id lookup failed:", e)
        return v


def _slack_profile(slack: str) -> dict:
    """Slack users.info로 Full name(real_name)·Display name(display_name) 조회."""
    try:
        from app.slack.app import app as slack_app
        r = slack_app.client.users_info(user=slack)
        p = (r.get("user") or {}).get("profile", {}) or {}
        full = p.get("real_name") or r["user"].get("real_name") or ""
        disp = p.get("display_name") or ""
        return {"full": full.strip(), "display": disp.strip()}
    except Exception as e:
        print("slack profile lookup failed:", e)
        return {"full": "", "display": ""}


@api.post("/api/employees/add")
async def api_add_member(req: Request, admin: dict = Depends(require_admin)):
    """구성원 추가(Slack ID 기준) + 초대 DM 발송.
    이름 미입력 시 Slack Full name으로 등록, Display name도 함께 저장."""
    if admin.get("role") not in ("hr", "sysadmin"):
        raise HTTPException(403, "구성원 추가는 사무장·지회장만 가능합니다.")
    body = await req.json()
    slack_in = (body.get("slack") or "").strip()
    if not slack_in:
        raise HTTPException(400, "Slack ID 또는 이메일은 필수입니다.")
    slack = _resolve_slack_id(slack_in)   # 이메일이면 실제 멤버 ID(U...)로 변환
    prof = _slack_profile(slack)
    # 이름: 입력값 > Slack Full name > Display name > (그래도 없으면 Slack ID)
    # → 이름을 비워도 추가가 실패하지 않도록 항상 대체값을 채운다.
    name = (body.get("name") or "").strip() or prof["full"] or prof["display"] or slack
    display = (body.get("display_name") or "").strip() or prof["display"] or ""
    emp_id = (body.get("id") or slack).strip()
    dept = (body.get("dept") or "미지정").strip()
    position = body.get("position") or "전임 스탭"
    company = (body.get("company") or "").strip()
    repo.upsert_employee(emp_id, slack, name, dept, position, display_name=display, company=company)
    invited = _invite_dm(slack, display or name) if body.get("invite", True) else False
    return {"ok": True, "id": emp_id, "name": name, "display_name": display, "invited": invited}


@api.post("/api/off-announce/test")
def off_announce_test(_: dict = Depends(require_admin)):
    """오늘 실제 휴무자(연차·놀금·데이오프)를 집계해 채널에 즉시 발송(테스트)."""
    from app.scheduler.tasks import run_send_daily_off_announcement
    people = repo.today_off_people()
    sent = run_send_daily_off_announcement()   # people 없으면 발송 안 함
    return {"sent": sent, "channel": settings.OFF_ANNOUNCE_CHANNEL,
            "people": people}


@api.get("/api/companies")
def list_companies(_: dict = Depends(require_admin)):
    return {"companies": repo.list_companies()}


@api.post("/api/companies")
async def add_company(req: Request, admin: dict = Depends(require_admin)):
    if admin.get("role") not in ("hr", "sysadmin"):
        raise HTTPException(403, "권한이 없습니다.")
    body = await req.json()
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "법인명을 입력하세요.")
    repo.add_company(name)
    return {"ok": True, "companies": repo.list_companies()}


@api.post("/api/employees/update")
async def api_update_member(req: Request, admin: dict = Depends(require_admin)):
    """구성원 부서·이름·표시이름 수정(추가 이후에도 변경 가능)."""
    if admin.get("role") not in ("hr", "sysadmin"):
        raise HTTPException(403, "권한이 없습니다.")
    body = await req.json()
    eid = (body.get("employee_id") or "").strip()
    if not eid:
        raise HTTPException(400, "employee_id는 필수입니다.")
    try:
        repo.update_employee_fields(eid, dept=body.get("dept"),
                                    name=body.get("name"),
                                    display_name=body.get("display_name"),
                                    company=body.get("company"))
    except LookupError as e:
        raise HTTPException(404, str(e))
    return {"ok": True}


@api.post("/api/employees/refresh-slack")
async def api_refresh_slack(admin: dict = Depends(require_admin)):
    """모든 구성원의 Slack Full name·Display name을 다시 가져와 채운다(백필)."""
    if admin.get("role") not in ("hr", "sysadmin"):
        raise HTTPException(403, "권한이 없습니다.")
    updated = 0
    fixed_ids = 0
    skipped = []
    for e in repo.list_all_employees():
        cur = e["slack"]
        real = _resolve_slack_id(cur)          # 이메일로 저장돼 있으면 실제 ID로
        prof = _slack_profile(real)
        new_sid = real if real != cur else None
        if prof["full"] or prof["display"] or new_sid:
            try:
                repo.update_employee_fields(
                    e["id"], name=prof["full"] or None,
                    display_name=prof["display"] or None,
                    slack_user_id=new_sid)
                updated += 1
                if new_sid:
                    fixed_ids += 1
            except ValueError:   # 중복 Slack ID(예: 잘못 추가된 중복 구성원)
                skipped.append(e["name"])
    return {"ok": True, "updated": updated, "fixed_ids": fixed_ids, "skipped": skipped}


@api.post("/api/employees/invite")
async def api_invite_member(req: Request, admin: dict = Depends(require_admin)):
    """기존 구성원에게 초대 DM 재발송."""
    if admin.get("role") not in ("hr", "sysadmin"):
        raise HTTPException(403, "권한이 없습니다.")
    body = await req.json()
    emp = repo.try_employee_by_slack_id(body.get("slack", ""))
    if not emp:
        raise HTTPException(404, "구성원을 찾을 수 없습니다.")
    ok = _invite_dm(emp["slack_user_id"], emp["name"])
    if not ok:
        raise HTTPException(400, "DM 발송 실패 — Slack ID가 맞는지, 봇이 설치됐는지 확인하세요.")
    return {"ok": True}


@api.post("/api/employees/delete")
async def api_delete_member(req: Request, admin: dict = Depends(require_admin)):
    if admin.get("role") != "sysadmin":   # 삭제는 지회장(시스템관리자)만
        raise HTTPException(403, "구성원 삭제는 지회장만 가능합니다.")
    body = await req.json()
    repo.delete_employee(body.get("employee_id"))
    return {"ok": True}


@api.get("/api/positions")
def api_positions(_: dict = Depends(require_admin)):
    """선택 가능한 직책 목록 + 각 직책의 권한 등급."""
    return {"positions": list(repo.POSITIONS),
            "role_map": repo.POSITION_ROLE}


@api.post("/api/employees/position")
async def api_set_position(req: Request, admin: dict = Depends(require_admin)):
    body = await req.json()
    target, position = body.get("employee_id"), body.get("position")
    actor = admin.get("slack_user_id")
    try:
        repo.assign_position(actor, target, position)
    except PermissionError as e:
        raise HTTPException(403, str(e))
    except (ValueError, LookupError) as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "employee_id": target, "position": position}


# ── 직원 등록(간단) — key(JWT_SECRET) 보호. Slack user_id 매핑 ──
@api.get("/admin/add-employee")
def admin_add_employee(key: str = "", id: str = "", slack: str = "",
                       name: str = "", dept: str = "미지정", role: str = "employee"):
    """예: /admin/add-employee?key=시크릿&id=K-1001&slack=U02XXXX&name=창현&dept=경영지원&role=sysadmin"""
    if key != settings.JWT_SECRET:
        raise HTTPException(403, "잘못된 key")
    if not (id and slack and name):
        raise HTTPException(400, "id, slack, name 은 필수입니다.")
    from datetime import date
    from app.db import session_scope, init_db
    from app.models import Employee, WorkConfig
    init_db()
    with session_scope() as s:
        s.merge(Employee(id=id, slack_user_id=slack, name=name, dept=dept,
                         team_id="T1", hire_date=date.today(), role=role))
        if not s.get(WorkConfig, id):
            s.add(WorkConfig(employee_id=id, work_type="normal",
                             checkin="09:00", checkout="18:00",
                             break_start="12:00", break_end="13:00",
                             recovery={"mode": "none"}, short_rules=[]))
    return {"ok": True, "employee": {"id": id, "slack": slack, "name": name, "role": role}}


# ── 직원 삭제(관련 기록 포함) — key 보호 ──
@api.get("/admin/delete-employee")
def admin_delete_employee(key: str = "", id: str = ""):
    if key != settings.JWT_SECRET:
        raise HTTPException(403, "잘못된 key")
    repo.delete_employee(id)
    return {"ok": True, "deleted": id}


# ── Slack 이벤트 수신 (HTTP 모드) — 토큰이 설정된 경우에만 활성화 ──
if settings.SLACK_BOT_TOKEN and settings.SLACK_SIGNING_SECRET:
    from slack_bolt.adapter.fastapi import SlackRequestHandler
    from app.slack.app import app as slack_app

    _slack_handler = SlackRequestHandler(slack_app)

    @api.post("/slack/events")
    async def slack_events(req: Request):
        return await _slack_handler.handle(req)


# ── Cron: 예약 출퇴근 알림 스캔 (Celery beat 대체) ─────
@api.get("/cron/checkin-scan")
def cron_checkin_scan(authorization: str = Header(default="")):
    """Vercel Cron이 주기적으로 호출(GET). Authorization: Bearer <CRON_SECRET>로 보호.
    CRON_SECRET 미설정 시 JWT_SECRET을 사용."""
    expected = os.getenv("CRON_SECRET", settings.JWT_SECRET)
    if authorization != f"Bearer {expected}":
        raise HTTPException(403, "forbidden")
    from app.scheduler.tasks import (run_send_due_checkin_prompts,
                                     run_send_due_checkout_prompts,
                                     run_apply_auto_dayoffs)
    dayoffs = run_apply_auto_dayoffs()   # 소정 충족자 자동 데이오프 + DM
    return {"checkin_sent": run_send_due_checkin_prompts(),
            "checkout_sent": run_send_due_checkout_prompts(),
            "auto_dayoffs": dayoffs}


# ── Cron: 매일 09시(KST) 휴무자 요약 발송 ─────────────
@api.get("/cron/off-announce")
def cron_off_announce(authorization: str = Header(default="")):
    """Vercel Cron이 매일 00:00 UTC(=09:00 KST)에 호출. 그날 휴무자를 채널에 요약 발송."""
    expected = os.getenv("CRON_SECRET", settings.JWT_SECRET)
    if authorization != f"Bearer {expected}":
        raise HTTPException(403, "forbidden")
    from app.scheduler.tasks import run_send_daily_off_announcement
    return {"ok": True, "off_count": run_send_daily_off_announcement()}


# ── Cron: 연장근로 신청 안내 (시차=다음날 · 선택적=월초) ─
@api.get("/cron/overtime-notify")
def cron_overtime_notify(authorization: str = Header(default="")):
    expected = os.getenv("CRON_SECRET", settings.JWT_SECRET)
    if authorization != f"Bearer {expected}":
        raise HTTPException(403, "forbidden")
    from app.config import today_kst
    from app.links import make_code
    today = today_kst()
    sent = 0
    for n in repo.pending_overtime_notifications(today):
        rkey = f"ot_{n['kind']}_{n['ref']}"
        if not repo.mark_reminder_once(n["emp_id"], today, rkey):
            continue  # 이미 안내함
        try:
            from app.slack.app import app as slack_app
            link = f"{settings.WEB_BASE_URL}/go/{make_code(n['slack'], 'me')}"
            if n["kind"] == "flex":
                txt = (f":stopwatch: 어제({n['ref']}) 연장근로 *{n['hours']}시간* 발생 — "
                       f"오늘 신청해 주세요.\n• 신청: {link} → 시간외근무 탭")
            else:
                txt = (f":stopwatch: 지난달({n['ref']}) 연장근로 *{n['hours']}시간* 발생 — "
                       f"이번 주 안에 신청해 주세요.\n• 신청: {link} → 시간외근무 탭")
            slack_app.client.chat_postMessage(channel=n["slack"], text=txt)
            sent += 1
        except Exception as ex:
            print("overtime notify failed:", ex)
    return {"ok": True, "sent": sent}
