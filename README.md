# 크루유니언 근태봇

Slack 기반 근태·연차 관리 시스템. 직원은 Slack에서, 관리자는 웹 통계에서 사용한다.

## 확정된 규칙

- **출퇴근**: 각 직원의 설정 시각에 봇이 예약 메시지로 버튼을 보내면 눌러서 기록. 출근 버튼 옆에 재택·외근 버튼이 함께 노출된다.
- **연차**: 연차(종일 8h) / 오전반차(4h) / 오후반차(4h). 승인 없이 즉시 반영, 팀장에 통보. 신청 시 구글 캘린더에 자동 등록(선택).
- **관리자 승인**: 초과(연장)근무·휴일근무만 승인 필요. 나머지는 즉시 확정.
- **놀금(리커버리데이)**: 매월 마지막주 금요일 / 격주 금요일 / 직접 지정.
- **단축근무**: 규칙 추가형. 출근 늦춤·퇴근 당김을 매주 요일/매일/특정일로 여러 개 등록(예: 출근 매주 월 30분 + 퇴근 매주 금 30분).
- **근무제도**: 일반·시차·선택·탄력. 선택근로는 출근/퇴근 시각으로 설정.

## 역할 분리

- **직원 (Slack)**: 출퇴근·재택·외근, 연차 신청(`/연차` 또는 "연차" 입력), 조회.
- **개인 근무 설정**: 근무제도·출퇴근 같은 단순 설정은 Slack 모달. 놀금·단축근무처럼 캘린더로 확인해야 하는 설정은 **본인이 웹 설정 페이지**에서 편집(어느 컴퓨터에서든 로그인 후 접근).
- **관리자 (웹 통계 전용)**: 월/직원별 통계, 출근율, 연차 사용률, 실시간 근무현황, 연장·휴일 승인센터. 어느 컴퓨터에서든 브라우저로 접근.

### 권한(역할) 지정 — 누가 관리자를 정하나
`employee < manager(승인) < hr(전체 조회·통계·설정) < sysadmin(회사설정·권한·API)`.
- 앱을 워크스페이스에 **최초 설치한 사람이 자동으로 sysadmin**이 된다(`repo.bootstrap_first_admin`).
- 이후 **hr/sysadmin만** 다른 구성원의 역할을 지정한다(`repo.assign_role`, 그 외에는 `PermissionError`).
- 팀장(manager)은 조직도/그룹웨어 연동의 `manager_id`로 매핑되며, 승인권자 판정에 쓰인다.
- 통계 API는 hr/sysadmin 토큰만 통과한다(`app/web/admin.py`의 `require_admin`).

**지정 해제·인수인계.**
- **해제(강등)**: `revoke_role`로 일반 직원으로 내린다. sysadmin 권한의 부여·회수는 sysadmin만 가능(hr는 불가).
- **마지막 관리자 보호**: 시스템에 sysadmin이 최소 1명은 남아야 한다. 마지막 한 명은 후임 없이 강등되지 않는다.
- **인수인계**: `handover_admin(현재관리자, 후임)` 한 번으로 후임을 sysadmin으로 올리고 본인은 내려온다. 두 변경이 한 트랜잭션이라 '관리자 0명' 공백이 생기지 않는다. 즉 권한을 놓으면서 다른 사람을 지정하는 인수인계 형식이다.

## 구조

```
app/
  config.py              환경설정
  db.py                  SQLAlchemy 엔진·세션 (테스트용 configure)
  models.py              SQLAlchemy 모델 (role 포함)
  repo.py                저장소 계층 (구현 완료: 출퇴근·연차·승인·통계·권한)
  domain/                ★ 순수 비즈니스 로직 (테스트 완비)
    schedule.py          놀금·단축·반차·예약시각 계산
    attendance.py        실근무·연장·야간·휴일 집계
    leave.py             연차/오전·오후반차 소진
    approval.py          연장·휴일 승인 상태머신
  slack/
    app.py               Bolt 핸들러(홈탭·출퇴근·연차·설정·승인·웹링크)
    views.py             Block Kit 뷰
  scheduler/tasks.py     예약 출퇴근 알림·캘린더 동기화 (Celery + Cron 공용)
  integrations/gcal.py   구글 캘린더
  web/admin.py           관리자 통계 API + Slack HTTP + Cron
  web/dashboard.html     관리자 통계 대시보드 (통계 API 연결, 미연결 시 데모)
tests/
  test_domain.py         도메인 검증 (11 케이스)
  test_repo.py           저장소·통계·권한 통합 (SQLite, 6 케이스)
```

## 빠른 실행 (데모)

