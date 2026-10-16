"""구글 캘린더 내보내기 파일(.ics) 읽기·쓰기.

.ics: 캘린더 프로그램끼리 일정을 주고받는 표준 텍스트 파일 형식.
외부 라이브러리 없이 동작하도록 직접 구현했다. 반복 일정(RRULE)은
구글 캘린더가 실제로 쓰는 형태(매일/매주/매월/매년 + 요일 + 횟수/종료일)를 지원한다.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import TZ

WEEKDAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]


@dataclass
class Event:
    title: str
    start: datetime            # 항상 한국 시간(aware)
    end: datetime
    all_day: bool = False
    location: str = ""
    description: str = ""
    uid: str = field(default_factory=lambda: f"{uuid.uuid4()}@schedule-agent")
    category: str = ""
    source: str = "calendar"   # calendar | agent
    reminders: list[int] = field(default_factory=list)  # 분 단위
    recurring: bool = False
    absent: bool = False       # 제목에 '결석'이 있으면 True (출석 세기용)
    course: str = ""           # '결석'을 뺀 강의 이름

    def to_dict(self) -> dict:
        d = asdict(self)
        d["start"] = self.start.isoformat()
        d["end"] = self.end.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Event":
        d = dict(d)
        d["start"] = datetime.fromisoformat(d["start"]).astimezone(TZ)
        d["end"] = datetime.fromisoformat(d["end"]).astimezone(TZ)
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


# ───────────────────────── 읽기 ─────────────────────────

def _unfold(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw.startswith((" ", "\t")) and lines:
            lines[-1] += raw[1:]
        elif raw:
            lines.append(raw)
    return lines


def _split_prop(line: str) -> tuple[str, dict, str]:
    # 이름;파라미터=값:내용  (따옴표 안의 : ; 는 무시)
    in_q = False
    colon = -1
    for i, ch in enumerate(line):
        if ch == '"':
            in_q = not in_q
        elif ch == ":" and not in_q:
            colon = i
            break
    head, value = (line, "") if colon < 0 else (line[:colon], line[colon + 1:])
    parts = head.split(";")
    params = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.upper()] = v.strip('"')
    return parts[0].upper(), params, value


def _unescape(s: str) -> str:
    return (s.replace("\\n", "\n").replace("\\N", "\n").replace("\\,", ",")
             .replace("\\;", ";").replace("\\\\", "\\"))


def _parse_dt(value: str, params: dict) -> tuple[datetime, bool]:
    """(한국 시간 datetime, 종일 여부)"""
    value = value.strip()
    if params.get("VALUE") == "DATE" or (len(value) == 8 and value.isdigit()):
        d = datetime.strptime(value[:8], "%Y%m%d")
        return d.replace(tzinfo=TZ), True
    if value.endswith("Z"):
        dt = datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    else:
        dt = datetime.strptime(value[:15], "%Y%m%dT%H%M%S")
        tzid = params.get("TZID")
        try:
            dt = dt.replace(tzinfo=ZoneInfo(tzid) if tzid else TZ)
        except Exception:  # 알 수 없는 시간대 이름이면 한국 시간으로 본다
            dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ), False


def _parse_rrule(value: str) -> dict:
    out = {}
    for part in value.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            out[k.upper()] = v
    return out


def _expand(start: datetime, rule: dict, exdates: set, window_end: datetime) -> list[datetime]:
    """반복 규칙을 실제 시작 시각 목록으로 펼친다 (window_end 까지)."""
    freq = rule.get("FREQ", "WEEKLY")
    interval = int(rule.get("INTERVAL", "1") or 1)
    count = int(rule["COUNT"]) if "COUNT" in rule else None
    until = None
    if "UNTIL" in rule:
        u = rule["UNTIL"]
        until, _ = _parse_dt(u, {"VALUE": "DATE"} if len(u) == 8 else {})
        if len(u) == 8:
            until = until + timedelta(days=1) - timedelta(seconds=1)
    byday = [d[-2:] for d in rule.get("BYDAY", "").split(",") if d]
    limit = min(window_end, until) if until else window_end

    results: list[datetime] = []
    local_time = start.timetz()
    n = 0  # COUNT 는 제외일 포함해서 센다 (표준)

    def emit(dt: datetime) -> bool:
        nonlocal n
        if dt < start:
            return True
        if dt > limit or (count is not None and n >= count):
            return False
        n += 1
        if dt.replace(tzinfo=None) not in exdates:
            results.append(dt)
        return True

    if freq == "WEEKLY":
        days = sorted(WEEKDAYS.index(d) for d in byday) if byday else [start.weekday()]
        week0 = start.date() - timedelta(days=start.weekday())
        k = 0
        while True:
            wk = week0 + timedelta(weeks=k * interval)
            if datetime.combine(wk, time(), TZ) > limit:
                break
            for wd in days:
                dt = datetime.combine(wk + timedelta(days=wd), local_time).replace(tzinfo=TZ)
                if not emit(dt):
                    return results
            k += 1
            if k > 5000:
                break
    else:
        k = 0
        while k < 20000:
            if freq == "DAILY":
                d = start.date() + timedelta(days=k * interval)
            elif freq == "MONTHLY":
                m = start.month - 1 + k * interval
                y, m = start.year + m // 12, m % 12 + 1
                try:
                    d = date(y, m, start.day)
                except ValueError:  # 31일이 없는 달은 건너뜀
                    k += 1
                    continue
            elif freq == "YEARLY":
                try:
                    d = start.date().replace(year=start.year + k * interval)
                except ValueError:
                    k += 1
                    continue
            else:
                break
            dt = datetime.combine(d, local_time).replace(tzinfo=TZ)
            if dt > limit:
                break
            if byday and WEEKDAYS[d.weekday()] not in byday:
                k += 1
                continue
            if not emit(dt):
                break
            k += 1
    return results


def parse_ics(text: str, window_end: datetime | None = None) -> list[Event]:
    """ics 텍스트 → 일정 목록 (반복 일정은 window_end 까지 펼침)."""
    window_end = window_end or datetime(2027, 12, 31, tzinfo=TZ)
    raw_events: list[dict] = []
    cur: dict | None = None
    depth_alarm = False
    for line in _unfold(text):
        name, params, value = _split_prop(line)
        if name == "BEGIN" and value.upper() == "VEVENT":
            cur = {"EXDATE": set()}
        elif name == "BEGIN" and value.upper() == "VALARM":
            depth_alarm = True
        elif name == "END" and value.upper() == "VALARM":
            depth_alarm = False
        elif name == "END" and value.upper() == "VEVENT":
            if cur is not None:
                raw_events.append(cur)
            cur = None
        elif cur is not None and not depth_alarm:
            if name in ("DTSTART", "DTEND", "RECURRENCE-ID"):
                cur[name] = _parse_dt(value, params)
            elif name == "EXDATE":
                for v in value.split(","):
                    dt, _ = _parse_dt(v, params)
                    cur["EXDATE"].add(dt.replace(tzinfo=None))
            elif name == "DURATION":
                cur["DURATION"] = value
            elif name in ("SUMMARY", "LOCATION", "DESCRIPTION", "UID", "RRULE", "STATUS"):
                cur[name] = _unescape(value)

    # 반복 일정 중 '이번 한 번만 바뀐' 회차(RECURRENCE-ID)
    overrides: dict[str, set] = {}
    for r in raw_events:
        if "RECURRENCE-ID" in r:
            overrides.setdefault(r.get("UID", ""), set()).add(r["RECURRENCE-ID"][0].replace(tzinfo=None))

    events: list[Event] = []
    for r in raw_events:
        if "DTSTART" not in r or r.get("STATUS", "").upper() == "CANCELLED":
            continue
        start, all_day = r["DTSTART"]
        if "DTEND" in r:
            end = r["DTEND"][0]
        elif "DURATION" in r:
            end = start + _parse_duration(r["DURATION"])
        else:
            end = start + (timedelta(days=1) if all_day else timedelta(hours=1))
        length = end - start
        base = dict(title=r.get("SUMMARY", "(제목 없음)"), all_day=all_day,
                    location=r.get("LOCATION", ""), description=r.get("DESCRIPTION", ""))
        uid = r.get("UID", str(uuid.uuid4()))
        if "RRULE" in r and "RECURRENCE-ID" not in r:
            ex = r["EXDATE"] | overrides.get(uid, set())
            for i, s in enumerate(_expand(start, _parse_rrule(r["RRULE"]), ex, window_end)):
                events.append(Event(start=s, end=s + length, uid=f"{uid}#{i}", recurring=True, **base))
        else:
            events.append(Event(start=start, end=end, uid=uid, **base))
    events.sort(key=lambda e: e.start)
    return events


def _parse_duration(v: str) -> timedelta:
    import re
    m = re.match(r"P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", v)
    if not m:
        return timedelta(hours=1)
    w, d, h, mi, s = (int(x or 0) for x in m.groups())
    return timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=s)


def load_ics_files(paths: list[Path]) -> list[Event]:
    events: list[Event] = []
    for p in paths:
        events.extend(parse_ics(p.read_text("utf-8", errors="replace")))
    events.sort(key=lambda e: e.start)
    return events


# ───────────────────────── 쓰기 ─────────────────────────

def _escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fmt(dt: datetime) -> str:
    return dt.astimezone(TZ).strftime("%Y%m%dT%H%M%S")


def to_ics(events: list[Event]) -> str:
    """일정 목록 → ics 텍스트 (알림 포함). 구글 캘린더 '가져오기'로 넣을 수 있다."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//schedule-agent//KO", "CALSCALE:GREGORIAN"]
    for e in events:
        out += ["BEGIN:VEVENT", f"UID:{e.uid}", f"DTSTAMP:{stamp}"]
        if e.all_day:
            out += [f"DTSTART;VALUE=DATE:{e.start:%Y%m%d}", f"DTEND;VALUE=DATE:{e.end:%Y%m%d}"]
        else:
            out += [f"DTSTART;TZID=Asia/Seoul:{_fmt(e.start)}", f"DTEND;TZID=Asia/Seoul:{_fmt(e.end)}"]
        out.append(f"SUMMARY:{_escape(e.title)}")
        if e.location:
            out.append(f"LOCATION:{_escape(e.location)}")
        if e.description:
            out.append(f"DESCRIPTION:{_escape(e.description)}")
        for m in e.reminders:
            out += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_escape(e.title)}",
                    f"TRIGGER:-PT{int(m)}M", "END:VALARM"]
        out.append("END:VEVENT")
    out.append("END:VCALENDAR")
    return "\r\n".join(out) + "\r\n"
