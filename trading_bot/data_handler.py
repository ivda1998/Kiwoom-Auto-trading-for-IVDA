# data_handler.py - 실시간 틱 → 3분봉 변환 엔진

import logging
from datetime import datetime, timedelta
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional, List, Callable

import config

logger = logging.getLogger(__name__)


@dataclass
class Candle:
    """3분봉 단위 캔들"""
    code: str
    datetime: datetime
    open: int
    high: int
    low: int
    close: int
    volume: int
    is_closed: bool = False      # True = 봉 확정 완료

    @property
    def body(self) -> int:
        return self.close - self.open

    @property
    def is_bullish(self) -> bool:
        return self.close >= self.open

    @property
    def range(self) -> int:
        return self.high - self.low


class CandleBuilder:
    """
    실시간 틱 데이터를 N분봉으로 집계
    """

    def __init__(self, code: str, interval_min: int = 3):
        self.code = code
        self.interval = timedelta(minutes=interval_min)
        self._current: Optional[Candle] = None
        self._candles: List[Candle] = []
        self._on_close_callbacks: List[Callable] = []

    def on_candle_close(self, callback: Callable):
        """봉 확정 시 호출될 콜백 등록"""
        self._on_close_callbacks.append(callback)

    def update_tick(self, price: int, volume: int, time_str: str) -> Optional[Candle]:
        """
        틱 데이터 입력 → 봉 업데이트
        Returns: 방금 확정된 Candle (없으면 None)
        """
        try:
            tick_dt = self._parse_time(time_str)
        except Exception:
            return None

        candle_start = self._floor_to_interval(tick_dt)
        closed_candle = None

        # 새 봉 시작 감지
        if self._current is None:
            self._current = Candle(
                code=self.code,
                datetime=candle_start,
                open=price, high=price,
                low=price, close=price,
                volume=volume
            )
        elif candle_start > self._current.datetime:
            # 이전 봉 확정
            self._current.is_closed = True
            self._candles.append(self._current)
            closed_candle = self._current
            logger.debug(
                f"[Candle] {self.code} 봉확정: "
                f"{self._current.datetime} O={self._current.open} "
                f"H={self._current.high} L={self._current.low} "
                f"C={self._current.close} V={self._current.volume}"
            )
            for cb in self._on_close_callbacks:
                cb(self._current)

            # 새 봉 시작
            self._current = Candle(
                code=self.code,
                datetime=candle_start,
                open=price, high=price,
                low=price, close=price,
                volume=volume
            )
        else:
            # 현재 봉 갱신
            self._current.high = max(self._current.high, price)
            self._current.low = min(self._current.low, price)
            self._current.close = price
            self._current.volume += volume

        return closed_candle

    def get_candles(self, n: int = None) -> List[Candle]:
        """확정된 봉 리스트 반환 (최신 n개)"""
        if n:
            return self._candles[-n:]
        return list(self._candles)

    def get_current_candle(self) -> Optional[Candle]:
        return self._current

    def get_latest_close(self) -> Optional[Candle]:
        """가장 최근 확정 봉"""
        return self._candles[-1] if self._candles else None

    # ─────────────────────────────────────────
    # 유틸
    # ─────────────────────────────────────────
    def _floor_to_interval(self, dt: datetime) -> datetime:
        """dt를 interval 단위로 내림"""
        total_sec = int(dt.timestamp())
        interval_sec = int(self.interval.total_seconds())
        floored = total_sec - (total_sec % interval_sec)
        return datetime.fromtimestamp(floored)

    def _parse_time(self, time_str: str) -> datetime:
        """
        키움 체결시간 포맷: "HHMMSS" or "HHMMSSmmm" (ms 포함) or "YYYYMMDDHHMMss"
        """
        now = datetime.now()
        ts = time_str.strip()
        if len(ts) >= 14:
            return datetime.strptime(ts[:14], "%Y%m%d%H%M%S")
        elif len(ts) >= 6:
            # HHMMSS (6자리) 또는 HHMMSSmmm (9자리, ms 포함) 모두 앞 6자리만 사용
            h, m, s = int(ts[:2]), int(ts[2:4]), int(ts[4:6])
            return now.replace(hour=h, minute=m, second=s, microsecond=0)
        raise ValueError(f"알 수 없는 시간 포맷: {time_str}")


class DataManager:
    """
    후보 종목 전체 캔들 빌더 관리 + 초기 분봉 데이터 로딩
    """

    def __init__(self, kiwoom):
        self.kiwoom = kiwoom
        self._builders: Dict[str, CandleBuilder] = {}
        self._initial_candles: Dict[str, List[Candle]] = {}

    def init_stock(self, code: str,
                   on_candle_close: Callable = None) -> CandleBuilder:
        """
        종목 초기화: 과거 분봉 로딩 + 빌더 생성
        """
        builder = CandleBuilder(code, interval_min=config.CANDLE_INTERVAL)
        if on_candle_close:
            builder.on_candle_close(on_candle_close)

        # 과거 3분봉 로딩 (전략 판단용 초기 히스토리)
        try:
            df = self.kiwoom.get_minute_data(
                code, tick_range=config.CANDLE_INTERVAL, count=60
            )
            for _, row in df.iterrows():
                c = Candle(
                    code=code,
                    datetime=datetime.strptime(
                        str(row["datetime"]).strip(), "%Y%m%d%H%M%S"
                    ) if len(str(row["datetime"])) >= 14
                    else datetime.now(),
                    open=abs(int(row["open"])),
                    high=abs(int(row["high"])),
                    low=abs(int(row["low"])),
                    close=abs(int(row["close"])),
                    volume=int(row["volume"]),
                    is_closed=True
                )
                builder._candles.append(c)
            logger.info(f"[DataManager] {code} 초기 봉 {len(builder._candles)}개 로딩")
        except Exception as e:
            logger.warning(f"[DataManager] {code} 초기 데이터 로딩 실패: {e}")

        self._builders[code] = builder
        return builder

    def on_tick(self, tick: dict):
        """
        실시간 틱 수신 → 해당 종목 빌더에 전달
        tick = {"code": ..., "price": ..., "volume": ..., "time": ...}
        """
        code = tick.get("code")
        if code and code in self._builders:
            self._builders[code].update_tick(
                price=tick["price"],
                volume=tick["volume"],
                time_str=tick["time"]
            )

    def get_builder(self, code: str) -> Optional[CandleBuilder]:
        return self._builders.get(code)

    def get_candles(self, code: str, n: int = None) -> List[Candle]:
        builder = self._builders.get(code)
        return builder.get_candles(n) if builder else []

    def remove_stock(self, code: str):
        self._builders.pop(code, None)
