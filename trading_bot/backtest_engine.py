# backtest_engine.py - 과거 데이터 기반 백테스트 엔진

import sqlite3
import os
import logging
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import List, Dict, Optional

import pandas as pd
import numpy as np

import config
from skills.market_data_skill import Candle
from strategy import BreakoutStrategy, SignalType

logger = logging.getLogger(__name__)


@dataclass
class BacktestTrade:
    code: str
    entry_date: str
    entry_time: str
    entry_price: float
    exit_date: str
    exit_time: str
    exit_price: float
    qty: int
    pnl: float
    pnl_rate: float
    exit_reason: str          # PROFIT / STOP / EOD (장마감)
    hold_candles: int         # 보유 봉 수


@dataclass
class BacktestResult:
    total_trades: int = 0
    win_count: int = 0
    lose_count: int = 0
    total_pnl: float = 0.0
    max_profit: float = 0.0
    max_loss: float = 0.0
    win_rate: float = 0.0
    profit_factor: float = 0.0
    avg_pnl: float = 0.0
    avg_hold_candles: float = 0.0
    trades: List[BacktestTrade] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"총 거래: {self.total_trades}회 | "
            f"승률: {self.win_rate:.1%} | "
            f"총 손익: {self.total_pnl:+,.0f}원 | "
            f"PF: {self.profit_factor:.2f} | "
            f"평균 손익: {self.avg_pnl:+,.0f}원"
        )


