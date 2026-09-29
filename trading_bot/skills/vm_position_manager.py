# skills/vm_position_manager.py
# VM picks 전용 다중 포지션 관리 + JSON 영속성 + 고정금액 매수
# BaseSkill 상속 → harness.register_skill("vm", ...) 으로 등록

from dataclasses import dataclass, field, fields as dc_fields
from datetime import datetime
from typing import Dict, List, Optional
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
    tp_ord_no: str = ""   # TP 지정가 매도 주문 번호 (SL 발동 시 취소용)

    def pnl(self, current_price: float) -> float:
        return (current_price - self.entry_price) * self.qty

    def pnl_rate(self, current_price: float) -> float:
        return (
            (current_price - self.entry_price) / self.entry_price
            if self.entry_price
            else 0.0
        )


@dataclass
class VMPendingOrder:
    ord_no:      str
    code:        str
    name:        str
    tranche:     int    # 1차 or 2차
    price:       int    # 지정가 (threshold2=1차, threshold1=2차)
    qty:         int
    stop_loss:   float
    take_profit: float
    created_at:  str
    placed_at:   str    # ISO timestamp
    tp_ord_no:   str = ""
    filled:      bool = False
    fill_price:  int = 0


def _load_dataclass(cls, item: dict):
    """dataclass 필드 중 JSON에 있는 것만 골라 안전하게 생성 (여분 필드 무시)."""
    known = {f.name for f in dc_fields(cls)}
    return cls(**{k: v for k, v in item.items() if k in known})


