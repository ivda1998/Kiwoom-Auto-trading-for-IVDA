"""VM picks 진입 시장필터 — 갭하락·장중 급락·손절 묶음 시 신규 진입 보류.

2026-10-22 최근 1달 실거래 26건 재생에서 세 규칙 모두 '차단하면 오히려 손해'였다
(급락 한가운데 진입이 반등으로 수익, 큰 손실은 필터가 안 걸리는 급락 초입). 표본이 작아
결론을 못 내므로 기본은 기록 모드(shadow): 실제 진입은 그대로 두고 '차단 대상이었음'만
CSV·텔레그램에 남긴다. 4~8주 누적 후 그 거래들의 손익으로 차단(block) 여부를 결정한다.

VM_GUARD_MODE: off(검사 안 함) / shadow(기록만, 기본) / block(실제 보류)
지수 프록시는 122630(코스피200 2배, 모의에서 이미 폴링 중 → 추가 호출 없음)이라
임계값(코스피 기준)에 VM_GUARD_PROXY_LEV를 곱해 비교한다.

VM_TREND_GATE (2026-11-16 추가, VM_GUARD_MODE와 별개로 실제 차단): off / or / and
  삼성전자(005930)·지수(122630) 일봉 MA10 > MA20(전일까지, 당일 미포함 — 레버리지 전략과 같은 기준)을
  'or'=둘 중 하나, 'and'=둘 다 만족할 때만 VM 신규 매수. 보유 종목 매도는 영향 없음.
  근거: 키움 VM 177건 재생 — AND 시 남긴 67건 +198만(건당 +0.97%) vs 빠진 110건 −83만, 차이 t 1.41.
  일봉 조회 실패 시 통과(기존 필터와 같은 fail-open) + 경고 로그."""
import csv
import logging
import os
import sqlite3
import time
from datetime import datetime

import config

logger = logging.getLogger(__name__)

_LEV_CODES = ("0193W0", "0193L0", "122630", "114800")


