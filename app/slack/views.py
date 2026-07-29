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


def me_link(emp: dict, base_url: str = "https://your-app.vercel.app") -> str:
    """개인 근태관리 웹으로 가는 매직 링크.

    자동 로그인처럼 쓰려면 만료형 서명 토큰(JWT, 짧은 TTL)을 붙여
    웹에서 검증 후 본인 페이지를 연다. 실제로는 render 시점에 발급.
    """
    return f"{base_url}/me?token=<signed-jwt:{emp['id']}>"


def home_view(emp: dict, state: dict) -> dict:
    """홈 탭. state: {"status": "none|work|remote|field|off", "worked": "2시간 10분"}"""
    status_label = {"none": "미출근", "work": "근무중", "remote": "재택근무 중",
                    "field": "외근 중", "off": "휴가"}.get(state["status"], "미출근")
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"*안녕하세요, {emp['name']}님* :wave:\n오늘 상태: *{status_label}*"}},
    ]
    if state["status"] == "none":
        blocks.append({"type": "actions", "elements": [
            _button("출근", "checkin", style="primary"),
            _button("재택", "remote"),
            _button("외근", "field"),
        ]})
    else:
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"현재 근무 *{state.get('worked','-')}*"}]})
        blocks.append({"type": "actions", "elements": [
            _button("퇴근", "checkout", style="primary"),
            _button("외근 전환", "field"),
        ]})
    blocks.append({"type": "divider"})
    blocks.append({"type": "actions", "elements": [
        _button("연차 신청", "open_leave"),
        _button("근무 설정", "open_settings"),
        _button("내 통계", "open_mystats"),
        _link_button("웹에서 근태관리", me_link(emp)),   # 브라우저로 개인 대시보드 열기
    ]})
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


def checkin_prompt(emp: dict, checkin_hm: str) -> list:
    """예약 출근 알림 메시지."""
    return [
        {"type": "section", "text": {"type": "mrkdwn",
            "text": f"좋은 아침이에요, {emp['name']}님 :sunny:\n출근할 시간입니다. "
                    f"오늘 근무 형태를 선택하세요. _(설정: {checkin_hm} 출근)_"}},
        {"type": "actions", "elements": [
            _button("출근", "checkin", style="primary"),
            _button("재택", "remote"),
            _button("외근", "field"),
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
                    {"text": {"type": "plain_text", "text": "시차출퇴근"}, "value": "flex"},
                    {"text": {"type": "plain_text", "text": "선택근로"}, "value": "selective"},
                    {"text": {"type": "plain_text", "text": "탄력근로"}, "value": "elastic"},
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
