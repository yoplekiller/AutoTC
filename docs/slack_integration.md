# AutoTC Slack 슬래시 커맨드 연동 가이드

## 전체 흐름

```
Slack에서 /testplan 또는 /ticket 입력
→ Slack이 app.py 서버로 POST 요청
→ 서버가 Jira + Groq AI + Confluence 처리 (백그라운드)
→ Slack 채널에 결과 응답
```

---

## 구현된 스크립트

| 파일 | 역할 |
|---|---|
| `src/create_ticket.py` | 자연어 → AI → Jira 티켓 생성 |
| `src/generate_test_plan.py` | Jira 티켓들 → AI → Confluence 테스트 계획서 생성 |
| `app.py` | Flask 서버 + ngrok으로 Slack 슬래시 커맨드 처리 |

---

## 사용법

### CLI로 직접 실행

```powershell
cd "C:\Users\jmlim\OneDrive\Desktop\AutoTC"

# Jira 티켓 생성
python src/create_ticket.py "결제 시 쿠폰 적용이 안 됨"
python src/create_ticket.py "회원 환영 이메일 발송 기능" --type Story
python src/create_ticket.py "내용" --dry-run   # 미리보기만

# 테스트 계획서 생성
python src/generate_test_plan.py MKQA-1 MKQA-4 MKQA-5
python src/generate_test_plan.py MKQA-1 MKQA-4 --title "Sprint 1 테스트 계획서"
python src/generate_test_plan.py MKQA-1 --dry-run   # 미리보기만
```

### Slack 슬래시 커맨드로 실행

```
/testplan MKQA-1 MKQA-4 MKQA-5
/testplan MKQA-1 MKQA-2 --title Sprint 1 테스트 계획서

/ticket 결제 시 쿠폰 적용이 안 됨
/ticket 회원가입 이메일 인증 기능 추가 --type Story
```

---

## 서버 실행 방법

```powershell
cd "C:\Users\jmlim\OneDrive\Desktop\AutoTC"
python app.py
```

실행하면 ngrok URL이 출력돼요:
```
==================================================
  AutoTC Slack 서버 시작
  Public URL: https://xxxx.ngrok-free.app

  Slack App에 아래 URL 등록:
  /testplan → https://xxxx.ngrok-free.app/slack/testplan
  /ticket   → https://xxxx.ngrok-free.app/slack/ticket
==================================================
```

---

## Slack App 설정 (최초 1회)

1. [api.slack.com/apps](https://api.slack.com/apps) 접속
2. **Create New App** → From scratch → 이름: `AutoTC`
3. **OAuth & Permissions** → Bot Token Scopes → `commands`, `chat:write` 추가
4. **Install App** → Install to Workspace
5. **Slash Commands** → Create New Command
   - `/testplan` → Request URL: `https://ngrok주소/slack/testplan`
   - `/ticket` → Request URL: `https://ngrok주소/slack/ticket`

> ngrok URL은 서버 껐다 켤 때마다 바뀌므로 Slack App에서 URL 업데이트 필요

---

## 주의사항

- `python app.py` 실행 중에만 Slack 커맨드 작동
- ngrok 무료 플랜은 URL이 매번 바뀜 → 서버 재시작 시 Slack App의 Request URL도 다시 업데이트해야 함
- `.env` 파일에 API 키 모두 설정되어 있어야 함

---

## .env 필수 항목

```
GROQ_API_KEY=...
JIRA_URL=https://jmlim9244-1775142889491.atlassian.net
JIRA_EMAIL=jmlim9244@gmail.com
JIRA_API_TOKEN=...
JIRA_PROJECT_KEY=MKQA
SLACK_WEBHOOK_URL=...
CONFLUENCE_SPACE_KEY=QATEST
```
