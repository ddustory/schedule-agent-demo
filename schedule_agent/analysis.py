"""① 내 기록 분석: 1월부터의 일정으로 생활 패턴을 찾는다.

결과는 '제안'으로만 쓴다. 고정 일정을 마음대로 바꾸지 않는다.
화면·AI에 보내는 예시 제목은 다른 사람 이름을 ○○ 로 가린다.
"""
from __future__ import annotations

import re
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime

from .categories import CATEGORIES, mask_people
from .dates import WD_NAMES
from .ics import Event


def _eun(word: str) -> str:
    """받침에 맞춰 '은/는'을 붙인다. 예) 수업은, 대학 강의는"""
    ch = word[-1]
    if "가" <= ch <= "힣":
        return word + ("은" if (ord(ch) - 0xAC00) % 28 else "는")
    return word + "은(는)"


def _hm(dt: datetime) -> str:
    return dt.strftime("%H:%M")


def _book_key(title: str) -> str:
    t = re.sub(r"도서관|도서|대출|반납|연장|책|[\[\]()<>『』「」\"'·:\-]", " ", title)
    return re.sub(r"\s+", " ", t).strip()


def library_durations(events: list[Event]) -> list[dict]:
    """대출 일정 → 같은 책의 다음 반납 일정까지 걸린 날짜 수."""
    loans = [e for e in events if "대출" in e.title]
    returns = [e for e in events if "반납" in e.title]
    used: set[str] = set()
    pairs = []
    for ln in loans:
        key = _book_key(ln.title)
        cands = [r for r in returns if r.start > ln.start and r.uid not in used]
        match = next((r for r in cands if key and _book_key(r.title) == key), None)
        if match is None and not key:
            match = cands[0] if cands else None
        if match:
            used.add(match.uid)
            pairs.append({"loan": ln.start.date().isoformat(), "return": match.start.date().isoformat(),
                          "days": (match.start.date() - ln.start.date()).days})
    return pairs


