"""에이전트: 목표를 받으면 도구를 골라 여러 단계로 일한다. 모든 단계를 기록해 화면에 보여준다.

두뇌(backend)는 세 가지 중 하나:
- rule   : AI 없이 규칙으로 도구를 고른다. 인터넷·키 없이 항상 동작 (기본 대비책)
- ollama : 노트북 안에서 도는 로컬 AI. 일정 내용이 밖으로 나가지 않는다.
- claude : Anthropic Claude API. 더 똑똑하지만 일정 내용이 인터넷으로 전송된다.
어느 두뇌든 캘린더를 직접 바꾸지 못하고 '등록 제안'까지만 만든다.
"""
from __future__ import annotations

import json
import os
import re
import urllib.request
from datetime import date, datetime, timedelta

from .dates import fmt_date, parse_when
from .store import Store
from .tools import Toolbox


def _short(obj, n: int = 300) -> str:
    s = json.dumps(obj, ensure_ascii=False)
    return s if len(s) <= n else s[:n] + "…"


class Trace:
    def __init__(self):
        self.steps: list[dict] = []

    def run(self, tb: Toolbox, name: str, args: dict, why: str) -> dict:
        out = tb.call(name, args)
        self.steps.append({"tool": name, "why": why, "input": args, "output": _short(out)})
        return out


# ─────────────────────── 규칙 기반 에이전트 ───────────────────────

class RuleAgent:
    name = "rule"

    def __init__(self):
        self.waiting: dict | None = None  # 날짜·시간을 되물은 상태

    def _merge_answer(self, answer: str) -> str:
        base = self.waiting["text"]
        a = answer.strip()
        if re.search(r"오전|오후|아침|저녁|밤|낮", a):
            per = re.search(r"오전|오후|아침|저녁|밤|낮", a)[0]
            return re.sub(r"(\d{1,2}\s*시|\d{1,2}:\d{2})", per + r" \1", base, count=1)
        if "다음" in a:
            if re.search(r"이번\s*주", base):
                return re.sub(r"이번\s*주", "다음 주", base, count=1)
            return re.sub(r"([월화수목금토일]요일)", r"다음 주 \1", base, count=1)
        if "오늘" in a:
            return "오늘 " + re.sub(r"(?<![가-힣])[월화수목금토일]요일", "", base, count=1)
        if a in ("네", "예", "응", "맞아") and re.search(r"이번\s*주", base):
            return re.sub(r"이번\s*주", "다음 주", base, count=1)
        return base + " " + a

    def run(self, message: str, tb: Toolbox) -> dict:
        tr = Trace()
        msg = message.strip()
        if self.waiting:
            merged = self._merge_answer(msg)
            tr.steps.append({"tool": "(대화 기억)", "why": "앞에서 물어본 것에 대한 답으로 보고 원래 문장에 합침",
                             "input": {"원래": self.waiting["text"], "답": msg}, "output": merged})
            self.waiting = None
            return self._add_event(merged, tb, tr)

        if re.search(r"분석|패턴|습관|통계|기록", msg):
            r = tr.run(tb, "analyze_patterns", {}, "생활 패턴을 물어봐서 기록 분석 도구를 고름")
            lines = ["1월부터의 기록을 분석했어요. (제안일 뿐, 고정 일정은 바꾸지 않아요)"]
            lines += [f"· {s}" for s in r.get("suggestions", [])]
            return self._done(lines, tr, tb)

        if re.search(r"출석|결석|에프|F\b", msg):
            r = tr.run(tb, "attendance", {}, "출석·결석을 물어봐서 출석 현황 도구를 고름")
            lines = ["이번 학기 강의별 출석 현황이에요. (제목에 '결석'이 붙은 강의를 결석으로 셌어요)"]
            for c in r["courses"]:
                lines.append(f"· [{c['status']}] {c['course']}: {c['message']} (지금까지 {c['held']}번 중)")
            if not r["courses"]:
                lines.append("· 대학 강의로 분류된 일정이 없어요. 설정 → 일정 종류 정하기에서 강의를 '대학 강의'로 골라 주세요.")
            return self._done(lines, tr, tb)

        if re.search(r"할\s*일|할일|투두|todo", msg, re.I) and not re.search(r"계획|짜", msg):
            r = tr.run(tb, "list_todos", {}, "할 일 정리를 요청해서 할 일 도구를 고름")
            lines = ["할 일을 마감순 → 중요도순으로 정리했어요."]
            for t in r["todos"]:
                rep = f", {t['repeat_label']}" if t.get("repeat_label") else ""
                lines.append(f"· [{t['d_day']}] {t['title']} (중요도 {t.get('importance', 2)}, {t.get('minutes', 30)}분{rep})")
            return self._done(lines, tr, tb)

        if re.search(r"계획|요약|브리핑|일정\s*(뭐|어때|알려)|뭐\s*있", msg):
            w = parse_when(msg, tb.today)
            day = w.date or tb.today
            tr.steps.append({"tool": "resolve_datetime", "why": "어느 날의 계획인지 코드로 계산",
                             "input": {"text": msg}, "output": fmt_date(day)})
            r = tr.run(tb, "plan_day", {"date": day.isoformat()}, "빈 시간에 할 일을 넣는 하루 계획 도구를 고름")
            return self._done([r["summary"]], tr, tb)

        return self._add_event(msg, tb, tr)

    def _add_event(self, text: str, tb: Toolbox, tr: Trace) -> dict:
        w = tr.run(tb, "resolve_datetime", {"text": text}, "일정 등록으로 보고, 날짜·시간을 코드로 계산")
        if not w.get("date") and not w.get("start"):
            return self._done(["무엇을 도와드릴까요? 예) '다음 주 화요일 7시 ○○랑 저녁', '내일 계획 짜줘', '내 패턴 분석해줘', '할 일 정리'"], tr, tb)
        qs = list(w.get("questions", []))
        if not w.get("date"):
            qs.append("며칠인가요?")
        title = w.get("rest") or ""
        if not title:
            qs.append("어떤 일정인가요? (제목)")
        if qs:
            self.waiting = {"text": text}
            return self._done(["확인이 필요해요:"] + [f"· {q}" for q in qs], tr, tb, asking=True)
        args = {"title": title, "date": w["date"]}
        if w.get("start"):
            args["start"] = w["start"]
            if w.get("end"):
                args["end"] = w["end"]
            c = tr.run(tb, "check_conflicts", {**args}, "등록 전에 겹침·이동 시간·빽빽함을 미리 확인")
        r = tr.run(tb, "propose_event", args, "등록 '제안'을 만들고 종류별 알림을 붙임 (아직 등록 안 됨)")
        if "error" in r:
            return self._done([f"문제가 생겼어요: {r['error']}"], tr, tb)
        ev = r["event"]
        lines = [f"이렇게 등록할까요? → {ev['date']} {ev['time']} [{ev['category']}] {ev['title']}"]
        lines += ["· 알림: " + ", ".join(x["label"] for x in r["reminders"])]
        lines += [f"· {x}" for x in r["warnings"]]
        lines.append("아래 '네, 등록' 버튼을 눌러야 실제로 등록돼요.")
        return self._done(lines, tr, tb)

    def _done(self, lines, tr: Trace, tb: Toolbox, asking: bool = False) -> dict:
        return {"reply": "\n".join(lines), "steps": tr.steps, "pending": tb.proposed, "backend": self.name,
                "asking": asking}


