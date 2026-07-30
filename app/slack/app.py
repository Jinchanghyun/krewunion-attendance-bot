"""Slack Bolt 앱 — 홈 탭·출퇴근·연차·설정·승인 이벤트 라우팅.

핵심 원칙:
- 인터랙션은 ack()를 가장 먼저 호출(3초 룰).
- 모달을 열려면 trigger_id 필요 → 슬래시 명령/버튼에서만 발급됨.
- 무거운 작업(캘린더 등)은 Celery/큐로 위임.
"""
from __future__ import annotations

from datetime import datetime

from slack_bolt import App

from app.config import settings
from app import repo
from app.domain import leave as leave_engine
from app.slack import views

# process_before_response=True: 서버리스(Vercel)에서 응답 전에 처리를 끝내야 하므로 필수
# token_verification_enabled=False: 앱 초기화 때 Slack auth_test 네트워크 호출을 하지 않음
#   (이게 실패하면 앱 임포트 자체가 깨져 사이트 전 경로가 404가 되므로 반드시 끔)
app = App(token=settings.SLACK_BOT_TOKEN, signing_secret=settings.SLACK_SIGNING_SECRET,
          process_before_response=True, token_verification_enabled=False)


# ── 접근 통제: 등록된 임직원 + (선택)화이트리스트만 ────
def _allowed(uid: str) -> bool:
    return (not settings.SLACK_ALLOWLIST) or (uid in settings.SLACK_ALLOWLIST)


def _resolve(uid: str) -> dict | None:
    """미등록/미허용이면 None."""
    return repo.try_employee_by_slack_id(uid) if _allowed(uid) else None


def _guard(client, uid: str) -> dict | None:
    """등록/허용된 직원이면 dict, 아니면 미등록 홈을 띄우고 None."""
    emp = _resolve(uid)
    if emp is None:
        client.views_publish(user_id=uid, view=views.unregistered_home())
    return emp


# ── 홈 탭 ─────────────────────────────────────────────
@app.event("app_home_opened")
def render_home(event, client):
    emp = _guard(client, event["user"])
    if not emp:
        return
    client.views_publish(user_id=event["user"],
                         view=views.home_view(emp, repo.today_state(emp["id"])))


# ── 출근/재택/외근/퇴근 ───────────────────────────────
def _refresh_home(client, emp):
    client.views_publish(user_id=emp["slack_user_id"],
                         view=views.home_view(emp, repo.today_state(emp["id"])))


def _do_checkin(client, emp, kind):
    if repo.is_on_full_leave(emp["id"]):   # 연차 등 종일 휴가면 출근 불가
        lk = repo.today_leave_kind(emp["id"])
        client.chat_postMessage(channel=emp["slack_user_id"],
            text=f":palm_tree: 오늘은 *{leave_engine.LEAVE_LABEL.get(lk, '휴가')}*로 등록돼 있어 출근 기록을 할 수 없습니다.")
        return
    info = repo.record_checkin(emp["id"], kind, datetime.now())
    client.chat_postMessage(channel=emp["slack_user_id"], text=views.checkin_confirm(info))
    _refresh_home(client, emp)


def _do_checkout(client, emp):
    summary = repo.record_checkout(emp["id"], datetime.now())
    if summary:
        client.chat_postMessage(channel=emp["slack_user_id"], text=views.checkout_confirm(summary))
    _refresh_home(client, emp)


@app.action("checkin")
def on_checkin(ack, body, client):
    ack()
    emp = _guard(client, body["user"]["id"])
    if emp:
        _do_checkin(client, emp, "office")


@app.action("remote")
def on_remote(ack, body, client):
    ack()
    emp = _guard(client, body["user"]["id"])
    if emp:
        _do_checkin(client, emp, "remote")


@app.action("field")
def on_field(ack, body, client):
    ack()
    emp = _guard(client, body["user"]["id"])
    if emp:
        _do_checkin(client, emp, "field")


@app.action("checkout")
def on_checkout(ack, body, client):
    ack()
    emp = _guard(client, body["user"]["id"])
    if emp:
        _do_checkout(client, emp)


# ── 연차: "연차" 치면 등록 화면 (두 경로) ─────────────
@app.command("/leave")   # Slack은 한글 슬래시 명령 불가 → /leave (키워드 "연차"는 아래 메시지 핸들러)
def open_leave_command(ack, body, client, respond):
    ack()  # 슬래시 명령은 trigger_id를 주므로 모달 즉시 오픈
    emp = _resolve(body["user_id"])
    if not emp:
        respond(views.UNREGISTERED_MSG)
        return
    client.views_open(trigger_id=body["trigger_id"],
                      view=views.leave_modal(repo.work_config(emp["id"]).get("leave_config")))


_STATUS_KO = {"work": "근무중", "remote": "재택", "field": "외근", "off": "휴가", "none": "미출근"}


