"""맥에서 띄우는 작은 웹 서버. 브라우저로 http://127.0.0.1:8765 에 접속해 쓴다.

python3 -m schedule_agent.server            # 이 맥에서만 접속 가능 (기본, 안전)
python3 -m schedule_agent.server --lan      # 같은 와이파이의 폰에서도 접속 (접속 코드 필요)
"""
from __future__ import annotations

import argparse
import json
import re
import secrets
import socket
import threading
import webbrowser
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config
from .agent import Agent
from .analysis import analyze
from .config import TZ
from .ics import parse_ics
from .categories import mask_people
from .planner import plan_day, sort_todos
from .store import Store

WEB = Path(__file__).resolve().parent / "web"
IMAGE_TYPES = {b"\xff\xd8\xff": "image/jpeg", b"\x89PNG": "image/png", b"GIF8": "image/gif", b"RIFF": "image/webp"}


def _image_type(data: bytes) -> str | None:
    return next((t for sig, t in IMAGE_TYPES.items() if data.startswith(sig)), None)


def range_view(store: Store, start: date, end: date) -> list[dict]:
    """start~end(포함) 날짜별 일정 + 그날 마감인 할 일. (다른 사람 이름은 가림)"""
    from datetime import timedelta
    a = datetime.combine(start, datetime.min.time()).replace(tzinfo=TZ)
    b = datetime.combine(end, datetime.min.time()).replace(tzinfo=TZ) + timedelta(days=1)
    days: dict[str, dict] = {}
    d = start
    while d <= end:
        days[d.isoformat()] = {"date": d.isoformat(), "items": [], "todos": [], "busy_min": 0}
        d += timedelta(days=1)
    for e in store.events_between(a, b):
        cur = max(e.start.date(), start)
        last = min((e.end - timedelta(seconds=1)).date(), end)
        while cur <= last:
            day = days[cur.isoformat()]
            day["items"].append({"category": e.category, "title": mask_people(e.title), "all_day": e.all_day,
                                 "start": "" if e.all_day else f"{e.start:%H:%M}",
                                 "end": "" if e.all_day else f"{e.end:%H:%M}", "source": e.source})
            if not e.all_day:
                day["busy_min"] += int((e.end - e.start).total_seconds() // 60)
            cur += timedelta(days=1)
    for t in store.todos():
        if t.get("due") and not t.get("done") and t["due"] in days:
            days[t["due"]]["todos"].append(t["title"])
    for day in days.values():
        day["items"].sort(key=lambda x: (not x["all_day"], x["start"]))
    return list(days.values())


class App:
    def __init__(self, data_dir: Path | None = None):
        self.lock = threading.RLock()
        self.data_dir = data_dir
        self.reset()

    def reset(self):
        with self.lock:
            if self.data_dir is None:
                self.store = Store()
            else:  # 공개 체험판: 가짜 예시 일정 + 방문자별 임시 폴더
                self.store = Store(data_dir=self.data_dir, calendar_paths=[config.SAMPLES / "sample_calendar.ics"])
                self.store.demo = True
            self.agent = Agent(self.store)


ACCESS_KEY: str | None = None

# ── 공개 체험판 ──
# 인터넷에 올려 여러 사람이 동시에 써 보는 모드. 가짜 예시 일정만 쓰고,
# 방문자마다 따로(쿠키로 구분) 시험하며, 서버를 다시 켜면 모두 사라진다.
# 내 캘린더·그림 올리기와 설정 저장은 막는다.
PUBLIC_DEMO = False
SESSIONS: dict[str, tuple[App, float]] = {}
SESSIONS_LOCK = threading.Lock()
MAX_SESSIONS = 300
SESSION_TTL = 3 * 3600
DEMO_BLOCKED = "공개 체험판에서는 이 기능을 쓸 수 없어요. (내 캘린더·그림 올리기, 설정 저장은 개인 노트북에서만 돼요)"
APP: App | None = None


def _new_session() -> tuple[str, App]:
    import shutil
    import tempfile
    import time
    sid = secrets.token_urlsafe(12)
    app = App(Path(tempfile.mkdtemp(prefix="demo-")))
    with SESSIONS_LOCK:
        now = time.time()
        for k, (a, t) in list(SESSIONS.items()):
            if now - t > SESSION_TTL:
                shutil.rmtree(a.data_dir, ignore_errors=True)
                del SESSIONS[k]
        while len(SESSIONS) >= MAX_SESSIONS:
            k = min(SESSIONS, key=lambda x: SESSIONS[x][1])
            shutil.rmtree(SESSIONS[k][0].data_dir, ignore_errors=True)
            del SESSIONS[k]
        SESSIONS[sid] = (app, now)
    return sid, app


class Handler(BaseHTTPRequestHandler):
    server_version = "ScheduleAgent/0.1"

    def log_message(self, fmt, *args):  # 일정 내용이 터미널 로그에 남지 않도록 조용히
        pass

    # ── 공통 ──
    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        if getattr(self, "_set_cookie", None):
            self.send_header("Set-Cookie", self._set_cookie)
            self._set_cookie = None
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200):
        self._send(code, json.dumps(obj, ensure_ascii=False, default=str).encode(), "application/json; charset=utf-8")

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        return json.loads(raw or b"{}")

    def _authed(self) -> bool:
        if ACCESS_KEY is None:
            return True
        q = parse_qs(urlparse(self.path).query)
        return self.headers.get("X-Key") == ACCESS_KEY or q.get("key", [""])[0] == ACCESS_KEY

    def _app(self) -> App:
        """이 요청을 처리할 App. 공개 체험판이면 방문자(쿠키)별 App."""
        if not PUBLIC_DEMO:
            return APP
        import time
        from http.cookies import SimpleCookie
        sid = None
        try:
            c = SimpleCookie(self.headers.get("Cookie", ""))
            if "sid" in c:
                sid = c["sid"].value
        except Exception:
            sid = None
        with SESSIONS_LOCK:
            hit = SESSIONS.get(sid) if sid else None
            if hit:
                SESSIONS[sid] = (hit[0], time.time())
                return hit[0]
        sid, app = _new_session()
        self._set_cookie = f"sid={sid}; Path=/; Max-Age={SESSION_TTL}; HttpOnly; SameSite=Lax"
        return app

    def _today(self) -> tuple[date, datetime]:
        now = datetime.now(TZ)
        return now.date(), now

    # ── GET ──
    def do_GET(self):
        url = urlparse(self.path)
        if url.path in ("/", "/index.html"):
            self._app()
            return self._send(200, (WEB / "index.html").read_bytes(), "text/html; charset=utf-8")
        if url.path == "/healthz":
            return self._send(200, b"ok", "text/plain")
        if url.path.startswith("/stickers/") and PUBLIC_DEMO:
            return self._send(404, b"", "text/plain")
        if url.path.startswith("/stickers/") and self._authed():
            slot = url.path.rsplit("/", 1)[-1]
            f = config.STICKERS_DIR / slot
            if slot in config.STICKER_SLOTS and f.exists():
                data = f.read_bytes()
                return self._send(200, data, _image_type(data) or "application/octet-stream")
            return self._send(404, b"", "text/plain")
        if not self._authed():
            return self._json({"error": "접속 코드가 필요해요."}, 401)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        app = self._app()
        st = app.store
        today, now = self._today()
        try:
            if url.path == "/api/status":
                return self._json({"demo": st.demo, "today": today.isoformat(), "backend": app.agent.pick(), "public_demo": PUBLIC_DEMO,
                                   "events": len(st.calendar_events), "files": [p.name for p in st.calendar_paths],
                                   "answered": st.settings.get("answered", False)})
            if url.path == "/api/analysis":
                return self._json(analyze(st.all_events(), today))
            if url.path == "/api/plan":
                d = date.fromisoformat(q.get("date") or today.isoformat())
                return self._json(plan_day(d, st.all_events(), st.todos(), st.settings, now=now,
                                           att=st.attendance(today)))
            if url.path == "/api/attendance":
                from .attendance import semester_range, summary
                a, b = semester_range(today, st.settings)
                return self._json({**summary(st.attendance(today)), "semester": [a.isoformat(), b.isoformat()],
                                   "limit": int(st.settings.get("absence_fail_count", 4))})
            if url.path == "/api/reminders":
                # 오늘(또는 date) 울릴 알림 목록 — 브라우저가 열려 있을 때 맥 알림으로 띄우는 데 씀
                from datetime import timedelta
                from .planner import reminders_for
                d = date.fromisoformat(q.get("date") or today.isoformat())
                att = st.attendance(today)
                a = datetime.combine(d, datetime.min.time()).replace(tzinfo=TZ)
                items = []
                for e in st.events_between(a, a + timedelta(days=3)):
                    if e.absent:
                        continue
                    for r in reminders_for(e, st.settings, att):
                        at = e.start - timedelta(minutes=r["minutes"])
                        if at.date() != d:
                            continue
                        name = mask_people(e.course or e.title)
                        body = f"{e.start:%H:%M} 시작 · {r['label']}"
                        if r["kind"] == "출석":
                            info = att.get(e.course or e.title)
                            if info and info["status"] != "안전":
                                body += " · " + info["message"]
                        items.append({"id": f"{e.uid}|{r['minutes']}", "at": at.isoformat(), "title": f"[{e.category}] {name}",
                                      "body": body, "kind": r["kind"]})
                items.sort(key=lambda x: x["at"])
                return self._json({"reminders": items})
            if url.path == "/api/todos":
                return self._json({"todos": st.todos(), "sorted": sort_todos(st.todos(), today)})
            if url.path == "/api/pending":
                return self._json({"pending": list(st.pending.values())})
            if url.path == "/api/log":
                return self._json({"log": list(reversed(st.change_log()))})
            if url.path == "/api/added":
                return self._json({"events": [e.to_dict() for e in st.added]})
            if url.path == "/api/settings":
                return self._json(st.settings)
            if url.path == "/api/range":
                a, b = date.fromisoformat(q["from"]), date.fromisoformat(q["to"])
                if (b - a).days > 62:
                    raise ValueError("기간이 너무 길어요.")
                return self._json({"days": range_view(st, a, b), "today": today.isoformat()})
            if url.path == "/api/titles":
                # 캘린더에 나온 제목들 (반복 일정은 한 번만) + 지금 분류 결과. 자주 나온 순.
                from collections import Counter
                from .categories import CATEGORIES
                cnt = Counter(e.title.strip() for e in st.calendar_events)
                cat = {e.title.strip(): e.category for e in st.calendar_events}
                tmap = st.settings.get("title_category", {})
                rows = [{"title": t, "shown": mask_people(t), "count": n, "category": cat[t], "fixed": t in tmap}
                        for t, n in cnt.most_common(80)]
                return self._json({"titles": rows, "categories": CATEGORIES})
            if url.path == "/api/stickers" and PUBLIC_DEMO:
                return self._json({"slots": config.STICKER_SLOTS, "have": []})
            if url.path == "/api/stickers":
                return self._json({"slots": config.STICKER_SLOTS,
                                   "have": [s for s in config.STICKER_SLOTS if (config.STICKERS_DIR / s).exists()]})
            if url.path == "/api/export":
                body = st.export_path().read_bytes()
                return self._send(200, body, "text/calendar; charset=utf-8",
                                  {"Content-Disposition": 'attachment; filename="agent_events.ics"'})
        except Exception as e:
            return self._json({"error": str(e)}, 400)
        self._json({"error": "없는 주소"}, 404)

    # ── POST ──
    def do_POST(self):
        if not self._authed():
            return self._json({"error": "접속 코드가 필요해요."}, 401)
        path = urlparse(self.path).path
        app = self._app()
        st = app.store
        today, now = self._today()
        try:
            if PUBLIC_DEMO and path in ("/api/upload_ics", "/api/upload_sticker", "/api/settings"):
                n = int(self.headers.get("Content-Length") or 0)
                if n:
                    self.rfile.read(min(n, 10_000_000))
                return self._json({"error": DEMO_BLOCKED}, 403)
            if path == "/api/upload_ics":
                n = int(self.headers.get("Content-Length") or 0)
                text = self.rfile.read(n).decode("utf-8", errors="replace")
                if "BEGIN:VCALENDAR" not in text:
                    raise ValueError("구글 캘린더에서 내보낸 .ics 파일이 아니에요.")
                count = len(parse_ics(text))
                name = re.sub(r"[^\w.\-가-힣]", "_", self.headers.get("X-Filename", "calendar.ics"))
                if not name.endswith(".ics"):
                    name += ".ics"
                dest = config.PRIVATE / "calendar"
                dest.mkdir(parents=True, exist_ok=True)
                (dest / name).write_text(text, "utf-8")
                app.reset()
                return self._json({"ok": True, "saved": f"private/calendar/{name}", "events": count})
            if path == "/api/upload_sticker":
                slot = self.headers.get("X-Slot", "")
                if slot not in config.STICKER_SLOTS:
                    raise ValueError("모르는 자리예요.")
                n = int(self.headers.get("Content-Length") or 0)
                if n > 5_000_000:
                    raise ValueError("그림이 너무 커요. (5MB 이하)")
                data = self.rfile.read(n)
                if not _image_type(data):
                    raise ValueError("jpg·png·gif·webp 그림만 넣을 수 있어요.")
                config.STICKERS_DIR.mkdir(parents=True, exist_ok=True)
                (config.STICKERS_DIR / slot).write_bytes(data)
                return self._json({"ok": True})
            b = self._body()
            if path == "/api/chat":
                with app.lock:
                    r = app.agent.run(b.get("message", ""), today=today, now=now)
                r["pending"] = [st.pending[i] for i in r.get("pending", []) if i in st.pending]
                return self._json(r)
            if path == "/api/apply":
                return self._json({"ok": True, "log": st.apply(b["id"], b.get("confirm", ""), b.get("confirm2", ""))})
            if path == "/api/cancel":
                st.cancel(b["id"])
                return self._json({"ok": True})
            if path == "/api/propose_delete":
                return self._json({"ok": True, "pending": st.propose_delete(b["uid"])})
            if path == "/api/undo":
                return self._json({"ok": True, "log": st.undo(b["log_id"])})
            if path == "/api/todos":
                st.save_todos(b["todos"])
                return self._json({"ok": True})
            if path == "/api/settings":
                if "travel_minutes" in b:  # '처음 질문' 양식을 저장했을 때만
                    b["answered"] = True
                config.save_settings(b)
                st.reload()
                return self._json({"ok": True, "settings": st.settings})
        except PermissionError as e:
            return self._json({"error": str(e), "need_confirm": True}, 403)
        except Exception as e:
            return self._json({"error": str(e)}, 400)
        self._json({"error": "없는 주소"}, 404)