class VMMarketGuard:
    def __init__(self, kiwoom, notifier, price_fn):
        self.kiwoom = kiwoom
        self.notifier = notifier
        self.price_fn = price_fn            # code -> 최근가(없으면 None)
        self._day = None
        self._prev_close = None
        self._today_open = None
        self._daily_tried_at = 0.0
        self._stops_cache = (0.0, 0, 0)     # (ts, 당일, 직전3거래일)
        self._notified = set()              # (date, code) — 텔레그램 하루 1회
        self.log_path = os.path.join(config.DATA_DIR, "vm_guard_shadow.csv")
        self._trend = {}                    # {"day": YYYYMMDD, "sam": bool|None, "idx": bool|None, "tried": ts}
        self._trend_logged = set()          # (date, code, event) — 같은 차단 반복 기록 방지

    # ── 공개 API ─────────────────────────────────────────
    def check(self, code: str, name: str, event: str):
        """(blocked, reasons). shadow 모드는 reasons를 기록만 하고 blocked=False."""
        tblock = self._trend_block()
        if tblock:
            self._record_trend(code, name, event, tblock)
            return True, [tblock]
        mode = getattr(config, "VM_GUARD_MODE", "shadow")
        if mode == "off":
            return False, []
        try:
            reasons = self._reasons()
        except Exception as e:
            logger.warning(f"[VMGuard] 판정 실패 → 통과: {e}")
            return False, []
        if not reasons:
            return False, []
        blocked = mode == "block"
        self._record(code, name, event, mode, blocked, reasons)
        return blocked, reasons

    # ── 추세 조건 (실제 차단) ─────────────────────────────
    def _ma_up(self, code, today):
        """전일까지 일봉 MA10 > MA20 → True/False, 데이터 부족·실패 → None."""
        df = self.kiwoom.get_daily_data(code, count=40)
        if df is None or df.empty:
            return None
        rows = [r for r in df.to_dict("records") if str(r.get("date")) < today and r.get("close")]
        rows.sort(key=lambda r: str(r.get("date")))
        c = [float(r["close"]) for r in rows]
        if len(c) < 20:
            return None
        return sum(c[-10:]) / 10 > sum(c[-20:]) / 20

    def _trend_block(self):
        """차단 사유 문자열 또는 None(통과)."""
        mode = getattr(config, "VM_TREND_GATE", "off")
        if mode not in ("or", "and"):
            return None
        today = datetime.now().strftime("%Y%m%d")
        t = self._trend
        if t.get("day") != today or ((t.get("sam") is None or t.get("idx") is None)
                                     and time.time() - t.get("tried", 0) >= 600):
            try:
                sam = self._ma_up(getattr(config, "VM_TREND_SAMSUNG_CODE", "005930"), today)
                idx = self._ma_up(getattr(config, "VM_TREND_INDEX_CODE", "122630"), today)
            except Exception as e:
                logger.warning(f"[VMGuard] 추세 조건 일봉 조회 실패 → 통과: {e}")
                sam = idx = None
            self._trend = t = {"day": today, "sam": sam, "idx": idx, "tried": time.time()}
            lbl = lambda v: "UP" if v else ("DOWN" if v is not None else "?")
            logger.info(f"[VMGuard] 추세 조건({mode.upper()}) 판정: 삼성 {lbl(sam)} · 지수 {lbl(idx)}")
        sam, idx = t["sam"], t["idx"]
        if sam is None or idx is None:
            return None                       # 판정 불가 → 통과(fail-open)
        ok = (sam or idx) if mode == "or" else (sam and idx)
        if ok:
            return None
        return (f"추세 조건 {mode.upper()} 미충족(일봉 MA10/20: 삼성 {'UP' if sam else 'DOWN'}"
                f" · 지수 {'UP' if idx else 'DOWN'})")

    def _record_trend(self, code, name, event, reason):
        now = datetime.now()
        key = (now.strftime("%Y-%m-%d"), code, event)
        if key in self._trend_logged:
            return
        self._trend_logged.add(key)
        try:
            new = not os.path.exists(self.log_path)
            with open(self.log_path, "a", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["date", "time", "code", "name", "event", "mode", "blocked", "reasons"])
                w.writerow([key[0], now.strftime("%H:%M:%S"), code, name, event, "trend", 1, reason])
        except Exception as e:
            logger.warning(f"[VMGuard] 기록 실패: {e}")
        logger.info(f"[VMGuard] {name}({code}) {event} — {reason} → 신규 매수 보류")
        dkey = (key[0], "trend")
        if self.notifier and dkey not in self._notified:
            self._notified.add(dkey)
            self.notifier.send(f"🧭 <b>VM 신규 매수 보류</b> — {reason}\n"
                               f"오늘 VM 지정종목은 새로 사지 않음(보유 종목 매도는 그대로). 첫 해당: {name}({code}) {event}")

    # ── 규칙 ─────────────────────────────────────────────
    def _reasons(self) -> list:
        lev = getattr(config, "VM_GUARD_PROXY_LEV", 2.0)
        proxy = getattr(config, "VM_GUARD_PROXY_CODE", "122630")
        out = []
        self._refresh_daily(proxy)
        if self._prev_close:
            gap_th = getattr(config, "VM_GUARD_GAP", -0.015)
            if self._today_open:
                g = (self._today_open / self._prev_close - 1) / lev
                if g <= gap_th:
                    out.append(f"갭하락 코스피 {g:+.1%}")
            px = self.price_fn(proxy)
            if px:
                r = (px / self._prev_close - 1) / lev
                if r <= getattr(config, "VM_GUARD_INTRADAY", -0.02):
                    out.append(f"장중하락 코스피 {r:+.1%}")
        today_n, recent_n = self._stop_counts()
        if today_n >= getattr(config, "VM_GUARD_STOPS_TODAY", 2):
            out.append(f"당일 손절 {today_n}회")
        if recent_n >= getattr(config, "VM_GUARD_STOPS_3D", 3):
            out.append(f"직전 3거래일 손절 {recent_n}회")
        return out

    def _refresh_daily(self, proxy):
        """전일 종가·당일 시가 — 하루 1~2회 일봉 조회(개장 전이면 시가 없음 → 개장 후 1회 재조회)."""
        today = datetime.now().strftime("%Y%m%d")
        if self._day != today:
            self._day, self._prev_close, self._today_open, self._daily_tried_at = today, None, None, 0.0
        need = self._prev_close is None or (self._today_open is None and datetime.now().strftime("%H%M") >= "0901")
        if not need or time.time() - self._daily_tried_at < 600:
            return
        self._daily_tried_at = time.time()
        df = self.kiwoom.get_daily_data(proxy, count=5)
        if df is None or df.empty:
            return
        rows = df.to_dict("records")
        if str(rows[-1].get("date")) == today:
            self._today_open = rows[-1].get("open") or None
            rows = rows[:-1]
        if rows:
            self._prev_close = rows[-1].get("close") or None

    def _stop_counts(self):
        ts, t_n, r_n = self._stops_cache
        if time.time() - ts < 60:
            return t_n, r_n
        today = datetime.now().strftime("%Y-%m-%d")
        ph = ",".join("?" * len(_LEV_CODES))
        with sqlite3.connect(config.DB_PATH) as conn:
            t_n = conn.execute(f"select count(*) from trades where side='SELL' and reason='손절' "
                               f"and trade_date=? and code not in ({ph})", (today, *_LEV_CODES)).fetchone()[0]
            days = [r[0] for r in conn.execute("select distinct trade_date from trades where trade_date<? "
                                               "order by trade_date desc limit 3", (today,))]
            r_n = 0
            if days:
                dp = ",".join("?" * len(days))
                r_n = conn.execute(f"select count(*) from trades where side='SELL' and reason='손절' "
                                   f"and trade_date in ({dp}) and code not in ({ph})",
                                   (*days, *_LEV_CODES)).fetchone()[0]
        self._stops_cache = (time.time(), t_n, r_n)
        return t_n, r_n

    # ── 기록 ─────────────────────────────────────────────
    def _record(self, code, name, event, mode, blocked, reasons):
        now = datetime.now()
        new = not os.path.exists(self.log_path)
        try:
            with open(self.log_path, "a", encoding="utf-8", newline="") as f:
                w = csv.writer(f)
                if new:
                    w.writerow(["date", "time", "code", "name", "event", "mode", "blocked", "reasons"])
                w.writerow([now.strftime("%Y-%m-%d"), now.strftime("%H:%M:%S"), code, name, event,
                            mode, int(blocked), " · ".join(reasons)])
        except Exception as e:
            logger.warning(f"[VMGuard] 기록 실패: {e}")
        tag = "보류" if blocked else "기록만(진입 진행)"
        logger.info(f"[VMGuard] {name}({code}) {event} — 시장필터 해당: {' · '.join(reasons)} → {tag}")
        key = (now.strftime("%Y-%m-%d"), code)
        if self.notifier and key not in self._notified:
            self._notified.add(key)
            self.notifier.send(
                f"🔎 <b>VM 시장필터 {'차단' if blocked else '[기록모드]'}</b> {name}({code}) {event}\n"
                f"해당 조건: {' · '.join(reasons)}\n"
                + ("신규 진입 보류." if blocked else "실제 진입은 그대로 진행 — 나중에 이 거래 손익으로 필터 효과 판정.")
            )
