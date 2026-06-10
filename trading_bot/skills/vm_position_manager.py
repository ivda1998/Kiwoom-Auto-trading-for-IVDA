# skills/vm_position_manager.py
# VM picks 전용 다중 포지션 관리 + JSON 영속성 + 고정금액 매수
# BaseSkill 상속 → harness.register_skill("vm", ...) 으로 등록

from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List
import json
import logging
import os

import config
from skills.base_skill import BaseSkill

logger = logging.getLogger(__name__)


@dataclass
class VMPosition:
    position_id: str      # f"{code}_{YYYYmmdd_HHMMSS}"
    code: str
    name: str
    qty: int
    entry_price: float
    stop_loss: float      # 절대 가격 (json에서)
    take_profit: float    # 절대 가격 (json에서)
    entered_at: str       # ISO datetime 문자열
    order_no: str = ""
    created_at: str = ""  # json의 created_at (같은 날 중복 매수 방지용)

    def pnl(self, current_price: float) -> float:
        return (current_price - self.entry_price) * self.qty

    def pnl_rate(self, current_price: float) -> float:
        return (
            (current_price - self.entry_price) / self.entry_price
            if self.entry_price
            else 0.0
        )


class VMPositionManager(BaseSkill):
    """
    VM picks 전용 포지션 매니저. BaseSkill 상속으로 하네스에 등록 가능.
    - 코드당 다중 포지션 허용 (position_id로 구분)
    - 고정 금액(VM_TRADE_AMOUNT, 기본 100만원) 시장가 매수
    - json에 명시된 절대 stop_loss / take_profit 사용
    - 재시작 후 vm_positions.json에서 오버나잇 포지션 복구
    - created_at 기반 중복 매수 방지 (같은 날 추천 = 1회만 매수)

    harness 액션 타입:
      execute("BUY",      code, name, price, sl, tp, created_at, amount) → bool
      execute("SELL",     position_id, current_price, reason)             → bool
      execute("SELL_ALL", code, current_price, reason)                    → None
      execute("LOAD")                                                      → None
      execute("SAVE")                                                      → None
    """

    def __init__(self, kiwoom, risk_manager, persist_path: str):
        super().__init__(kiwoom)          # BaseSkill: self.api = kiwoom
        self.kiwoom = self.api            # 하위 호환 alias
        self.risk = risk_manager
        self._positions: Dict[str, VMPosition] = {}  # position_id → VMPosition
        self._persist_path = persist_path
        self._screen_counter = 2000

    # ── BaseSkill 인터페이스 ────────────────────────────────────────────────────

    def execute(self, action_type: str, *args, **kwargs):
        """
        harness.execute_action("vm", action_type, ...) 진입점.
        훅(RiskHook 등) 파이프라인을 거쳐 호출됨.
        """
        action_type = action_type.upper()
        if action_type == "BUY":
            return self.buy_vm(*args, **kwargs)
        if action_type == "SELL":
            return self.sell_vm(*args, **kwargs)
        if action_type == "SELL_ALL":
            return self.sell_all_by_code(*args, **kwargs)
        if action_type == "LOAD":
            return self.load()
        if action_type == "SAVE":
            return self.save()
        raise ValueError(f"[VMSkill] 알 수 없는 액션: {action_type}")

    # ── 영속성 ──────────────────────────────────────────────────────────────────

    def load(self):
        """봇 재시작 시 vm_positions.json에서 포지션 복구"""
        if not os.path.exists(self._persist_path):
            logger.info("[VMManager] vm_positions.json 없음 — 신규 시작")
            return
        try:
            with open(self._persist_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for item in data:
                pos = VMPosition(**item)
                self._positions[pos.position_id] = pos
            logger.info(f"[VMManager] {len(self._positions)}개 포지션 복구")
        except Exception as e:
            logger.error(f"[VMManager] 포지션 로드 실패: {e}")

    def save(self):
        """포지션 변경마다 즉시 JSON으로 저장"""
        try:
            os.makedirs(os.path.dirname(self._persist_path), exist_ok=True)
            data = [vars(p) for p in self._positions.values()]
            with open(self._persist_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[VMManager] 포지션 저장 실패: {e}")

    # ── 매수 ────────────────────────────────────────────────────────────────────

    def buy_vm(
        self,
        code: str,
        name: str,
        price: float,
        stop_loss: float,
        take_profit: float,
        created_at: str = "",
        amount: int = None,
    ) -> bool:
        """
        시장가 매수.
        amount: 매수 금액 (None이면 config.VM_TRADE_AMOUNT 사용).
        예수금 부족 시 False 반환 — 호출자가 대기열에 추가해야 함.
        성공 시 True 반환.
        """
        if amount is None:
            amount = getattr(config, "VM_TRADE_AMOUNT", 1_000_000)
        qty = max(1, int(amount / price))
        cost = qty * price

        available = self.risk.get_available_capital()
        if cost > available:
            logger.warning(
                f"[VMManager] {name}({code}) 예수금 부족 "
                f"(필요:{cost:,.0f} > 가용:{available:,.0f}) → 대기열"
            )
            return False

        screen_no = str(self._next_screen())
        ret = self.kiwoom.send_order(
            order_name=f"VM매수_{code}",
            screen_no=screen_no,
            code=code,
            qty=qty,
            price=0,
            order_type=1,
            hoga_type="03",
        )
        if ret == 0:
            position_id = f"{code}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            pos = VMPosition(
                position_id=position_id,
                code=code,
                name=name,
                qty=qty,
                entry_price=price,
                stop_loss=stop_loss,
                take_profit=take_profit,
                entered_at=datetime.now().isoformat(),
                order_no=screen_no,
                created_at=created_at,
            )
            self._positions[position_id] = pos
            self.risk.on_buy(cost)
            self.save()
            logger.info(
                f"[VMManager] 매수 완료: {name}({code}) {qty}주 @ {price:,}원 "
                f"SL={stop_loss:,} TP={take_profit:,}"
            )
            return True
        else:
            logger.error(f"[VMManager] 매수 실패: {name}({code}) ret={ret}")
            return False

    # ── 매도 ────────────────────────────────────────────────────────────────────

    def sell_vm(
        self, position_id: str, current_price: float, reason: str = ""
    ) -> bool:
        """단일 포지션 청산. 성공 시 True, 포지션 없으면 False."""
        pos = self._positions.get(position_id)
        if not pos:
            return False

        screen_no = str(self._next_screen())
        ret = self.kiwoom.send_order(
            order_name=f"VM매도_{pos.code}",
            screen_no=screen_no,
            code=pos.code,
            qty=pos.qty,
            price=0,
            order_type=2,
            hoga_type="03",
        )
        if ret == 0:
            pnl = pos.pnl(current_price)
            self.risk.on_sell(pnl)
            del self._positions[position_id]
            self.save()
            logger.info(
                f"[VMManager] 매도 완료: {pos.name}({pos.code}) "
                f"{pos.qty}주 @ {current_price:,}원 손익:{pnl:+,.0f} 사유:{reason}"
            )
            return True
        logger.error(f"[VMManager] 매도 실패: {pos.name}({pos.code}) ret={ret}")
        return False

    def sell_all_by_code(self, code: str, current_price: float, reason: str):
        """한 종목 코드의 모든 포지션 청산 (장마감·강제청산 시)."""
        for pid in [k for k, v in list(self._positions.items()) if v.code == code]:
            self.sell_vm(pid, current_price, reason)

    # ── SL/TP 업데이트 ────────────────────────────────────────────────────────

    def update_sltp(self, code: str, stop_loss: float, take_profit: float):
        """동일 종목 재추천 시 열린 포지션의 SL/TP를 최신값으로 갱신."""
        changed = False
        for pos in self._positions.values():
            if pos.code == code:
                pos.stop_loss = stop_loss
                pos.take_profit = take_profit
                changed = True
                logger.info(
                    f"[VMManager] SL/TP 업데이트: {pos.name}({code}) "
                    f"SL={stop_loss:,} TP={take_profit:,}"
                )
        if changed:
            self.save()

    # ── 조회 ────────────────────────────────────────────────────────────────────

    def get_by_code(self, code: str) -> List[VMPosition]:
        return [p for p in self._positions.values() if p.code == code]

    def has_any(self, code: str) -> bool:
        return any(p.code == code for p in self._positions.values())

    def has_position_for_date(self, code: str, created_at: str) -> bool:
        """같은 날짜 추천에 대한 포지션이 이미 있는지 확인."""
        return any(
            p.code == code and p.created_at == created_at
            for p in self._positions.values()
        )

    def count_positions_for_date(self, code: str, created_at: str) -> int:
        """같은 날짜 추천에 대해 보유 중인 트랜치 수 반환 (분할 매수 추적용)."""
        return sum(
            1 for p in self._positions.values()
            if p.code == code and p.created_at == created_at
        )

    def get_all(self) -> Dict[str, VMPosition]:
        return dict(self._positions)

    def count(self) -> int:
        return len(self._positions)

    # ── 내부 ────────────────────────────────────────────────────────────────────

    def _next_screen(self) -> int:
        self._screen_counter += 1
        if self._screen_counter > 9999:
            self._screen_counter = 2000
        return self._screen_counter
