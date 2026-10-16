"""② 하루 계획 + 겹침·무리한 일정 찾기 + 할 일 정리 + 알림 계산."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta

from .categories import mask_people
from .config import TZ
from .dates import fmt_date
from .ics import Event

MOVE_CATS = {"약속", "수업", "대학 강의", "대타"}  # 이동이 필요하다고 보는 종류


def _t(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def same_place(a: Event, b: Event) -> bool:
    """이동이 필요 없는 사이인지. 장소가 같거나, 장소 없이 같은 종류(예: 강의→강의, 학원→학원)면 같은 곳으로 본다."""
    if a.location and b.location:
        return a.location == b.location
    return not a.location and not b.location and a.category == b.category


def _fmt_rng(a: datetime, b: datetime) -> str:
    return f"{a:%H:%M}~{b:%H:%M}"


# ── 할 일 정리 ──
WD_KO = "월화수목금토일"


def expand_todos(todos: list[dict], today: date) -> list[dict]:
    """매주 반복하는 할 일은 '이번 회차' 하나로 펼친다.
    회차 마감 = 오늘 이후 가장 가까운 그 요일. 그 회차를 완료했으면 다음 주 회차.
    지난 회차를 못 했으면 그 회차가 '마감 지남'으로 남는다 (최근 1회만)."""
    out = []
    for t in todos:
        if t.get("repeat") != "weekly":
            if not t.get("done"):
                out.append(t)
            continue
        wd = int(t.get("weekday", 5))
        done = set(t.get("done_dates", []))
        nxt = today + timedelta(days=(wd - today.weekday()) % 7)
        prev = nxt - timedelta(days=7)
        start = date.fromisoformat(t["since"]) if t.get("since") else prev
        if start <= prev < today and prev.isoformat() not in done:
            due = prev
        else:
            due = nxt
            while due.isoformat() in done:
                due += timedelta(days=7)
        out.append({**t, "due": due.isoformat(), "repeat_label": f"매주 {WD_KO[wd]}"})
    return out


def sort_todos(todos: list[dict], today: date) -> list[dict]:
    """마감 빠른 순 → 중요도 높은 순. 마감 지난 것은 맨 앞. 매주 할 일은 이번 회차로."""
    def key(t):
        due = date.fromisoformat(t["due"]) if t.get("due") else date.max
        return (due, -int(t.get("importance", 2)))
    out = []
    for t in sorted(expand_todos(todos, today), key=key):
        t = dict(t)
        if t.get("due"):
            left = (date.fromisoformat(t["due"]) - today).days
            t["d_day"] = "마감 지남" if left < 0 else ("오늘 마감" if left == 0 else f"D-{left}")
        else:
            t["d_day"] = "마감 없음"
        out.append(t)
    return out


# ── 알림 ──
def reminders_for(ev: Event, settings: dict, att: dict | None = None) -> list[dict]:
    """종류별 알림. minutes 는 '일정 시작 몇 분 전'. 대학 강의는 '출석 알림'(결석 횟수 포함)."""
    mins = list(settings["reminders"].get(ev.category or "기타", settings["reminders"]["기타"]))
    out = []
    for m in mins:
        if ev.all_day and m >= 1440:
            # 종일 일정(예: 반납일)은 'N일 전 오전 9시'로 바꾼다
            days = m // 1440
            m = days * 1440 - 9 * 60
            label = f"{days}일 전 오전 9시"
        elif m >= 1440:
            label = f"{m // 1440}일 전"
        elif m >= 60 and m % 60 == 0:
            label = f"{m // 60}시간 전"
        else:
            label = f"{m}분 전"
        out.append({"minutes": m, "label": label, "kind": "일반"})
    if ev.category == "대학 강의":
        info = (att or {}).get(ev.course or ev.title)
        limit = int(settings.get("absence_fail_count", 4))
        absent = info["absent"] if info else 0
        for r in out:
            r["kind"] = "출석"
            r["label"] = f"출석 알림 {r['label']} (결석 {absent}/{limit})"
    if ev.category == "약속" and not ev.all_day:
        travel = int(settings["travel_minutes"])
        out.append({"minutes": travel + 10, "label": f"출발 알림({travel}분 이동 + 10분 여유)", "kind": "출발"})
    for r in out:
        r["at"] = (ev.start - timedelta(minutes=r["minutes"])).strftime("%m/%d %H:%M")
    return out


# ── 겹침·무리한 일정 ──
def check_conflicts(new: Event, events: list[Event], settings: dict, att: dict | None = None) -> list[str]:
    warnings: list[str] = []
    travel = timedelta(minutes=int(settings["travel_minutes"]))
    if new.all_day:
        return warnings
    day = new.start.date()
    same_day = [e for e in events if not e.all_day and e.start.date() == day and e.uid != new.uid]
    for e in same_day:
        if e.start < new.end and e.end > new.start:
            warnings.append(f"⛔ 겹침: '{mask_people(e.title)}'({_fmt_rng(e.start, e.end)})와 시간이 겹쳐요.")
            if e.category == "대학 강의" and not e.absent and new.category != "대학 강의":
                info = (att or {}).get(e.course or e.title)
                n = (info["absent"] if info else 0) + 1
                limit = int(settings.get("absence_fail_count", 4))
                tail = " F 기준이에요!" if n >= limit else (" 한 번 더 빠지면 F!" if n == limit - 1 else "")
                warnings.append(f"🎓 이 시간엔 '{e.course or e.title}' 강의가 있어요. 빠지면 결석 {n}/{limit}번이 돼요.{tail}")
    needs_move = new.category in MOVE_CATS or bool(new.location)
    for e in same_day:
        if not (needs_move or e.category in MOVE_CATS or e.location):
            continue
        if same_place(new, e):
            continue
        if timedelta(0) <= new.start - e.end < travel:
            warnings.append(f"⚠️ 이동 시간 부족: '{mask_people(e.title)}'이 {e.end:%H:%M}에 끝나고 바로 {new.start:%H:%M} 시작이에요. (이동 {settings['travel_minutes']}분 필요)")
        if timedelta(0) <= e.start - new.end < travel:
            warnings.append(f"⚠️ 이동 시간 부족: 끝나는 {new.end:%H:%M} 직후 {e.start:%H:%M}에 '{mask_people(e.title)}'이 있어요.")
    hours = sum((e.end - e.start).total_seconds() for e in same_day + [new]) / 3600
    if hours > float(settings["busy_hours_warning"]):
        warnings.append(f"⚠️ 빽빽한 하루: 이 일정을 넣으면 {fmt_date(day)} 일정이 총 {hours:.1f}시간이에요.")
    if len(same_day) + 1 >= 5:
        warnings.append(f"⚠️ 이 날 일정이 {len(same_day) + 1}개예요.")
    return warnings


def day_warnings(day_events: list[Event], settings: dict) -> list[str]:
    timed = sorted((e for e in day_events if not e.all_day), key=lambda e: e.start)
    out = []
    travel = timedelta(minutes=int(settings["travel_minutes"]))
    for a, b in zip(timed, timed[1:]):
        if b.start < a.end:
            out.append(f"⛔ 겹침: '{mask_people(a.title)}' / '{mask_people(b.title)}'")
        elif (b.start - a.end) < travel and (a.category in MOVE_CATS or b.category in MOVE_CATS) \
                and not same_place(a, b):
            out.append(f"⚠️ '{mask_people(a.title)}' → '{mask_people(b.title)}' 사이 이동 시간이 {int((b.start - a.end).total_seconds() // 60)}분뿐이에요.")
    hours = sum((e.end - e.start).total_seconds() for e in timed) / 3600
    if hours > float(settings["busy_hours_warning"]):
        out.append(f"⚠️ 일정이 총 {hours:.1f}시간으로 빽빽해요.")
    return out


# ── 빈 시간 ──
def free_slots(day: date, events: list[Event], settings: dict, now: datetime | None = None) -> list[tuple[datetime, datetime]]:
    start = datetime.combine(day, _t(settings["day_start"])).replace(tzinfo=TZ)
    end = datetime.combine(day, _t(settings["day_end"])).replace(tzinfo=TZ)
    if now and now.date() == day:
        start = max(start, now.replace(second=0, microsecond=0))
    travel = timedelta(minutes=int(settings["travel_minutes"]))
    busy = []
    for e in events:
        if e.all_day:
            continue
        pad = travel if (e.category in MOVE_CATS or e.location) else timedelta(0)
        busy.append((e.start - pad, e.end + pad))
    busy.sort()
    slots, cur = [], start
    for a, b in busy:
        if b <= cur:
            continue
        if a > cur:
            slots.append((cur, min(a, end)))
        cur = max(cur, b)
        if cur >= end:
            break
    if cur < end:
        slots.append((cur, end))
    return [(a, b) for a, b in slots if (b - a) >= timedelta(minutes=15)]


# ── 하루 계획 ──
def plan_day(day: date, events: list[Event], todos: list[dict], settings: dict,
             now: datetime | None = None, light_day: bool = False, att: dict | None = None) -> dict:
    day_events = sorted((e for e in events if e.start.date() <= day <= (e.end - timedelta(seconds=1)).date()),
                        key=lambda e: (not e.all_day, e.start))
    slots = free_slots(day, [e for e in day_events if not e.absent], settings, now)  # 결석 표시한 강의 시간은 빈 시간
    busy_h = sum((e.end - e.start).total_seconds() for e in day_events if not e.all_day) / 3600
    if busy_h > float(settings["busy_hours_warning"]) * 0.6:
        light_day = True  # 바쁜 날은 할 일을 2시간 이내로만
    ordered = sort_todos(todos, day)
    placed, left = [], []
    slot_list = [list(s) for s in slots]
    budget_min = 120 if light_day else 10_000
    used_min = 0
    for t in ordered:
        need = int(t.get("minutes", 30))
        if used_min + need > budget_min:
            left.append(t)
            continue
        for s in slot_list:
            if (s[1] - s[0]) >= timedelta(minutes=need):
                a = s[0]
                b = a + timedelta(minutes=need)
                placed.append({"todo": t, "start": a, "end": b})
                s[0] = b + timedelta(minutes=10)  # 10분 쉬는 시간
                used_min += need
                break
        else:
            left.append(t)

    timeline = []
    for e in day_events:
        timeline.append({"type": "일정", "category": e.category, "title": mask_people(e.title),
                         "start": "종일" if e.all_day else f"{e.start:%H:%M}",
                         "end": "" if e.all_day else f"{e.end:%H:%M}", "sort": "" if e.all_day else f"{e.start:%H:%M}",
                         "source": e.source})
    for p in placed:
        timeline.append({"type": "할 일", "category": "할 일", "title": p["todo"]["title"],
                         "start": f"{p['start']:%H:%M}", "end": f"{p['end']:%H:%M}", "sort": f"{p['start']:%H:%M}",
                         "d_day": p["todo"]["d_day"]})
    timeline.sort(key=lambda x: x["sort"])
    warnings = day_warnings(day_events, settings)
    # 출석 챙기기: 이날 강의별 결석 현황
    attendance_today = []
    for e in day_events:
        if e.category != "대학 강의" or e.all_day:
            continue
        info = (att or {}).get(e.course or e.title)
        limit = int(settings.get("absence_fail_count", 4))
        absent = info["absent"] if info else 0
        attendance_today.append({"course": e.course or e.title, "start": f"{e.start:%H:%M}", "absent": absent,
                                 "limit": limit, "status": info["status"] if info else "안전", "skipped": e.absent})
        if info and info["status"] in ("주의", "위험", "F 기준") and not e.absent:
            warnings.append(f"🎓 '{e.course or e.title}' {info['message']}")

    # 아침 요약
    lines = [f"☀️ {fmt_date(day)} 하루 요약"]
    if not day_events:
        lines.append("· 잡힌 일정이 없어요.")
    for x in timeline:
        rng = x["start"] if x["start"] == "종일" else f"{x['start']}~{x['end']}"
        tag = f"[{x['category']}]"
        lines.append(f"· {rng} {tag} {x['title']}" + (f" ({x['d_day']})" if x.get("d_day") else ""))
    free_total = sum(int((b - a).total_seconds() // 60) for a, b in slots)
    lines.append(f"· 빈 시간 합계 약 {free_total // 60}시간 {free_total % 60}분")
    urgent = [t for t in left if t["d_day"] in ("마감 지남", "오늘 마감", "D-1")]
    if urgent:
        lines.append("· 못 넣은 급한 할 일: " + ", ".join(t["title"] for t in urgent))
    if attendance_today:
        lines.append("· 출석 체크: " + ", ".join(
            f"{a['course']}({a['start']}, 결석 {a['absent']}/{a['limit']}{' · 오늘 결석 표시됨' if a['skipped'] else ''})"
            for a in attendance_today))
    if light_day:
        lines.append("· 바쁜 날이라 할 일은 2시간 이내로만 넣었어요.")
    lines += warnings
    return {
        "date": day.isoformat(), "date_text": fmt_date(day),
        "timeline": timeline,
        "free_slots": [{"start": f"{a:%H:%M}", "end": f"{b:%H:%M}", "minutes": int((b - a).total_seconds() // 60)} for a, b in slots],
        "unplaced_todos": [{"title": t["title"], "d_day": t["d_day"]} for t in left],
        "warnings": warnings,
        "attendance": attendance_today,
        "summary": "\n".join(lines),
    }
