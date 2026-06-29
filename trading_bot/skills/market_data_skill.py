# data_handler.py - 실시간 틱 → 3분봉 변환 엔진

import logging
from datetime import datetime, timedelta
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional, List, Callable, Dict

import config
from .base_skill import BaseSkill

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
        return self.close > self.open  # strict: doji(open==close)는 양봉 아님

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

    def add_closed_candle(self, candle: 'Candle'):
        """확정봉 직접 주입 (API 방식 갱신용). 콜백을 발동한다."""
        candle.is_closed = True
        self._candles.append(candle)
        for cb in self._on_close_callbacks:
            cb(candle)

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


class MarketDataSkill(BaseSkill):
    """
    후보 종목 전체 캔들 빌더 관리 + 초기 분봉 데이터 로딩
    """

    def __init__(self, kiwoom):
        super().__init__(kiwoom)   # BaseSkill: self.api = kiwoom
        self.kiwoom = self.api     # 하위 호환 alias
        self._builders: Dict[str, CandleBuilder] = {}
        self._initial_candles: Dict[str, List[Candle]] = {}

    def execute(self, action_type: str, *args, **kwargs):
        """
        BaseSkill 구현부
        """
        if action_type == "init_stock":
            return self.init_stock(*args, **kwargs)
        elif action_type == "get_candles":
            return self.get_candles(*args, **kwargs)
        elif action_type == "get_builder":
            return self.get_builder(*args, **kwargs)
        return False

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
            now_dt = datetime.now()
            skipped_future = 0
            for _, row in df.iterrows():
                dt_str = str(row["datetime"]).strip()
                try:
                    if len(dt_str) >= 14:
                        c_dt = datetime.strptime(dt_str[:14], "%Y%m%d%H%M%S")
                    elif len(dt_str) >= 6:
                        # cntr_tm이 HHMMSS 형식일 때 오늘 날짜 결합
                        h, m, s = int(dt_str[:2]), int(dt_str[2:4]), int(dt_str[4:6])
                        c_dt = now_dt.replace(hour=h, minute=m, second=s, microsecond=0)
                    else:
                        logger.warning(f"[DataManager] {code} dt 형식 불명: '{dt_str}'")
                        continue
                except Exception as ex:
                    logger.warning(f"[DataManager] {code} dt 파싱 실패: '{dt_str}' → {ex}")
                    continue
                # 아직 확정되지 않은 봉 제외 (미래 시각)
                if c_dt > now_dt:
                    skipped_future += 1
                    continue
                c = Candle(
                    code=code,
                    datetime=c_dt,
                    open=abs(int(row["open"])),
                    high=abs(int(row["high"])),
                    low=abs(int(row["low"])),
                    close=abs(int(row["close"])),
                    volume=int(row["volume"]),
                    is_closed=True
                )
                builder._candles.append(c)
            n = len(builder._candles)
            logger.info(f"[DataManager] {code} 초기 봉 {n}개 로딩 (미래봉 제외={skipped_future})")
            if builder._candles:
                last = builder._candles[-1]
                logger.info(
                    f"[DataManager] {code} 최신 초기봉: "
                    f"{last.datetime} O={last.open} H={last.high} L={last.low} C={last.close} V={last.volume}"
                )
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

    def refresh_candles(self, code: str, count: int = 5) -> int:
        """
        ka10080으로 최근 분봉을 조회해 새 확정봉만 builder에 주입.
        Returns: 주입된 신규 봉 수(≥0), API 실패 시 -1
        """
        builder = self._builders.get(code)
        if not builder:
            return -1

        last_dt = builder._candles[-1].datetime if builder._candles else datetime.min

        try:
            df = self.kiwoom.get_minute_data(
                code, tick_range=config.CANDLE_INTERVAL, count=count
            )
            if df is None or df.empty:
                logger.warning(f"[MarketData] {code} 분봉 갱신: API 응답 없음 (builder candles={len(builder._candles)})")
                return -1
        except Exception as e:
            logger.warning(f"[MarketData] {code} 분봉 갱신 실패: {e}")
            return -1

        injected = 0
        skipped_old = 0
        skipped_bad_dt = 0
        skipped_future = 0
        now_dt = datetime.now()
        for _, row in df.iterrows():
            try:
                dt_str = str(row["datetime"]).strip()
                if len(dt_str) >= 14:
                    dt = datetime.strptime(dt_str[:14], "%Y%m%d%H%M%S")
                elif len(dt_str) >= 6:
                    # cntr_tm이 HHMMSS 형식일 때 오늘 날짜 결합
                    h, m, s = int(dt_str[:2]), int(dt_str[2:4]), int(dt_str[4:6])
                    dt = now_dt.replace(hour=h, minute=m, second=s, microsecond=0)
                else:
                    skipped_bad_dt += 1
                    logger.warning(f"[MarketData] {code} dt 형식 불명: '{dt_str}'")
                    continue
            except Exception as ex:
                skipped_bad_dt += 1
                logger.warning(f"[MarketData] {code} dt 파싱 예외: '{row.get('datetime', '')}' → {ex}")
                continue
            # 아직 확정되지 않은 봉 제외 (미래 시각)
            if dt > now_dt:
                skipped_future += 1
                continue
            if dt <= last_dt:
                skipped_old += 1
                continue

            candle = Candle(
                code=code,
                datetime=dt,
                open=abs(int(row["open"])),
                high=abs(int(row["high"])),
                low=abs(int(row["low"])),
                close=abs(int(row["close"])),
                volume=int(row["volume"]),
                is_closed=True,
            )
            builder.add_closed_candle(candle)
            injected += 1
            logger.info(
                f"[MarketData] {code} 신규봉 주입: "
                f"{dt} O={candle.open} H={candle.high} "
                f"L={candle.low} C={candle.close} V={candle.volume}"
            )

        # 항상 갱신 결과 로그 (진단용)
        newest = df.iloc[-1] if not df.empty else None
        logger.info(
            f"[MarketData] {code} refresh: last_dt={last_dt}, "
            f"API행={len(df)}, 신규={injected}, 과거스킵={skipped_old}, "
            f"dt불량={skipped_bad_dt}, 미래봉={skipped_future}"
            + (f", API최신raw={newest['datetime']} O={newest['open']} C={newest['close']} V={newest['volume']}"
               if newest is not None else "")
        )

        return injected

    def remove_stock(self, code: str):
        self._builders.pop(code, None)
