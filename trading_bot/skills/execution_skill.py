# execution.py - 주문 실행 및 포지션 관리

import logging
from datetime import datetime
from dataclasses import dataclass, field
from typing import Optional, Dict

import config
from .base_skill import BaseSkill

logger = logging.getLogger(__name__)


@dataclass
class Position:
    """보유 포지션 정보"""
    code: str
    name: str
    qty: int
    entry_price: float
    stoploss_price: float
    entered_at: datetime
    order_no: str = ""

    @property
    def entry_amount(self) -> float:
        return self.qty * self.entry_price

    def pnl(self, current_price: float) -> float:
        return (current_price - self.entry_price) * self.qty

    def pnl_rate(self, current_price: float) -> float:
        if self.entry_price <= 0:
            return 0.0
        return (current_price - self.entry_price) / self.entry_price


class ExecutionSkill(BaseSkill):
    """
    실제 주문 전송 + 포지션 상태 관리 (Execution Skill)
    """

    def __init__(self, kiwoom, risk_manager):
        super().__init__(kiwoom)
        self.risk = risk_manager
        self.kiwoom = kiwoom
        self._positions: Dict[str, Position] = {}
        self._today_traded: set = set()   # 오늘 매매한 종목 코드
        self._screen_counter = 1000       # 화면번호 자동 증가

    # ─────────────────────────────────────────
    # 매수
    # ─────────────────────────────────────────
    def buy(self, code: str, name: str,
            current_price: float, stoploss_price: float) -> bool:
        """
        시장가 매수
        Returns: True = 주문 성공
        """
        # 중복 진입 방지
        if code in self._positions:
            logger.warning(f"[Execution] {name}({code}) 이미 보유 중 → 매수 취소")
            return False

        # 동일 종목 하루 1회 제한
        if code in self._today_traded:
            logger.warning(f"[Execution] {name}({code}) 오늘 이미 매매 → 매수 취소")
            return False

        # 최대 동시 보유 종목 제한
        if len(self._positions) >= config.MAX_POSITIONS:
            logger.warning(f"[Execution] 최대 보유 종목 수 초과 → 매수 취소")
            return False

        # 리스크 관리 통과 여부
        if not self.risk.can_buy():
            logger.warning(f"[Execution] 리스크 제한 → 매수 취소")
            return False

        # 수량 계산
        available = self.risk.get_available_capital()
        qty = self.risk.calc_position_size(available, current_price)
        if qty <= 0:
            logger.warning(f"[Execution] 수량 0 → 매수 취소")
            return False

        screen_no = str(self._next_screen())

        if config.IS_SIMULATION:
            # 모의투자: 시장가 매수
            ret = self.kiwoom.send_order(
                order_name=f"매수_{code}",
                screen_no=screen_no,
                code=code,
                qty=qty,
                price=0,           # 시장가 = 0
                order_type=1,      # 신규매수
                hoga_type="03"     # 시장가
            )
        else:
            ret = self.kiwoom.send_order(
                order_name=f"매수_{code}",
                screen_no=screen_no,
                code=code,
                qty=qty,
                price=0,
                order_type=1,
                hoga_type="03"
            )

        if ret == 0:
            pos = Position(
                code=code,
                name=name,
                qty=qty,
                entry_price=current_price,
                stoploss_price=stoploss_price,
                entered_at=datetime.now(),
                order_no=screen_no
            )
            self._positions[code] = pos
            self._today_traded.add(code)
            self.risk.on_buy(current_price * qty)
            logger.info(
                f"[Execution] 매수 완료: {name}({code}) "
                f"{qty}주 @ {current_price:,}원 "
                f"(손절가: {stoploss_price:,})"
            )
            return True
        else:
            logger.error(f"[Execution] 매수 실패: {name}({code}) ret={ret}")
            return False

    # ─────────────────────────────────────────
    # 매도
    # ─────────────────────────────────────────
    def sell(self, code: str, current_price: float,
             reason: str = "") -> bool:
        """
        시장가 매도
        Returns: True = 주문 성공
        """
        pos = self._positions.get(code)
        if not pos:
            logger.warning(f"[Execution] {code} 포지션 없음 → 매도 취소")
            return False

        screen_no = str(self._next_screen())
        ret = self.kiwoom.send_order(
            order_name=f"매도_{code}",
            screen_no=screen_no,
            code=code,
            qty=pos.qty,
            price=0,
            order_type=2,      # 신규매도
            hoga_type="03"     # 시장가
        )

        if ret == 0:
            pnl = pos.pnl(current_price)
            pnl_rate = pos.pnl_rate(current_price)
            self.risk.on_sell(pnl)
            del self._positions[code]
            logger.info(
                f"[Execution] 매도 완료: {pos.name}({code}) "
                f"{pos.qty}주 @ {current_price:,}원 "
                f"손익: {pnl:+,.0f}원 ({pnl_rate:+.2%}) "
                f"사유: {reason}"
            )
            return True
        else:
            logger.error(f"[Execution] 매도 실패: {code} ret={ret}")
            return False

    # ─────────────────────────────────────────
    # 조회 및 보유 종목 복원
    # ─────────────────────────────────────────
    def sync_position(self, code: str, name: str, qty: int, entry_price: float):
        """재시작 시 기존에 보유하고 있던 종목을 상태 정보에 복원"""
        if code not in self._positions:
            pos = Position(
                code=code,
                name=name,
                qty=qty,
                entry_price=entry_price,
                stoploss_price=round(entry_price * (1 - config.STOP_LOSS_RATE)),
                entered_at=datetime.now(),
                order_no="recovered"
            )
            self._positions[code] = pos
            self._today_traded.add(code)

    def get_position(self, code: str) -> Optional[Position]:
        return self._positions.get(code)

    def has_position(self, code: str) -> bool:
        return code in self._positions

    def get_all_positions(self) -> Dict[str, Position]:
        return dict(self._positions)

    def reset_daily(self):
        """장 종료 후 일별 상태 초기화"""
        self._today_traded.clear()
        logger.info("[Execution] 일별 상태 초기화 완료")

    def _next_screen(self) -> int:
        self._screen_counter += 1
        if self._screen_counter > 9999:
            self._screen_counter = 1000
        return self._screen_counter

    def execute(self, action_type: str, *args, **kwargs):
        """
        BaseSkill 구현부 (필요 시 Agent가 일관된 인터페이스로 호출하도록 작성)
        """
        if action_type == "BUY":
            return self.buy(*args, **kwargs)
        elif action_type == "SELL":
            return self.sell(*args, **kwargs)
        return False
