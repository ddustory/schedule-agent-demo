"""한국어 날짜·시간 표현을 코드로 정확히 계산한다.

"다음 주 화요일", "모레", "오후 7시 반" 같은 말을 AI가 짐작하지 않고
여기서 계산한다. 애매하면 추측하지 않고 questions 에 '물어볼 것'을 담는다.

주(週)는 월요일에 시작한다고 본다. 예) 오늘이 목요일이면
  '이번 주 화요일' = 이미 지난 이번 주 화요일, '다음 주 화요일' = 다음 주 월~일 중 화요일.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

from .config import TZ

WD = {"월": 0, "화": 1, "수": 2, "목": 3, "금": 4, "토": 5, "일": 6}
WD_NAMES = "월화수목금토일"


def weekday_ko(d: date) -> str:
    return WD_NAMES[d.weekday()] + "요일"


def fmt_date(d: date) -> str:
    return f"{d.year}년 {d.month}월 {d.day}일({WD_NAMES[d.weekday()]})"


@dataclass
class When:
    date: date | None = None
    start: time | None = None
    end: time | None = None
    duration_min: int | None = None
    questions: list[str] = field(default_factory=list)   # 애매해서 물어볼 것
    notes: list[str] = field(default_factory=list)       # 어떻게 계산했는지
    rest: str = ""                                       # 날짜·시간을 뺀 나머지 글

    def to_dict(self) -> dict:
        return {
            "date": self.date.isoformat() if self.date else None,
            "date_text": fmt_date(self.date) if self.date else None,
            "start": self.start.strftime("%H:%M") if self.start else None,
            "end": self.end.strftime("%H:%M") if self.end else None,
            "duration_min": self.duration_min,
            "questions": self.questions,
            "notes": self.notes,
            "rest": self.rest,
        }


_REL_DAYS = {"오늘": 0, "금일": 0, "내일": 1, "명일": 1, "모레": 2, "내일모레": 2, "글피": 3, "어제": -1, "그저께": -2}
_NUM_KO = {"한": 1, "두": 2, "세": 3, "네": 4, "다섯": 5, "여섯": 6, "일곱": 7, "여덟": 8, "아홉": 9, "열": 10,
           "열한": 11, "열두": 12}


def _num(s: str) -> int:
    return int(s) if s.isdigit() else _NUM_KO[s]


def parse_when(text: str, today: date | None = None) -> When:
    today = today or datetime.now(TZ).date()
    w = When()
    rest = text
    used: list[tuple[int, int]] = []

    def take(m: re.Match):
        used.append(m.span())

    # ── 날짜 ──
    m = re.search(r"(\d{4})[-./년]\s*(\d{1,2})[-./월]\s*(\d{1,2})일?", text)
    if m:
        w.date = date(int(m[1]), int(m[2]), int(m[3])); take(m)
        w.notes.append(f"'{m[0]}' → {fmt_date(w.date)}")
    if w.date is None:
        m = re.search(r"(다음\s*달|이번\s*달|다다음\s*달)?\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일", text) or \
            re.search(r"(?<![\d:])(\d{1,2})/(\d{1,2})(?![\d:/])", text)
        if m and "월" in m[0]:
            mo, dd = int(m[2]), int(m[3])
            y = today.year
            cand = date(y, mo, dd)
            if cand < today:
                cand = date(y + 1, mo, dd)
                w.notes.append(f"올해 {mo}월 {dd}일은 지났으므로 내년으로 계산")
            w.date = cand; take(m)
            w.notes.append(f"'{m[0].strip()}' → {fmt_date(w.date)}")
        elif m:
            mo, dd = int(m[1]), int(m[2])
            cand = date(today.year, mo, dd)
            if cand < today:
                cand = date(today.year + 1, mo, dd)
            w.date = cand; take(m)
            w.notes.append(f"'{m[0]}' → {fmt_date(w.date)}")
    if w.date is None:
        m = re.search(r"(다다음\s*달|다음\s*달|이번\s*달)\s*(\d{1,2})\s*일", text)
        if m:
            add = 2 if "다다음" in m[1] else (1 if "다음" in m[1] else 0)
            mo = today.month - 1 + add
            w.date = date(today.year + mo // 12, mo % 12 + 1, int(m[2])); take(m)
            w.notes.append(f"'{m[0]}' → {fmt_date(w.date)}")
    if w.date is None:
        m = re.search(r"(다다음\s*주|다음\s*주|담주|이번\s*주|금주|저번\s*주|지난\s*주)\s*([월화수목금토일])\s*요일?", text)
        if m:
            key = m[1].replace(" ", "")
            off = {"다다음주": 2, "다음주": 1, "담주": 1, "이번주": 0, "금주": 0, "저번주": -1, "지난주": -1}[key]
            monday = today - timedelta(days=today.weekday())
            w.date = monday + timedelta(weeks=off, days=WD[m[2]]); take(m)
            w.notes.append(f"'{m[0]}' → {fmt_date(w.date)} (주는 월요일 시작으로 계산)")
            if off == 0 and w.date < today:
                w.questions.append(f"이번 주 {m[2]}요일({fmt_date(w.date)})은 이미 지났어요. 다음 주 {m[2]}요일을 말씀하신 건가요?")
    if w.date is None:
        m = re.search(r"(\d+|한|두|세|네|다섯|여섯|일곱)\s*(일|주)\s*(후|뒤)", text)
        if m:
            n = _num(m[1])
            w.date = today + (timedelta(days=n) if m[2] == "일" else timedelta(weeks=n)); take(m)
            w.notes.append(f"'{m[0]}' → {fmt_date(w.date)}")
    if w.date is None:
        for word in sorted(_REL_DAYS, key=len, reverse=True):
            i = text.find(word)
            if i >= 0:
                w.date = today + timedelta(days=_REL_DAYS[word]); used.append((i, i + len(word)))
                w.notes.append(f"'{word}' → {fmt_date(w.date)}")
                break
    if w.date is None:
        m = re.search(r"(?<![가-힣])([월화수목금토일])요일", text)
        if m:
            diff = (WD[m[1]] - today.weekday()) % 7
            w.date = today + timedelta(days=diff); take(m)
            if diff == 0:
                w.questions.append(f"오늘이 {m[1]}요일이에요. 오늘({fmt_date(today)})인가요, 다음 주({fmt_date(today + timedelta(days=7))})인가요?")
            else:
                w.notes.append(f"'{m[0]}' → 가장 가까운 {m[0]} {fmt_date(w.date)}")
    if w.date and w.date < today and not w.questions:
        w.questions.append(f"{fmt_date(w.date)}은 이미 지난 날짜예요. 맞나요?")

    # ── 시간 ──
    period = r"(오전|오후|아침|점심|낮|저녁|밤|새벽)?\s*"
    hm = r"(\d{1,2}|한|두|세|네|다섯|여섯|일곱|여덟|아홉|열|열한|열두)\s*시(?!간)\s*(?:(\d{1,2})\s*분|(반))?"
    colon = r"(\d{1,2}):(\d{2})"
    pat = re.compile(period + r"(?:" + hm + r"|" + colon + r")")
    times = [m for m in pat.finditer(text) if m[2] or m[5]]

    def resolve(m: re.Match, prev_pm: bool | None = None) -> tuple[time | None, bool | None]:
        per = m[1]
        if m[2]:
            h = _num(m[2]); mi = 30 if m[4] else int(m[3] or 0)
        else:
            h, mi = int(m[5]), int(m[6])
        if h > 23 or mi > 59:
            return None, None
        if h == 24:
            h = 0
        pm = None
        if per in ("오후", "저녁", "밤"):
            pm = True
        elif per in ("오전", "아침", "새벽"):
            pm = False
        elif per in ("점심", "낮"):
            pm = h <= 6 or h == 12
        if h >= 13 or h == 0 or m[5]:  # 24시간 표기는 그대로
            return time(h, mi), h >= 12
        if pm is None:
            pm = prev_pm
        if pm is None:
            ctx = text
            if re.search(r"저녁|밤|퇴근|야간", ctx):
                pm = True
            elif re.search(r"아침|오전|새벽|출근", ctx):
                pm = False
            elif re.search(r"점심", ctx) and h in (11, 12, 1, 2):
                pm = h != 11
        if pm is None:
            return None, None
        if pm and h < 12:
            h += 12
        if not pm and h == 12:
            h = 0
        return time(h, mi), pm

    if times:
        t1, pm1 = resolve(times[0])
        take(times[0])
        if t1 is None:
            hour = times[0][2] or times[0][5]
            w.questions.append(f"'{times[0][0].strip()}'는 오전인가요, 오후인가요?")
            w.notes.append(f"{hour}시: 오전/오후가 정해지지 않음")
        else:
            w.start = t1
            w.notes.append(f"'{times[0][0].strip()}' → {t1:%H:%M}")
        if len(times) > 1 and re.search(r"부터|~|-|에서", text[times[0].end():times[1].start()]):
            t2, _ = resolve(times[1], pm1)
            take(times[1])
            if t2 and w.start and t2 <= w.start and t2.hour < 12:
                t2 = time(t2.hour + 12, t2.minute)
            w.end = t2
            if t2:
                w.notes.append(f"끝나는 시각 → {t2:%H:%M}")
    m = re.search(r"(\d+|한|두|세|네)\s*시간(?:\s*(반))?(?:\s*(?:동안|짜리))?", text)
    if m and not re.search(r"시간\s*(후|뒤)", m[0]):
        w.duration_min = _num(m[1]) * 60 + (30 if m[2] else 0); take(m)
    m2 = re.search(r"(\d+)\s*분\s*(?:동안|짜리)", text)
    if m2:
        w.duration_min = int(m2[1]); take(m2)
    if w.start and not w.end and w.duration_min:
        end_dt = datetime.combine(date.today(), w.start) + timedelta(minutes=w.duration_min)
        w.end = end_dt.time()

    # 날짜/시간 표현을 뺀 나머지 = 일정 제목 후보
    chars = list(text)
    for a, b in used:
        for i in range(a, b):
            chars[i] = " "
    rest = re.sub(r"\s*(부터|까지|에|~)\s+", " ", "".join(chars))
    w.rest = re.sub(r"\s+", " ", rest).strip(" ,.-~")
    return w


def to_datetime(d: date, t: time) -> datetime:
    return datetime.combine(d, t).replace(tzinfo=TZ)