# ─────────────────────── AI(LLM) 에이전트 ───────────────────────

SYSTEM = """너는 사용자의 개인 일정 비서다. 쉬운 한국어로 짧게 답한다.
오늘은 {today} 이고 시간대는 한국(Asia/Seoul)이다.
규칙:
- 날짜·요일·'다음 주'·'모레' 계산은 절대 짐작하지 말고 resolve_datetime 도구로 한다.
- 날짜나 시간이 애매하면(questions 가 있으면) 도구를 더 쓰지 말고 사용자에게 물어본다.
- 일정 등록은 propose_event 로 '제안'만 만든다. 실제 등록은 사용자가 화면에서 '네'를 누를 때만 된다. 등록됐다고 말하지 마라.
- 등록 제안 전에 check_conflicts 로 겹침을 확인하고, 경고가 있으면 알려준다.
- 생활 패턴은 '제안'으로만 말하고, 고정 일정을 바꾸자고 우기지 않는다.
- 생활 방식에 관한 판단(대타를 받을지 등)은 추측하지 말고 사용자에게 묻는다."""


class OllamaAgent:
    """노트북 안의 로컬 AI (https://ollama.com). 일정이 밖으로 나가지 않는다."""
    name = "ollama"
    URL = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434")

    def __init__(self, model: str):
        self.model = model
        self.history: list[dict] = []

    @classmethod
    def available(cls) -> bool:
        try:
            with urllib.request.urlopen(cls.URL + "/api/tags", timeout=0.7) as r:
                return r.status == 200
        except Exception:
            return False

    def _post(self, body: dict) -> dict:
        req = urllib.request.Request(self.URL + "/api/chat", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=180) as r:
            return json.loads(r.read())

    def run(self, message: str, tb: Toolbox) -> dict:
        tr = Trace()
        tools = [{"type": "function", "function": {"name": t["name"], "description": t["description"],
                                                   "parameters": t["input_schema"]}} for t in Toolbox.SPECS]
        msgs = [{"role": "system", "content": SYSTEM.format(today=fmt_date(tb.today))}] + self.history[-10:] + \
               [{"role": "user", "content": message}]
        reply = ""
        for _ in range(8):
            r = self._post({"model": self.model, "messages": msgs, "tools": tools, "stream": False})
            m = r.get("message", {})
            msgs.append(m)
            calls = m.get("tool_calls") or []
            if not calls:
                reply = m.get("content", "")
                break
            for c in calls:
                fn = c["function"]
                args = fn.get("arguments") or {}
                if isinstance(args, str):
                    args = json.loads(args or "{}")
                out = tr.run(tb, fn["name"], args, "로컬 AI가 고른 도구")
                msgs.append({"role": "tool", "content": json.dumps(out, ensure_ascii=False)})
        self.history += [{"role": "user", "content": message}, {"role": "assistant", "content": reply}]
        return {"reply": reply, "steps": tr.steps, "pending": tb.proposed, "backend": f"ollama:{self.model}"}


