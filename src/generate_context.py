"""
서비스 컨텍스트 문서 자동 생성 스크립트
AI가 서비스를 분석해 TC 생성에 활용할 컨텍스트 초안을 만들어줍니다.

사용법:
  # 서비스명만으로 생성 (AI 지식 기반)
  python src/generate_context.py --name kream

  # URL 추가 분석
  python src/generate_context.py --name kream --url https://kream.co.kr

  # 특정 기능 영역 명시
  python src/generate_context.py --name kream --feature "스타일 탭 사진 등록"
"""

import argparse
import os
import sys
import io
from datetime import datetime

import requests
from groq import Groq
from dotenv import load_dotenv

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
load_dotenv()

groq_client = Groq(api_key=os.getenv("GROQ_API_KEY"))
CONTEXTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "contexts")

# URL에서 텍스트 내용을 가져오는 함수
def fetch_url_text(url: str) -> str:
    """URL에서 텍스트 내용을 가져옵니다."""
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        # 간단하게 HTML 태그 제거
        import re
        text = re.sub(r"<[^>]+>", " ", resp.text)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:3000]  # 앞부분 3000자만 사용
    except Exception as e:
        print(f"  [경고] URL 페치 실패: {e}")
        return ""

# Groq AI로 컨텍스트 문서 생성
def generate_context(service_name: str, url_text: str = "", feature: str = "") -> str:
    """Groq AI로 서비스 컨텍스트 문서를 생성합니다."""

    url_section = f"\n\n[URL에서 가져온 내용]\n{url_text}" if url_text else ""
    feature_section = f"\n\n[주목할 기능 영역]\n{feature}" if feature else ""

    response = groq_client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[
            {
                "role": "system",
                "content": (
                    "당신은 QA 엔지니어입니다. "
                    "서비스 정보를 분석해 테스트 케이스 작성에 활용할 컨텍스트 문서를 작성합니다. "
                    "공개적으로 알 수 있는 정보만 작성하고, 내부 로직/정책은 '확인 필요'로 표시하세요. "
                    "한국어로 작성하세요."
                ),
            },
            {
                "role": "user",
                "content": f"""다음 서비스에 대한 QA 테스트 컨텍스트 문서를 작성해주세요.

서비스명: {service_name}{url_section}{feature_section}

아래 형식으로 작성하세요. 확실하지 않은 내용은 "(확인 필요)"로 표시하세요.

## 서비스 개요
- 서비스 유형:
- 주요 플랫폼: (iOS / Android / Web)
- 주요 사용자:

## 주요 기능
(핵심 기능 목록)

## 공통 제약사항
- 지원 OS:
- 지원 파일 형식: (파일 업로드 기능이 있는 경우)
- 최대 파일 크기: (확인 필요)
- 로그인 필요 기능:

## 테스트 시 주의사항
- 플랫폼별 차이점:
- 자주 발생하는 이슈 유형:
- 회귀 테스트 필요 영역:

## 내부 정책 (QA가 직접 채워야 할 항목)
- 비밀번호 조건: (확인 필요)
- 최소/최대 주문 금액: (확인 필요)
- 기타 비즈니스 룰: (확인 필요)""",
            },
        ],
    )
    return response.choices[0].message.content.strip()


def save_context(service_name: str, content: str) -> str:
    """컨텍스트 문서를 파일로 저장합니다."""
    os.makedirs(CONTEXTS_DIR, exist_ok=True)
    filename = f"{service_name.lower().replace(' ', '_')}.md"
    filepath = os.path.join(CONTEXTS_DIR, filename)

    header = f"""---
service: {service_name}
generated: {datetime.now().strftime('%Y-%m-%d')}
note: AI 생성 초안입니다. 내부 정책/비즈니스 룰은 직접 확인 후 채워주세요.
---

"""
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(header + content)

    return filepath


def main():
    parser = argparse.ArgumentParser(description="서비스 컨텍스트 문서 자동 생성")
    parser.add_argument("--name", required=True, help="서비스명 (예: kream, kurly)")
    parser.add_argument("--url", default="", help="서비스 URL (선택)")
    parser.add_argument("--feature", default="", help="주목할 기능 영역 (선택)")
    args = parser.parse_args()

    print(f"\n=== '{args.name}' 컨텍스트 문서 생성 중 ===\n")

    url_text = ""
    if args.url:
        print(f"  URL 분석 중: {args.url}")
        url_text = fetch_url_text(args.url)
        print(f"  URL 텍스트 {len(url_text)}자 추출 완료")

    print("  AI 컨텍스트 생성 중...")
    content = generate_context(args.name, url_text, args.feature)

    filepath = save_context(args.name, content)
    print(f"\n✅ 저장 완료: {filepath}")
    print("\n[생성된 내용 미리보기]")
    print("-" * 50)
    print(content[:800] + "..." if len(content) > 800 else content)
    print("-" * 50)
    print(f"\n💡 '(확인 필요)' 항목은 직접 채워주세요.")
    print(f"   TC 생성 시: python src/generate_tc_from_url.py TICKET-1 --context {args.name}")


if __name__ == "__main__":
    main()
