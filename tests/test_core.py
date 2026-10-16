"""python3 -m unittest discover tests   (가짜 예시 일정만 사용)"""
import tempfile
import unittest
from datetime import date, datetime, time
from pathlib import Path

from schedule_agent.agent import RuleAgent
from schedule_agent.analysis import analyze
from schedule_agent.categories import mask_people
from schedule_agent.config import TZ
from schedule_agent.dates import parse_when
from schedule_agent.ics import Event, parse_ics, to_ics
from schedule_agent.planner import check_conflicts, free_slots, sort_todos
from schedule_agent.store import Store
from schedule_agent.tools import Toolbox

SAMPLE = Path(__file__).resolve().parent.parent / "samples" / "sample_calendar.ics"
THU = date(2026, 10, 15)  # 목요일


def new_store() -> Store:
    return Store(data_dir=Path(tempfile.mkdtemp()), calendar_paths=[SAMPLE])


class Dates(unittest.TestCase):
    def test_next_week_tuesday(self):
        w = parse_when("다음 주 화요일 7시 ○○랑 저녁", THU)
        self.assertEqual(w.date, date(2026, 10, 20))
        self.assertEqual(w.start, time(19, 0))   # '저녁' → 오후
        self.assertEqual(w.questions, [])

    def test_relative(self):
        self.assertEqual(parse_when("모레", THU).date, date(2026, 10, 17))
        self.assertEqual(parse_when("글피", THU).date, date(2026, 10, 18))
        self.assertEqual(parse_when("3일 후", THU).date, date(2026, 10, 18))
        self.assertEqual(parse_when("2주 뒤", THU).date, date(2026, 10, 29))
        self.assertEqual(parse_when("다음 달 3일", THU).date, date(2026, 11, 3))

    def test_year_rollover(self):
        self.assertEqual(parse_when("다음 주 금요일", date(2026, 12, 28)).date, date(2027, 1, 8))
        self.assertEqual(parse_when("1월 5일", date(2026, 12, 28)).date, date(2027, 1, 5))

    def test_ambiguous_hour_asks(self):
        w = parse_when("화요일 7시 미팅", THU)
        self.assertIsNone(w.start)
        self.assertTrue(any("오전" in q for q in w.questions))

    def test_bare_weekday_today_asks(self):
        self.assertTrue(parse_when("목요일 오후 2시", THU).questions)

    def test_range_and_duration(self):
        w = parse_when("내일 오전 10시부터 12시까지 스터디", THU)
        self.assertEqual((w.start, w.end), (time(10), time(12)))
        w = parse_when("오늘 저녁 8시 2시간 운동", THU)
        self.assertEqual((w.start, w.end), (time(20), time(22)))
        self.assertEqual(w.rest, "운동")


class Ics(unittest.TestCase):
    def test_recurring_with_exdate(self):
        evs = parse_ics(SAMPLE.read_text("utf-8"))
        classes = [e for e in evs if e.title.startswith("학원 수업")]
        days = {e.start.date() for e in classes}
        self.assertIn(date(2026, 10, 15), days)
        self.assertNotIn(date(2026, 3, 3), days)            # EXDATE (휴일)
        self.assertTrue(all(e.start.weekday() in (1, 3) for e in classes))
        self.assertEqual(len([e for e in evs if e.title == "학원 주말 특강"]), 16)  # COUNT

    def test_roundtrip(self):
        s = datetime(2026, 10, 20, 19, tzinfo=TZ)
        e = Event(title="저녁; 약속, 테스트", start=s, end=s.replace(hour=21), reminders=[60])
        back = parse_ics(to_ics([e]))[0]
        self.assertEqual((back.title, back.start, back.end), (e.title, e.start, e.end))

    def test_utc_converted_to_seoul(self):
        txt = "BEGIN:VCALENDAR\nBEGIN:VEVENT\nDTSTART:20261015T100000Z\nDTEND:20261015T110000Z\nSUMMARY:x\nEND:VEVENT\nEND:VCALENDAR"
        self.assertEqual(parse_ics(txt)[0].start.hour, 19)


class Privacy(unittest.TestCase):
    def test_mask(self):
        self.assertEqual(mask_people("민수랑 저녁"), "○○랑 저녁")
        self.assertNotIn("1234", mask_people("연락 010-1234-5678"))
        self.assertEqual(mask_people("친구랑 카페"), "친구랑 카페")