class ClaudeAgent:
    """Anthropic Claude API. ANTHROPIC_API_KEY 가 있어야 하고, 일정 내용이 인터넷으로 전송된다."""
    name = "claude"

    def __init__(self, model: str):
        import anthropic  # pip install anthropic
        self.client = anthropic.Anthropic()
        self.model = model
        self.history: list[dict] = []

    @staticmethod
    def available() -> bool:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return False
        try:
            import anthropic  # noqa: F401
            return True
        except ImportError:
            return False

    def run(self, message: str, tb: Toolbox) -> dict:
        tr = Trace()
        messages = self.history[-10:] + [{"role": "user", "content": message}]
        reply = ""
        for _ in range(10):
            resp = self.client.beta.messages.create(
                model=self.model, max_tokens=16000,
                system=SYSTEM.format(today=fmt_date(tb.today)),
                tools=Toolbox.SPECS, messages=messages,
                thinking={"type": "adaptive"}, output_config={"effort": "low"},
                betas=["server-side-fallback-2026-07-01"], fallbacks="default",
            )
            if resp.stop_reason == "refusal":
                reply = "AI가 이 요청을 처리하지 않았어요. 다르게 말해 주세요."
                break
            messages.append({"role": "assistant", "content": resp.content})
            uses = [b for b in resp.content if b.type == "tool_use"]
            if resp.stop_reason != "tool_use" or not uses:
                reply = "".join(b.text for b in resp.content if b.type == "text")
                break
            results = []
            for b in uses:
                out = tr.run(tb, b.name, dict(b.input), "Claude가 고른 도구")
                results.append({"type": "tool_result", "tool_use_id": b.id,
                                "content": json.dumps(out, ensure_ascii=False)})
            messages.append({"role": "user", "content": results})
        self.history += [{"role": "user", "content": message}, {"role": "assistant", "content": reply or "(응답 없음)"}]
        return {"reply": reply, "steps": tr.steps, "pending": tb.proposed, "backend": f"claude:{self.model}"}


class Agent:
    """설정에 따라 두뇌를 고르고, AI가 실패하면 규칙 기반으로 대신한다."""

    def __init__(self, store: Store):
        self.store = store
        self.rule = RuleAgent()
        self._llm = None
        self._llm_key = None

    def pick(self) -> str:
        cfg = self.store.settings["llm"]
        b = cfg.get("backend", "auto")
        if b == "auto":
            # 개인정보 우선: 로컬 AI → (키가 있으면) Claude → 규칙
            if OllamaAgent.available():
                return "ollama"
            return "rule"
        return b

    def run(self, message: str, today: date | None = None, now: datetime | None = None) -> dict:
        tb = Toolbox(self.store, today=today, now=now)
        backend = self.pick()
        cfg = self.store.settings["llm"]
        if backend != "rule":
            try:
                key = (backend, cfg.get("ollama_model"), cfg.get("claude_model"))
                if self._llm is None or self._llm_key != key:
                    self._llm = OllamaAgent(cfg["ollama_model"]) if backend == "ollama" else ClaudeAgent(cfg["claude_model"])
                    self._llm_key = key
                return self._llm.run(message, tb)
            except Exception as e:  # AI 연결 실패 → 규칙 기반으로
                res = self.rule.run(message, Toolbox(self.store, today=today, now=now))
                res["reply"] = f"(AI 연결 실패: {e} → 규칙 기반으로 처리)\n" + res["reply"]
                return res
        return self.rule.run(message, tb)
