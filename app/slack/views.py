"""Block Kit 뷰 빌더 — 홈 탭, 예약 알림, 연차/설정 모달, 승인 메시지."""
from __future__ import annotations


def _button(text, action_id, value="", style=None):
    b = {"type": "button", "text": {"type": "plain_text", "text": text}, "action_id": action_id}
    if value:
        b["value"] = value
    if style:
        b["style"] = style
    return b


_link_seq = [0]


def _link_button(text, url, style=None):
    """url 버튼(클릭 시 브라우저로 이동). action_id는 뷰 내에서 유일해야 하므로 자동 부여."""
    _link_seq[0] += 1
    b = {"type": "button", "text": {"type": "plain_text", "text": text},
         "url": url, "action_id": f"open_web_{_link_seq[0]}"}
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


def _go(emp: dict, target: str) -> str:
    """짧은 코드형 매직 링크(/go/{code}) — hover 시 긴 토큰이 노출되지 않게."""
    from app.config import settings
    from app.links import make_code
    return f"{settings.WEB_BASE_URL}/go/{make_code(emp['slack_user_id'], target)}"


def settings_link(emp: dict) -> str:
    """개인 근무설정 웹페이지 (모든 직원)."""
    return _go(emp, "settings")


def my_link(emp: dict) -> str:
    """직원 대시보드(내 근태)."""
    return _go(emp, "me")


def dashboard_link(emp: dict) -> str | None:
    """관리자(hr/sysadmin) + 슈퍼유저(창현)에게 대시보드 매직 링크 반환."""
    from app.repo import SUPERUSER_SLACK_IDS
    is_super = emp.get("slack_user_id") in SUPERUSER_SLACK_IDS
    if not is_super and emp.get("role") not in ("hr", "sysadmin"):
        return None
    return _go(emp, "dashboard")


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

    # 실제 종일 휴가(연차 등)면 버튼 숨김. 놀금은 휴무이지만 출근 시 근무 처리 → 버튼 유지
    _is_recovery = state.get("leave_kind") == "recovery"
    if state.get("status") == "off" and not _is_recovery:
        from app.domain.leave import LEAVE_LABEL
        lbl = LEAVE_LABEL.get(state.get("leave_kind"), "휴가")
        blocks.append({"type": "section", "text": {"type": "mrkdwn",
            "text": f":palm_tree: *오늘은 {lbl}입니다.* 출근 기록은 필요 없어요. 즐거운 하루 보내세요!"}})
    else:
        if _is_recovery:
            blocks.append({"type": "section", "text": {"type": "mrkdwn",
                "text": ":palm_tree: *오늘은 놀금(리커버리데이)입니다.* 쉬셔도 되고, 근무하시면 아래에서 출근을 눌러주세요."}})
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

    # 휴가 신청
    blocks += group(":memo: *휴가 신청*", "/attend leave", [
        _button("연차·반차 신청", "open_leave"),
    ])
    # 내 정보 — 웹 링크는 모바일 홈탭에서 'URL 버튼'이 안 열리는 이슈가 있어
    #           탭 가능한 텍스트 링크(<url|라벨>)로 제공(데스크톱·모바일 모두 동작).
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": (
        ":bar_chart: *내 정보*   `/attend status · /attend team`\n"
        f"›  <{my_link(emp)}|내 근무 현황 열기>          "
        f"›  <{settings_link(emp)}|근무 설정 열기>")}})

    # 누락 등록 (버튼 없이 명령어만, 다른 항목과 같은 볼드+아이콘 스타일)
    blocks.append({"type": "section", "text": {"type": "mrkdwn",
        "text": ":pencil: *누락 등록*   `/attend miss 2026-08-01 09:00 18:00`"}})

    dlink = dashboard_link(emp)
    if dlink:   # 관리자(hr/sysadmin·창현)에게만 — 텍스트 링크(모바일 호환)
        blocks.append({"type": "divider"})
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": (
            ":gear: *관리자*\n"
            f"›  <{dlink}|관리자 대시보드 열기>")}})
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


_WD_KO = ["월", "화", "수", "목", "금", "토", "일"]


def _kdate(d) -> str:
    return f"{d.month:02d}월 {d.day:02d}일({_WD_KO[d.weekday()]})"


def _hm(m: int) -> str:
    h, mm = divmod(m, 60)
    return f"{h}시간 {mm}분" if h else f"{mm}분"


def checkin_confirm(info: dict) -> str:
    """출근/재택/외근 확인 메시지."""
    label = {"office": "출근", "remote": "재택근무 출근", "field": "외근 출근"}.get(info.get("kind"), "출근")
    return f"`{_kdate(info['date'])}` {label}했습니다.\n근무 시작 시간: {info['checkin']}"


