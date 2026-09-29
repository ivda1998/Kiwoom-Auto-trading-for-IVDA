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
        self.api_feed = False  # True면 ka10080 주입 전용 (틱 집계 안 함)
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

    def add_closed_candle(self, candle: 'Candle', fire_callback: bool = True):
        """확정봉 직접 주입 (API 방식 갱신용). fire_callback=True면 콜백 발동."""
        candle.is_closed = True
        self._candles.append(candle)
        if fire_callback:
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
                   on_candle_close: Callable = None,
                   interval_min: int = None,
                   api_feed: bool = False) -> CandleBuilder:
        """
        종목 초기화: 과거 분봉 로딩 + 빌더 생성
        interval_min: 봉 주기(분). 미지정 시 config.CANDLE_INTERVAL
        api_feed: True면 봉을 ka10080 주입으로만 갱신 (틱 집계 차단, 중복 방지)
        """
        # api_feed 전용 빌더(레버리지 신호원 등)는 일반 init이 덮어쓰지 못하게 보호
        # (기존 빌더에 이미 CANDLE 브로드캐스트 콜백이 있으므로 재등록도 생략 — 중복 이벤트 방지)
        existing = self._builders.get(code)
        if existing and existing.api_feed and not api_feed:
            logger.info(
                f"[DataManager] {code} api_feed 빌더 유지 — 일반 init 요청 무시"
            )
            return existing

        interval_min = interval_min or config.CANDLE_INTERVAL
        builder = CandleBuilder(code, interval_min=interval_min)
        builder.api_feed = api_feed
        if on_candle_close:
            builder.on_candle_close(on_candle_close)

        # 과거 분봉 로딩 (전략 판단용 초기 히스토리)
        # count=400: 3분봉 기준 약 3거래일치 — MACD 등 지표가 당일 장 시작(09:00)부터
        # 바로 워밍업되도록 전일 이전 봉까지 충분히 확보 (ka10080 한 번 호출로 반환되는
        # 데이터를 그대로 더 많이 쓰는 것이라 API 호출 비용은 동일함)
        try:
            df = self.kiwoom.get_minute_data(
                code, tick_range=interval_min, count=400
            )
            if df is None or df.empty:
                logger.warning(f"[DataManager] {code} 초기 분봉 API 응답 없음 — refresh에서 자가복구 예정")
                self._builders[code] = builder
                return builder
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
                # 아직 확정되지 않은 봉 제외 (형성 중인 봉 포함: 시작+주기 > 현재)
                if c_dt + builder.interval > now_dt:
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
            builder = self._builders[code]
            if builder.api_feed:
                return  # API 주입 전용 종목: 틱 집계 생략 (중복 봉 방지)
            builder.update_tick(
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

        # 초기 로드 실패(빈 builder)면 전체 로드로 자가복구 (init_stock과 동일하게 400)
        if not builder._candles:
            count = 400
            logger.info(f"[MarketData] {code} builder 비어있음 → 전체 로드(count=400) 자가복구")

        last_dt = builder._candles[-1].datetime if builder._candles else datetime.min

        # 빌더가 생성될 때의 봉 주기를 그대로 사용 (종목별 주기 상이 가능)
        interval_min = int(builder.interval.total_seconds() // 60) or config.CANDLE_INTERVAL

        # 갭 자가복구: 마지막 봉이 count개로 닿지 않을 만큼 오래됐으면 전체 로드로 승격.
        # 장 시작 직후 첫 refresh가 전일 봉에서 출발하면 당일 초반 봉이 영구 누락되고,
        # 볼린저/MACD 창이 어긋난 채(가격이 이미 밴드 밖) 하루 종일 진입이 막힌다.
        if builder._candles:
            gap_bars = (datetime.now() - last_dt).total_seconds() / (interval_min * 60)
            if gap_bars > count:
                logger.info(
                    f"[MarketData] {code} 마지막 봉({last_dt})까지 갭 {gap_bars:.0f}봉 > count={count} "
                    f"→ 전체 로드(count=400)로 승격"
                )
                count = 400

        try:
            df = self.kiwoom.get_minute_data(
                code, tick_range=interval_min, count=count
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
        pending = []   # 주입 대기 확정봉 (오래된 순)
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
            # 아직 확정되지 않은 봉 제외 (형성 중인 봉 포함: 시작+주기 > 현재)
            if dt + builder.interval > now_dt:
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
            pending.append(candle)

        # 여러 봉이 한 번에 주입될 때 콜백(CANDLE 이벤트)은 최신 봉 1개만 발동
        # — 낡은 봉으로 진입/청산이 연쇄 실행되는 것 방지
        for idx, candle in enumerate(pending):
            is_last = idx == len(pending) - 1
            builder.add_closed_candle(candle, fire_callback=is_last)
            injected += 1
            logger.info(
                f"[MarketData] {code} 신규봉 주입: "
                f"{candle.datetime} O={candle.open} H={candle.high} "
                f"L={candle.low} C={candle.close} V={candle.volume}"
                + ("" if is_last else " (콜백 생략)")
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
