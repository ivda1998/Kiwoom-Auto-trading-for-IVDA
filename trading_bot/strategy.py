# strategy.py - 핵심 전략 로직: 신고가 근접 진입 + 청산

import logging
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Dict
from datetime import datetime

from skills.market_data_skill import Candle
import config

logger = logging.getLogger(__name__)


class SignalType(Enum):
    NONE         = "NONE"
    BUY          = "BUY"           # 신고가 근접 매수
    SELL_PROFIT  = "SELL_PROFIT"   # 5MA 음전환 익절
    SELL_STOP    = "SELL_STOP"     # 손절가 이탈 손절
    SELL_TARGET  = "SELL_TARGET"   # 목표가 7% 수익실현
    SELL_TRAILING= "SELL_TRAILING" # 고점 대비 5% 하락 트레일링 스탑
    TIMEOUT      = "TIMEOUT"       # WATCHING 타임아웃 → 후보 제거


@dataclass
class StrategyState:
    """
    종목별 전략 상태 추적

    phase 전이:
      WATCHING → (신고가 ±1% 근접) → BUY 신호 → ENTERED
      ENTERED  → (5MA 음전환 or 손절가 이탈) → 청산
    """
    code:           str
    name:           str            = ""
    phase:          str            = "WATCHING"
    # ── 일봉 기준 전고점 ──
    high_20:        float          = 0.0    # 20일 고점
    high_60:        float          = 0.0    # 60일 고점
    daily_high:     float          = 0.0    # max(high_20, high_60)
    # ── 돌파 및 눌림 대기 ──
    is_breakout:    bool           = False  # 3분봉 상향 돌파 여부
    breakout_price: float          = 0.0    # 돌파를 확정한 3분봉 종가
    # ── 진입/청산 ──
    entry_price:    float          = 0.0
    highest_price:  float          = 0.0    # 진입 후 최고가 (트레일링 스탑 목적)
    stoploss_price: float          = 0.0    # 진입가 × (1 - STOP_LOSS_RATE)
    entered_at:     Optional[datetime] = None
    # ── 타임아웃 추적 ──
    total_candles:  int            = 0      # 등록 후 총 봉 수 (ENTERED 상태 제외)


