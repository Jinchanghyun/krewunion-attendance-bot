"""Block Kit 뷰 빌더 — 홈 탭, 예약 알림, 연차/설정 모달, 승인 메시지."""
from __future__ import annotations


def _button(text, action_id, value="", style=None):
    b = {"type": "button", "text": {"type": "plain_text", "text": text}, "action_id": action_id}
    if value:
        b["value"] = value
    if style:
        b["style"] = style
    return b


def _link_button(text, url, style=None):
    """url 속성이 있으면 클릭 시 브라우저로 해당 주소를 연다(백엔드 이벤트 없음)."""
    b = {"type": "button", "text": {"type": "plain_text", "text": text},
         "url": url, "action_id": "open_web"}
    if style:
        b["style"] = style
    return b


def _magic_token(slack_user_id: str, hours: int = 12) -> str:
    import jwt
    from datetime import datetime, timedelta, timezone
    from app.config import settings
    return jwt.encode({"slack_user_id": slack_user_id,
                       "exp": datetime.now(timezone.utc) + timedelta(hours=hours)},
                      settings.JWT_SECRET, algorithm="HS256")


def settings_link(emp: dict) -> str:
    """개인 근무설정 웹페이지 매직 링크 (모든 직원)."""
    from app.config import settings
    return f"{settings.WEB_BASE_URL}/settings?token={_magic_token(emp['slack_user_id'])}"


def my_link(emp: dict) -> str:
    """직원 대시보드(내 근태) 매직 링크."""
    from app.config import settings
    return f"{settings.WEB_BASE_URL}/me?token={_magic_token(emp['slack_user_id'])}"


def dashboard_link(emp: dict) -> str | None:
    """관리자(hr/sysadmin)에게만 대시보드 매직 링크 반환. 토큰을 담아 자동 로그인."""
    if emp.get("role") not in ("hr", "sysadmin"):
        return None
    from app.config import settings
    return f"{settings.WEB_BASE_URL}/?token={_magic_token(emp['slack_user_id'])}"


def home_view(emp: dict, state: dict) -> dict:
    """홈 탭. state: {"status": "none|work|remote|field|off", "worked": "2시간 10분"}"""
    status_label = {"none": "미출근", "work": "근무중", "remote": "재택근무 중",
                    "field": "외근 중", "off": "휴가"}.get(state["status"], "미출근")
    worked = state.get("worked", "-")
    head = f"*안녕하세요, {emp['name']}님* :wave:\n오늘 상태: *{status_label}*"
    if state["status"] in ("work", "remote", "field") and worked and worked != "-":
        head += f"  ·  근무 {worked}"

    def group(title, cmd, elements):
        return [
            {"type": "section", "text": {"type": "mrkdwn", "text": f"{title}   `{cmd}`"}},
            {"type": "actions", "elements": elements},
        ]

    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": head}},
              {"type": "divider"}]

    # 출퇴근
    blocks += group(":clock9: *출퇴근*", "/attend in · /attend out", [
        _button("출근", "checkin", style="primary"),
        _button("퇴근", "checkout"),
    ])
    # 재택근무
    blocks += group(":house: *재택근무*", "/attend remote", [
        _button("재택근무로 출근", "remote"),
    ])
    # 외근
    blocks += group(":round_pushpin: *외근*", "/attend field", [
        _button("외근으로 전환", "field"),
    ])
    blocks.append({"type": "divider"})

    # 근태 신청
    blocks += group(":memo: *근태 신청*", "/attend leave", [
        _button("연차·근태 신청", "open_leave"),
    ])
    # 내 정보
    blocks += group(":bar_chart: *내 정보*", "/attend status · /attend team", [
        _link_button("내 근무 현황", my_link(emp)),
        _link_button("근무 설정", settings_link(emp)),
    ])

    dlink = dashboard_link(emp)
    if dlink:   # 관리자(hr/sysadmin)에게만
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": ":gear: *관리자*"}})
        blocks.append({"type": "actions", "elements": [
            _link_button("통계", dlink),
            _link_button("대시보드", dlink, style="primary"),
        ]})

    blocks.append({"type": "divider"})
    blocks.append({"type": "context", "elements": [{"type": "mrkdwn",
        "text": "누락 등록: `/attend miss 2026-08-01 09:00 18:00`  ·  전체 명령어: `/attend`"}]})
    return {"type": "home", "blocks": blocks}


def unregistered_home() -> dict:
    """미등록/미허용 사용자에게 보여줄 홈 탭."""
    return {"type": "home", "blocks": [
        {"type": "section", "text": {"type": "mrkdwn",
            "text": ":lock: *크루유니언 근태봇*\n\n아직 근태 사용 권한이 없습니다. "
                    "이 앱은 등록된 임직원만 사용할 수 있어요."}},
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": "등록이 필요하면 인사담당자에게 문의해 주세요."}]},
    ]}


UNREGISTERED_MSG = "이 앱은 등록된 임직원만 사용할 수 있어요. 인사담당자에게 등록을 문의해 주세요."

COMMAND_HELP = (
    "*명령어* (메시지 입력창에 그대로 입력)\n"
    "• 출근 `/attend in`   • 퇴근 `/attend out`\n"
    "• 재택 출근 `/attend remote`   • 외근 `/attend field`\n"
    "• 내 상태 `/attend status`   • 팀 현황 `/attend team`\n"
    "• 연차 신청 `/attend leave`\n"
    "• 누락 등록 `/attend miss 2026-08-01 09:00 18:00`\n"
    "• 퇴근 누락 `/attend missout 2026-08-01 18:00`"
)


