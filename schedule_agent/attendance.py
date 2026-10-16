"""대학 강의 출석 챙기기.

캘린더에서 강의 제목에 '결석'을 붙여 두면(예: '결석국어음운론') 그 강의의 결석으로 센다.
결석이 absence_fail_count(기본 4)번이 되면 F이므로, 그 전에 미리 알려준다.
"""
from __future__ import annotations

from datetime import date

from .ics import Event


def semester_range(today: date, settings: dict) -> tuple[date, date]:
    """이번 학기 기간. 설정이 없으면 3~8월은 3/1부터, 9~2월은 9/1부터로 본다."""
    s = settings.get("semester_start")
    if s:
        start = date.fromisoformat(s)
    elif 3 <= today.month <= 8:
        start = date(today.year, 3, 1)
    else:
        start = date(today.year if today.month >= 9 else today.year - 1, 9, 1)
    end = date(start.year, 8, 31) if start.month < 9 else date(start.year + 1, 2, 28)
    return start, end


def status_of(absent: int, limit: int) -> tuple[str, str]:
    left = limit - 1 - absent  # F 없이 더 빠질 수 있는 횟수
    if absent >= limit:
        return "F 기준", f"결석 {absent}번 — F 기준({limit}번)에 닿았어요. 학과·교수님 기준을 꼭 확인하세요."
    if left == 0:
        return "위험", f"결석 {absent}번 — 한 번만 더 빠지면 F예요!"
    if left == 1:
        return "주의", f"결석 {absent}번 — 앞으로 1번까지만 빠질 수 있어요."
    return "안전", f"결석 {absent}번 — 앞으로 {left}번까지 빠질 수 있어요."


def attendance(events: list[Event], today: date, settings: dict) -> dict:
    """강의별 출석 현황. {course: {...}}"""
    start, end = semester_range(today, settings)
    limit = int(settings.get("absence_fail_count", 4))
    out: dict[str, dict] = {}
    for e in events:
        if e.category != "대학 강의" or e.all_day or not (start <= e.start.date() <= end):
            continue
        course = e.course or e.title
        c = out.setdefault(course, {"course": course, "held": 0, "absent": 0, "absent_dates": [],
                                    "upcoming": 0, "next": None, "limit": limit})
        if e.start.date() <= today:
            c["held"] += 1
            if e.absent:
                c["absent"] += 1
                c["absent_dates"].append(e.start.date().isoformat())
        else:
            c["upcoming"] += 1
            if c["next"] is None or e.start.isoformat() < c["next"]:
                c["next"] = e.start.isoformat()
    for c in out.values():
        c["status"], c["message"] = status_of(c["absent"], limit)
    return out


def summary(att: dict) -> dict:
    return {"courses": sorted(att.values(), key=lambda c: (-c["absent"], c["course"]))}
