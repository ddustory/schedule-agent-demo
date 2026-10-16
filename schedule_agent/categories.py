"""일정 종류 나누기 + 다른 사람 이름 가리기."""
from __future__ import annotations

import re

CATEGORIES = ["수업", "대학 강의", "대타", "도서", "스케줄 변동", "약속", "기타"]
# 앞에 있을수록 우선 (예: '대타 근무 변경' → 대타, '스케줄 변동: 수업 시간 변경' → 스케줄 변동)
_PRIORITY = ["대타", "스케줄 변동", "도서", "대학 강의", "수업", "약속"]


def classify(title: str, keywords: dict[str, list[str]], title_map: dict[str, str] | None = None) -> str:
    """1) 사용자가 '일정 종류 정하기'에서 정한 제목이면 그대로  2) 아니면 제목 속 단어로."""
    if title_map:
        cat = title_map.get(title.strip())
        if cat in CATEGORIES:
            return cat
    t = title.lower()
    for cat in _PRIORITY:
        for kw in keywords.get(cat, []):
            if kw.lower() in t:
                return cat
    return "기타"


def split_absent(title: str) -> tuple[str, bool]:
    """'결석국어음운론' → ('국어음운론', True). 결석 표시가 없으면 (제목, False)."""
    if "결석" not in title:
        return title.strip(), False
    base = title.replace("결석", " ")
    base = re.sub(r"^[\s\-:·()\[\]]+|[\s\-:·()\[\]]+$", "", re.sub(r"\s+", " ", base))
    return base or title.strip(), True


# '민수랑', '김OO님', '지영이와' 처럼 사람을 가리키는 말 앞의 이름을 ○○ 로 바꾼다.
_NAME_BEFORE = re.compile(
    r"([가-힣A-Za-z]{1,4}?)(이랑|랑|이와|와|과|하고|님|씨|쌤|선배|후배|언니|오빠|누나|형)(?=[\s,.)]|$)")
_PHONE = re.compile(r"01[016789][-\s.]?\d{3,4}[-\s.]?\d{4}")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
# 이름으로 오인하기 쉬운 일반 단어
_NOT_NAMES = {"친구", "가족", "엄마", "아빠", "동생", "학생", "학원", "선생", "회사", "팀", "동기", "도서관", "모임"}


def mask_people(text: str) -> str:
    if not text:
        return text
    text = _PHONE.sub("☎(가림)", text)
    text = _EMAIL.sub("✉(가림)", text)

    def repl(m: re.Match) -> str:
        name = m.group(1)
        if name in _NOT_NAMES:
            return m.group(0)
        return "○○" + m.group(2)

    return _NAME_BEFORE.sub(repl, text)