별도 설정 없이 SQLite 샘플 데이터로 관리자 통계를 바로 볼 수 있다.

- **Windows**: `run.bat` 더블클릭 (또는 터미널에서 실행)
- **macOS/Linux**: `bash run.sh`

실행하면 가상환경 생성 → 패키지 설치 → 데모 서버(http://127.0.0.1:8000) 기동까지 자동으로 진행되고,
콘솔에 관리자 접속 토큰이 출력된다. 그 다음 `app/web/dashboard.html`을 브라우저로 열어
상단에 API 주소(`http://127.0.0.1:8000`)와 토큰을 붙여넣으면 실데이터가 렌더된다.

> 참고: 이 프로젝트는 Slack 봇 + 웹 API 서버라 단일 실행파일(.exe)이 아니라 Python으로 구동한다.
> 실제 Slack 연동은 `.env`에 토큰을 넣고 `python -m app.slack.app`(봇) / `uvicorn app.web.admin:api`(웹)로 띄운다.

## 실행 / 테스트

```bash
pip install -r requirements-full.txt   # 테스트·봇 포함 전체 설치
cp .env.example .env                    # 값 채우기
python -m pytest tests/ -q              # 19개 통과
```

> 배포(웹 대시보드·API)와 로컬 데모는 가벼운 `requirements.txt`만으로 충분합니다.
> Slack 봇·스케줄러·테스트까지 돌리려면 `requirements-full.txt`를 설치하세요.

관리자 대시보드는 `app/web/dashboard.html`을 브라우저로 열고 상단에 API 주소·토큰을 입력하면 통계 API에 연결된다(미입력 시 데모 데이터로 렌더).

## 배포 옵션

### A. 상시 워커 호스트 (Railway/Render/Fly 등)
- Bolt 앱은 Socket Mode(`USE_SOCKET_MODE=true`)로 방화벽 개방 없이 구동.
- Celery worker + beat가 예약 출퇴근 알림·캘린더 동기화를 처리.
- Postgres·Redis는 관리형 서비스 사용.

### B. Vercel (서버리스) — 서버가 따로 없을 때
가능하다. 서버리스 특성에 맞춰 다음을 바꾼다.

- **관리자 대시보드 + 개인 웹 설정**: Next.js로 Vercel에 배포. 어느 컴퓨터에서든 접근.
- **Slack 앱**: Socket Mode는 상시 연결이라 서버리스에서 안 됨 → **HTTP Request URL 모드**(`USE_SOCKET_MODE=false`)로 `/slack/events` 서버리스 함수가 처리. 3초 룰 위해 즉시 ack.
- **예약 출퇴근 알림**: 상시 워커(Celery beat)가 없으므로 **Vercel Cron**이 5~10분마다 `POST /cron/checkin-scan`을 호출 → 대상자에게 발송. (`app/web/admin.py`의 cron 엔드포인트, `run_send_due_checkin_prompts`)
- **DB/Redis**: Vercel Postgres 또는 Neon/Supabase, Redis는 Upstash.
- **비동기 작업(캘린더 등)**: 요청 내 처리하거나 Upstash QStash 사용.

> 참고: 백엔드가 Python이라 Vercel Python 함수로도 되지만, 상시 실행이 필요한 부분(원래 Celery)이 있어 **대시보드=Vercel + 봇/스케줄러=작은 상시 호스트(Railway 등)** 조합이 가장 매끄럽다.

### 도메인(커스텀 도메인)이 필요한가?
**필수 아니다.** Vercel이 무료 HTTPS 주소(`your-app.vercel.app`)를 자동 발급하고, Slack Request URL·Google OAuth 리디렉트 모두 이 주소로 동작한다. 회사 브랜딩이나 SSO를 위해 `attendance.회사.com` 같은 커스텀 도메인을 붙이는 건 선택이며, DNS만 Vercel로 연결하면 된다.

## 진행 상태
- [x] 도메인 엔진(놀금·단축·반차·연장·야간·휴일·승인) + 테스트
- [x] `repo.py` SQLAlchemy 구현(출퇴근·연차·승인·권한)
- [x] 관리자 통계 집계 + JWT 역할 기반 접근 제어
- [x] 관리자 대시보드(통계 API 연결)
- [ ] Slack 앱 실배포(OAuth 설치, Request URL/Socket 선택), 실제 워크스페이스 연동
- [ ] 개인 웹 설정 페이지(놀금·단축 캘린더)와 매직 링크 로그인 연결
- [ ] 그룹웨어/조직도 연동(직원·부서·manager_id 동기화), SSO
- [ ] 주 52시간 초과 판정(주 단위 집계)