def analyze(events: list[Event], today: date | None = None, since: date | None = None) -> dict:
    today = today or date.today()
    since = since or date(today.year, 1, 1)
    past = [e for e in events if since <= e.start.date() <= today]
    # 평균은 '기록이 실제로 있는 기간'으로 나눈다 (1주치만 넣었는데 10개월로 나누지 않도록)
    if past:
        since = max(since, min(e.start.date() for e in past))
        today = min(today, max(e.start.date() for e in past))
    by_cat: dict[str, list[Event]] = defaultdict(list)
    for e in past:
        by_cat[e.category or "기타"].append(e)

    months = sorted({(e.start.year, e.start.month) for e in past})
    n_months = max(1, len(months))
    weeks = max(1, ((today - since).days + 1) / 7)

    result: dict = {
        "period": {"from": since.isoformat(), "to": today.isoformat(), "months": n_months},
        "total": len(past),
        "by_category": {c: len(by_cat.get(c, [])) for c in CATEGORIES},
        "monthly": {f"{y}-{m:02d}": Counter(e.category for e in past if (e.start.year, e.start.month) == (y, m))
                    for y, m in months},
        "patterns": {},
        "suggestions": [],
    }
    if past and weeks < 4:
        result["suggestions"].append(f"아직 기록이 약 {max(1, round(weeks))}주치뿐이라, 아래 패턴은 참고용이에요. 1월부터의 캘린더를 넣으면 더 정확해져요.")

    # 수업(학원)·대학 강의: 요일·시작시간
    for cat, note in (("수업", "학원 고정 시간표와 맞는지 확인해 주세요"), ("대학 강의", "이번 학기 시간표와 맞는지 확인해 주세요")):
        cls = [e for e in by_cat.get(cat, []) if not e.all_day]
        if not cls:
            continue
        slots = Counter((e.start.weekday(), _hm(e.start), _hm(e.end)) for e in cls)
        top = [{"weekday": WD_NAMES[w] + "요일", "start": s, "end": en, "count": c,
                "per_week": round(c / weeks, 2)}
               for (w, s, en), c in slots.most_common(8)]
        wd = Counter(e.start.weekday() for e in cls)
        result["patterns"][cat] = {
            "count": len(cls), "top_slots": top,
            "weekday_counts": {WD_NAMES[k]: v for k, v in sorted(wd.items())},
        }
        regular = [t for t in top if t["per_week"] >= 0.5] or (top[:6] if weeks <= 2 else [])
        if regular:
            desc = ", ".join(f"{t['weekday']} {t['start']}~{t['end']}" for t in regular)
            result["suggestions"].append(f"{_eun(cat)} 주로 {desc}에 있어요. 이 시간엔 다른 일을 넣지 않도록 할게요. ({note})")

    # 대타: 빈도
    sub = by_cat.get("대타", [])
    if sub:
        per_month = Counter(f"{e.start.year}-{e.start.month:02d}" for e in sub)
        wd = Counter(WD_NAMES[e.start.weekday()] for e in sub)
        result["patterns"]["대타"] = {
            "count": len(sub), "per_month_avg": round(len(sub) / n_months, 1),
            "per_month": dict(sorted(per_month.items())),
            "weekday_counts": dict(wd.most_common()),
        }
        top_wd = "·".join(k for k, _ in wd.most_common(2))
        freq = f"한 달 평균 {len(sub) / n_months:.1f}번" if weeks >= 4 else f"기록된 기간에 {len(sub)}번"
        result["suggestions"].append(
            f"대타는 {freq}, 주로 {top_wd}요일에 들어왔어요. 그 요일 계획은 여유 있게 잡는 걸 제안해요.")

    # 도서: 대출→반납 기간
    lib = by_cat.get("도서", [])
    if lib:
        pairs = library_durations(lib)
        info: dict = {"count": len(lib), "pairs": len(pairs)}
        if pairs:
            days = [p["days"] for p in pairs]
            info.update(avg_days=round(statistics.mean(days), 1), median_days=statistics.median(days),
                        min_days=min(days), max_days=max(days))
            result["suggestions"].append(
                f"책은 빌린 뒤 보통 {statistics.median(days):g}일 만에 반납했어요(최소 {min(days)}일, 최대 {max(days)}일). 반납일 전날 알림을 제안해요.")
        result["patterns"]["도서"] = info

    # 스케줄 변동: 자주 바뀌는 때
    chg = by_cat.get("스케줄 변동", [])
    if chg:
        per_month = Counter(f"{e.start.year}-{e.start.month:02d}" for e in chg)
        wd = Counter(WD_NAMES[e.start.weekday()] for e in chg)
        dom = Counter("월초(1~10일)" if e.start.day <= 10 else "월중(11~20일)" if e.start.day <= 20 else "월말(21일~)" for e in chg)
        result["patterns"]["스케줄 변동"] = {
            "count": len(chg), "per_month": dict(sorted(per_month.items())),
            "weekday_counts": dict(wd.most_common()), "part_of_month": dict(dom.most_common()),
        }
        busiest = per_month.most_common(1)[0]
        result["suggestions"].append(
            f"스케줄 변동은 {busiest[0]}에 가장 많았고({busiest[1]}번), 주로 {dom.most_common(1)[0][0]}에 생겼어요.")

    # 약속: 요일·시간대
    apt = by_cat.get("약속", [])
    if apt:
        wd = Counter(WD_NAMES[e.start.weekday()] for e in apt)
        hours = Counter(e.start.hour for e in apt if not e.all_day)
        result["patterns"]["약속"] = {
            "count": len(apt), "weekday_counts": dict(wd.most_common()),
            "common_hours": [f"{h:02d}시" for h, _ in hours.most_common(3)],
        }

    # 요일별 평균 일정 시간
    busy = defaultdict(float)
    for e in past:
        if not e.all_day:
            busy[e.start.weekday()] += (e.end - e.start).total_seconds() / 3600
    if busy:
        avg = {WD_NAMES[k]: round(v / weeks, 1) for k, v in sorted(busy.items())}
        result["patterns"]["요일별 평균 일정 시간"] = avg
        top = max(avg, key=avg.get)
        result["suggestions"].append(f"{top}요일이 가장 바빠요(평균 {avg[top]}시간). 이 날엔 할 일을 적게 배치할게요.")

    # 최근 예시 (이름 가림)
    result["recent_examples"] = [
        {"date": e.start.strftime("%m/%d"), "category": e.category, "title": mask_people(e.title)}
        for e in past[-8:]
    ]
    result["monthly"] = {k: dict(v) for k, v in result["monthly"].items()}
    return result
