# 배포 가이드 — 어느 컴퓨터에서든 접속하는 공개 주소 만들기

로컬 데모(`127.0.0.1`)는 그 PC에서만 열립니다. 인터넷 어디서든 관리자가 통계를 보게 하려면
클라우드에 올려 공개 HTTPS 주소를 받아야 합니다. 무료로 가능한 **Render**를 기준으로 안내하고,
아래에 **Railway** 방법도 덧붙입니다. 사전 준비는 GitHub 계정 하나면 됩니다.

배포에 필요한 파일은 이미 프로젝트에 들어 있습니다: `Procfile`, `render.yaml`, `runtime.txt`,
`manage.py`, `.gitignore`.

---

## 0. 코드를 GitHub에 올리기 (공통)

컴퓨터에서 프로젝트 폴더(`krewunion-attendance-bot`)를 열고 터미널에서:

```bash
git init
git add .
git commit -m "krewunion attendance bot"
```

그다음 github.com에서 새 저장소(repository)를 만들고, 안내에 나오는 두 줄을 붙여넣어 올립니다:

```bash
git remote add origin https://github.com/<본인계정>/krewunion-attendance-bot.git
git branch -M main
git push -u origin main
```

> `.gitignore` 덕분에 `venv`, `.env`, `*.db` 같은 건 올라가지 않습니다. 비밀 토큰은 절대 커밋하지 마세요.

---

## 1. Vercel로 배포 (Vercel 계정이 있다면)

이 프로젝트에는 Vercel용 파일(`vercel.json`, `api/index.py`)이 이미 들어 있습니다.
단, **Vercel은 서버리스라 SQLite가 유지되지 않으므로 외부 PostgreSQL이 반드시 필요**합니다.

1. **DB 준비**: Vercel 대시보드 → **Storage → Create Database → Postgres(Neon)** 생성 →
   연결 문자열(`postgres://...`) 복사. (또는 supabase.com 무료 Postgres도 가능)
2. **프로젝트 추가**: Vercel → **Add New → Project** → GitHub 저장소 import →
   Framework Preset은 **Other**.
3. **환경변수(Environment Variables)** 추가:
   - `DATABASE_URL` = 위 연결 문자열
   - `JWT_SECRET` = 임의의 긴 문자열
   - `CORS_ORIGINS` = `*`
4. **Deploy** → `https://<프로젝트>.vercel.app` 공개 주소가 생깁니다.
5. **DB 초기화 + 관리자 등록** (Vercel엔 서버 셸이 없으니 *내 PC에서* 클라우드 DB를 가리켜 실행):
   ```bat
   set DATABASE_URL=<연결 문자열>
   set JWT_SECRET=<Vercel에 넣은 값과 동일>
   python manage.py initdb
   python manage.py add-employee K-3001 U3001 김인사 경영지원 sysadmin
   python manage.py issue-token U3001
   ```
   > `JWT_SECRET`은 Vercel에 넣은 값과 **반드시 같아야** 토큰이 통합니다.
6. 공개 주소(`https://<프로젝트>.vercel.app/`)를 열고 토큰을 붙여넣으면 통계가 보입니다.

**주의(중요)**: Vercel은 서버리스라 Slack **Socket Mode**와 상시 워커(Celery)는 못 씁니다.
Slack 봇은 HTTP 이벤트(`/slack/events`), 예약 출퇴근 알림은 **Vercel Cron**으로 붙여야 합니다.
관리자 대시보드 공개가 목적이면 Vercel로 충분하고, 봇까지 상시로 매끄럽게 돌리려면 아래 Render/Railway가 더 편합니다.

---

## 2. Render로 배포 (권장, 무료)

### 1-1. 블루프린트로 한 번에 생성
1. https://render.com 가입 → 대시보드에서 **New → Blueprint**.
2. 방금 올린 GitHub 저장소를 선택하면, `render.yaml`을 읽어
   **웹 서비스 + PostgreSQL(무료)** 를 자동으로 만들어 줍니다.
3. **Apply** 를 누르면 빌드가 시작됩니다. (`JWT_SECRET`은 자동 생성, `DATABASE_URL`은 DB에서 자동 연결)

### 1-2. 배포 완료 확인
- 빌드가 끝나면 서비스에 `https://krewunion-attendance.onrender.com` 같은 **공개 주소**가 생깁니다.
- 브라우저로 그 주소를 열면 관리자 대시보드가 뜹니다(첫 부팅 때 테이블이 자동 생성됩니다).
  아직 데이터·계정이 없으니 다음 단계로 관리자를 등록합니다.

