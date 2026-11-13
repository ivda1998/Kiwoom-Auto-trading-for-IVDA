# skills/candle_probe.py - 분봉 확정 시점 측정(기록 전용)
"""봉 마감 직후 몇 초 만에 공식 분봉이 확정되는지 잰다.

봉 마감 뒤 +1, +2, +3, +4, +6, +8, +30초에 분봉을 다시 조회해 '방금 마감된 봉'의
시고저종·거래량을 CSV로 남긴다. +30초 값을 최종으로 보고, 그보다 일찍 조회한 값이
언제부터 같아지는지로 신호 분봉 조회 여유(config.CANDLE_DUE_MARGIN_SEC)를 정한다.

매매 상태는 읽지도 쓰지도 않는다. 예외는 전부 삼키고, 15:21에 스스로 끝난다.
config.CANDLE_PROBE=true 일 때만 main이 띄운다(나무 인스턴스 전용 — 일일 호출 한도 없음).
"""
import csv
import logging
import os
import threading
import time
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)

OFFSETS = (1, 2, 3, 4, 6, 8, 30)   # +10초는 봇 자신의 조회와 겹치지 않게 뺌
FIELDS = ["date", "bar_start", "code", "offset_s", "fetched_at", "latency_ms",
          "found", "open", "high", "low", "close", "volume"]


def next_boundary(now: datetime, interval_min: int) -> datetime:
    """now 이후 첫 봉 마감 시각(09:00 기준 interval 배수)."""
    base = now.replace(hour=9, minute=0, second=0, microsecond=0)
    if now < base:
        return base + timedelta(minutes=interval_min)
    k = int((now - base).total_seconds() // (interval_min * 60)) + 1
    return base + timedelta(minutes=interval_min * k)


def closed_bar(df, bar_start: datetime):
    """조회 결과에서 bar_start에 시작한 봉 한 줄(dict) 또는 None."""
    if df is None or getattr(df, "empty", True):
        return None
    key = bar_start.strftime("%Y%m%d%H%M%S")
    for _, row in df.iterrows():
        if str(row["datetime"]).strip()[:14] == key:
            return {k: abs(int(row[k])) for k in ("open", "high", "low", "close", "volume")}
    return None


class CandleProbe:
    def __init__(self, api, codes, interval_min: int, out_path: str,
                 offsets=OFFSETS, start_hm: str = "09:03", end_hm: str = "15:21",
                 now_fn=datetime.now, sleep_fn=time.sleep):
        self.api, self.codes, self.interval_min = api, list(dict.fromkeys(codes)), int(interval_min)
        self.out_path, self.offsets = out_path, tuple(sorted(offsets))
        self.start_hm, self.end_hm = start_hm, end_hm
        self._now, self._sleep = now_fn, sleep_fn
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="candle-probe", daemon=True)
        self._thread.start()
        logger.info(f"[CandleProbe] 분봉 확정 시점 측정 시작: {self.codes} {self.interval_min}분봉 "
                    f"오프셋 {self.offsets}초 → {self.out_path}")

    def stop(self):
        self._stop.set()

    def _wait_until(self, target: datetime) -> bool:
        while not self._stop.is_set():
            left = (target - self._now()).total_seconds()
            if left <= 0:
                return True
            self._sleep(min(left, 0.5))
        return False

    def _write(self, rows):
        new = not os.path.exists(self.out_path)
        with open(self.out_path, "a", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=FIELDS)
            if new:
                w.writeheader()
            w.writerows(rows)

    def probe_boundary(self, boundary: datetime):
        """한 봉 마감에 대해 모든 오프셋을 조회해 기록."""
        bar_start = boundary - timedelta(minutes=self.interval_min)
        for off in self.offsets:
            if not self._wait_until(boundary + timedelta(seconds=off)):
                return
            rows = []
            for code in self.codes:
                t0 = time.time()
                at = self._now()
                try:
                    df = self.api.get_minute_data(code, tick_range=self.interval_min, count=3)
                except Exception as e:
                    logger.debug(f"[CandleProbe] {code} 조회 오류: {e}")
                    df = None
                bar = closed_bar(df, bar_start)
                rows.append({
                    "date": bar_start.strftime("%Y%m%d"), "bar_start": bar_start.strftime("%H%M%S"),
                    "code": code, "offset_s": off, "fetched_at": at.strftime("%H:%M:%S.%f")[:-3],
                    "latency_ms": int((time.time() - t0) * 1000), "found": 1 if bar else 0,
                    **(bar or {k: "" for k in ("open", "high", "low", "close", "volume")}),
                })
            try:
                self._write(rows)
            except Exception as e:
                logger.debug(f"[CandleProbe] 기록 실패: {e}")

    def _run(self):
        try:
            while not self._stop.is_set():
                now = self._now()
                hm = now.strftime("%H:%M")
                if hm >= self.end_hm:
                    break
                boundary = next_boundary(now, self.interval_min)
                if boundary.strftime("%H:%M") < self.start_hm:
                    if not self._wait_until(boundary):
                        break
                    continue
                if boundary.strftime("%H:%M") > self.end_hm:
                    break
                if not self._wait_until(boundary):
                    break
                self.probe_boundary(boundary)
        except Exception as e:
            logger.warning(f"[CandleProbe] 중단: {e}")
        logger.info("[CandleProbe] 측정 종료")
