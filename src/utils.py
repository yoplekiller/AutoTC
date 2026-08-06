import re


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
