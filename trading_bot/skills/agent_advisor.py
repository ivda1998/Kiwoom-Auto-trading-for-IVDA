# skills/agent_advisor.py
"""지수 레버리지 진입 신호 '섀도 에이전트' — 기록만 하고 주문은 절대 바꾸지 않는다.

연구(tools/agent_lab.py, run_llm_offline.py)와 같은 정보·같은 문장으로
  - 규칙 에이전트 R1~R4 (tools/agent_context.RULES, 2026-10-25 사전등록)
  - LLM 에이전트 (agent_context.SYSTEM + format_snapshot, AGENT_LLM_MODEL)
가 각자 사이즈 배수 {0, 0.5, 1, 1.5}를 매기면 data/agent_shadow.csv 에 남긴다.
나중에 tools/score_agent_shadow.py 가 실제 거래 수익과 맞춰 '그 배수였다면'을 채점한다.

안전: on_signal()은 입력 복사 + 스레드 시작만 하고 바로 반환(진입 지연 0). 모든 예외는 삼킨다.
켜기: .env AGENT_SHADOW_ENABLED=true (+ ANTHROPIC_API_KEY, 없으면 규칙만 기록).
"""
import csv
import json
import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta

import config

logger = logging.getLogger(__name__)

_TOOLS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools")
COLUMNS = ["signal_id", "date", "time", "direction", "signal_px", "agent", "size", "reason",
           "latency_s", "model", "err"]

_instance = None
_instance_lock = threading.Lock()


def get_advisor():
    """켜져 있으면 단일 인스턴스, 꺼져 있으면 None (호출부는 None이면 아무것도 안 함)."""
    global _instance
    if not getattr(config, "AGENT_SHADOW_ENABLED", False):
        return None
    with _instance_lock:
        if _instance is None:
            _instance = AgentAdvisor()
        return _instance


