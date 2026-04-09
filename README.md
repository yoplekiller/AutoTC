# AutoTC — AI 기반 TC 자동 생성 도구

> Jira 티켓을 구글 시트에 입력하면  
> AI(Groq LLaMA 3.3 70B)가 매뉴얼 테스트 케이스를 자동으로 생성하고  
> 구글 시트에 저장 + Slack으로 알림을 보내는 QA 자동화 도구

---

## 왜 만들었나

| 문제 | 해결 |
|------|------|
| 티켓 1개당 TC 작성에 평균 20~30분 소요 | AI가 초안 생성 → 검토만 하면 됨 |
| 티켓 설명이 부실해 TC 작성 기준이 불명확 | 2단계 AI 파이프라인으로 요구사항 먼저 추론 후 TC 생성 |
| QA 혼자 여러 기능을 동시에 검증해야 할 때 병목 발생 | 구글 시트에 티켓 키만 입력하면 10분 내 TC 초안 생성 |
| 서비스마다 비즈니스 룰이 달라 범용 TC 품질이 낮음 | 서비스 컨텍스트 파일(`contexts/`)로 도메인 지식 주입 |

---

## 파이프라인 흐름

```
구글 시트 '티켓 입력' 탭에 Jira 티켓 키 입력
              ↓
watch_sheet.py 폴링 (로컬 또는 GitHub Actions 10분 주기)
              ↓
Jira REST API로 티켓 정보 조회
              ↓
Groq AI — 1단계: 티켓 설명 보완 (요구사항 추론)
              ↓
Groq AI — 2단계: 매뉴얼 TC 생성 (JSON)
              ↓
구글 시트에 티켓별 시트 생성 (TC ID / 테스트유형 / 단계 / 기대결과 / 결과 드롭다운)
              ↓
Slack 알림 발송 + 입력 시트 상태 '완료'로 업데이트
```

---

## 주요 기능

### AI 2단계 파이프라인
- **1단계 (요구사항 추론)**: 설명이 부족한 티켓도 AI가 기능 목적 / 주요 요구사항 / 예외 케이스를 먼저 추론
- **2단계 (TC 생성)**: 추론된 요구사항 기반으로 실무 수준의 TC 작성
- 이슈 유형별 분기 (Bug / Story / Task / Epic)

### 생성 TC 품질
- 테스트유형 자동 분류: 기능 / 예외처리 / 경계값 / 회귀 / 보안
- 기대결과 형식 통일: `~됨` 으로 끝나는 검증 가능한 문장
- 우선순위 자동 분류 (High / Medium / Low) + 색상 표시
- 결과 입력 드롭다운 자동 생성 (P / F / N/A)

### 서비스 컨텍스트 주입
- `contexts/{서비스명}.md` 파일에 도메인 정보 작성
- `--context kream` 플래그로 TC 생성 시 반영
- AI가 서비스 특화 TC를 생성할 수 있도록 가이드

### 구글 시트 자동 포맷
- 헤더 색상, 열 너비, URL 하이퍼링크 자동 설정
- 티켓 제목으로 시트명 생성
- 이미 있는 시트는 초기화 후 재생성 (중복 방지)

---

## 사용 기술

| 항목 | 기술 |
|------|------|
| AI 모델 | Groq API (LLaMA 3.3 70B) |
| 이슈 관리 | Jira REST API |
| 결과 저장 | Google Sheets API (gspread) |
| 알림 | Slack Incoming Webhook |
| CI/CD | GitHub Actions (10분 스케줄) |
| 언어 | Python 3.11 |

---

## 프로젝트 구조

```
AutoTC/
├── src/
│   ├── watch_sheet.py       # 구글 시트 폴링 + TC 자동 생성 (메인)
│   ├── generate_tc.py       # 단일/일괄 TC 생성 (CLI)
│   └── generate_context.py  # 서비스 컨텍스트 초안 AI 생성
├── contexts/
│   └── kream.md             # 서비스 컨텍스트 예시
├── reports/                 # TC JSON, 엑셀 출력
├── .github/workflows/
│   └── watch.yml            # GitHub Actions 스케줄 실행
├── .env.example
└── requirements.txt
```

---

## 실행 방법

### 로컬 실행

```bash
# 1. 패키지 설치
pip install -r requirements.txt

# 2. .env 설정
cp .env.example .env
# .env 파일에 API 키 입력

# 3. 구글 시트 폴링 실행
python src/watch_sheet.py

# 4. 단일 티켓 TC 생성 (CLI)
python src/generate_tc.py MKQA-1
python src/generate_tc.py MKQA-1 --context kream
```

### GitHub Actions 자동 실행

1. 레포 **Settings → Secrets** 에 아래 값 설정

| Secret | 설명 |
|--------|------|
| `GROQ_API_KEY` | Groq API 키 |
| `JIRA_URL` | Jira 도메인 |
| `JIRA_EMAIL` | Jira 계정 이메일 |
| `JIRA_API_TOKEN` | Jira API 토큰 |
| `JIRA_PROJECT_KEY` | 프로젝트 키 (예: MKQA) |
| `SPREADSHEET_ID` | 구글 스프레드시트 ID |
| `GOOGLE_CREDENTIALS_JSON` | 서비스 계정 credentials.json 전체 내용 |
| `SLACK_WEBHOOK_URL` | Slack Incoming Webhook URL |

2. 구글 시트 `티켓 입력` 탭 A열에 티켓 키 입력 → 10분 내 TC 자동 생성

---

## 구글 시트 TC 출력 예시

| TC ID | 테스트유형 | 테스트 항목 | 사전 조건 | 테스트 단계 | 기대 결과 | 결과 | 우선순위 |
|-------|-----------|------------|----------|------------|---------|------|--------|
| TC-001 | 기능 | 정상 로그인 | 가입된 계정 존재 | 1. 이메일 입력<br>2. 비밀번호 입력<br>3. 로그인 버튼 클릭 | 홈 화면으로 이동됨 | P/F/N/A | High |
| TC-002 | 예외처리 | 잘못된 비밀번호 입력 | 가입된 계정 존재 | 1. 이메일 입력<br>2. 잘못된 비밀번호 입력<br>3. 로그인 버튼 클릭 | 에러 메시지가 표시됨 | P/F/N/A | High |

---

## Before / After TC 품질 비교

| 항목 | 기존 (수동 작성) | AutoTC (AI 생성) |
|------|----------------|-----------------|
| TC 초안 작성 시간 | 티켓당 20~30분 | 1~2분 (검토 포함) |
| 테스트유형 분류 | 작성자 재량 | 자동 분류 (5가지) |
| 기대결과 형식 | 불규칙 | `~됨` 형식 통일 |
| 예외/경계값 케이스 | 누락 많음 | AI가 자동 포함 |
| 우선순위 | 수동 지정 | 자동 분류 + 색상 |

---

## 한계 및 주의사항

- AI가 서비스 내부 비즈니스 룰은 모름 → `contexts/` 파일에 직접 입력 필요
- 생성된 TC는 초안 수준 (검토 후 사용 권장)
- 공개된 서비스 정보 범위 내에서만 AI에 전달 (내부 기밀 정보 입력 금지)