def _lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def main():
    global ACCESS_KEY, PUBLIC_DEMO, APP
    import os
    ap = argparse.ArgumentParser(description="내 일정 비서")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", 8765)))
    ap.add_argument("--lan", action="store_true", help="같은 와이파이의 폰에서 접속 허용 (접속 코드 필요)")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--public-demo", action="store_true",
                    help="인터넷 공개 체험판: 가짜 예시 일정, 방문자별로 따로, 실제 저장 안 함")
    args = ap.parse_args()
    if args.public_demo or os.environ.get("PUBLIC_DEMO") == "1":
        PUBLIC_DEMO = True
        config.DEFAULT_SETTINGS["llm"]["backend"] = "rule"   # 서버에는 AI가 없으니 규칙 기반 두뇌
        config.DEFAULT_SETTINGS["answered"] = True
        config.SETTINGS_FILE = config.ROOT / "__no_settings__.json"  # 혹시 있는 개인 설정 파일을 읽지 않게
        srv = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
        print(f"공개 체험판이 켜졌어요 · 포트 {args.port}", flush=True)
        try:
            srv.serve_forever()
        except KeyboardInterrupt:
            pass
        return
    APP = App()
    host = "127.0.0.1"
    if args.lan:
        host = "0.0.0.0"
        ACCESS_KEY = secrets.token_urlsafe(8)
    srv = ThreadingHTTPServer((host, args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print("=" * 50)
    print(" 내 일정 비서가 켜졌어요.")
    print(f" 맥에서:  {url}" + (f"?key={ACCESS_KEY}" if ACCESS_KEY else ""))
    if ACCESS_KEY:
        print(f" 폰에서:  http://{_lan_ip()}:{args.port}/?key={ACCESS_KEY}  (같은 와이파이)")
    print(" 체험 모드(가짜 예시 일정)" if APP.store.demo else f" 내 캘린더 파일 {len(APP.store.calendar_paths)}개를 읽었어요.")
    print(" 끄려면 이 창을 닫거나 Ctrl+C")
    print("=" * 50)
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url + (f"?key={ACCESS_KEY}" if ACCESS_KEY else ""))).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
