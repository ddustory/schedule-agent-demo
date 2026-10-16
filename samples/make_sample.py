"""가짜 예시 일정(samples/sample_calendar.ics)을 만든다.

실제 일정이 아니다. 이름·장소는 모두 지어낸 것이다.
다시 만들기:  python3 samples/make_sample.py
"""
import random
import sys
from datetime import date, datetime, time, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from schedule_agent.config import TZ  # noqa: E402
from schedule_agent.ics import Event, to_ics  # noqa: E402

random.seed(2026)
OUT = Path(__file__).resolve().parent


def dt(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(d, time(hh, mm)).replace(tzinfo=TZ)


def all_day(d: date, title: str) -> Event:
    s = datetime.combine(d, time()).replace(tzinfo=TZ)
    return Event(title=title, start=s, end=s + timedelta(days=1), all_day=True)


events: list[Event] = []
start, end = date(2026, 1, 2), date(2026, 12, 31)
days = [start + timedelta(days=i) for i in range((end - start).days + 1)]
holidays = {date(2026, 2, 17), date(2026, 3, 3), date(2026, 5, 5), date(2026, 9, 24), date(2026, 10, 8)}

DEMO_DAYS = {date(2026, 10, 15), date(2026, 10, 16), date(2026, 10, 17)}
days = [d for d in days if d not in DEMO_DAYS]

# 대타 (월 평균 ~3번, 주로 토·월)
for d in days:
    if d in holidays or d.weekday() in (1, 3):
        continue
    p = {5: 0.30, 0: 0.15}.get(d.weekday(), 0.05)
    if random.random() < p:
        events.append(Event(title="카페 대타", start=dt(d, 13), end=dt(d, 18), location="해오름카페"))

# 도서 대출/반납
books = ["데미안", "코스모스", "어린 왕자", "사피엔스", "총균쇠", "아몬드", "채식주의자", "1984", "미움받을 용기", "모모"]
d = date(2026, 1, 9)
for i in range(18):
    b = books[i % len(books)]
    events.append(all_day(d, f"도서 대출 - {b}"))
    ret = d + timedelta(days=random.choice([10, 12, 13, 14, 14, 14, 15]))
    events.append(all_day(ret, f"도서 반납 - {b}"))
    d = ret + timedelta(days=random.randint(1, 6))

# 약속 (가짜 이름)
friends = ["가람", "하늘", "보라", "다온", "새솔"]
for d in days:
    if d.weekday() in (4, 5) and random.random() < 0.35:
        f = random.choice(friends)
        h = random.choice([12, 18, 19])
        events.append(Event(title=f"{f}{'이랑' if f[-1] in '람늘온솔' else '랑'} {'점심' if h == 12 else '저녁'}",
                            start=dt(d, h), end=dt(d, h + 2), location=random.choice(["강남역", "홍대입구", "신촌"])))

# 스케줄 변동 (월초·3월에 많음)
for d in days:
    p = 0.12 if d.day <= 7 else 0.02
    if d.month == 3:
        p *= 2
    if random.random() < p:
        events.append(all_day(d, random.choice(["스케줄 변동: 수업 시간 변경", "근무 스케줄 변경", "스케줄 변동: 휴무"])))

# 시연용: 오늘(10/15)·내일(10/16) 일정
events += [
    Event(title="카페 대타", start=dt(date(2026, 10, 16), 13), end=dt(date(2026, 10, 16), 18, 40), location="해오름카페"),
    Event(title="가람이랑 저녁", start=dt(date(2026, 10, 16), 19), end=dt(date(2026, 10, 16), 21), location="강남역"),
    all_day(date(2026, 10, 17), "도서 반납 - 모모"),
]

body = to_ics(sorted(events, key=lambda e: e.start)).replace("\r\nEND:VCALENDAR\r\n", "\r\n")

# 학원 수업은 구글처럼 '반복 일정'(RRULE)으로 넣는다
exd = ",".join(f"{h:%Y%m%d}T180000" for h in sorted(holidays) if h.weekday() in (1, 3))
recurring = [
    "BEGIN:VEVENT", "UID:class-tuthu@sample", "DTSTAMP:20260101T000000Z",
    "DTSTART;TZID=Asia/Seoul:20260106T180000", "DTEND;TZID=Asia/Seoul:20260106T210000",
    "RRULE:FREQ=WEEKLY;BYDAY=TU,TH;UNTIL=20261231T235959Z",
    f"EXDATE;TZID=Asia/Seoul:{exd}",
    "SUMMARY:학원 수업 (중등 수학)", "LOCATION:새봄학원", "END:VEVENT",
    "BEGIN:VEVENT", "UID:class-sat@sample", "DTSTAMP:20260101T000000Z",
    "DTSTART;TZID=Asia/Seoul:20260307T100000", "DTEND;TZID=Asia/Seoul:20260307T130000",
    "RRULE:FREQ=WEEKLY;COUNT=16", "SUMMARY:학원 주말 특강", "LOCATION:새봄학원", "END:VEVENT",
    # 2학기 대학 강의 (월·수 10:30~12:00). 결석한 날은 구글 캘린더에서 그 회차 제목만 '결석…'으로 바꾼 형태
    "BEGIN:VEVENT", "UID:lecture-edu@sample", "DTSTAMP:20260101T000000Z",
    "DTSTART;TZID=Asia/Seoul:20260902T103000", "DTEND;TZID=Asia/Seoul:20260902T120000",
    "RRULE:FREQ=WEEKLY;BYDAY=MO,WE;UNTIL=20261216T235959Z",
    "SUMMARY:교육학개론 강의", "END:VEVENT",
    "BEGIN:VEVENT", "UID:lecture-edu@sample", "DTSTAMP:20260101T000000Z",
    "RECURRENCE-ID;TZID=Asia/Seoul:20260916T103000",
    "DTSTART;TZID=Asia/Seoul:20260916T103000", "DTEND;TZID=Asia/Seoul:20260916T120000",
    "SUMMARY:결석교육학개론 강의", "END:VEVENT",
    "BEGIN:VEVENT", "UID:lecture-edu@sample", "DTSTAMP:20260101T000000Z",
    "RECURRENCE-ID;TZID=Asia/Seoul:20261007T103000",
    "DTSTART;TZID=Asia/Seoul:20261007T103000", "DTEND;TZID=Asia/Seoul:20261007T120000",
    "SUMMARY:결석교육학개론 강의", "END:VEVENT",
    "BEGIN:VEVENT", "UID:lecture-lit@sample", "DTSTAMP:20260101T000000Z",
    "DTSTART;TZID=Asia/Seoul:20260903T130000", "DTEND;TZID=Asia/Seoul:20260903T143000",
    "RRULE:FREQ=WEEKLY;BYDAY=TH;UNTIL=20261217T235959Z",
    "SUMMARY:현대문학 강의", "END:VEVENT",
    "END:VCALENDAR", "",
]
(OUT / "sample_calendar.ics").write_text(body + "\r\n".join(recurring), "utf-8")
print("만듦:", OUT / "sample_calendar.ics", len(events), "개 + 반복 일정 2개")