class VMPositionManager(BaseSkill):
    """
    VM picks 전용 포지션 매니저. BaseSkill 상속으로 하네스에 등록 가능.
    - 코드당 다중 포지션 허용 (position_id로 구분)
    - 고정 금액(VM_TRADE_AMOUNT, 기본 100만원) 시장가 매수 or 지정가 매수
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
        self._pending: Dict[str, VMPendingOrder] = {}  # ord_no → VMPendingOrder
        self._persist_path = persist_path
        self._pending_path = os.path.join(
            os.path.dirname(persist_path), "vm_pending_orders.json"
        )
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

    # ── 영속성 (포지션) ─────────────────────────────────────────────────────────

    def load(self):
        """봇 재시작 시 vm_positions.json에서 포지션 복구"""
        if not os.path.exists(self._persist_path):
            logger.info("[VMManager] vm_positions.json 없음 — 신규 시작")
        else:
            try:
                with open(self._persist_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for item in data:
                    pos = _load_dataclass(VMPosition, item)
                    self._positions[pos.position_id] = pos
                logger.info(f"[VMManager] {len(self._positions)}개 포지션 복구")
            except Exception as e:
                logger.error(f"[VMManager] 포지션 로드 실패: {e}")

        self._reconcile_with_account()
        self.load_pending()

    def _reconcile_with_account(self):
        """
        복구된 포지션을 실제 계좌 보유 수량과 대조.
        모의계좌 재발급 등으로 계좌가 초기화되면 JSON에만 남은 유령 포지션이
        생기는데, 이를 방치하면 존재하지 않는 수량을 계속 매도 시도해
        '매도가능수량 부족' 에러가 반복 발생한다.
        """
        if not self._positions:
            return
        try:
            real = {p["code"]: p["qty"] for p in self.kiwoom.get_positions()}
        except Exception as e:
            logger.error(f"[VMManager] 계좌 대조 실패 — 기존 포지션 그대로 유지: {e}")
            return

        for pid, pos in list(self._positions.items()):
            real_qty = real.get(pos.code, 0)
            if real_qty <= 0:
                logger.warning(
                    f"[VMManager] 계좌 대조: {pos.name}({pos.code}) {pos.qty}주 → "
                    f"실계좌 미보유 확인, 유령 포지션 제거"
                )
                del self._positions[pid]
            elif real_qty < pos.qty:
                logger.warning(
                    f"[VMManager] 계좌 대조: {pos.name}({pos.code}) 수량 "
                    f"{pos.qty}→{real_qty}주 조정"
                )
                pos.qty = real_qty
        self.save()

    def save(self):
        """포지션 변경마다 즉시 JSON으로 저장"""
        try:
            os.makedirs(os.path.dirname(self._persist_path), exist_ok=True)
            data = [vars(p) for p in self._positions.values()]
            with open(self._persist_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[VMManager] 포지션 저장 실패: {e}")

    # ── 영속성 (pending 지정가 주문) ────────────────────────────────────────────

    def load_pending(self):
        """봇 재시작 시 vm_pending_orders.json에서 미체결 주문 복구."""
        if not os.path.exists(self._pending_path):
            return
        try:
            with open(self._pending_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            for item in data:
                p = _load_dataclass(VMPendingOrder, item)
                if not p.filled:
                    self._pending[p.ord_no] = p
            logger.info(f"[VMManager] {len(self._pending)}개 미체결 pending 주문 복구")
        except Exception as e:
            logger.error(f"[VMManager] pending 로드 실패: {e}")

    def save_pending(self):
        """pending 변경마다 즉시 JSON으로 저장."""
        try:
            os.makedirs(os.path.dirname(self._pending_path), exist_ok=True)
            data = [vars(p) for p in self._pending.values()]
            with open(self._pending_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"[VMManager] pending 저장 실패: {e}")

    # ── 지정가 매수 주문 발주 ───────────────────────────────────────────────────

    def place_limit_buy(
        self,
        code: str,
        name: str,
        tranche: int,
        price: int,
        qty: int,
        stop_loss: float,
        take_profit: float,
        created_at: str,
    ) -> str:
        """지정가 매수 등록. 실전: API 발주. 모의투자: 가상 pending(틱 도달 시 시장가 체결)."""
        import time as _time
        if config.IS_SIMULATION:
            # 키움 모의투자 API는 대기 지정가 주문 미지원(RC4027) → 가상 pending 등록
            ord_no = f"SIM_{code}_{tranche}_{int(_time.time())}"
            logger.info(
                f"[VMManager] [Mock] 가상 지정가 등록: {name}({code}) {tranche}차 "
                f"{qty}주 @ {price:,}원 (틱 도달 시 시장가 체결)"
            )
        else:
            ret, ord_no = self.kiwoom.send_order(
                order_name=f"VM지정매수{tranche}_{code}",
                screen_no=str(self._next_screen()),
                code=code,
                qty=qty,
                price=price,
                order_type=1,
                hoga_type="00",
            )
            if ret != 0 or not ord_no:
                logger.error(f"[VMManager] 지정가 매수 발주 실패: {name}({code}) {tranche}차")
                return ""
            logger.info(
                f"[VMManager] 지정가 매수 발주: {name}({code}) {tranche}차 "
                f"{qty}주 @ {price:,}원 ord_no={ord_no}"
            )
        self._pending[ord_no] = VMPendingOrder(
            ord_no=ord_no,
            code=code,
            name=name,
            tranche=tranche,
            price=price,
            qty=qty,
            stop_loss=stop_loss,
            take_profit=take_profit,
            created_at=created_at,
            placed_at=datetime.now().isoformat(),
        )
        self.save_pending()
        return ord_no

    def place_limit_sell(self, code: str, qty: int, tp_price: int) -> str:
        """TP 매도 발주. 실전: 지정가 API. 모의투자: 가상 ord_no(틱 도달 시 시장가 체결)."""
        import time as _time
        if config.IS_SIMULATION:
            return f"SIM_TP_{code}_{int(_time.time())}"
        ret, ord_no = self.kiwoom.send_order(
            order_name=f"VM지정매도_{code}",
            screen_no=str(self._next_screen()),
            code=code,
            qty=qty,
            price=tp_price,
            order_type=2,
            hoga_type="00",
        )
        if ret == 0 and ord_no:
            logger.info(
                f"[VMManager] 지정가 매도 발주: {code} {qty}주 @ {tp_price:,}원 "
                f"ord_no={ord_no}"
            )
            return ord_no
        logger.error(f"[VMManager] 지정가 매도 발주 실패: {code}")
        return ""

    def on_fill_detected(self, ord_no: str, fill_price: int) -> bool:
        """틱 기반 fill 추론 시 호출 — 포지션 생성 + TP 발주.
        모의투자: 가상 pending이므로 실제 시장가 매수 주문도 함께 발주."""
        pending = self._pending.get(ord_no)
        if not pending or pending.filled:
            return False

        # 모의투자: 가상 pending이므로 실제 매수를 시장가로 발주
        if config.IS_SIMULATION:
            ret, _ = self.kiwoom.send_order(
                order_name=f"VM분할매수{pending.tranche}_{pending.code}",
                screen_no=str(self._next_screen()),
                code=pending.code,
                qty=pending.qty,
                price=0,
                order_type=1,
                hoga_type="03",
            )
            if ret != 0:
                logger.error(
                    f"[VMManager] [Mock] 시장가 매수 실패: {pending.name}({pending.code}) "
                    f"{pending.tranche}차"
                )
                return False

        pending.filled = True
        pending.fill_price = fill_price

        position_id = (
            f"{pending.code}_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            f"_t{pending.tranche}"
        )
        pos = VMPosition(
            position_id=position_id,
            code=pending.code,
            name=pending.name,
            qty=pending.qty,
            entry_price=fill_price,
            stop_loss=pending.stop_loss,
            take_profit=pending.take_profit,
            entered_at=datetime.now().isoformat(),
            order_no=pending.ord_no,
            created_at=pending.created_at,
        )
        self._positions[position_id] = pos
        self.risk.on_buy(pending.qty * fill_price)

        tp_price = int(pending.take_profit)
        tp_ord_no = self.place_limit_sell(pending.code, pending.qty, tp_price)
        pending.tp_ord_no = tp_ord_no
        pos.tp_ord_no = tp_ord_no

        self.save()
        self.save_pending()
        logger.info(
            f"[VMManager] fill 확인: {pending.name}({pending.code}) {pending.tranche}차 "
            f"{pending.qty}주 @ {fill_price:,}원 tp_ord={tp_ord_no}"
        )
        return True

    def infer_tp_filled(self, position_id: str, fill_price: float):
        """TP 지정가 매도 체결 추론 — 주문 재발행 없이 포지션 제거."""
        pos = self._positions.pop(position_id, None)
        if not pos:
            return None
        pnl = pos.pnl(fill_price)
        self.risk.on_sell(pnl)
        self.save()
        logger.info(
            f"[VMManager] TP 체결 추론: {pos.name}({pos.code}) "
            f"{pos.qty}주 @ {fill_price:,}원 손익:{pnl:+,.0f}"
        )
        return pos

    def cancel_pending_buys(self, code: Optional[str] = None):
        """미체결 지정가 매수 주문 취소 (EOD 또는 종목 지정)."""
        for ord_no, p in list(self._pending.items()):
            if p.filled:
                continue
            if code and p.code != code:
                continue
            if ord_no.startswith("SIM_"):
                logger.info(f"[VMManager] [Mock] 가상 매수 취소: {p.name}({p.code}) {p.tranche}차")
            else:
                ret = self.kiwoom.cancel_order(ord_no, p.code, p.qty)
                if ret == 0:
                    logger.info(f"[VMManager] 미체결 매수 취소: {p.name}({p.code}) {p.tranche}차")
            del self._pending[ord_no]
        self.save_pending()

    # ── pending 조회 ────────────────────────────────────────────────────────────

    def get_pending_for_code(self, code: str) -> List[VMPendingOrder]:
        """특정 종목의 미체결(미fill) pending 주문 목록."""
        return [p for p in self._pending.values() if p.code == code and not p.filled]

    def has_pending(self, code: str, created_at: str) -> bool:
        """같은 created_at의 pending 주문이 있는지 확인 (중복 발주 방지)."""
        return any(
            p.code == code and p.created_at == created_at
            for p in self._pending.values()
        )

    def get_tp_ord_no(self, position_id: str) -> str:
        """SL 발동 시 취소할 TP 주문 번호 조회."""
        pos = self._positions.get(position_id)
        return pos.tp_ord_no if pos else ""

    # ── 시장가 매수 ─────────────────────────────────────────────────────────────

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
        ret, _ = self.kiwoom.send_order(
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
        ret, _ = self.kiwoom.send_order(
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
        self._reconcile_single(position_id, pos)
        return False

    def _reconcile_single(self, position_id: str, pos: "VMPosition"):
        """
        매도 실패 시 해당 종목만 실계좌와 대조. 실보유가 없으면 유령 포지션으로
        간주해 제거 — 그대로 두면 다음 SL/TP 체크 주기마다 같은 실패가 반복된다.
        """
        try:
            real_list = self.kiwoom.get_positions()
        except Exception as e:
            logger.debug(f"[VMManager] 매도 실패 후 계좌 대조 실패: {e}")
            return
        real_qty = next((p["qty"] for p in real_list if p["code"] == pos.code), 0)
        if real_qty <= 0:
            logger.warning(
                f"[VMManager] {pos.name}({pos.code}) 실계좌 미보유 확인 → 유령 포지션 제거"
            )
            self._positions.pop(position_id, None)
            self.save()
        elif real_qty < pos.qty:
            logger.warning(
                f"[VMManager] {pos.name}({pos.code}) 수량 {pos.qty}→{real_qty}주 조정"
            )
            pos.qty = real_qty
            self.save()

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
