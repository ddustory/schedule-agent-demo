"""일정·할 일 저장소 + 변경 기록(되돌리기용).

- 캘린더 원본(.ics)은 읽기만 한다. 절대 고치지 않는다.
- 에이전트가 추가/수정/삭제하는 일정은 '확인 → 실행' 두 단계로만 바뀐다.
  1) propose_*(): 바꿀 내용을 '대기 중 변경'으로 만든다 (아직 아무것도 안 바뀜)
  2) apply(change_id, confirm="네"): 사용자가 '네' 한 뒤에만 실제로 바꾼다.
     삭제는 confirm2="삭제" 까지 한 번 더 받아야 한다.
- 실제로 바뀐 내용은 change_log.jsonl 에 '무엇을/언제/어떻게(전→후)' 남기고, undo() 로 되돌린다.
"""
from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from . import config
from .categories import classify, split_absent
from .config import TZ
from .ics import Event, load_ics_files, to_ics

YES = {"네", "예", "응", "ㅇㅇ", "좋아", "yes", "y", "ok", "확인"}


class Store:
    def __init__(self, data_dir: Path | None = None, calendar_paths: list[Path] | None = None):
        self.demo = config.is_demo_mode() if calendar_paths is None else False
        if data_dir is None:
            data_dir = config.PRIVATE / "demo" if self.demo else config.PRIVATE
        self.dir = data_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        if calendar_paths is None:
            calendar_paths = [config.SAMPLES / "sample_calendar.ics"] if self.demo else config.calendar_files()
        self.calendar_paths = calendar_paths
        self.added_file = self.dir / "added_events.json"
        self.todos_file = self.dir / "todos.json"
        self.log_file = self.dir / "change_log.jsonl"
        self.export_dir = self.dir / "exports"
        self.pending: dict[str, dict] = {}
        self.lock = threading.RLock()
        self.settings = config.load_settings()
        self.reload()

    # ── 읽기 ──
    def reload(self) -> None:
        self.settings = config.load_settings()
        self.calendar_events = load_ics_files(self.calendar_paths)
        kw, tmap = self.settings["category_keywords"], self.settings.get("title_category")
        for e in self.calendar_events:
            base, absent = split_absent(e.title)
            e.category = classify(base if absent else e.title, kw, tmap)
            e.course, e.absent = base, absent
        self.added = self._read_added()

    def _read_added(self) -> list[Event]:
        if not self.added_file.exists():
            return []
        return [Event.from_dict(d) for d in json.loads(self.added_file.read_text("utf-8"))]

    def _write_added(self) -> None:
        self.added_file.write_text(json.dumps([e.to_dict() for e in self.added], ensure_ascii=False, indent=2), "utf-8")
        self._export()

    def all_events(self) -> list[Event]:
        return sorted(self.calendar_events + self.added, key=lambda e: e.start)

    def attendance(self, today) -> dict:
        from .attendance import attendance
        return attendance(self.all_events(), today, self.settings)

    def events_between(self, start: datetime, end: datetime) -> list[Event]:
        return [e for e in self.all_events() if e.start < end and e.end > start]

    def find_added(self, uid: str) -> Event | None:
        return next((e for e in self.added if e.uid == uid), None)

    # ── 할 일 ──
    def todos(self) -> list[dict]:
        if self.todos_file.exists():
            return json.loads(self.todos_file.read_text("utf-8"))
        if self.demo:
            sample = config.SAMPLES / "sample_todos.json"
            if sample.exists():
                return json.loads(sample.read_text("utf-8"))
        return []

    def save_todos(self, todos: list[dict]) -> None:
        self.todos_file.write_text(json.dumps(todos, ensure_ascii=False, indent=2), "utf-8")

    # ── 1단계: 제안 (아무것도 안 바뀜) ──
    def _new_pending(self, action: str, **kw) -> dict:
        cid = uuid.uuid4().hex[:8]
        ch = {"id": cid, "action": action, "created": datetime.now(TZ).isoformat(), **kw}
        with self.lock:
            self.pending[cid] = ch
        return ch

    def propose_add(self, event: Event, warnings: list[str] | None = None) -> dict:
        event.source = "agent"
        return self._new_pending("add", after=event.to_dict(), warnings=warnings or [])

    def propose_update(self, uid: str, changes: dict, warnings: list[str] | None = None) -> dict:
        ev = self.find_added(uid)
        if ev is None:
            raise ValueError("에이전트가 추가한 일정만 수정할 수 있어요. (구글 캘린더 원본은 아직 읽기 전용)")
        after = {**ev.to_dict(), **changes}
        return self._new_pending("update", uid=uid, before=ev.to_dict(), after=after, warnings=warnings or [])

    def propose_delete(self, uid: str) -> dict:
        ev = self.find_added(uid)
        if ev is None:
            raise ValueError("에이전트가 추가한 일정만 삭제할 수 있어요. (구글 캘린더 원본은 아직 읽기 전용)")
        return self._new_pending("delete", uid=uid, before=ev.to_dict(), needs_second_confirm=True)

    # ── 2단계: 확인 후 실행 ──
    def apply(self, change_id: str, confirm: str, confirm2: str = "") -> dict:
        with self.lock:
            ch = self.pending.get(change_id)
            if ch is None:
                raise ValueError("대기 중인 변경을 찾을 수 없어요. (이미 실행했거나 취소됨)")
            if confirm.strip().lower() not in YES:
                raise PermissionError("'네'라고 확인해 주셔야 실행해요.")
            if ch["action"] == "delete" and confirm2.strip() != "삭제":
                raise PermissionError("삭제는 한 번 더 확인이 필요해요. '삭제'라고 입력해 주세요.")
            if ch["action"] == "add":
                self.added.append(Event.from_dict(ch["after"]))
            elif ch["action"] == "update":
                self.added = [Event.from_dict(ch["after"]) if e.uid == ch["uid"] else e for e in self.added]
            elif ch["action"] == "delete":
                self.added = [e for e in self.added if e.uid != ch["uid"]]
            self._write_added()
            entry = {"log_id": uuid.uuid4().hex[:8], "time": datetime.now(TZ).isoformat(),
                     "action": ch["action"], "before": ch.get("before"), "after": ch.get("after"),
                     "undone": False}
            self._append_log(entry)
            del self.pending[change_id]
            return entry

    def cancel(self, change_id: str) -> None:
        with self.lock:
            self.pending.pop(change_id, None)

    # ── 변경 기록 / 되돌리기 ──
    def _append_log(self, entry: dict) -> None:
        with self.log_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def change_log(self) -> list[dict]:
        if not self.log_file.exists():
            return []
        rows = [json.loads(l) for l in self.log_file.read_text("utf-8").splitlines() if l.strip()]
        undone = {r["undo_of"] for r in rows if r.get("undo_of")}
        for r in rows:
            r["undone"] = r["log_id"] in undone
        return rows

    def undo(self, log_id: str) -> dict:
        """기록 하나를 되돌린다. (되돌린 것도 기록으로 남는다)"""
        with self.lock:
            row = next((r for r in self.change_log() if r["log_id"] == log_id), None)
            if row is None:
                raise ValueError("기록을 찾을 수 없어요.")
            if row["undone"] or row.get("undo_of"):
                raise ValueError("이미 되돌린 기록이거나 되돌리기 기록이에요.")
            before, after = row.get("before"), row.get("after")
            if row["action"] == "add":
                self.added = [e for e in self.added if e.uid != after["uid"]]
            elif row["action"] == "update":
                self.added = [Event.from_dict(before) if e.uid == before["uid"] else e for e in self.added]
            elif row["action"] == "delete":
                self.added.append(Event.from_dict(before))
            self._write_added()
            entry = {"log_id": uuid.uuid4().hex[:8], "time": datetime.now(TZ).isoformat(),
                     "action": "undo", "undo_of": log_id, "before": after, "after": before}
            self._append_log(entry)
            return entry

    # ── 구글 캘린더로 옮길 파일 ──
    def _export(self) -> Path:
        self.export_dir.mkdir(exist_ok=True)
        p = self.export_dir / "agent_events.ics"
        p.write_text(to_ics(self.added), "utf-8")
        return p

    def export_path(self) -> Path:
        return self._export()