def command_help() -> str:
    return COMMAND_HELP


def checkin_prompt(emp: dict, checkin_hm: str) -> list:
    """예약 출근 알림 메시지. 재택/외근을 누르면 그 형태로 자동 출근 기록된다."""
    return [
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"좋은 아침이에요, {emp['name']}님 :sunny:\n출근할 시간입니다. "
                    f"오늘 근무 형태를 선택하세요. _(설정: {checkin_hm} 출근)_"}},
        {"type": "actions", "elements": [
            _button("출근(사무실)", "checkin", style="primary"),
            _button("재택근무", "remote"),
            _button("외근", "field"),
        ]},
        {"type": "context", "elements": [{"type": "mrkdwn",
            "text": ":house: *재택근무* 를 누르면 재택으로 자동 출근 처리됩니다."}]},
    ]


def checkout_prompt(emp: dict, checkout_hm: str = "") -> list:
    """예약 퇴근 알림 메시지."""
    tail = f" _(설정: {checkout_hm} 퇴근)_" if checkout_hm else ""
    return [
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"{emp['name']}님, 퇴근할 시간입니다. 오늘도 수고하셨어요 :clap:{tail}"}},
        {"type": "actions", "elements": [
            _button("퇴근", "checkout", style="primary"),
        ]},
    ]


def leave_modal() -> dict:
    """연차 신청 모달 — 연차 / 오전반차(4h) / 오후반차(4h)."""
    return {
        "type": "modal", "callback_id": "leave_modal",
        "title": {"type": "plain_text", "text": "연차 신청"},
        "submit": {"type": "plain_text", "text": "신청"},
        "close": {"type": "plain_text", "text": "취소"},
        "blocks": [
            {"type": "input", "block_id": "kind", "label": {"type": "plain_text", "text": "종류"},
             "element": {"type": "static_select", "action_id": "v",
                "options": [
                    {"text": {"type": "plain_text", "text": "연차 (종일)"}, "value": "annual"},
                    {"text": {"type": "plain_text", "text": "오전반차 (4시간)"}, "value": "half_am"},
                    {"text": {"type": "plain_text", "text": "오후반차 (4시간)"}, "value": "half_pm"},
                ]}},
            {"type": "input", "block_id": "start", "label": {"type": "plain_text", "text": "시작일"},
             "element": {"type": "datepicker", "action_id": "v"}},
            {"type": "input", "block_id": "end", "optional": True,
             "label": {"type": "plain_text", "text": "종료일 (연차 종일만)"},
             "element": {"type": "datepicker", "action_id": "v"}},
            {"type": "input", "block_id": "reason", "optional": True,
             "label": {"type": "plain_text", "text": "사유"},
             "element": {"type": "plain_text_input", "action_id": "v"}},
        ],
    }


def settings_modal(config: dict) -> dict:
    """근무 설정 모달(간단 항목). 놀금·단축 상세는 웹 설정 페이지 링크로 유도."""
    return {
        "type": "modal", "callback_id": "settings_modal",
        "title": {"type": "plain_text", "text": "근무 설정"},
        "submit": {"type": "plain_text", "text": "저장"},
        "close": {"type": "plain_text", "text": "취소"},
        "blocks": [
            {"type": "input", "block_id": "work_type", "label": {"type": "plain_text", "text": "근무제도"},
             "element": {"type": "static_select", "action_id": "v",
                "options": [
                    {"text": {"type": "plain_text", "text": "일반근무"}, "value": "normal"},
                    {"text": {"type": "plain_text", "text": "시차근무"}, "value": "flex"},
                    {"text": {"type": "plain_text", "text": "선택적근무"}, "value": "selective"},
                    {"text": {"type": "plain_text", "text": "탄력근무"}, "value": "elastic"},
                ]}},
            {"type": "input", "block_id": "checkin", "label": {"type": "plain_text", "text": "출근"},
             "element": {"type": "timepicker", "action_id": "v",
                         "initial_time": config.get("checkin", "09:00")}},
            {"type": "input", "block_id": "checkout", "label": {"type": "plain_text", "text": "퇴근"},
             "element": {"type": "timepicker", "action_id": "v",
                         "initial_time": config.get("checkout", "18:00")}},
            {"type": "context", "elements": [{"type": "mrkdwn",
                "text": ":information_source: 놀금·단축근무 규칙은 <https://sam.example.com/settings|웹 설정>에서 "
                        "캘린더로 확인하며 편집하세요."}]},
        ],
    }


def approval_message(req: dict) -> list:
    """팀장에게 보내는 연장/휴일근무 승인 요청 메시지."""
    label = {"overtime": "연장근무", "holiday": "휴일근무"}[req["kind"]]
    return [
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f":bell: *{label} 승인 요청*\n{req['emp_name']} · {req['detail']}"}},
        {"type": "actions", "elements": [
            _button("승인", "approve", value=str(req["id"]), style="primary"),
            _button("반려", "reject", value=str(req["id"]), style="danger"),
        ]},
    ]


def approval_result(req: dict) -> list:
    label = {"overtime": "연장근무", "holiday": "휴일근무"}[req["kind"]]
    mark = "승인됨" if req["status"] == "approved" else "반려됨"
    return [{"type": "section", "text": {"type": "mrkdwn",
             "text": f"~{label} 요청~ *{mark}* · {req.get('approver_name','')} {req.get('decided_hm','')}"}}]