### 1-3. 첫 관리자 등록 + 로그인 토큰 발급
Render 서비스 화면 상단의 **Shell** 탭을 열고 아래를 실행합니다(사번·이름은 실제 값으로):

```bash
python manage.py add-employee K-3001 U3001 김인사 경영지원 sysadmin
python manage.py issue-token U3001
```

두 번째 명령이 긴 문자열(토큰)을 출력합니다. 이 토큰을 복사하세요.

### 1-4. 대시보드 접속
- 공개 주소(`https://....onrender.com/`)를 어느 컴퓨터에서든 엽니다.
- 상단 **토큰** 칸에 방금 복사한 토큰을 붙여넣고 **불러오기**.
- 이제 통계가 보입니다. (API 주소는 자동으로 그 사이트 주소로 잡힙니다.)

> 무료 플랜은 트래픽이 없으면 잠들었다가 첫 접속 시 20~30초 깨어납니다. 상시 가동이 필요하면 유료(Starter)로 올리면 됩니다.

---

## 3. Railway로 배포 (대안)

1. https://railway.app 가입 → **New Project → Deploy from GitHub repo** → 저장소 선택.
2. 같은 프로젝트에서 **New → Database → PostgreSQL** 추가.
3. 웹 서비스의 **Variables** 에 다음을 추가:
   - `DATABASE_URL` = `${{Postgres.DATABASE_URL}}` (Postgres 서비스 참조)
   - `JWT_SECRET` = 임의의 긴 문자열
   - `CORS_ORIGINS` = `*`
4. 시작 명령은 `Procfile`이 자동 인식됩니다(`uvicorn app.web.admin:api ...`).
5. 배포 후 **Settings → Networking → Generate Domain** 으로 공개 주소를 만듭니다.
6. 관리자 등록/토큰 발급은 Railway CLI로:
   ```bash
   railway run python manage.py add-employee K-3001 U3001 김인사 경영지원 sysadmin
   railway run python manage.py issue-token U3001
   ```
7. 공개 주소를 열고 토큰을 붙여넣으면 끝.

---

## 4. Slack 봇까지 붙이려면 (선택)

위까지는 **관리자 통계 대시보드**를 공개하는 단계입니다. 직원이 Slack에서 출퇴근·연차를 찍게 하려면
Slack 앱을 추가로 연결합니다.

1. https://api.slack.com/apps → **Create New App** → 워크스페이스 선택.
2. **OAuth & Permissions** 에서 봇 스코프 추가: `chat:write`, `commands`, `users:read`,
   `im:write`, 홈 탭용 `app_home` 등. 설치 후 **Bot User OAuth Token**(`xoxb-...`) 복사.
3. **Basic Information** 의 **Signing Secret** 복사.
4. 배포 서비스의 환경변수에 넣기:
   - `SLACK_BOT_TOKEN=xoxb-...`, `SLACK_SIGNING_SECRET=...`, `USE_SOCKET_MODE=false`
5. `app/web/admin.py`에서 주석 처리된 **Slack HTTP 핸들러**(`/slack/events`) 블록을 활성화하고 재배포.
6. Slack 앱의 **Event Subscriptions**·**Interactivity**·**Slash Commands(/연차)** 의
   Request URL을 `https://<공개주소>/slack/events` 로 지정.
7. 예약 출퇴근 알림은 **Cron**으로: Render는 **Cron Job**, Railway는 스케줄 기능으로
   10분마다 `POST https://<공개주소>/cron/checkin-scan` (헤더 `X-Cron-Secret: <JWT_SECRET>`) 호출.

> 봇은 상시 실행이 필요하므로 서버리스보다 Render/Railway 같은 상시 호스트가 잘 맞습니다.
> 최초 앱 설치자를 시스템 관리자로 만들려면 설치 시 `repo.bootstrap_first_admin(user_id)`가 한 번 호출되게 하면 됩니다.

---

## 요약

- **공개 주소가 생기면** 집·회사·외부 어디서든 관리자가 로그인해 통계를 봅니다.
- **필요한 것**: GitHub 저장소 + Render(또는 Railway) 무료 계정.
- **핵심 명령**: `manage.py add-employee`(관리자 등록) → `manage.py issue-token`(로그인 토큰).
- **보안**: `JWT_SECRET`은 반드시 임의의 긴 값으로, 토큰은 관리자에게만 공유하세요.
