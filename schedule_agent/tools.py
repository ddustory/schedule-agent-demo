"""에이전트가 고를 수 있는 도구들.

중요: 캘린더를 실제로 바꾸는 도구는 없다. propose_event 는 '대기 중 변경'만 만들고,
실행은 사용자가 화면에서 '네'를 눌러야(store.apply) 일어난다.
"""
from __future__ import annotations

import datetime as _dt
from datetime import date, datetime, timedelta

from .analysis import analyze
from .categories import classify, mask_people
from .config import TZ
from .dates import fmt_date, parse_when, to_datetime
from .ics import Event
from .planner import check_conflicts, plan_day, reminders_for, sort_todos
from .store import Store


class Toolbox:
    def __init__(self, store: Store, today: date | None = None, now: datetime | None = None):
        self.store = store
        self.now = now or datetime.now(TZ)
        self.today = today or self.now.date()
        self.proposed: list[str] = []  # 이번 대화에서 만든 '대기 중 변경'

    # 도구 설명 (LLM 에게 그대로 전달)
    SPECS = [
        {"name": "resolve_datetime",
         "description": "한국어 날짜·시간 표현(예: '다음 주 화요일 7시', '모레 오후 3시 반')을 코드로 정확히 계산한다. 날짜 계산은 반드시 이 도구로 한다. questions 가 있으면 사용자에게 물어봐야 한다.",
         "input_schema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
        {"name": "list_events",
         "description": "기간 안의 일정 목록. 날짜는 YYYY-MM-DD.",
         "input_schema": {"type": "object", "properties": {"from_date": {"type": "string"}, "to_date": {"type": "string"}},
                          "required": ["from_date", "to_date"]}},
        {"name": "analyze_patterns",
         "description": "올해 1월부터의 기록으로 생활 패턴(수업 요일·시간, 대타 빈도, 도서 대출 기간, 스케줄 변동 시기)을 분석한다.",
         "input_schema": {"type": "object", "properties": {}}},
        {"name": "plan_day",
         "description": "특정 날짜의 빈 시간을 찾아 할 일을 배치하고 아침 요약을 만든다.",
         "input_schema": {"type": "object", "properties": {"date": {"type": "string", "description": "YYYY-MM-DD"}},
                          "required": ["date"]}},
        {"name": "attendance",
         "description": "이번 학기 대학 강의별 결석 횟수와 F까지 남은 횟수를 보여준다. (제목에 '결석'이 붙은 강의를 결석으로 셈)",
         "input_schema": {"type": "object", "properties": {}}},
        {"name": "list_todos",
         "description": "할 일을 마감순·중요도순으로 정리해 보여준다.",
         "input_schema": {"type": "object", "properties": {}}},
        {"name": "check_conflicts",
         "description": "새 일정이 기존 일정과 겹치는지, 이동 시간이 부족한지, 하루가 너무 빽빽한지 확인한다.",
         "input_schema": {"type": "object", "properties": {
             "title": {"type": "string"}, "date": {"type": "string"}, "start": {"type": "string", "description": "HH:MM"},
             "end": {"type": "string", "description": "HH:MM (모르면 생략)"}, "location": {"type": "string"}},
             "required": ["title", "date", "start"]}},
        {"name": "propose_event",
         "description": "일정 등록 '제안'을 만든다. 실제 등록은 사용자가 화면에서 '네'를 눌러야 된다. 종류별 알림도 자동으로 붙는다. 날짜·시간이 애매하면 이 도구를 쓰지 말고 먼저 물어본다.",
         "input_schema": {"type": "object", "properties": {
             "title": {"type": "string"}, "date": {"type": "string"}, "start": {"type": "string", "description": "HH:MM, 종일이면 생략"},
             "end": {"type": "string"}, "location": {"type": "string"}, "all_day": {"type": "boolean"}},
             "required": ["title", "date"]}},
    ]

    def call(self, name: str, args: dict) -> dict:
        fn = getattr(self, "t_" + name, None)
        if fn is None:
            return {"error": f"모르는 도구: {name}"}
        try:
            return fn(**args)
        except Exception as e:  # 도구 오류는 에이전트에게 돌려준다
            return {"error": str(e)}

    # ── 구현 ──
    def t_resolve_datetime(self, text: str) -> dict:
        return parse_when(text, self.today).to_dict()

    def t_list_events(self, from_date: str, to_date: str) -> dict:
        a = to_datetime(date.fromisoformat(from_date), datetime.min.time())
        b = to_datetime(date.fromisoformat(to_date), datetime.min.time()) + timedelta(days=1)
        evs = self.store.events_between(a, b)
        return {"events": [{"date": fmt_date(e.start.date()), "time": "종일" if e.all_day else f"{e.start:%H:%M}~{e.end:%H:%M}",
                            "category": e.category, "title": mask_people(e.title), "uid": e.uid, "source": e.source}
                           for e in evs[:60]], "count": len(evs)}

    def t_analyze_patterns(self) -> dict:
        r = analyze(self.store.all_events(), self.today)
        return {k: r[k] for k in ("period", "total", "by_category", "patterns", "suggestions")}

    def t_plan_day(self, date: str) -> dict:  # noqa: A002
        from datetime import date as _d
        d = _d.fromisoformat(date)
        return plan_day(d, self.store.all_events(), self.store.todos(), self.store.settings, now=self.now,
                        att=self.store.attendance(self.today))

    def t_attendance(self) -> dict:
        from .attendance import summary
        return summary(self.store.attendance(self.today))

    def t_list_todos(self) -> dict:
        return {"todos": sort_todos(self.store.todos(), self.today)}

    def _make_event(self, title, date, start=None, end=None, location="", all_day=False) -> Event:  # noqa: A002
        d = _dt.date.fromisoformat(date)
        cat = classify(title, self.store.settings["category_keywords"], self.store.settings.get("title_category"))
        if all_day or not start:
            s = to_datetime(d, datetime.min.time())
            return Event(title=title, start=s, end=s + timedelta(days=1), all_day=True, location=location or "", category=cat)
        h, m = map(int, start.split(":"))
        s = to_datetime(d, _dt.time(h, m))
        if end:
            h2, m2 = map(int, end.split(":"))
            e = to_datetime(d, _dt.time(h2, m2))
            if e <= s:
                e += timedelta(days=1)
        else:
            e = s + timedelta(hours=3 if cat == "수업" else 1.5 if cat == "대학 강의" else 2 if cat == "약속" else 1)
        return Event(title=title, start=s, end=e, location=location or "", category=cat)

    def t_check_conflicts(self, title, date, start, end=None, location="") -> dict:  # noqa: A002
        ev = self._make_event(title, date, start, end, location)
        return {"warnings": check_conflicts(ev, self.store.all_events(), self.store.settings,
                                            self.store.attendance(self.today))}

    def t_propose_event(self, title, date, start=None, end=None, location="", all_day=False) -> dict:  # noqa: A002
        ev = self._make_event(title, date, start, end, location, all_day)
        att = self.store.attendance(self.today)
        rem = reminders_for(ev, self.store.settings, att)
        ev.reminders = [r["minutes"] for r in rem]
        warnings = check_conflicts(ev, self.store.all_events(), self.store.settings, att)
        ch = self.store.propose_add(ev, warnings)
        self.proposed.append(ch["id"])
        return {"pending_change_id": ch["id"], "event": {
            "title": ev.title, "category": ev.category, "date": fmt_date(ev.start.date()),
            "time": "종일" if ev.all_day else f"{ev.start:%H:%M}~{ev.end:%H:%M}", "location": ev.location},
            "reminders": rem, "warnings": warnings,
            "note": "아직 등록되지 않았어요. 사용자가 '네'를 눌러야 등록돼요."}
