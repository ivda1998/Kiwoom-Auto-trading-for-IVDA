import logging
from datetime import datetime
from typing import Optional, List

import config
from agents.base_agent import BaseAgent
from logger import TradeLogger

logger = logging.getLogger(__name__)


class LeverageInverseAgent(BaseAgent):
    """
    삼성전자 방향성 모멘텀 기반 레버리지/인버스 ETF 당일 매매 전략.

    신호원: 삼성전자(005930) 3분봉
      - 현재가 > 5MA + 연속 2봉 양봉 + 거래량 1.5배 이상 → 0193W0 매수
      - 현재가 < 5MA + 연속 2봉 음봉 + 거래량 1.5배 이상 → 0193L0 매수
    청산: SL -1.5% / TP +3.0% (ETF 기준) / MA 역전 / 14:50 EOD 강제
    """

    LEVERAGE_CODE    = "0193W0"
    INVERSE_CODE     = "0193L0"
    SAMSUNG_CODE     = "005930"

    SIGNAL_MA_PERIOD = 5
    SIGNAL_VOL_RATIO = 1.5
    SIGNAL_CONSEC    = 2

    SL_RATE          = 0.015
    TP_RATE          = 0.030

    ENTRY_START      = "09:00"
    ENTRY_END        = "15:00"
    FORCE_EXIT_TIME  = "15:20"

    _SCREEN_BASE     = 9100

    def __init__(self):
        super().__init__()
        self.trade_logger       = TradeLogger()
        self._position: Optional[dict] = None
        self._last_etf_price: dict     = {}
        self._screen_counter           = 0
        self._force_exited_today       = False

    # ─────────────────────────────────────────
    # 이벤트 디스패치
    # ─────────────────────────────────────────

    def analyze_and_act(self, event: dict, context: dict):
        etype = event["type"]
        data  = event["data"]

        if etype == "TICK":
            code  = data.get("code", "")
            price = data.get("price", 0)
            if code in (self.LEVERAGE_CODE, self.INVERSE_CODE):
                self._last_etf_price[code] = price
                self._check_etf_sl_tp(code, price, context)
            elif code == self.SAMSUNG_CODE:
                self._check_force_exit(context)

        elif etype == "CANDLE":
            candle = data  # Candle dataclass (code, open, high, low, close, volume, is_closed)
            if candle.code == self.SAMSUNG_CODE and candle.is_closed:
                self._on_samsung_candle(candle, context)

    # ─────────────────────────────────────────
    # 신호 처리 (캔들 확정 시)
    # ─────────────────────────────────────────

    def _on_samsung_candle(self, candle, context):
        harness     = context["harness"]
        market_data = harness.skills.get("market_data")
        candles     = market_data.get_candles(self.SAMSUNG_CODE)
        now_hm      = datetime.now().strftime("%H:%M")

        # 시뮬 모드에서는 Samsung TICK이 없으므로 캔들 도착 시에도 EOD 체크
        self._check_force_exit(context)
        if self._force_exited_today:
            return

        # 포지션 보유 중: MA 역전 청산 체크
        if self._position:
            ma = self._calc_ma(candles, self.SIGNAL_MA_PERIOD)
            if ma:
                direction = self._position["direction"]
                if direction == "LEVERAGE" and candle.close < ma:
                    self._exit("삼성MA하향(방향역전)", candle.close, context)
                elif direction == "INVERSE" and candle.close > ma:
                    self._exit("삼성MA상향(방향역전)", candle.close, context)
            return

        # 신규 진입 체크
        if not getattr(config, "LEVERAGE_ENABLED", True):
            return
        if self._force_exited_today:
            return
        if not (self.ENTRY_START <= now_hm <= self.ENTRY_END):
            return
        if len(candles) < self.SIGNAL_MA_PERIOD + self.SIGNAL_CONSEC:
            return

        direction = self._calc_signal(candles)
        if direction:
            self._enter(direction, context)

    # ─────────────────────────────────────────
    # 신호 계산
    # ─────────────────────────────────────────

    def _calc_signal(self, candles: list) -> Optional[str]:
        closed = [c for c in candles if c.is_closed]
        ma = self._calc_ma(closed, self.SIGNAL_MA_PERIOD)
        if not ma or len(closed) < self.SIGNAL_CONSEC:
            return None

        last    = closed[-1]
        avg_vol = self._calc_avg_volume(closed, 10)
        vol_ok  = avg_vol > 0 and last.volume >= avg_vol * self.SIGNAL_VOL_RATIO

        recent   = closed[-self.SIGNAL_CONSEC:]
        all_bull = all(c.is_bullish for c in recent)
        all_bear = all(c.close < c.open for c in recent)  # 순수 음봉(doji 제외)

        if last.close > ma and all_bull and vol_ok:
            return "LEVERAGE"
        if last.close < ma and all_bear and vol_ok:
            return "INVERSE"
        return None

    def _calc_ma(self, candles: list, period: int) -> Optional[float]:
        closed = [c for c in candles if c.is_closed]
        if len(closed) < period:
            return None
        return sum(c.close for c in closed[-period:]) / period

    def _calc_avg_volume(self, candles: list, n: int) -> float:
        closed = [c for c in candles if c.is_closed]
        # 직전봉(closed[-1])을 제외한 최근 n봉 평균
        sample = closed[-n - 1:-1] if len(closed) >= n + 1 else closed[:-1]
        if not sample:
            return 0.0
        return sum(c.volume for c in sample) / len(sample)

    # ─────────────────────────────────────────
    # 진입
    # ─────────────────────────────────────────

    def _enter(self, direction: str, context):
        harness  = context["harness"]
        etf_code = self.LEVERAGE_CODE if direction == "LEVERAGE" else self.INVERSE_CODE
        etf_name = "KODEX삼성레버리지" if direction == "LEVERAGE" else "PLUS삼성인버스2x"

        price = self._last_etf_price.get(etf_code, 0)
        if price <= 0:
            try:
                info  = harness.kiwoom.get_stock_info(etf_code)
                price = info.get("current_price", 0)
            except Exception as e:
                logger.warning(f"[LeverageAgent] {etf_code} 현재가 조회 실패: {e}")
        if price <= 0:
            logger.warning(f"[LeverageAgent] {etf_code} 현재가 미확인 → 진입 보류")
            return

        amount = getattr(config, "LEVERAGE_AMOUNT", 500_000)
        qty    = max(1, amount // price)

        ret, ord_no = harness.kiwoom.send_order(
            order_name=f"LEV_{direction}_{etf_code}",
            screen_no=self._next_screen(),
            code=etf_code,
            qty=qty,
            price=0,
            order_type=1,
            hoga_type="03",
        )

        if ret != 0:
            logger.error(f"[LeverageAgent] 매수 주문 실패 ({etf_code}) ret={ret}")
            return

        sl = round(price * (1 - self.SL_RATE))
        tp = round(price * (1 + self.TP_RATE))

        self._position = {
            "code":        etf_code,
            "name":        etf_name,
            "direction":   direction,
            "qty":         qty,
            "entry_price": price,
            "stop_loss":   sl,
            "take_profit": tp,
            "entered_at":  datetime.now().isoformat(),
        }

        logger.info(
            f"[LeverageAgent] {direction} 진입: {etf_name}({etf_code}) "
            f"{qty}주 @ {price:,}원 | SL {sl:,} TP {tp:,} | ord={ord_no}"
        )
        self.trade_logger.log_trade(
            code=etf_code, name=etf_name, side="BUY",
            qty=qty, price=price, reason=f"레버리지전략_{direction}"
        )
        notifier = harness.get_context().get("notifier")
        if notifier:
            label = "레버리지(롱)" if direction == "LEVERAGE" else "인버스(숏)"
            notifier.send(
                f"🟢 <b>레버리지 전략 진입</b> [{etf_name}({etf_code})]\n"
                f"방향: {label} | {qty}주 @ {price:,}원\n"
                f"손절: {sl:,}원 (-{self.SL_RATE:.1%}) | "
                f"익절: {tp:,}원 (+{self.TP_RATE:.1%})"
            )

    # ─────────────────────────────────────────
    # 청산
    # ─────────────────────────────────────────

    def _exit(self, reason: str, ref_price: int, context):
        if not self._position:
            return

        harness  = context["harness"]
        pos      = self._position
        etf_code = pos["code"]
        etf_name = pos["name"]
        qty      = pos["qty"]

        ret, ord_no = harness.kiwoom.send_order(
            order_name=f"LEV_EXIT_{etf_code}",
            screen_no=self._next_screen(),
            code=etf_code,
            qty=qty,
            price=0,
            order_type=2,
            hoga_type="03",
        )

        if ret != 0:
            logger.error(f"[LeverageAgent] 매도 주문 실패 ({etf_code}) ret={ret}")

        exit_price = self._last_etf_price.get(etf_code, 0) or ref_price
        pnl_rate   = (exit_price - pos["entry_price"]) / pos["entry_price"] if pos["entry_price"] else 0.0
        self._position = None

        logger.info(
            f"[LeverageAgent] 청산({reason}): {etf_name}({etf_code}) "
            f"{qty}주 @ {exit_price:,}원 | PnL {pnl_rate:+.2%} | ord={ord_no}"
        )
        self.trade_logger.log_trade(
            code=etf_code, name=etf_name, side="SELL",
            qty=qty, price=exit_price, reason=reason
        )
        notifier = harness.get_context().get("notifier")
        if notifier:
            emoji = "🔴" if pnl_rate < 0 else "🟡"
            notifier.send(
                f"{emoji} <b>레버리지 전략 청산</b> [{etf_name}({etf_code})]\n"
                f"사유: {reason} | {qty}주 @ {exit_price:,}원\n"
                f"손익: {pnl_rate:+.2%}"
            )

    # ─────────────────────────────────────────
    # SL/TP 실시간 체크 (ETF 틱)
    # ─────────────────────────────────────────

    def _check_etf_sl_tp(self, code: str, price: int, context):
        if not self._position or self._position["code"] != code:
            return
        pos = self._position
        if price <= pos["stop_loss"]:
            self._exit("손절", price, context)
        elif price >= pos["take_profit"]:
            self._exit("익절", price, context)

    # ─────────────────────────────────────────
    # EOD 강제 청산
    # ─────────────────────────────────────────

    def _check_force_exit(self, context):
        if not self._position or self._force_exited_today:
            return
        force_time = getattr(config, "LEVERAGE_FORCE_EXIT", self.FORCE_EXIT_TIME)
        if datetime.now().strftime("%H:%M") >= force_time:
            self._exit("EOD강제청산", 0, context)
            self._force_exited_today = True

    # ─────────────────────────────────────────
    # 일일 초기화
    # ─────────────────────────────────────────

    def reset_daily(self):
        self._force_exited_today = False
        if self._position:
            logger.warning(
                f"[LeverageAgent] 일일 초기화 시 잔여 포지션 발견: "
                f"{self._position['code']} → 내부 상태 초기화"
            )
            self._position = None

    # ─────────────────────────────────────────
    # 유틸
    # ─────────────────────────────────────────

    def _next_screen(self) -> str:
        self._screen_counter = (self._screen_counter + 1) % 10
        return str(self._SCREEN_BASE + self._screen_counter)
