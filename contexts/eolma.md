---
service: eolma-budget
generated: 2026-08-23
note: 실제 코드/작업이력 기반 컨텍스트 — 코드+과거버그 주입이 TC 품질에 미치는 영향을 확인하기 위한 실험용 파일
---

## 서비스 개요
- 서비스 유형: Apps in Toss WebView 미니앱 (React + TypeScript + Vite)
- "다음 월급날까지 하루 얼마 쓸 수 있는지" 계산해주는 예산 관리 앱
- 실제 설정 파일은 `apps-in-toss.config.ts`(SDK 3.x)이다. `toss.config.js`, `manifest.json`은 이 프로젝트에 존재하지 않는다.
- SDK 초기화는 별도 `initializeSdk()` 함수 호출이 아니라, `main.tsx`에서 앱 전체를 `<TDSMobileAITProvider>`로 감싸는 방식이다.
- 권한 선언 자체가 없다(카메라/위치 등 요구 안 함) — "불필요한 권한 검사"는 이 프로젝트엔 해당 없는 항목이다.

## 실제 코드 구조 (domain/ — UI 의존성 없는 순수 로직, Vitest로 전량 커버)
- `budget.ts`: `usableBudget = startingBudget - reservedExpense`(클램핑 없음, 음수 허용). `remainingBudget = usableBudget - totalExpenses`. `dailyBudget()`은 `Math.floor(remaining / days)`로 원단위 버림. `isPeriodEnded`는 `remainingDays <= 0`(기준일 당일부터 종료). `referenceDay`는 최초 설정 시 한 번 정해지면 갱신에도 불변, `nextPaydayFrom()`이 짧은 달은 마지막 날로 클램프.
- `date.ts`: `parseYMD`/`formatYMD`가 로컬 자정 기준(new Date("YYYY-MM-DD")의 UTC 파싱 버그 회피).
- `expense.ts`: `expensesInPeriod`는 `date >= startDate && date < payday`로 필터링(payday 당일은 다음 기간).

## 화면 4개 (SetupScreen / HomeScreen / ExpenseFormScreen / HistoryScreen)
- SetupScreen: 최초 설정("보유 금액"+"미리 뺄 금액"+"기준일") / 진행 중 수정 / 갱신("지난 기간 남은 생활비" 자동계산+"새로 들어온 금액").
- HomeScreen: 기간 종료 시(`isPeriodEnded`) 전용 종료 화면으로 전환되고 지출 등록 버튼이 사라짐(과거 이 처리가 없어서 실버그 발생했었음, 아래 참고).
- ExpenseFormScreen: 금액 1원 미만 등록 차단. **날짜 필드에 범위 제한 없음**(기준일 입력엔 "오늘 이후" 검증이 있지만 지출 날짜엔 없음 — 정책 미확정 지점).
- HistoryScreen: 기본 현재 기간, "이전 기간" 순회, "직접 선택"(임의 날짜 범위, 홈 계산과 무관), 삭제는 `ConfirmDialog`로 확인.

## 실제 발생했던 버그 / 회귀 이력 (테스트 우선순위에 반드시 반영할 것)
1. `dailyBudget()`이 소수점을 버리지 않던 버그 — 도메인 함수 자체가 스펙(원단위만 사용) 위반이었음. 수정 완료.
2. 지출이 기간별로 필터링 안 되고 전체 합산되던 버그 — `expensesInPeriod` 도입으로 수정.
3. 기간 종료 후에도 홈이 이전 값 그대로 노출되고 지출 등록 버튼이 남아있던 버그 — 등록해도 집계에서 조용히 사라지는 실질 결함이었음. 전용 종료 화면으로 교체.
4. `crypto.randomUUID()`가 secure context(HTTPS/localhost)에서만 존재 — plain HTTP(LAN IP 실기기 테스트)에서 지출 등록/설정 완료 버튼이 조용히 무반응이었음. `generateId()` 폴백으로 수정.
5. 네이티브 뒤로가기(`graniteEvent.backEvent`)를 구독 안 해서, 홈이 아닌 화면에서 뒤로가기 시 앱이 즉시 종료되던 버그. 지금은 홈으로 복귀/홈에서만 종료.
6. 삭제 확인에 `window.confirm()`을 쓰다가 TDS 모달 가이드 위반 + 일부 WebView에서 무시될 위험 발견 → `ConfirmDialog`로 교체.
7. 실기기 WebView에서 날짜 `<input type="date">`의 네이티브 화살표를 숨기는 CSS 트릭이 인식 안 돼 화살표가 이중으로 보이던 버그 → 완전 커스텀 박스 + 투명 오버레이 패턴으로 교체.
8. eruda 디버그 콘솔이 프로덕션에 남아있었고 핀치줌 차단 설정이 없었음(둘 다 앱인토스 비게임 출시 가이드 위반) → 제거/추가.

## 테스트 시 주의사항
- 로컬 `npm run dev`는 `@apps-in-toss/devtools`가 WebView 환경을 모킹한다. `getSafeAreaInsets` 관련 콘솔 경고는 이 모킹 환경 특유의 무해한 경고이며 실제 버그가 아니다(라이브러리 자체가 try/catch로 폴백 처리함).
- 로컬 스토리지는 devtools 모킹 시 `window.localStorage`가 아니라 `@apps-in-toss/web-framework`의 `Storage`로 감싸이며, dev 서버 재시작 시 초기화되는 인메모리 저장소다. "재실행 후 데이터 유지"를 검증하려면 실기기(정식 Toss 앱)에서 해야 하며, dev 서버 재시작으로는 검증되지 않는다.
- 이 프로젝트는 git 저장소가 아니다(파일이 곧 저장 상태). "커밋"이라는 개념 자체가 없다.

## 내부 정책 (아직 확정 안 된 항목 — TC 작성 시 임의로 기대결과를 정하지 말 것)
- 지출 등록 날짜가 현재 예산 기간(시작일~기준일 전날) 밖이어도 되는지: 등록 자체를 막을지, 경고만 줄지, 지금처럼 조용히 다른 기간 집계로 흡수되게 둘지 미정.