def checkout_confirm(s: dict) -> str:
    """퇴근 확인 메시지 — 근무시간 요약."""
    return (f"*근무시간*\n"
            f"`{_kdate(s['date'])}` 퇴근했습니다.\n"
            f"{_hm(s['work'])} ({s['checkin']} ~ {s['checkout']})\n"
            f"휴게시간: {_hm(s['break'])}\n"
            f"제외시간: 0분\n"
            f"저녁시간: {_hm(s['night'])}")


def _leave_option_groups(leave_config: dict | None, custom_types: list | None = None) -> list:
    """개인 휴가 관리 설정(leave_config)에서 켠 휴가만 옵션으로 구성.
    custom_types: 전역 공유 커스텀 휴가 카탈로그([{key,name,unit,...}])."""
    def opt(text, value):
        return {"text": {"type": "plain_text", "text": text}, "value": value}
    lc = leave_config or {}
    def on(k):
        return bool((lc.get(k) or {}).get("on"))
    groups = []
    if (lc.get("annual") or {}).get("on", True):
        groups.append({"label": {"type": "plain_text", "text": "연차"}, "options": [
            opt("연차 (종일)", "annual"), opt("오전반차 (4시간)", "half_am"),
            opt("오후반차 (4시간)", "half_pm")]})
    sp = []
    if on("bd"):
        sp.append(opt("BD", "bd"))
    if on("seollal"):
        sp.append(opt("설날 휴가", "seollal"))
    if on("chuseok"):
        sp.append(opt("추석 휴가", "chuseok"))
    if on("family_care_paid"):
        sp.append(opt("가족돌봄(유급)", "family_care_paid"))
    if on("family_care_unpaid"):
        sp.append(opt("가족돌봄(무급)", "family_care_unpaid"))
    if on("refresh"):
        sp.append(opt("리프레쉬 휴가", "refresh"))
    if on("special"):
        sp.append(opt("특별휴가", "special"))
    if on("health"):
        sp.append(opt("건강휴가", "health"))
    if on("sabbatical"):
        sp.append(opt("안식휴가", "sabbatical"))
    if on("health_check"):
        if (lc.get("health_check") or {}).get("hours", 8) == 8:
            sp.append(opt("건강검진 (8h)", "health_check_full"))
        else:
            sp += [opt("건강검진 (오전 4h)", "health_check_am"), opt("건강검진 (오후 4h)", "health_check_pm")]
    if on("birthday"):
        if (lc.get("birthday") or {}).get("hours", 8) == 8:
            sp.append(opt("생일 (8h)", "birthday_full"))
        else:
            sp += [opt("생일 (오전 4h)", "birthday_am"), opt("생일 (오후 4h)", "birthday_pm")]
    if sp:
        groups.append({"label": {"type": "plain_text", "text": "특수 휴가"}, "options": sp})
    # 공유 커스텀 휴가(개인이 켠 항목만)
    cust = [opt(c["name"], c["key"]) for c in (custom_types or [])
            if c.get("key") and c.get("name") and (lc.get(c["key"]) or {}).get("on")]
    if cust:
        groups.append({"label": {"type": "plain_text", "text": "추가 휴가"}, "options": cust})
    if not groups:
        groups.append({"label": {"type": "plain_text", "text": "연차"},
                       "options": [opt("연차 (종일)", "annual")]})
    return groups


def leave_modal(leave_config: dict | None = None, custom_types: list | None = None) -> dict:
    """휴가 신청 모달 — 개인이 켠 휴가 종류만 표시."""
    return {
        "type": "modal", "callback_id": "leave_modal",
        "title": {"type": "plain_text", "text": "휴가 신청"},
        "submit": {"type": "plain_text", "text": "신청"},
        "close": {"type": "plain_text", "text": "취소"},
        "blocks": [
            {"type": "input", "block_id": "kind", "label": {"type": "plain_text", "text": "종류"},
             "element": {"type": "static_select", "action_id": "v",
                "option_groups": _leave_option_groups(leave_config, custom_types)}},
            {"type": "input", "block_id": "start", "label": {"type": "plain_text", "text": "시작일"},
             "element": {"type": "datepicker", "action_id": "v"}},
            {"type": "input", "block_id": "end", "optional": True,
             "label": {"type": "plain_text", "text": "종료일 (반일·당일이면 비워두세요)"},
             "element": {"type": "datepicker", "action_id": "v"}},
            {"type": "input", "block_id": "reason", "optional": True,
             "label": {"type": "plain_text", "text": "사유 (선택)"},
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