class BacktestEngine:
    """
    과거 분봉 데이터를 이용한 전략 백테스트
    """

    def __init__(self, initial_capital: float = 10_000_000):
        self.initial_capital = initial_capital
        self.strategy = BreakoutStrategy()
        self.db_path = config.BACKTEST_DB_PATH
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    # ─────────────────────────────────────────
    # DB 초기화
    # ─────────────────────────────────────────
    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS backtest_trades (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id        TEXT,
                    code          TEXT,
                    entry_date    TEXT,
                    entry_time    TEXT,
                    entry_price   REAL,
                    exit_date     TEXT,
                    exit_time     TEXT,
                    exit_price    REAL,
                    qty           INTEGER,
                    pnl           REAL,
                    pnl_rate      REAL,
                    exit_reason   TEXT,
                    hold_candles  INTEGER,
                    created_at    TEXT DEFAULT (datetime('now','localtime'))
                )
            """)
            conn.commit()

    # ─────────────────────────────────────────
    # 메인 백테스트 실행
    # ─────────────────────────────────────────
    def run(self, code: str, candles_df: pd.DataFrame,
            daily_df: pd.DataFrame, run_id: str = None) -> BacktestResult:
        """
        Parameters:
            candles_df: 3분봉 DataFrame (datetime, open, high, low, close, volume)
            daily_df: 일봉 DataFrame (date, open, high, low, close, volume)
        """
        if run_id is None:
            run_id = datetime.now().strftime("%Y%m%d_%H%M%S")

        result = BacktestResult()
        capital = self.initial_capital

        # 일봉 기준 고점 계산
        if len(daily_df) < 20:
            logger.warning(f"[Backtest] {code} 일봉 데이터 부족")
            return result

        high_20 = float(daily_df["high"].rolling(20).max().iloc[-1])
        high_60 = float(daily_df["high"].rolling(60).max().iloc[-1]) \
            if len(daily_df) >= 60 else high_20
        ref_high = max(high_20, high_60)

        # 전략 초기화
        self.strategy.init_stock(code, code, high_20, high_60)

        # 분봉 캔들 변환
        candles = self._df_to_candles(code, candles_df)
        if len(candles) < 20:
            return result

        position: Optional[dict] = None
        trade_history = []

        for i in range(10, len(candles)):
            candle = candles[i]
            history = candles[:i + 1]

            # 시간 필터
            if not self._in_trade_hours(candle.datetime):
                # 장마감 시 강제 청산
                if position:
                    trade = self._close_position(
                        position, candle, "EOD", i
                    )
                    result.trades.append(trade)
                    capital += trade.pnl
                    position = None
                    self.strategy.reset_stock(code)
                continue

            if position is None:
                # 진입 신호 탐지
                signal = self.strategy.on_candle_close(candle, history)
                if signal == SignalType.BUY:
                    qty = int((capital * config.POSITION_RATIO) / candle.close)
                    if qty > 0:
                        position = {
                            "entry_candle": candle,
                            "entry_idx": i,
                            "qty": qty,
                            "stoploss": self.strategy.get_state(code).stoploss_price
                        }
            else:
                # 청산 신호 탐지
                state = self.strategy.get_state(code)
                if not state:
                    position = None
                    continue

                # 손절 체크
                if candle.low < position["stoploss"]:
                    trade = self._close_position(
                        position, candle, "STOP", i,
                        exit_price=position["stoploss"]
                    )
                    result.trades.append(trade)
                    capital += trade.pnl
                    position = None
                    self.strategy.reset_stock(code)
                    continue

                # 익절 체크 (5MA 기울기)
                closed = [c for c in history if c.is_closed]
                if self.strategy._is_ma_declining(closed, config.MA_EXIT_PERIOD):
                    trade = self._close_position(
                        position, candle, "PROFIT", i
                    )
                    result.trades.append(trade)
                    capital += trade.pnl
                    position = None
                    self.strategy.reset_stock(code)

        # 집계
        self._calc_result(result)
        self._save_to_db(result.trades, run_id, code)

        logger.info(f"[Backtest] {code} | {result.summary()}")
        return result

    # ─────────────────────────────────────────
    # 보조
    # ─────────────────────────────────────────
    def _df_to_candles(self, code: str, df: pd.DataFrame) -> List[Candle]:
        candles = []
        for _, row in df.iterrows():
            try:
                dt = pd.to_datetime(str(row["datetime"]))
                c = Candle(
                    code=code,
                    datetime=dt.to_pydatetime(),
                    open=int(row["open"]),
                    high=int(row["high"]),
                    low=int(row["low"]),
                    close=int(row["close"]),
                    volume=int(row["volume"]),
                    is_closed=True
                )
                candles.append(c)
            except Exception:
                continue
        return candles

    def _in_trade_hours(self, dt: datetime) -> bool:
        t = dt.strftime("%H:%M")
        start = config.TRADE_START_TIME
        end = config.TRADE_END_TIME
        lunch_s = config.LUNCH_START
        lunch_e = config.LUNCH_END
        return (start <= t <= end) and not (lunch_s <= t <= lunch_e)

    def _close_position(self, position: dict, candle: Candle,
                        reason: str, current_idx: int,
                        exit_price: float = None) -> BacktestTrade:
        ep = candle.close if exit_price is None else exit_price
        entry_c: Candle = position["entry_candle"]
        pnl = (ep - entry_c.close) * position["qty"]
        pnl_rate = (ep - entry_c.close) / entry_c.close if entry_c.close > 0 else 0.0
        return BacktestTrade(
            code=candle.code,
            entry_date=entry_c.datetime.strftime("%Y-%m-%d"),
            entry_time=entry_c.datetime.strftime("%H:%M"),
            entry_price=entry_c.close,
            exit_date=candle.datetime.strftime("%Y-%m-%d"),
            exit_time=candle.datetime.strftime("%H:%M"),
            exit_price=ep,
            qty=position["qty"],
            pnl=pnl,
            pnl_rate=pnl_rate,
            exit_reason=reason,
            hold_candles=current_idx - position["entry_idx"]
        )

    def _calc_result(self, result: BacktestResult):
        trades = result.trades
        result.total_trades = len(trades)
        if not trades:
            return
        result.win_count = sum(1 for t in trades if t.pnl > 0)
        result.lose_count = sum(1 for t in trades if t.pnl <= 0)
        result.total_pnl = sum(t.pnl for t in trades)
        result.max_profit = max(t.pnl for t in trades)
        result.max_loss = min(t.pnl for t in trades)
        result.win_rate = result.win_count / result.total_trades
        gross_profit = sum(t.pnl for t in trades if t.pnl > 0)
        gross_loss = abs(sum(t.pnl for t in trades if t.pnl < 0))
        result.profit_factor = (
            gross_profit / gross_loss if gross_loss > 0 else float("inf")
        )
        result.avg_pnl = result.total_pnl / result.total_trades
        result.avg_hold_candles = sum(t.hold_candles for t in trades) / result.total_trades

    def _save_to_db(self, trades: List[BacktestTrade],
                    run_id: str, code: str):
        with sqlite3.connect(self.db_path) as conn:
            for t in trades:
                conn.execute("""
                    INSERT INTO backtest_trades
                    (run_id, code, entry_date, entry_time, entry_price,
                     exit_date, exit_time, exit_price, qty, pnl, pnl_rate,
                     exit_reason, hold_candles)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    run_id, t.code, t.entry_date, t.entry_time, t.entry_price,
                    t.exit_date, t.exit_time, t.exit_price, t.qty, t.pnl,
                    t.pnl_rate, t.exit_reason, t.hold_candles
                ))
            conn.commit()