class BreakoutStrategy:
    """
    신고가(20일/60일) ±NEAR_HIGH_BUY_THRESHOLD(1%) 이내 즉시 매수 전략 (3분봉)

    ┌──────────────────────────────────────────────────────┐
    │ 1. 스캔 (장 전)                                       │
    │    일봉 20/60일 전고점 ±3% 이내 종목 선별             │
    │                                                       │
    │ 2. WATCHING → BUY                                     │
    │    3분봉 종가가 20일 OR 60일 고점의 ±1% 이내          │
    │    → 즉시 매수 (진입가 = 신호봉 종가)                 │
    │    손절가 = 진입가 × (1 - STOP_LOSS_RATE = 3%)        │
    │                                                       │
    │ 3. ENTERED → 청산                                     │
    │    실시간 손절: 현재가 < stoploss_price               │
    │    봉 확정 익절: 3분봉 5MA 기울기 음전환              │
    └──────────────────────────────────────────────────────┘
    """

    def __init__(self):
        self._states: Dict[str, StrategyState] = {}

    def init_stock(self, code: str, name: str, high_20: float, high_60: float):
        """종목 등록 (스캐너 후보 등록 시 호출)"""
        self._states[code] = StrategyState(
            code=code,
            name=name,
            high_20=high_20,
            high_60=high_60,
            daily_high=max(high_20, high_60),
        )
        logger.debug(
            f"[Strategy] {code} 등록 | "
            f"20일고점={high_20:,} | 60일고점={high_60:,}"
        )

    def remove_stock(self, code: str):
        """타임아웃/취소 시 상태 완전 제거"""
        self._states.pop(code, None)

    # ─────────────────────────────────────────
    # 봉 확정 시 전략 판단 (메인 진입점)
    # ─────────────────────────────────────────
    def on_candle_close(self, candle: Candle,
                        all_candles: List[Candle]) -> SignalType:
        """
        확정된 봉을 받아 신호 판단.
        ENTERED 상태의 익절(5MA)도 여기서 처리.
        """
        state = self._states.get(candle.code)
        if not state:
            return SignalType.NONE

        if state.phase == "ENTERED":
            # 수동 지정 종목의 독립 익절/손절 체크
            custom_targets = config.get_custom_targets()
            custom_conf = custom_targets.get(state.name) or custom_targets.get(candle.code)
            if custom_conf:
                return self._check_custom_exit(candle, state, custom_conf)
            return self._check_exit(candle, all_candles, state)

        # ENTERED 이외 상태에서 총 봉 수 증가
        state.total_candles += 1

        # ★ 수동 종목 독립 진입 체크
        custom_targets = config.get_custom_targets()
        custom_conf = custom_targets.get(state.name) or custom_targets.get(candle.code)
        if custom_conf:
            return self._check_custom_entry(candle, state, custom_conf)

        # ★ 신고가 근접 매수 체크 (타임아웃보다 먼저 — 마지막 봉에서도 진입 기회 유지)
        signal = self._check_near_high_entry(candle, all_candles, state)
        if signal == SignalType.BUY:
            return signal

        # ★ WATCHING 타임아웃 체크: N봉 경과 후에도 진입 없으면 후보 제거
        if state.total_candles >= config.WATCHING_TIMEOUT_CANDLES:
            logger.info(
                f"[Strategy] {candle.code} 대기 타임아웃 "
                f"({state.total_candles}봉 / {config.WATCHING_TIMEOUT_CANDLES}봉 한도) "
                f"→ 후보 제거"
            )
            return SignalType.TIMEOUT

        return SignalType.NONE

    def on_candle_update(self, candle: Candle,
                         all_candles: List[Candle],
                         current_price: float) -> SignalType:
        """
        포지션 보유 중 틱마다 실시간 손절 체크.
        ※ 익절(5MA)은 봉 확정 시점(on_candle_close)에서만 처리.
        """
        state = self._states.get(candle.code)
        if not state or state.phase != "ENTERED":
            return SignalType.NONE

        # 수동 지정 종목인 경우 실시간 틱 평가 (손절/목표가 도달 즉시 청산)
        custom_targets = config.get_custom_targets()
        custom_conf = custom_targets.get(state.name) or custom_targets.get(candle.code)
        if custom_conf:
            return self._check_custom_exit_tick(candle, current_price, state, custom_conf)

        # 트레일링 스탑용 최고가 갱신
        if current_price > state.highest_price:
            state.highest_price = current_price

        # 1. 목표가 자동 청산 (7%)
        target_price = state.entry_price * (1 + config.TARGET_PROFIT_RATE)
        if current_price >= target_price:
            logger.info(
                f"[Strategy] {candle.code} 익절(목표가 도달): "
                f"현재가({current_price:,}) >= 목표가({target_price:,.0f}) "
                f"(+{config.TARGET_PROFIT_RATE:.0%})"
            )
            state.phase = "WATCHING"
            return SignalType.SELL_TARGET

        # 2. 트레일링 스탑 (수익 보존 스탑)
        # 예: 수익이 +3% 이상 났을 경우, 5% 목표가에 도달하지 못하고 다시 3% 이하로 내려가면 익절
        TRAIL_ACTIVATION_RATE = config.TRAILING_STOP_RATE # 3% (설정값 기준)
        if state.highest_price >= state.entry_price * (1 + TRAIL_ACTIVATION_RATE):
            profit_preservation_price = state.entry_price * (1 + TRAIL_ACTIVATION_RATE)
            if current_price <= profit_preservation_price:
                logger.info(
                    f"[Strategy] {candle.code} 트레일링 스탑 발동: "
                    f"현재가({current_price:,}) <= 보존가({profit_preservation_price:,.0f}) "
                    f"[+3% 수익 달성 후 하락, +3% 수익 확보]"
                )
                state.phase = "WATCHING"
                return SignalType.SELL_TRAILING



        # 3. 기존 손절: 진입가 대비 -STOP_LOSS_RATE(3%) 이탈
        if current_price < state.stoploss_price:
            logger.info(
                f"[Strategy] {candle.code} 손절: "
                f"현재가({current_price:,}) < 손절가({state.stoploss_price:,}) "
                f"[진입가({state.entry_price:,}) × {1 - config.STOP_LOSS_RATE:.0%}]"
            )
            state.phase = "WATCHING"
            return SignalType.SELL_STOP

        return SignalType.NONE

    def notify_entered(self, code: str, entry_price: float):
        """매수 체결 통보 — entry_price = 신호봉 종가"""
        state = self._states.get(code)
        if state:
            state.phase          = "ENTERED"
            state.entry_price    = entry_price
            state.highest_price  = entry_price
            state.stoploss_price = round(entry_price * (1 - config.STOP_LOSS_RATE))
            state.entered_at     = datetime.now()
            logger.info(
                f"[Strategy] {code} 진입 완료 | "
                f"진입가={entry_price:,} | 손절가={state.stoploss_price:,} "
                f"(-{config.STOP_LOSS_RATE:.0%})"
            )

    def reset_stock(self, code: str):
        """청산 후 상태 초기화 (고점 정보 유지)"""
        state = self._states.get(code)
        if state:
            self._states[code] = StrategyState(
                code=code,
                name=state.name,
                high_20=state.high_20,
                high_60=state.high_60,
                daily_high=state.daily_high,
                is_breakout=False,
                breakout_price=0.0
            )

    # ─────────────────────────────────────────
    # 수동 종목 독립 전략 (CUSTOM_TARGETS)
    # ─────────────────────────────────────────
    def _check_custom_entry(self, candle: Candle, state: StrategyState, custom_conf: dict) -> SignalType:
        """수동 지정 종목 진입 규칙: 지정된 범위 내 진입 시 즉시 돌파 로직 무시하고 매수"""
        buy_min = custom_conf.get("buy_min", 0)
        buy_max = custom_conf.get("buy_max", float('inf'))
        
        if buy_min <= candle.close <= buy_max:
            logger.info(
                f"[Strategy/Custom] {candle.code} 수동 지정 매수 조건 도달! "
                f"현재가({candle.close:,}) 범위({buy_min:,}~{buy_max:,})"
            )
            state.phase = "ENTERING"
            return SignalType.BUY
        return SignalType.NONE

    def _check_custom_exit_tick(self, candle: Candle, current_price: float, state: StrategyState, custom_conf: dict) -> SignalType:
        """수동 지정 종목 실시간 틱 청산 규칙: 지정된 손절가/목표가 닿으면 즉각 매도"""
        stop_loss = custom_conf.get("stop_loss", -1)
        take_profit = custom_conf.get("take_profit", float('inf'))
        
        if stop_loss > 0 and current_price <= stop_loss:
            logger.info(f"[Strategy/Custom] {candle.code} 수동 손절가 도달: 현재가({current_price:,}) <= 손절가({stop_loss:,})")
            state.phase = "WATCHING"
            return SignalType.SELL_STOP
            
        if take_profit > 0 and current_price >= take_profit:
            logger.info(f"[Strategy/Custom] {candle.code} 수동 목표가 도달: 현재가({current_price:,}) >= 목표가({take_profit:,})")
            state.phase = "WATCHING"
            return SignalType.SELL_TARGET
            
        return SignalType.NONE

    def _check_custom_exit(self, candle: Candle, state: StrategyState, custom_conf: dict) -> SignalType:
        """봉 마감 시 추가 수동 종목 청산 검사 (일반 자동 5MA 규칙 등을 덮어씌워서 무시함)"""
        # 실시간 틱(current_price)에서 이미 대부분 걸러지지만 혹시나 봉 마감 종가로 갭을 띄운 경우를 방어
        return self._check_custom_exit_tick(candle, candle.close, state, custom_conf)

    # ─────────────────────────────────────────
    # 청산: 익절 (ENTERED → 봉 확정 시)
    # ─────────────────────────────────────────
    def _check_exit(self, candle: Candle,
                    all_candles: List[Candle],
                    state: StrategyState) -> SignalType:
        """
        봉 확정 시 익절 조건 확인.
        5MA 기울기 음전환(ma[-1] < ma[-2])이면 SELL_PROFIT.
        """
        closed = [c for c in all_candles if c.is_closed]
        if self._is_ma_declining(closed, config.MA_EXIT_PERIOD):
            logger.info(
                f"[Strategy] {candle.code} 익절: 5MA 기울기 음전환 | "
                f"종가({candle.close:,})"
            )
            state.phase = "WATCHING"
            return SignalType.SELL_PROFIT
        return SignalType.NONE

    # ─────────────────────────────────────────
    # 신고가 근접 매수 (WATCHING → BUY)
    # ─────────────────────────────────────────
    def _check_near_high_entry(self, candle: Candle,
                                all_candles: List[Candle],
                                state: StrategyState) -> SignalType:
        """
        [1단계 - 돌파] 3분봉 종가가 20일 OR 60일 고점을 상향 돌파하면 is_breakout=True 로깅
        [2단계 - 눌림] 돌파 이후, 종가가 기존 돌파봉 종가(breakout_price)의 ±NEAR_HIGH_BUY_THRESHOLD(1%) 이내로 오면 매수
        """
        # 1. 돌파 (Breakout) 확인
        if not state.is_breakout:
            # daily_high(전일 종가고점) 상향 돌파 여부 
            # (단, 해당 돌파봉의 시가는 기준선 아래여야 하고 종가는 위인 양봉이어야 하며, 이전 5봉 평균 거래량의 2배를 넘어야 함)
            is_yangbong = candle.close > candle.open
            is_cross_up = candle.open < state.daily_high
            
            prev_candles = [c for c in all_candles if c.datetime != candle.datetime][-5:]
            avg_vol_5 = self._avg_volume(prev_candles)
            has_volume_surge = candle.volume > (avg_vol_5 * 2) if avg_vol_5 > 0 else True

            if state.daily_high > 0 and candle.close > state.daily_high and is_cross_up and is_yangbong and has_volume_surge:
                state.is_breakout = True
                state.breakout_price = candle.close
                
                strategy_type = getattr(config, "ENTRY_STRATEGY_TYPE", 1)
                if strategy_type == 2:
                    # 2번 전략: 돌파봉 확정 후 '다음 봉 시가'에서 즉시 매수 
                    # (on_candle_close에서 호출되므로 여기서 종가로 주문을 넣으면 다음 봉 시초가로 체결 효과)
                    logger.info(
                        f"[Strategy] {candle.code} ★ 전일 종가 돌파 포착! "
                        f"기준가({state.daily_high:,}) < 현재돌파가({candle.close:,}) "
                        f"→ 다음 봉 시초가 매수 진입 대기"
                    )
                    state.phase = "ENTERING"
                    return SignalType.BUY
                elif strategy_type == 3:
                    # 3번 전략: 3분봉 20MA 눌림 진입 대기
                    logger.info(
                        f"[Strategy] {candle.code} ★ 신고가 돌파 포착! "
                        f"일봉기준가({state.daily_high:,}) < 현재돌파가({candle.close:,}) | "
                        f"→ 3분봉 20MA ±{config.NEAR_HIGH_BUY_THRESHOLD:.0%} 눌림 진입 대기"
                    )
                else:
                    # 1번 전략: 기존 ±1% 근접 대기
                    logger.info(
                        f"[Strategy] {candle.code} ★ 신고가 돌파 포착! "
                        f"일봉기준가({state.daily_high:,}) < 현재돌파가({candle.close:,}) | "
                        f"거래량({candle.volume:,} > 평균 {avg_vol_5:,.0f} * 2) "
                        f"→ ±{config.NEAR_HIGH_BUY_THRESHOLD:.0%} 눌림 진입 대기"
                    )
            return SignalType.NONE

        # 2. 눌림 (Pullback) 진입
        threshold = config.NEAR_HIGH_BUY_THRESHOLD
        strategy_type = getattr(config, "ENTRY_STRATEGY_TYPE", 1)

        if strategy_type == 3:
            # 3번 전략: 20MA 기준 눌림목 매수
            ma_list = self._calculate_ma(all_candles, 20)
            if not ma_list:
                return SignalType.NONE  # 아직 20봉 데이터가 없음
            ma20 = ma_list[-1]
            proximity = abs(candle.close - ma20) / ma20
            
            if proximity <= threshold:
                state.phase = "ENTERING"
                logger.info(
                    f"[Strategy] {candle.code} ★ 20MA 눌림 매수 신호! "
                    f"20MA({ma20:,.0f}) | 종가({candle.close:,}) "
                    f"근접도({proximity:.2%}) "
                    f"→ 진입가={candle.close:,} / "
                    f"예상손절가={round(candle.close * (1 - config.STOP_LOSS_RATE)):,}"
                )
                return SignalType.BUY
        else:
            # 1번 전략: 돌파가 기준 눌림목 매수
            breakout = state.breakout_price
            proximity = abs(candle.close - breakout) / breakout
            if proximity <= threshold:
                state.phase = "ENTERING"
                logger.info(
                    f"[Strategy] {candle.code} ★ 돌파 후 눌림 매수 신호! "
                    f"돌파가({breakout:,}) | 종가({candle.close:,}) "
                    f"근접도({proximity:.2%}) "
                    f"→ 진입가={candle.close:,} / "
                    f"예상손절가={round(candle.close * (1 - config.STOP_LOSS_RATE)):,}"
                )
                return SignalType.BUY

        return SignalType.NONE

    # ─────────────────────────────────────────
    # 보조 계산
    # ─────────────────────────────────────────
    def _avg_volume(self, candles: List[Candle]) -> float:
        if not candles:
            return 0.0
        return sum(c.volume for c in candles) / len(candles)

    def _calculate_ma(self, candles: List[Candle], period: int) -> list:
        closes = [c.close for c in candles]
        if len(closes) < period:
            return []
        return [
            sum(closes[i:i + period]) / period
            for i in range(len(closes) - period + 1)
        ]

    def _is_ma_declining(self, candles: List[Candle], period: int) -> bool:
        """이동평균 기울기 음전환: ma[-1] < ma[-2]"""
        ma = self._calculate_ma(candles, period)
        if len(ma) < 2:
            return False
        return ma[-1] < ma[-2]

    def get_state(self, code: str) -> Optional[StrategyState]:
        return self._states.get(code)

    def get_all_states(self) -> dict:
        return dict(self._states)