class Category(unittest.TestCase):
    def test_priority(self):
        from schedule_agent.categories import classify
        from schedule_agent.config import DEFAULT_SETTINGS
        kw = DEFAULT_SETTINGS["category_keywords"]
        self.assertEqual(classify("스케줄 변동: 수업 시간 변경", kw), "스케줄 변동")
        self.assertEqual(classify("학원 수업", kw), "수업")
        self.assertEqual(classify("카페 대타", kw), "대타")
        self.assertEqual(classify("도서 반납 - 모모", kw), "도서")

    def test_title_map_first_but_keywords_still_work(self):
        from schedule_agent.categories import classify
        from schedule_agent.config import DEFAULT_SETTINGS
        kw = DEFAULT_SETTINGS["category_keywords"]
        tmap = {"수학": "수업", "문학사2": "대학 강의"}
        self.assertEqual(classify("수학", kw), "기타")            # 정하기 전엔 모름
        self.assertEqual(classify("수학", kw, tmap), "수업")
        self.assertEqual(classify("문학사2", kw, tmap), "대학 강의")
        self.assertEqual(classify("수학 대타", kw, tmap), "대타")  # 정확히 같은 제목만 지도로


class Planning(unittest.TestCase):
    def test_analysis(self):
        s = new_store()
        r = analyze(s.all_events(), THU)
        slots = {(t["weekday"], t["start"]) for t in r["patterns"]["수업"]["top_slots"]}
        self.assertIn(("화요일", "18:00"), slots)
        self.assertGreater(r["patterns"]["도서"]["pairs"], 5)

    def test_conflict_detected(self):
        s = new_store()
        tb = Toolbox(s, today=THU)
        ev = tb._make_event("가람이랑 저녁", "2026-10-20", "19:00")
        w = check_conflicts(ev, s.all_events(), s.settings)
        self.assertTrue(any("겹침" in x for x in w))

    def test_free_slots_respect_class(self):
        s = new_store()
        slots = free_slots(THU, [e for e in s.all_events() if e.start.date() == THU], s.settings)
        busy_a = datetime(2026, 10, 15, 17, 30, tzinfo=TZ)   # 수업 18~21시 + 이동 30분
        busy_b = datetime(2026, 10, 15, 21, 30, tzinfo=TZ)
        for a, b in slots:
            self.assertTrue(b <= busy_a or a >= busy_b)

    def test_sort_todos(self):
        t = sort_todos([{"title": "b", "due": "2026-10-20", "importance": 3},
                        {"title": "a", "due": "2026-10-16", "importance": 1},
                        {"title": "c", "due": None, "importance": 3}], THU)
        self.assertEqual([x["title"] for x in t], ["a", "b", "c"])


class TravelRule(unittest.TestCase):
    def _ev(self, t, h1, m1, h2, m2, cat, loc=""):
        d = date(2026, 9, 9)
        return Event(title=t, start=datetime(2026, 9, 9, h1, m1, tzinfo=TZ), end=datetime(2026, 9, 9, h2, m2, tzinfo=TZ),
                     category=cat, location=loc)

    def test_back_to_back_lectures_no_travel_warning(self):
        from schedule_agent.config import DEFAULT_SETTINGS
        from schedule_agent.planner import day_warnings
        evs = [self._ev("강의A", 10, 30, 12, 0, "대학 강의"), self._ev("강의B", 12, 0, 13, 30, "대학 강의")]
        self.assertEqual(day_warnings(evs, DEFAULT_SETTINGS), [])

    def test_lecture_to_academy_needs_travel(self):
        from schedule_agent.config import DEFAULT_SETTINGS
        from schedule_agent.planner import day_warnings
        evs = [self._ev("강의", 15, 0, 17, 50, "대학 강의"), self._ev("수학", 18, 0, 21, 0, "수업")]
        self.assertTrue(any("이동" in w for w in day_warnings(evs, DEFAULT_SETTINGS)))