class AgentAdvisor:
    def __init__(self):
        data_dir = getattr(config, "DATA_DIR", os.path.dirname(config.DB_PATH))
        self.csv_path = os.path.join(data_dir, "agent_shadow.csv")
        self.snap_dir = os.path.join(data_dir, "agent_snapshots")
        self.model = getattr(config, "AGENT_LLM_MODEL", "claude-sonnet-5-5")
        self.max_calls = int(getattr(config, "AGENT_LLM_MAX_CALLS", 10))
        self.db_path = getattr(config, "AGENT_MESSAGES_DB", "/home/ubuntu/invest/data/messages.db")
        self._write_lock = threading.Lock()
        self._day = None          # 준비된 날짜(YYYYMMDD)
        self._day_lock = threading.Lock()
        self._features = None     # 오늘 규칙용 피처
        self._ctx_built = None    # 오늘 LLM용 장전 컨텍스트
        self._calls = 0
        self._ac = None

    # ── 진입점 (지수 에이전트가 _enter 직전에 호출) ──
    def on_signal(self, direction, candles, daily_rows):
        """direction 'LEVERAGE'/'INVERSE', candles=신호 분봉 목록(Candle), daily_rows=[{'date','close'},...](오늘 제외).
        입력만 복사하고 스레드로 넘긴다."""
        try:
            now = datetime.now()
            d = now.strftime("%Y%m%d")
            today = [c for c in candles if c.is_closed and c.datetime.strftime("%Y%m%d") == d]
            sig = {
                "id": now.strftime("%Y%m%d_%H%M%S"),
                "d": d,
                "dir": "L" if direction == "LEVERAGE" else "S",
                "hhmm": (today[-1].datetime.strftime("%H%M") if today else now.strftime("%H%M")),
                "px": today[-1].close if today else 0,
                "bars": [(c.datetime.strftime("%H%M"), c.close) for c in today],
                "daily": [(str(r.get("date", "")), float(r.get("close", 0))) for r in (daily_rows or [])],
            }
            threading.Thread(target=self._run, args=(sig,), daemon=True, name="AgentShadow").start()
        except Exception as e:   # 절대 진입을 막지 않는다
            logger.warning(f"[AgentShadow] on_signal 실패(무시): {e}")

    # ── 백그라운드 ──
    def _run(self, sig):
        try:
            self._prepare(sig)
        except Exception as e:
            logger.warning(f"[AgentShadow] 장전 컨텍스트 준비 실패: {e}")
        ac = self._ac
        feats = dict(self._features or {})
        closes = [c for _, c in sig["daily"]]
        if ac:
            feats["regime"] = ac.regime(closes)
            for name, (fn, _, _) in ac.RULES.items():
                try:
                    self._write(sig, name, fn(sig["dir"], feats), "", 0.0, "rule", "")
                except Exception as e:
                    self._write(sig, name, "", "", 0.0, "rule", repr(e)[:200])
        self._llm(sig, closes)

    def _load_ac(self):
        if self._ac is None:
            if _TOOLS not in sys.path:
                sys.path.insert(0, _TOOLS)
            import agent_context
            self._ac = agent_context
        return self._ac

    def _prepare(self, sig):
        """하루 1회: 메시지 DB(읽기 전용)·해외지표로 장전 컨텍스트와 어휘점수 편차 계산."""
        with self._day_lock:
            if self._day == sig["d"]:
                return
            ac = self._load_ac()
            days = sorted({dd for dd, _ in sig["daily"]} | {sig["d"]})
            cb = ac.ContextBuilder(self.db_path, None, days)
            cb.glob = self._fetch_global(sig["d"])
            built = cb.build(sig["d"])
            prev = [dd for dd in days if dd < sig["d"]][-25:]
            hist_m, hist_r = [], []
            for dd in prev:
                b = cb.build(dd)
                hist_m.append(b["msg_score"] if b["n_messages"] else None)
                hist_r.append(b["report_score"])
            self._features = {
                "global": built["global"],
                "msg_score_dev": ac.score_deviation(built["msg_score"] if built["n_messages"] else None, hist_m),
                "report_score_dev": ac.score_deviation(built["report_score"], hist_r),
            }
            self._ctx_built = built
            self._calls = 0
            self._day = sig["d"]
            logger.info(f"[AgentShadow] 장전 컨텍스트 준비: 메시지 {built['n_messages']}건, 리포트 "
                        f"{'있음' if built['report'] else '없음'}, 해외 {built['global']}")

    @staticmethod
    def _fetch_global(d):
        """FinanceDataReader로 최근 해외 종가 → ContextBuilder.glob 형식. 실패 시 None(해외 생략)."""
        try:
            import FinanceDataReader as fdr
            start = (datetime.strptime(d, "%Y%m%d") - timedelta(days=14)).strftime("%Y-%m-%d")
            rows = {}
            for k, sym in {"spx": "US500", "ndx": "IXIC", "sox": "^SOX", "usdkrw": "USD/KRW"}.items():
                df = fdr.DataReader(sym, start)
                for idx, v in df["Close"].items():
                    rows.setdefault(idx.strftime("%Y%m%d"), {})[k] = float(v)
            return rows
        except Exception as e:
            logger.warning(f"[AgentShadow] 해외지표 조회 실패(생략): {e}")
            return None

    def _llm(self, sig, closes):
        ac = self._ac
        if not ac or self._ctx_built is None:
            return
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return   # 키 없으면 규칙만
        if self._calls >= self.max_calls:
            self._write(sig, "LLM", "", "", 0.0, self.model, f"일일 상한 {self.max_calls}회 초과")
            return
        self._calls += 1
        pc = closes[-1] if closes else (sig["bars"][0][1] if sig["bars"] else 0)
        path = [(t, (c / pc - 1) * 100) for t, c in sig["bars"]] if pc else []
        text = ac.format_snapshot(sig["dir"], sig["hhmm"], ac.regime(closes), closes[-6:], path, self._ctx_built)
        self._save_snapshot(sig, text)
        t0 = time.time()
        try:
            import anthropic
            client = anthropic.Anthropic(timeout=60.0, max_retries=1)
            msg = client.messages.create(
                model=self.model, max_tokens=8000,
                system=[{"type": "text", "text": ac.SYSTEM, "cache_control": {"type": "ephemeral"}}],
                output_config={"effort": "medium", "format": {"type": "json_schema", "schema": ac.SCHEMA}},
                messages=[{"role": "user", "content": text}])
            if msg.stop_reason == "refusal":
                self._write(sig, "LLM", "", "", time.time() - t0, self.model, "refusal")
                return
            txt = next((b.text for b in msg.content if b.type == "text"), "")
            j = json.loads(txt)
            self._write(sig, "LLM", float(j["size"]), j.get("reason", ""), time.time() - t0, self.model, "")
        except Exception as e:
            self._write(sig, "LLM", "", "", time.time() - t0, self.model, f"{type(e).__name__}: {str(e)[:150]}")

    # ── 기록 ──
    def _save_snapshot(self, sig, text):
        try:
            os.makedirs(self.snap_dir, exist_ok=True)
            with open(os.path.join(self.snap_dir, f"{sig['id']}.txt"), "w", encoding="utf-8") as f:
                f.write(text)
        except Exception:
            pass

    def _write(self, sig, agent, size, reason, latency, model, err):
        try:
            with self._write_lock:
                new = not os.path.exists(self.csv_path)
                with open(self.csv_path, "a", newline="", encoding="utf-8") as f:
                    w = csv.writer(f)
                    if new:
                        w.writerow(COLUMNS)
                    w.writerow([sig["id"], sig["d"], sig["hhmm"], sig["dir"], sig["px"], agent, size,
                                str(reason).replace("\n", " ")[:300], round(latency, 1), model, err])
        except Exception as e:
            logger.warning(f"[AgentShadow] 기록 실패: {e}")
