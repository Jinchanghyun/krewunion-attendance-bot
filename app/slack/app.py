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

app = App(token=settings.SLACK_BOT_TOKEN, signing_secret=settings.SLACK_SIGNING_SECRET)


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


@app.action("checkin")
def on_checkin(ack, body, client):
    ack()
    emp = _guard(client, body["user"]["id"])
    if not emp:
        return
    repo.record_checkin(emp["id"], "office", datetime.now())
    _refresh_home(client, emp)


@app.action("remote")
def on_remote(ack, body, client):
    ack()
    emp = _guard(client, body["user"]["id"])
    if not emp:
        return
    repo.record_checkin(emp["id"], "remote", datetime.now())
    _refresh_home(client, emp)


@app.action("field")
def on_field(ack, body, client):
    ack()
    emp = _guard(client, body["user"]["id"])
    if not emp:
        return
    repo.record_checkin(emp["id"], "field", datetime.now())
    _refresh_home(client, emp)


@app.action("checkout")
def on_checkout(ack, body, client):
    ack()
    emp = _guard(client, body["user"]["id"])
    if not emp:
        return
    repo.record_checkout(emp["id"], datetime.now())
    _refresh_home(client, emp)


# ── 연차: "연차" 치면 등록 화면 (두 경로) ─────────────
@app.command("/연차")
def open_leave_command(ack, body, client, respond):
    ack()  # 슬래시 명령은 trigger_id를 주므로 모달 즉시 오픈
    if not _resolve(body["user_id"]):
        respond(views.UNREGISTERED_MSG)
        return
    client.views_open(trigger_id=body["trigger_id"], view=views.leave_modal())


@app.action("open_leave")            # 홈 탭 '연차 신청' 버튼
def open_leave_button(ack, body, client):
    ack()
    if not _resolve(body["user"]["id"]):
        return
    client.views_open(trigger_id=body["trigger_id"], view=views.leave_modal())


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

    cfg = repo.work_config(emp["id"])
    days = leave_engine.deduct_days(cfg, kind, start, end)
    req = repo.create_leave(emp["id"], kind, start, end, days)

    from app.scheduler import tasks  # 지연 임포트(순환 방지)
    tasks.sync_leave_calendar.delay(req["id"])  # 구글 캘린더 비동기 등록
    client.chat_postMessage(channel=emp["slack_user_id"],
        text=f":white_check_mark: {leave_engine.LEAVE_LABEL[kind]} 신청 완료 · {days}일 차감")


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
