# AutoTC — AI 기반 QA 업무 자동화 툴킷

> Jira 티켓 → TC 자동 생성, Playwright 테스트 결과 → 릴리즈 판단까지  
> AI(Groq LLaMA 3.3 70B)가 QA의 "판단 업무"를 자동화하는 툴킷  
> TC 작성은 AI가, QA는 검토와 의사결정에 집중

## 구성 요소

| 영역 | 기능 | 핵심 파일 |
|------|------|----------|
| **TC 생성** | Jira 티켓 → 스펙 추론 → 유형별 TC 자동 생성 | `generate_tc.py`, `watch_sheet.py` |
| **릴리즈 판단** | Playwright 테스트 결과 → 실패 패턴 분석 → Go/Caution/No-Go 권고 | `release_report.py` |
| **협업 자동화** | Slack 슬래시 커맨드로 기획서·회의록·Jira 티켓 생성 | `slack_app.py`, `app.py`, `create_ticket.py`, `generate_minutes.py`, `generate_test_plan.py` |

아래 내용은 핵심 모듈인 **TC 생성 파이프라인** 기준 설명입니다. 릴리즈 판단 파이프라인은 [QA Ops 파이프라인](#release_reportpy--릴리즈-판단-파이프라인) 섹션 참고.

---

## 성과 요약

- **처리 티켓**: MKQA 프로젝트 기준 6개 티켓 (MKQA-1, 37, 39, 40, 41, 42)
- **생성 TC**: 136개
- **TC 작성 시간**: 티켓당 20~30분 → **1~2분** (검토 포함)
- **지원 테스트 유형**: 기능 / 예외처리 / 경계값 / 회귀 / 보안 / UI/UX

---

## 왜 만들었나

| 문제 | 해결 |
|------|------|
| TC 초안 작성에 티켓당 20~30분 소요 | AI가 초안 생성 → QA는 검토만 |
| 티켓 설명이 부실해 TC 범위 판단 어려움 | AI가 요구사항 먼저 추론 후 TC 생성 |
| 수동 작성 시 예외/경계값 케이스 누락 빈번 | 유형별 분리 생성으로 체계적 커버리지 확보 |
| 서비스마다 비즈니스 룰이 달라 범용 TC 품질 낮음 | `contexts/` 파일로 서비스별 도메인 지식 주입 |

---

## 파이프라인 흐름

```
구글 시트 '티켓 입력' 탭에 Jira 티켓 키 입력
              ↓
watch_sheet.py 폴링 (로컬 or GitHub Actions 10분 주기)
              ↓
Jira REST API로 티켓 정보 조회
              ↓
[1단계] Groq AI — 티켓 설명 보완 (요구사항 추론)
              ↓
[2단계] Groq AI — 스펙 복잡도 분석 → 유형별 TC 수 동적 결정
              ↓
[3단계] Groq AI — 유형별 분리 TC 생성
         기능 / 예외처리 / 경계값 / 회귀 / 보안 / UI/UX
              ↓
구글 시트에 티켓별 시트 자동 생성 (포맷 포함)
```

---

## 핵심 설계: 동적 TC 플랜

TC 수를 고정값으로 정하지 않고, **AI가 스펙 복잡도를 분석해 유형별 수량을 직접 결정**합니다.

```
티켓 복잡도 분석
    ↓
[AI 플랜] {"기능": 8, "예외처리": 7, "경계값": 6, "회귀": 6, "보안": 5, "UI/UX": 4}
    ↓
유형별 분리 호출 후 합산 → 총 36개 TC
```

- 단순 티켓: 유형별 5~6개 수준
- 인증/결제/복잡한 정책 포함 티켓: 유형별 8~10개 수준
- 보안/UI/UX: 해당 없으면 0, 인증 포함 시 5개 이상 자동 배정

---

## 주요 기능

### AI 3단계 파이프라인
- **1단계 (요구사항 추론)**: 설명이 빈약한 티켓도 기능 목적 / 요구사항 / 예외 케이스를 먼저 추론
- **2단계 (동적 플랜)**: 스펙 복잡도 기반으로 유형별 TC 수 자동 결정 (고정값 아님)
- **3단계 (유형별 생성)**: 유형별 분리 호출로 중복 없는 고유 TC 생성

### TC 품질 관리
- 기대결과 형식 통일: `~됨` / `~함`으로 끝나는 검증 가능한 문장
- 우선순위 자동 분류 (High / Medium / Low) + 색상 표시
- 결과 입력 드롭다운 자동 생성 (P / F / N/A)
- 각 TC는 서로 중복되지 않는 고유 시나리오 강제

### 서비스 컨텍스트 주입
- `contexts/{서비스명}.md`에 도메인 정보 작성
- `--context kream` 플래그로 서비스 특화 TC 생성
- 크림(KREAM), 마켓컬리 컨텍스트 내장

### 구글 시트 자동 포맷
- 헤더 색상, 열 너비, URL 하이퍼링크 자동 설정
- 티켓 제목으로 시트명 자동 생성
- 기존 시트 초기화 후 재생성 (중복 방지)

---

## 기술적 챌린지

실제 개발 중 마주한 문제들과 해결 방법입니다.

### 1. Groq Rate Limit 자동 처리
**문제**: 분당 한도 초과(429)와 일일 한도 초과(TPD)가 동일한 에러 코드로 내려옴 → 일일 한도인데도 65초씩 3번 재시도 낭비 후 빈 배열 반환

**해결**: 에러 메시지에서 `"per day"` / `"tpd"` / `"tokens_per_day"` 키워드 감지 시 `DailyTokenLimitError`로 즉시 분기 → 재시도 없이 지금까지 생성된 TC만 저장 후 종료

```python
if "per day" in e_str or "tpd" in e_str or "tokens_per_day" in e_str:
    raise DailyTokenLimitError("일일 토큰 한도 초과")  # 재시도 없이 즉시 종료
```

### 2. Windows 환경 인코딩 오류
**문제**: AI가 가끔 일본어/베트남어 문자를 섞어 생성 → Windows 로컬에서 `cp949` 인코딩 오류로 시트 저장 전 크래시

**해결**: 진입점에서 stdout을 UTF-8로 강제 패치

```python
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
```

### 3. Google Sheets 드롭다운 잔존 버그
**문제**: `ws.clear()`로 시트를 초기화해도 데이터 유효성 검사(드롭다운)는 삭제되지 않음 → 재생성 시 중복 드롭다운 누적

**해결**: `spreadsheets.batchUpdate`로 전체 시트를 초기화한 뒤 K열(테스트 상태)만 재설정

### 4. AI가 TC 수 최솟값만 반환하는 문제
**문제**: 프롬프트에 "최솟값을 지킬 것"이라고 명시하자 AI가 항상 최솟값만 반환

**해결**: "최솟값" 표현 제거 → "이상" 목표값 + 복잡도별 추가 배정 기준으로 프롬프트 재설계. 코드 강제값도 제거하고 `max(ai_n, 1)`로 0개 방어만 유지

---

## 사용 기술

| 항목 | 기술 |
|------|------|
| AI 모델 | Groq API (LLaMA 3.3 70B) |
| 이슈 관리 | Jira REST API |
| 결과 저장 | Google Sheets API (gspread) |
| CI/CD | GitHub Actions (10분 스케줄) |
| 언어 | Python 3.13 |

---

## 프로젝트 구조

```
AutoTC/
├── src/
│   ├── generate_tc.py        # 단일/일괄 TC 생성 (CLI) — 동적 플랜 포함
│   ├── watch_sheet.py        # 구글 시트 폴링 + TC 자동 생성 (메인)
│   ├── release_report.py     # Playwright 결과 → AI 릴리즈 판단 → Slack 전송
│   ├── generate_spec.py      # 티켓 기반 기획서 자동 생성
│   ├── generate_context.py   # 서비스 컨텍스트 초안 AI 생성
│   ├── generate_test_plan.py # 티켓 묶음 → Confluence 테스트 계획서 생성
│   ├── generate_minutes.py   # 회의록 자동 생성
│   ├── create_ticket.py      # 자연어 → Jira 티켓 자동 생성
│   ├── slack_app.py          # Slack 슬래시 커맨드 (/tc, /review, /spec-review)
│   └── utils.py              # 공통 유틸
├── app.py                    # Slack 커맨드 서버 (Flask, 로컬 + ngrok)
├── contexts/
│   ├── kream.md               # 크림 서비스 컨텍스트
│   └── kurly.md                # 마켓컬리 서비스 컨텍스트
├── reports/                   # TC JSON, 엑셀 출력
├── .github/workflows/
│   └── watch.yml              # GitHub Actions 스케줄 실행 (TC 생성)
├── .env.example
└── requirements.txt
```

---

## 실행 방법

### 로컬 실행

```bash
# 1. 패키지 설치
pip install -r requirements.txt

# 2. 환경변수 설정
cp .env.example .env
# .env에 API 키 입력

# 3. 단일 티켓 TC 생성
python src/generate_tc.py MKQA-1
python src/generate_tc.py MKQA-1 --context kream

# 4. 엑셀 일괄 처리 (A열에 티켓 키 목록)
python src/generate_tc.py --template        # 입력 템플릿 생성
python src/generate_tc.py tickets.xlsx --context kream

# 5. 구글 시트 폴링 실행
python src/watch_sheet.py
```

### GitHub Actions 자동 실행

레포 **Settings → Secrets**에 아래 값 설정 후 구글 시트 `티켓 입력` 탭 A열에 티켓 키 입력 → 10분 내 TC 자동 생성

| Secret | 설명 |
|--------|------|
| `GROQ_API_KEY` | Groq API 키 |
| `JIRA_URL` | Jira 도메인 |
| `JIRA_EMAIL` | Jira 계정 이메일 |
| `JIRA_API_TOKEN` | Jira API 토큰 |
| `JIRA_PROJECT_KEY` | 프로젝트 키 (예: MKQA) |
| `SPREADSHEET_ID` | 구글 스프레드시트 ID |
| `GOOGLE_CREDENTIALS_JSON` | 서비스 계정 credentials.json 전체 내용 |

---

## TC 출력 컬럼

| TC ID | 대분류 | 소분류 | 테스트유형 | 우선순위 | 테스트 시나리오 | 사전 조건 | 테스트 단계 | 기대 결과 | 실제 결과 | 테스트 상태 | 비고 | 버그 링크 |
|-------|--------|--------|-----------|---------|--------------|---------|------------|---------|---------|-----------|------|---------|

---

## Before / After

| 항목 | 기존 (수동 작성) | AutoTC (AI 생성) |
|------|----------------|-----------------|
| TC 초안 작성 시간 | 티켓당 20~30분 | 1~2분 (검토 포함) |
| 테스트유형 분류 | 작성자 재량 | 6가지 유형 자동 분류 |
| 기대결과 형식 | 불규칙 | `~됨` 형식 통일 |
| 예외/경계값 케이스 | 누락 빈번 | AI가 유형별 자동 포함 |
| TC 수량 결정 | 작성자 감 | 스펙 복잡도 기반 동적 결정 |
| 우선순위 | 수동 지정 | High/Medium/Low 자동 분류 |

---

## release_report.py — 릴리즈 판단 파이프라인

TC 생성과는 독립된 모듈로, Playwright 자동화 테스트 결과를 받아 **릴리즈 가능 여부를 AI가 1차 판단**합니다.

```
Playwright 실행 (--reporter=json)
    ↓
results.json 파싱 — 통과/실패/건너뜀, 실패 테스트명 + 에러 메시지
    ↓
Groq AI 분석 — 실패 원인 패턴 → 릴리즈 권고(Go/Caution/No-Go) + 한 줄 요약
    ↓
Slack Block Kit 리포트 전송 (실패 상세 + Playwright Report 링크)
```

```bash
python src/release_report.py playwright-report/results.json
```

[PlaywrightQA](https://github.com/yoplekiller/PlaywrightQA) 레포의 GitHub Actions에서 테스트 종료 후 자동 호출되도록 연동되어 있습니다 (`actions/checkout`으로 본 레포를 함께 체크아웃).

---

## 한계 및 주의사항

- AI가 서비스 내부 비즈니스 룰은 모름 → `contexts/` 파일에 직접 입력 필요
- 생성된 TC는 초안 수준 (QA 검토 후 사용 권장)
- Groq 무료 플랜 기준 하루 10만 토큰 한도 (티켓 3~4개/일)
- 내부 기밀 정보는 AI에 전달하지 말 것
