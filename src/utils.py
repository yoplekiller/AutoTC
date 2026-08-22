import re


def rate_limit_wait_seconds(exception: Exception, attempt: int, base: int = 65) -> float:
    """Groq 429 응답에서 실제 권장 대기시간을 추출한다. "Please try again in 11.25s"(초만) 형식과
    "Please try again in 1m5.234s"(분+초) 형식을 둘 다 지원한다.
    응답에 없으면 기존 고정 백오프(base * (attempt+1))로 폴백한다.
    실제 권장 시간은 보통 고정 백오프보다 훨씬 짧아서, 있으면 그대로 쓰는 게 재시도를 훨씬 빠르게 만든다."""
    match = re.search(r"try again in (?:(\d+)m)?([\d.]+)s", str(exception), re.IGNORECASE)
    if match:
        minutes = float(match.group(1)) if match.group(1) else 0.0
        seconds = float(match.group(2))
        return minutes * 60 + seconds + 1  # 여유 1초
    return base * (attempt + 1)


def sanitize(data):
    """AI 응답에서 한글/영문/숫자/기본 특수문자 외 문자(한자, 러시아어 등)를 제거합니다."""
    if isinstance(data, str):
        # "toBeVisible" 관련 문장에서 반복 관측된 글자 누락 패턴("비isible") 보정
        data = re.sub(r"비\s*[Ii]sible", "비visible", data)
        return re.sub(
            r"[^가-힣ᄀ-ᇿ㄰-㆏"
            r"a-zA-Z0-9\s\.\,\!\?\(\)\[\]\{\}\-\_\/\:\;\'\"\n\t\%\&\@\#\+\=\<\>]",
            "",
            data,
        ).strip()
    elif isinstance(data, list):
        return [sanitize(item) for item in data]
    elif isinstance(data, dict):
        return {k: sanitize(v) for k, v in data.items()}
    return data
