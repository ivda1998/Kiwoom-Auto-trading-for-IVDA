import json
import logging
import os
from datetime import datetime
from typing import Optional

import config
from agents.base_agent import BaseAgent
from logger import TradeLogger

logger = logging.getLogger(__name__)

_STATE_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "personal_invest_state.json")


class PersonalInvestAgent(BaseAgent):
    """사용자가 직접 등록한 개별주 진입가/목표가/손절가 계획을 감시해 자동 매수·청산.

    /invest CODE entry=MIN-MAX target=T stop=S 로 등록만 하면:
      - 가격이 entry 범위에 들어오면 고정 금액(config.PERSONAL_INVEST_AMOUNT)으로 매수
      - 매수 후 target 도달 시 익절, stop 도달 시 손절 — 오버나잇 보유, EOD 강제청산 없음
        (단기 ETF 매매가 아니라 "계획을 세우고 그대로 지키는" 개별주 투자용이라 당일
        시간 제한을 두지 않음)

    목적: 계획 없이 즉흥적으로 사고파는 걸 막기 위해, 매수 전에 반드시 목표가/손절가를
    정하게 하고 등록된 계획대로만 기계적으로 실행한다.
    """

    _SCREEN_BASE = 9300

    def __init__(self):
        super().__init__()
        self.trade_logger = TradeLogger()
        self._plans: dict = {}       # code -> {name, entry_min, entry_max, target, stop, amount, qty_planned, risk_reward, registered_at}
        self._positions: dict = {}   # code -> {name, qty, entry_price, target, stop, entered_at, order_no}
        self._screen_counter = 0
        self._load_state()

    # ─────────────────────────────────────────
    # 영속화 (재시작 시에도 계획/포지션 유지 — leverage_agent의 미영속 버그 교훈)
    # ─────────────────────────────────────────
    def _load_state(self):
        try:
            if os.path.exists(_STATE_PATH):
                with open(_STATE_PATH, encoding="utf-8") as f:
                    state = json.load(f)
                self._plans = state.get("plans", {})
                self._positions = state.get("positions", {})
                if self._plans or self._positions:
                    logger.info(
                        f"[PersonalInvest] 상태 복원: 계획 {len(self._plans)}건, "
                        f"포지션 {len(self._positions)}건"
                    )
        except Exception as e:
            logger.error(f"[PersonalInvest] 상태 로드 실패: {e}")

    def _save_state(self):
        try:
            os.makedirs(os.path.dirname(_STATE_PATH), exist_ok=True)
            with open(_STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(
                    {"plans": self._plans, "positions": self._positions},
                    f, ensure_ascii=False, indent=2,
                )
        except Exception as e:
            logger.error(f"[PersonalInvest] 상태 저장 실패: {e}")

    def _next_screen(self) -> str:
        self._screen_counter = (self._screen_counter + 1) % 10
        return str(self._SCREEN_BASE + self._screen_counter)

    # ─────────────────────────────────────────
    # 계획 등록/취소/조회 (텔레그램 명령에서 호출)
    # ─────────────────────────────────────────
    def get_all_positions(self) -> dict:
        """보유 중인 개별주 계획매매 포지션 (code -> position dict)."""
        return dict(self._positions)

    def reconcile_with_account(self, real_qty_by_code: dict) -> list:
        """실계좌에 없는(=외부에서 청산된) 보유 포지션을 제거.

        이 에이전트가 직접 팔지 않은 경우(예: 다른 에이전트/일반 전략이 청산, 또는
        수동 매도) 자기 상태가 실계좌와 어긋나 유령 포지션으로 남는다. 방치하면 조회에
        계속 보유로 뜨고, 가격이 목표/손절에 닿으면 없는 수량을 팔려다 실패한다.
        vm_manager._reconcile_with_account()와 같은 취지.
        """
        removed = []
        for code in list(self._positions.keys()):
            if real_qty_by_code.get(code, 0) <= 0:
                pos = self._positions.pop(code)
                removed.append((code, pos))
                logger.warning(
                    f"[PersonalInvest] 실계좌 미보유 확인 → 유령 포지션 제거: "
                    f"{pos.get('name')}({code}) {pos.get('qty')}주"
                )
        if removed:
            self._save_state()
        return removed

    def get_all_plans(self) -> dict:
        """매수 대기 중인 등록 계획 (code -> plan dict)."""
        return dict(self._plans)

    def watched_codes(self) -> list:
        """REST 틱 폴링 대상에 추가할 종목코드 (대기 계획 + 보유 포지션)."""
        return list(dict.fromkeys(list(self._plans.keys()) + list(self._positions.keys())))

    def register_plan(self, code: str, name: str, entry_min: float, entry_max: float,
                       target: float, stop: float, amount: int) -> dict:
        """검증 후 계획 등록. 반환: {"ok": bool, "msg": str, "plan": dict|None}"""
        if code in self._positions:
            return {"ok": False, "msg": f"{name}({code})는 이미 보유 중입니다.", "plan": None}
        if code in self._plans:
            return {"ok": False, "msg": f"{name}({code})는 이미 대기 중인 계획이 있습니다. 먼저 /invest cancel {code}로 취소하세요.", "plan": None}
        if entry_min <= 0 or entry_max <= 0 or entry_min > entry_max:
            return {"ok": False, "msg": "진입가 범위가 올바르지 않습니다 (entry_min <= entry_max, 둘 다 양수).", "plan": None}
        if stop >= entry_min:
            return {"ok": False, "msg": "손절가는 진입가(최소값)보다 낮아야 합니다.", "plan": None}
        if target <= entry_max:
            return {"ok": False, "msg": "목표가는 진입가(최대값)보다 높아야 합니다.", "plan": None}

        qty = max(1, int(amount) // int(entry_max))
        risk = entry_max - stop
        reward = target - entry_max
        rr = round(reward / risk, 2) if risk > 0 else 0.0

        plan = {
            "name": name, "entry_min": entry_min, "entry_max": entry_max,
            "target": target, "stop": stop, "amount": int(amount), "qty_planned": qty,
            "risk_reward": rr,
            "registered_at": datetime.now().isoformat(),
        }
        self._plans[code] = plan
        self._save_state()
        return {"ok": True, "msg": "등록 완료", "plan": plan}

    def cancel_plan(self, code: str) -> bool:
        if code in self._plans:
            del self._plans[code]
            self._save_state()
            return True
        return False

    # ─────────────────────────────────────────
    # 이벤트 디스패치
    # ─────────────────────────────────────────
    def analyze_and_act(self, event: dict, context: dict):
        if event.get("type") != "TICK":
            return
        data  = event.get("data", {})
        code  = data.get("code", "")
        price = data.get("price", 0)
        if not code or price <= 0:
            return

        if code in self._plans:
            self._check_entry(code, price, context)
        elif code in self._positions:
            self._check_exit(code, price, context)

    # ─────────────────────────────────────────
    # 진입
    # ─────────────────────────────────────────
    def _check_entry(self, code: str, price: float, context):
        plan = self._plans.get(code)
        if not plan:
            return
        if not (plan["entry_min"] <= price <= plan["entry_max"]):
            return

        harness = context["harness"]
        qty = max(1, int(plan["amount"]) // int(price))

        ret, ord_no = harness.kiwoom.send_order(
            order_name=f"INVEST_{code}",
            screen_no=self._next_screen(),
            code=code,
            qty=qty,
            price=0,
            order_type=1,
            hoga_type="03",
        )
        if ret != 0:
            logger.error(f"[PersonalInvest] 매수 주문 실패 ({code}) ret={ret}")
            return

        del self._plans[code]
        self._positions[code] = {
            "name": plan["name"], "qty": qty, "entry_price": price,
            "target": plan["target"], "stop": plan["stop"],
            "entered_at": datetime.now().isoformat(), "order_no": ord_no,
        }
        self._save_state()

        logger.info(
            f"[PersonalInvest] 매수 체결: {plan['name']}({code}) {qty}주 @ {price:,.0f}원 | "
            f"목표 {plan['target']:,.0f} 손절 {plan['stop']:,.0f} | ord={ord_no}"
        )
        self.trade_logger.log_trade(
            code=code, name=plan["name"], side="BUY", qty=qty, price=price,
            reason="개별주계획매수",
        )
        notifier = harness.get_context().get("notifier")
        if notifier:
            notifier.send(
                f"🟢 <b>개별주 계획매수 체결</b> [{plan['name']}({code})]\n"
                f"{qty}주 @ {price:,.0f}원\n"
                f"목표가: {plan['target']:,.0f}원 | 손절가: {plan['stop']:,.0f}원"
            )

    # ─────────────────────────────────────────
    # 청산
    # ─────────────────────────────────────────
    def _check_exit(self, code: str, price: float, context):
        pos = self._positions.get(code)
        if not pos:
            return

        if price >= pos["target"]:
            reason = "목표가도달"
        elif price <= pos["stop"]:
            reason = "손절가도달"
        else:
            return

        harness = context["harness"]
        qty = pos["qty"]

        ret, ord_no = harness.kiwoom.send_order(
            order_name=f"INVEST_EXIT_{code}",
            screen_no=self._next_screen(),
            code=code,
            qty=qty,
            price=0,
            order_type=2,
            hoga_type="03",
        )
        if ret != 0:
            logger.error(f"[PersonalInvest] 매도 주문 실패 ({code}) ret={ret}")
            return  # 주문 실패 시 포지션 유지

        pnl = (price - pos["entry_price"]) * qty
        pnl_rate = (price - pos["entry_price"]) / pos["entry_price"] if pos["entry_price"] else 0.0
        del self._positions[code]
        self._save_state()

        logger.info(
            f"[PersonalInvest] 청산({reason}): {pos['name']}({code}) "
            f"{qty}주 @ {price:,.0f}원 | PnL {pnl:+,.0f}원 ({pnl_rate:+.2%}) | ord={ord_no}"
        )
        self.trade_logger.log_trade(
            code=code, name=pos["name"], side="SELL", qty=qty, price=price,
            pnl=pnl, pnl_rate=pnl_rate, reason=f"개별주_{reason}",
        )
        notifier = harness.get_context().get("notifier")
        if notifier:
            emoji = "🎯" if reason == "목표가도달" else "🔴"
            notifier.send(
                f"{emoji} <b>개별주 계획청산</b> [{pos['name']}({code})] ({reason})\n"
                f"{qty}주 @ {price:,.0f}원 | 손익 {pnl:+,.0f}원 ({pnl_rate:+.2%})"
            )