@app.command("/attend")
def attend_command(ack, body, client, respond):
    ack()
    emp = _resolve(body["user_id"])
    if not emp:
        respond(views.UNREGISTERED_MSG)
        return
    parts = (body.get("text") or "").strip().split()
    sub = parts[0].lower() if parts else "help"
    now = datetime.now()
    if sub == "in":
        respond(views.checkin_confirm(repo.record_checkin(emp["id"], "office", now)))
    elif sub == "out":
        _s = repo.record_checkout(emp["id"], now)
        respond(views.checkout_confirm(_s) if _s else "출근 기록이 없어 퇴근할 수 없습니다.")
    elif sub in ("remote", "home", "homein"):
        respond(views.checkin_confirm(repo.record_checkin(emp["id"], "remote", now)))
    elif sub == "field":
        respond(views.checkin_confirm(repo.record_checkin(emp["id"], "field", now)))
    elif sub == "status":
        st = repo.today_state(emp["id"])
        respond(f"오늘 상태: *{_STATUS_KO.get(st['status'], st['status'])}* · 근무 {st['worked']}")
    elif sub == "team":
        rows = repo.team_status()
        lines = "\n".join(f"• {r['name']} ({r['dept']}) — {_STATUS_KO.get(r['status'], r['status'])}"
                          for r in rows)
        respond("*팀 현황*\n" + (lines or "표시할 팀원이 없습니다."))
    elif sub in ("leave", "vacation"):
        client.views_open(trigger_id=body["trigger_id"],
                          view=views.leave_modal(repo.work_config(emp["id"]).get("leave_config")))
    elif sub in ("miss", "missout"):
        from datetime import date as _date
        try:
            d = _date.fromisoformat(parts[1])
            if sub == "miss":
                repo.record_manual(emp["id"], d, parts[2], parts[3])
            else:
                repo.record_manual(emp["id"], d, "09:00", parts[2])
            respond(":white_check_mark: 누락 근태를 등록했습니다.")
        except Exception:
            respond("형식: `/attend miss 2026-08-01 09:00 18:00` 또는 `/attend missout 2026-08-01 18:00`")
    else:
        respond(views.command_help())


@app.action("open_leave")            # 홈 탭 '연차 신청' 버튼
def open_leave_button(ack, body, client):
    ack()
    emp = _resolve(body["user"]["id"])
    if not emp:
        return
    client.views_open(trigger_id=body["trigger_id"],
                      view=views.leave_modal(repo.work_config(emp["id"]).get("leave_config")))


@app.message("연차")                  # 키워드 → 버튼 답장(텍스트만으론 모달 불가)
def suggest_leave(message, say):
    if not _resolve(message["user"]):
        return
    say(blocks=[
        {"type": "section", "text": {"type": "mrkdwn", "text": "연차를 신청하시겠어요?"}},
        {"type": "actions", "elements": [
            {"type": "button", "text": {"type": "plain_text", "text": "연차 신청하기"},
             "action_id": "open_leave", "style": "primary"}]}])


@app.view("leave_modal")
def submit_leave(ack, body, view, client):
    ack()
    emp = _guard(client, body["user"]["id"])
    if not emp:
        return
    vals = view["state"]["values"]
    kind = vals["kind"]["v"]["selected_option"]["value"]
    start = datetime.fromisoformat(vals["start"]["v"]["selected_date"]).date()
    end_raw = vals.get("end", {}).get("v", {}).get("selected_date")
    end = datetime.fromisoformat(end_raw).date() if end_raw else start
    if kind in leave_engine.HALF_DAY_KINDS:
        end = start
    reason = (vals.get("reason", {}).get("v", {}).get("value") or "").strip()

    cfg = repo.work_config(emp["id"])
    days = leave_engine.deduct_days(cfg, kind, start, end)
    req = repo.create_leave(emp["id"], kind, start, end, days, reason=reason)

    try:  # 연차만 구글 캘린더 동기화(설정된 경우) — 실패해도 신청은 유지
        from app.integrations import gcal
        gcal.sync_leave(req["id"])
    except Exception as e:
        print("gcal sync skipped:", e)
    tail = f"{days}일 차감" if days else "잔여 미차감"
    client.chat_postMessage(channel=emp["slack_user_id"],
        text=f":white_check_mark: {leave_engine.LEAVE_LABEL[kind]} 신청 완료 · {tail}")


# ── 근무 설정(간단 항목) ──────────────────────────────
@app.action("open_settings")
def open_settings(ack, body, client):
    ack()
    emp = _guard(client, body["user"]["id"])
    if not emp:
        return
    client.views_open(trigger_id=body["trigger_id"],
                      view=views.settings_modal(repo.work_config(emp["id"])))


@app.view("settings_modal")
def submit_settings(ack, body, view, client):
    ack()
    emp = _guard(client, body["user"]["id"])
    if not emp:
        return
    vals = view["state"]["values"]
    patch = {
        "work_type": vals["work_type"]["v"]["selected_option"]["value"],
        "checkin": vals["checkin"]["v"]["selected_time"],
        "checkout": vals["checkout"]["v"]["selected_time"],
    }
    repo.save_work_config(emp["id"], patch)
    _refresh_home(client, emp)


# ── 팀장 승인/반려 ────────────────────────────────────
@app.action("approve")
def on_approve(ack, body, client):
    _decide(ack, body, client, "approve")


@app.action("reject")
def on_reject(ack, body, client):
    _decide(ack, body, client, "reject")


def _decide(ack, body, client, decision):
    ack()
    from app.domain import approval
    approval_id = int(body["actions"][0]["value"])
    approver = body["user"]["id"]
    req = repo.get_approval(approval_id)
    if not repo.is_manager_of(approver, req["employee_id"]):
        return  # 권한 없음
    new_state = approval.decide(req["status"], decision, actor_is_manager=True)
    req = repo.update_approval(approval_id, new_state, approver)
    client.chat_update(channel=body["channel"]["id"], ts=body["message"]["ts"],
                       blocks=views.approval_result(req))