class Attendance(unittest.TestCase):
    def test_absences_counted_from_recurring_override(self):
        s = new_store()
        att = s.attendance(THU)
        c = att["교육학개론 강의"]
        self.assertEqual((c["absent"], c["limit"], c["status"]), (2, 4, "주의"))
        self.assertEqual(c["absent_dates"], ["2026-09-16", "2026-10-07"])

    def test_status_levels(self):
        from schedule_agent.attendance import status_of
        self.assertEqual(status_of(0, 4)[0], "안전")
        self.assertEqual(status_of(2, 4)[0], "주의")
        self.assertEqual(status_of(3, 4)[0], "위험")
        self.assertIn("F", status_of(3, 4)[1])
        self.assertEqual(status_of(4, 4)[0], "F 기준")

    def test_semester_auto(self):
        from schedule_agent.attendance import semester_range
        self.assertEqual(semester_range(date(2026, 10, 15), {})[0], date(2026, 9, 1))
        self.assertEqual(semester_range(date(2026, 4, 1), {})[0], date(2026, 3, 1))
        self.assertEqual(semester_range(date(2027, 1, 10), {})[0], date(2026, 9, 1))

    def test_attendance_reminder_and_conflict(self):
        s = new_store()
        tb = Toolbox(s, today=THU)
        r = tb.t_propose_event("치과", "2026-10-19", "10:00", "11:00")   # 월 교육학개론 10:30과 겹침
        self.assertTrue(any("결석 3/4" in w and "F" in w for w in r["warnings"]))
        lec = next(e for e in s.all_events() if e.course == "교육학개론 강의" and e.start.date() == date(2026, 10, 19))
        from schedule_agent.planner import reminders_for
        rem = reminders_for(lec, s.settings, s.attendance(THU))
        self.assertTrue(all(x["kind"] == "출석" and "결석 2/4" in x["label"] for x in rem))


class WeeklyTodo(unittest.TestCase):
    T = {"id": "w", "title": "과제하기", "repeat": "weekly", "weekday": 5, "minutes": 60, "since": "2026-10-01"}

    def test_next_instance(self):
        t = sort_todos([{**self.T, "since": "2026-10-11"}], THU)[0]   # 목 → 이번 토요일
        self.assertEqual((t["due"], t["repeat_label"]), ("2026-10-17", "매주 토"))

    def test_missed_week_shows_overdue_then_moves_on(self):
        t = sort_todos([self.T], THU)[0]
        self.assertEqual((t["due"], t["d_day"]), ("2026-10-10", "마감 지남"))
        t = sort_todos([{**self.T, "done_dates": ["2026-10-10"]}], THU)[0]
        self.assertEqual(t["due"], "2026-10-17")
        t = sort_todos([{**self.T, "done_dates": ["2026-10-10", "2026-10-17"]}], THU)[0]
        self.assertEqual(t["due"], "2026-10-24")


class Safety(unittest.TestCase):
    def test_nothing_changes_without_yes(self):
        s = new_store()
        res = RuleAgent().run("다음 주 수요일 오후 3시 치과", Toolbox(s, today=THU))
        self.assertEqual(len(res["pending"]), 1)
        self.assertEqual(s.added, [])                       # 제안만, 등록 안 됨
        with self.assertRaises(PermissionError):
            s.apply(res["pending"][0], confirm="아니")
        self.assertEqual(s.added, [])
        log = s.apply(res["pending"][0], confirm="네")
        self.assertEqual(len(s.added), 1)
        self.assertTrue(s.export_path().exists())
        s.undo(log["log_id"])                               # 되돌리기
        self.assertEqual(s.added, [])
        self.assertEqual(len(s.change_log()), 2)

    def test_delete_needs_second_confirm(self):
        s = new_store()
        res = RuleAgent().run("다음 주 수요일 오후 3시 치과", Toolbox(s, today=THU))
        s.apply(res["pending"][0], confirm="네")
        ch = s.propose_delete(s.added[0].uid)
        with self.assertRaises(PermissionError):
            s.apply(ch["id"], confirm="네")
        s.apply(ch["id"], confirm="네", confirm2="삭제")
        self.assertEqual(s.added, [])

    def test_calendar_original_is_read_only(self):
        s = new_store()
        with self.assertRaises(ValueError):
            s.propose_delete(s.calendar_events[0].uid)

    def test_ambiguous_asks_then_merges(self):
        s = new_store()
        ag = RuleAgent()
        r1 = ag.run("내일 6시 영화", Toolbox(s, today=THU))
        self.assertTrue(r1["asking"])
        self.assertEqual(r1["pending"], [])
        r2 = ag.run("오후", Toolbox(s, today=THU))
        self.assertEqual(len(r2["pending"]), 1)
        self.assertEqual(s.pending[r2["pending"][0]]["after"]["start"][11:16], "18:00")



class CalendarFiles(unittest.TestCase):
    def test_exports_and_demo_are_not_originals(self):
        from unittest import mock
        from schedule_agent import config
        tmp = Path(tempfile.mkdtemp())
        (tmp / "demo" / "exports").mkdir(parents=True)
        (tmp / "demo" / "exports" / "agent_events.ics").write_text("x")
        (tmp / "calendar").mkdir()
        (tmp / "calendar" / "mine.ics").write_text("x")
        with mock.patch.object(config, "PRIVATE", tmp):
            self.assertEqual([p.name for p in config.calendar_files()], ["mine.ics"])


if __name__ == "__main__":
    unittest.main()
