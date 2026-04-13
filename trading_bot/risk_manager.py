# risk_manager.py - 리스크 관리 (포지션 사이징, 일별 손실 한도)

import logging
from datetime import datetime, date

import config

logger = logging.getLogger(__name__)


class RiskManager:
    """
    자본 관리 및 리스크 제어
    - 종목당 자본 10% 투자
    - 하루 최대 손실 2% 도달 시 매매 중단
    - 동시 최대 3종목
    """

    def __init__(self, kiwoom):
        self.kiwoom = kiwoom
        self._initial_capital: float = 0.0
        self._today_pnl: float = 0.0        # 오늘 실현 손익
        self._today_date: date = date.today()
        self._invested: float = 0.0          # 현재 투자 중인 금액
        self._trade_count: int = 0           # 오늘 거래 횟수
        self._is_halted: bool = False        # 매매 중단 상태

    # ─────────────────────────────────────────
    # 초기화 (장 시작 전)
    # ─────────────────────────────────────────
    def initialize(self):
        """계좌 잔고 조회 후 초기 자본 설정"""
        try:
            balance = self.kiwoom.get_balance()
            self._initial_capital = balance.get("total", 0)
            
            # 모의투자(IS_SIMULATION)의 경우 get_balance()가 '예수금(현금 잔고)'만 반환하므로,
            # 현재 보유중인 주식의 매입금액 총합을 더하여 진짜 '총 계좌 평가액'을 구합니다.
            if config.IS_SIMULATION:
                positions = self.kiwoom.get_positions()
                stock_value = sum(p["qty"] * p["entry_price"] for p in positions)
                self._initial_capital += stock_value

            if self._initial_capital <= 0:
                self._initial_capital = 10_000_000  # 기본값 1,000만원
                logger.warning("[RiskManager] 잔고 0원 조회 → 기본값 10,000,000원으로 설정")
            self._today_pnl = 0.0
            self._invested = 0.0
            self._trade_count = 0
            self._is_halted = False
            self._today_date = date.today()
            logger.info(
                f"[RiskManager] 초기 자본: {self._initial_capital:,.0f}원"
            )
        except Exception as e:
            logger.error(f"[RiskManager] 초기화 실패: {e}")
            self._initial_capital = 10_000_000  # 기본값 1000만원

    def reset_daily(self):
        """장 종료 후 일별 상태 리셋"""
        logger.info(
            f"[RiskManager] 오늘 손익: {self._today_pnl:+,.0f}원 | "
            f"거래 횟수: {self._trade_count}회"
        )
        self._today_pnl = 0.0
        self._invested = 0.0
        self._trade_count = 0
        self._is_halted = False
        self._today_date = date.today()

    # ─────────────────────────────────────────
    # 매수 가능 여부
    # ─────────────────────────────────────────
    def can_buy(self) -> bool:
        if self._is_halted:
            logger.warning("[RiskManager] 매매 중단 상태")
            return False

        if self._initial_capital <= 0:
            logger.warning("[RiskManager] 자본 정보 없음")
            return False

        # 일별 최대 손실 초과 체크
        max_loss = self._initial_capital * config.MAX_DAILY_LOSS_RATE
        if self._today_pnl <= -max_loss:
            logger.warning(
                f"[RiskManager] 하루 최대 손실 도달 "
                f"({self._today_pnl:,.0f}원 / 한도 -{max_loss:,.0f}원)"
            )
            self._is_halted = True
            return False

        return True

    # ─────────────────────────────────────────
    # 수량 계산
    # ─────────────────────────────────────────
    def calc_position_size(self, available: float, price: float) -> int:
        """
        투자 비율(10%)로 수량 계산
        """
        if price <= 0:
            return 0

        invest_amount = self._initial_capital * config.POSITION_RATIO
        invest_amount = min(invest_amount, available * 0.95)  # 가용금액 95% 이내

        qty = int(invest_amount / price)
        
        # 1주의 가격이 종목당 할당 금액(invest_amount)보다 비싸서 수량이 0으로 계산되더라도,
        # 계좌의 총 가용금액(available * 0.95)으로 1주를 살 수 있는 여력이 있다면 최소 1주 매수 허용
        if qty == 0 and price <= available * 0.95:
            qty = 1

        return max(qty, 0)

    def get_available_capital(self) -> float:
        """
        주문 가능 금액 조회
        """
        try:
            balance = self.kiwoom.get_balance()
            available = balance.get("available", 0)
            if available <= 0:
                # 모의투자에서 available=0 반환 시 초기자본 전액으로 대체
                available = self._initial_capital
                logger.warning("[RiskManager] 주문가능금액 0원 → 초기자본으로 대체")
            return available
        except Exception:
            return self._initial_capital * (1 - config.POSITION_RATIO * 3)

    # ─────────────────────────────────────────
    # 체결 통보
    # ─────────────────────────────────────────
    def on_buy(self, amount: float):
        """매수 체결 시 투자금액 증가"""
        self._invested += amount
        self._trade_count += 1

    def on_sell(self, pnl: float):
        """매도 체결 시 손익 반영"""
        self._today_pnl += pnl
        self._invested = max(0.0, self._invested)

        # 손실 한도 실시간 체크
        max_loss = self._initial_capital * config.MAX_DAILY_LOSS_RATE
        if self._today_pnl <= -max_loss:
            logger.warning(
                f"[RiskManager] 손실 한도 도달 → 매매 중단! "
                f"오늘 손익: {self._today_pnl:,.0f}원"
            )
            self._is_halted = True

    # ─────────────────────────────────────────
    # 손절가 계산
    # ─────────────────────────────────────────
    def calc_stoploss(self, breakout_candle_low: float) -> float:
        """돌파봉 저가를 손절가로 설정"""
        return breakout_candle_low

    # ─────────────────────────────────────────
    # 상태 조회
    # ─────────────────────────────────────────
    def get_status(self) -> dict:
        return {
            "initial_capital": self._initial_capital,
            "today_pnl": self._today_pnl,
            "invested": self._invested,
            "trade_count": self._trade_count,
            "is_halted": self._is_halted,
            "pnl_rate": (
                self._today_pnl / self._initial_capital
                if self._initial_capital > 0 else 0.0
            )
        }

    @property
    def is_halted(self) -> bool:
        return self._is_halted
