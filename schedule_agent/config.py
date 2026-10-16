"""경로·설정·시간대.

실제 데이터는 전부 private/ 폴더(깃에 안 올라감)에 둔다.
private/ 에 캘린더 파일이 없으면 samples/ 의 가짜 예시로 '체험 모드'로 돈다.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Seoul")

ROOT = Path(__file__).resolve().parent.parent
PRIVATE = ROOT / "private"
SAMPLES = ROOT / "samples"

SETTINGS_FILE = PRIVATE / "settings.json"
TODOS_FILE = PRIVATE / "todos.json"
ADDED_EVENTS_FILE = PRIVATE / "added_events.json"
CHANGE_LOG_FILE = PRIVATE / "change_log.jsonl"
EXPORT_DIR = PRIVATE / "exports"
# 꾸미기용 캐릭터 그림. 저작권이 있는 그림일 수 있으니 GitHub에 올리지 않고 private/ 에만 둔다.
STICKERS_DIR = PRIVATE / "stickers"
STICKER_SLOTS = {
    "study": "공부하는 모습 → 수업·도서",
    "fork": "신난 모습 → 약속",
    "munch": "먹는 모습 → 쉬는 시간·빈 시간",
    "peek": "빼꼼 보는 모습 → 스케줄 변동·일정 없는 날",
    "run": "달리는 모습 → 대타·바쁜 날",
}

# 아래 값은 '임시 기본값'이다. 생활 방식에 관한 값이라 사용자가 직접 정해야 한다.
# (설정 화면의 '처음 질문'에 답하면 private/settings.json 에 저장된다.)
DEFAULT_SETTINGS: dict = {
    "answered": False,                # 처음 질문에 답했는지
    "library_loan_days": 14,          # 도서 대출 기간(일)
    "class_fixed": "",                # 학원 수업 고정 요일·시간 (자유 입력)
    "reminders": {                    # 종류별 알림 (일정 시작 몇 분 전)
        "수업": [60],
        "대학 강의": [30],
        "약속": [120],
        "대타": [180],
        "도서": [1440],               # 반납일 전날
        "스케줄 변동": [60],
        "기타": [30],
    },
    "substitute_rule": "",            # 대타 받는 기준 (자유 입력)
    "travel_minutes": 30,             # 보통 잡는 이동 시간(분)
    "daily_plan_time": "08:00",       # 하루 계획 받을 시각
    "day_start": "09:00",             # 계획을 세울 하루 시작
    "day_end": "23:00",               # 계획을 세울 하루 끝
    "busy_hours_warning": 9,
    "week_start": "일",               # 주간·월간 화면에서 한 주가 시작하는 요일 (월 | 일) — 사용자가 '일'로 정함
    "absence_fail_count": 4,          # 대학 강의 결석이 이 횟수가 되면 F (사용자 확인: 모든 과목 4번)
    "semester_start": "",             # 이번 학기 시작일 YYYY-MM-DD (비우면 3/1 또는 9/1로 계산. 사용자 확인: 2학기 개강 9/1 맞음)          # 하루 일정이 이 시간을 넘으면 '빽빽함' 경고
    "category_keywords": {            # 일정 제목으로 종류를 나누는 단어들
        "수업": ["수업", "학원", "레슨", "클래스"],
        "대학 강의": ["강의", "수강", "교수님", "대학"],
        "대타": ["대타", "대신 근무", "대근"],
        "도서": ["도서", "대출", "반납", "도서관", "책"],
        "스케줄 변동": ["변동", "변경", "스케줄", "근무", "시간 바뀜", "휴무"],
        "약속": ["약속", "저녁", "점심", "만남", "모임", "카페", "식사", "랑", "와 "],
    },
    # 제목 → 종류 (설정의 '일정 종류 정하기'에서 정함). 예) {"수학": "수업", "문학사2": "대학 강의"}
    "title_category": {},
    "llm": {
        "backend": "auto",            # auto | rule | ollama | claude
        "ollama_model": "qwen2.5:7b",
        "claude_model": "claude-opus-5-5",
    },
}


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            return _merge(DEFAULT_SETTINGS, json.loads(SETTINGS_FILE.read_text("utf-8")))
        except (OSError, json.JSONDecodeError):
            pass
    return copy.deepcopy(DEFAULT_SETTINGS)


REPLACE_KEYS = ("title_category",)  # 합치지 않고 통째로 바꾸는 값 (지운 항목이 남지 않게)


def save_settings(new: dict) -> dict:
    PRIVATE.mkdir(exist_ok=True)
    merged = _merge(load_settings(), {k: v for k, v in new.items() if k not in REPLACE_KEYS})
    for k in REPLACE_KEYS:
        if k in new:
            merged[k] = new[k]
    SETTINGS_FILE.write_text(json.dumps(merged, ensure_ascii=False, indent=2), "utf-8")
    return merged


def calendar_files() -> list[Path]:
    """내 캘린더 원본 .ics 들 (private/ 바로 아래 또는 private/calendar/). 없으면 빈 목록.

    에이전트가 만든 내보내기 파일(exports/)이나 체험 모드 폴더(demo/)는 원본이 아니므로 제외한다.
    """
    if not PRIVATE.exists():
        return []
    found = list(PRIVATE.glob("*.ics")) + list((PRIVATE / "calendar").rglob("*.ics"))
    return sorted(p for p in found if not {"exports", "demo"} & set(p.relative_to(PRIVATE).parts))


def is_demo_mode() -> bool:
    return not calendar_files()


def now() -> datetime:
    return datetime.now(TZ)
