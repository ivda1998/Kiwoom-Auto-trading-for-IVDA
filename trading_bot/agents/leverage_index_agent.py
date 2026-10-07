import logging
import time
from datetime import datetime
from typing import Optional

import config
from agents.base_agent import BaseAgent
from logger import TradeLogger

logger = logging.getLogger(__name__)


class LeverageIndexAgent(BaseAgent):
    """KOSPI200 지수 2x 방향성 당일 매매 — 삼성 단일종목 레버리지의 지수 버전.

    배경: 삼성 단일종목 레버리지(0193W0/0193L0)는 유동성·증거금 부담이 커 실전에
    3,000만원+가 필요하다. 지수 2x ETF는 거래대금이 수십 배 크고 증거금 부담이 낮아
    적은 자본으로 운용 가능하다. 2026-10-07 백테스트/워크포워드로 "삼성 전략을 지수로
    옮겨도 통한다"를 확인(안정형 BB29/1.0/09:30/SL2.5/trail1.5: 홀드아웃 t3.89·승률63%·
    13개월 중 12개월 양수, 숏이 롱보다 강한 레그). 실거래 전 모의 표본 축적용으로 삼성
    전략과 **병행** 운용한다(삼성 LeverageInverseAgent는 그대로 둠).

    구조:
      - 신호원 = 레버리지 ETF(122630) 자체 3분봉 (= KOSPI200 방향, 백테스트와 동일)
      - 방향: 122630 일봉 단기MA/장기MA 정배열 (INDEX_TREND_FAST_MA/SLOW_MA)
      - 진입: 그 방향으로 3분봉 볼린저밴드(INDEX_BB_PERIOD/MULT) 상/하단 돌파 시
             롱 → 122630 매수 / 숏 → 252670(인버스2X) 매수
      - 청산: 손절(진입가 -SL_RATE)·단일단계 트레일링(피크수익≥SL_RATE 후 GAP 이탈)·
             EOD 강제청산(FORCE_EXIT). 두 방향 모두 '매수'라 보유 ETF 가격 상승이 favorable
             → SL/트레일 로직은 방향 무관(백테스트 현실모델과 동일).

    주의: 이 에이전트 단독 소유 종목 = {122630, 252670}. 재시작 시 다른 에이전트/일반
    돌파 전략이 흡수하지 못하도록 main._sync_existing_positions의 skip_codes에 포함해야 함
    (교차 소유권 불변식).
    """

    ENTRY_END       = "15:00"
    FORCE_EXIT_TIME = "15:20"

    _SCREEN_BASE    = 9400   # 삼성레버리지(9100)·개별주(9300)와 겹치지 않게

    def __init__(self):
        super().__init__()
        self.trade_logger = TradeLogger()
        self._position: Optional[dict] = None
        self._last_etf_price: dict = {}
        self._screen_counter = 0
        self._force_exited_today = False
        self._last_exit_time: Optional[datetime] = None
        self._daily_trend_cache: dict = {}

    @property
    def LEVERAGE_CODE(self) -> str:   # 롱 ETF (= 신호원)
        return getattr(config, "LEVERAGE_INDEX_LEV_CODE", "122630")

    @property
    def INVERSE_CODE(self) -> str:    # 숏 ETF (인버스2X)
        return getattr(config, "LEVERAGE_INDEX_INV_CODE", "252670")

    @property
    def SIGNAL_CODE(self) -> str:     # 신호 분봉 종목 = 롱 ETF 자체
        return self.LEVERAGE_CODE

    # ─────────────────────────────────────────
    # 이벤트 디스패치
    # ─────────────────────────────────────────
    def analyze_and_act(self, event: dict, context: dict):
        etype = event["type"]
        data  = event["data"]

        if etype == "TICK":
            code  = data.get("code", "")
            price = data.get("price", 0)
            if code in (self.LEVERAGE_CODE, self.INVERSE_CODE) and price > 0:
                self._last_etf_price[code] = price
                self._check_sl_tp(code, price, context)

        elif etype == "CANDLE":
            candle = data
            if candle.code == self.SIGNAL_CODE and candle.is_closed:
                self._on_signal_candle(candle, context)

    # ─────────────────────────────────────────
    # 신호 처리 (신호 분봉 확정 시)
    # ─────────────────────────────────────────
    def _on_signal_candle(self, candle, context):
        if not getattr(config, "LEVERAGE_INDEX_ENABLED", False):
            return

        harness     = context["harness"]
        market_data = harness.skills.get("market_data")
        candles     = market_data.get_candles(self.SIGNAL_CODE) if market_data else []
        now_hm      = datetime.now().strftime("%H:%M")
        entry_start = getattr(config, "LEVERAGE_INDEX_ENTRY_START", "09:30")
        entry_end   = getattr(config, "LEVERAGE_INDEX_ENTRY_END", self.ENTRY_END)

        # 시뮬 모드에서는 ETF TICK 폴링이 끊겨도 봉 도착 시 EOD 체크
        self._check_force_exit(context)
        if self._force_exited_today:
            return

        # 보유 중: BOLLINGER 전략은 봉/신호 기반 청산 없음 — 손절·트레일(틱)·EOD만
        if self._position:
            return

        if not (entry_start <= now_hm <= entry_end):
            return

        # 청산 직후 1봉 쿨다운 (연속 전환 방지)
        cooldown_sec = getattr(config, "LEVERAGE_INDEX_CANDLE_INTERVAL", 3) * 60
        if self._last_exit_time and (datetime.now() - self._last_exit_time).total_seconds() < cooldown_sec:
            return

        direction = self._calc_signal_bollinger(candles, harness)
        if direction:
            self._enter(direction, context)

    # ─────────────────────────────────────────
    # 신호 계산 — 일봉추세 + 3분봉 볼린저 돌파
    # ─────────────────────────────────────────
    def _get_daily_trend(self, harness) -> Optional[str]:
        """신호원(122630) 일봉 단기MA vs 장기MA 정배열로 당일 허용 방향 판단. 하루 1회 캐시."""
        fast_ma = getattr(config, "LEVERAGE_INDEX_TREND_FAST_MA", 10)
        slow_ma = getattr(config, "LEVERAGE_INDEX_TREND_SLOW_MA", 20)

        today = datetime.now().strftime("%Y-%m-%d")
        if self._daily_trend_cache.get("date") == today:
            return self._daily_trend_cache.get("trend")

        # ka10081 max_retries=0 → 429 한 번에 None. 하루 1회 판단이라 재시도로 보강.
        df = None
        for attempt in range(3):
            try:
                df = harness.kiwoom.get_daily_data(self.SIGNAL_CODE, count=max(40, slow_ma + 15))
            except Exception as e:
                logger.warning(f"[LeverageIndexAgent] 일봉 조회 예외(시도 {attempt+1}/3): {e}")
                df = None
            if df is not None and not df.empty:
                break
            time.sleep(1.5)

        if df is None or df.empty:
            logger.warning("[LeverageIndexAgent] 일봉 조회 3회 실패 → 추세 판단불가 (이전 캐시 유지)")
            return self._daily_trend_cache.get("trend")

        rows = df.to_dict("records")
        today_str = datetime.now().strftime("%Y%m%d")
        if rows and str(rows[-1].get("date", "")) == today_str:
            rows = rows[:-1]   # 오늘 형성 중인 봉 제외

        if len(rows) < slow_ma:
            logger.warning(f"[LeverageIndexAgent] 일봉 부족({len(rows)}행/{slow_ma}, df원본 {len(df)}행) → 추세 판단 불가")
            return None

        closes = [r["close"] for r in rows]
        maf = sum(closes[-fast_ma:]) / fast_ma
        mas = sum(closes[-slow_ma:]) / slow_ma
        trend = "UP" if maf > mas else ("DOWN" if maf < mas else None)

        self._daily_trend_cache = {"date": today, "trend": trend}
        logger.info(f"[LeverageIndexAgent] 일봉추세 갱신: MA{fast_ma}={maf:,.1f} MA{slow_ma}={mas:,.1f} → {trend}")
        return trend

    def _calc_signal_bollinger(self, candles: list, harness) -> Optional[str]:
        period = getattr(config, "LEVERAGE_INDEX_BB_PERIOD", 29)
        mult   = getattr(config, "LEVERAGE_INDEX_BB_MULT", 1.0)
        closed = [c for c in candles if c.is_closed]
        closes = [c.close for c in closed]   # ETF는 애프터마켓 거래 제외 → 압축 불필요
        if len(closes) < period + 1 or harness is None:
            return None

        trend = self._get_daily_trend(harness)
        if trend is None:
            return None

        _, upper_now, lower_now = self._bollinger(closes, period, mult)
        _, upper_prev, lower_prev = self._bollinger(closes[:-1], period, mult)
        if upper_now is None or upper_prev is None:
            return None

        close_now, close_prev = closes[-1], closes[-2]
        breakout_up   = close_prev <= upper_prev and close_now > upper_now
        breakout_down = close_prev >= lower_prev and close_now < lower_now
        logger.debug(
            f"[LeverageIndexAgent] 추세={trend} BB상 {upper_prev:.0f}->{upper_now:.0f} "
            f"BB하 {lower_prev:.0f}->{lower_now:.0f} close {close_prev}->{close_now} "
            f"up={breakout_up} down={breakout_down}"
        )

        if trend == "UP" and breakout_up:
            return "LEVERAGE"   # 롱 → 122630
        if trend == "DOWN" and breakout_down:
            return "INVERSE"    # 숏 → 252670
        return None

    def _bollinger(self, closes: list, period: int, mult: float):
        if len(closes) < period:
            return None, None, None
        window = closes[-period:]
        m = sum(window) / period
        sd = (sum((c - m) ** 2 for c in window) / period) ** 0.5
        return m, m + mult * sd, m - mult * sd

    # ─────────────────────────────────────────
    # 진입
    # ─────────────────────────────────────────
    def _enter(self, direction: str, context):
        harness  = context["harness"]
        etf_code = self.LEVERAGE_CODE if direction == "LEVERAGE" else self.INVERSE_CODE
        etf_name = "KODEX레버리지" if direction == "LEVERAGE" else "KODEX200선물인버스2X"

        price = self._last_etf_price.get(etf_code, 0)
        if price <= 0:
            try:
                info  = harness.kiwoom.get_stock_info(etf_code)
                price = info.get("current_price", 0)
            except Exception as e:
                logger.warning(f"[LeverageIndexAgent] {etf_code} 현재가 조회 실패: {e}")
        if price <= 0:
            logger.warning(f"[LeverageIndexAgent] {etf_code} 현재가 미확인 → 진입 보류")
            return

        amount = getattr(config, "LEVERAGE_INDEX_AMOUNT", 2_000_000)
        qty    = max(1, int(amount) // int(price))

        ret, ord_no = harness.kiwoom.send_order(
            order_name=f"LEVIDX_{direction}_{etf_code}",
            screen_no=self._next_screen(),
            code=etf_code,
            qty=qty,
            price=0,
            order_type=1,
            hoga_type="03",
        )
        if ret != 0:
            logger.error(f"[LeverageIndexAgent] 매수 주문 실패 ({etf_code}) ret={ret}")
            return

        sl_rate = getattr(config, "LEVERAGE_INDEX_SL_RATE", 0.025)
        sl = round(price * (1 - sl_rate))

        self._position = {
            "code":        etf_code,
            "name":        etf_name,
            "direction":   direction,
            "qty":         qty,
            "entry_price": price,
            "stop_loss":   sl,
            "peak_ret":    0.0,
            "entered_at":  datetime.now().isoformat(),
        }

        logger.info(
            f"[LeverageIndexAgent] {direction} 진입: {etf_name}({etf_code}) "
            f"{qty}주 @ {price:,}원 | SL {sl:,} (-{sl_rate:.1%}) | ord={ord_no}"
        )
        self.trade_logger.log_trade(
            code=etf_code, name=etf_name, side="BUY",
            qty=qty, price=price, reason=f"지수레버리지전략_{direction}"
        )
        notifier = harness.get_context().get("notifier")
        if notifier:
            label = "지수레버리지(롱)" if direction == "LEVERAGE" else "지수인버스2X(숏)"
            notifier.send(
                f"🟢 <b>지수 레버리지 전략 진입</b> [{etf_name}({etf_code})]\n"
                f"방향: {label} | {qty}주 @ {price:,}원\n"
                f"손절: {sl:,}원 (-{sl_rate:.1%}) | 트레일링 활성화 +{sl_rate:.1%}"
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
            order_name=f"LEVIDX_EXIT_{etf_code}",
            screen_no=self._next_screen(),
            code=etf_code,
            qty=qty,
            price=0,
            order_type=2,
            hoga_type="03",
        )
        if ret != 0:
            logger.error(f"[LeverageIndexAgent] 매도 주문 실패 ({etf_code}) ret={ret}")
            return  # 주문 실패 시 포지션 유지

        exit_price = self._last_etf_price.get(etf_code, 0) or ref_price
        pnl        = (exit_price - pos["entry_price"]) * qty
        pnl_rate   = (exit_price - pos["entry_price"]) / pos["entry_price"] if pos["entry_price"] else 0.0
        self._position = None
        self._last_exit_time = datetime.now()

        logger.info(
            f"[LeverageIndexAgent] 청산({reason}): {etf_name}({etf_code}) "
            f"{qty}주 @ {exit_price:,}원 | PnL {pnl:+,.0f}원 ({pnl_rate:+.2%}) | ord={ord_no}"
        )
        self.trade_logger.log_trade(
            code=etf_code, name=etf_name, side="SELL",
            qty=qty, price=exit_price,
            pnl=pnl, pnl_rate=pnl_rate, reason=f"지수_{reason}"
        )
        notifier = harness.get_context().get("notifier")
        if notifier:
            emoji = "🔴" if pnl_rate < 0 else "🟡"
            notifier.send(
                f"{emoji} <b>지수 레버리지 전략 청산</b> [{etf_name}({etf_code})]\n"
                f"사유: {reason} | {qty}주 @ {exit_price:,}원\n"
                f"손익: {pnl_rate:+.2%}"
            )

    # ─────────────────────────────────────────
    # SL/트레일링 (ETF 틱) — 단일단계, 백테스트 현실모델과 동일
    #   price <= stop_loss                         : 손절
    #   peak_ret >= SL_RATE 이고 (peak-cur) >= GAP : 트레일링 청산
    # 두 방향 모두 '매수'라 보유 ETF 가격 상승이 favorable → 방향 무관.
    # ─────────────────────────────────────────
    def _check_sl_tp(self, code: str, price: int, context):
        if not self._position or self._position["code"] != code:
            return
        pos = self._position
        if price <= pos["stop_loss"]:
            self._exit("손절", price, context)
            return

        entry = pos["entry_price"]
        cur_ret = (price - entry) / entry if entry else 0.0
        pos["peak_ret"] = max(pos.get("peak_ret", 0.0), cur_ret)
        peak = pos["peak_ret"]

        sl_rate   = getattr(config, "LEVERAGE_INDEX_SL_RATE", 0.025)
        trail_gap = getattr(config, "LEVERAGE_INDEX_TRAIL_GAP", 0.015)
        if trail_gap and peak >= sl_rate and (peak - cur_ret) >= trail_gap:
            self._exit("트레일링청산", price, context)

    # ─────────────────────────────────────────
    # EOD 강제청산
    # ─────────────────────────────────────────
    def _check_force_exit(self, context):
        if not self._position or self._force_exited_today:
            return
        force_time = getattr(config, "LEVERAGE_INDEX_FORCE_EXIT", self.FORCE_EXIT_TIME)
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
                f"[LeverageIndexAgent] 일일 초기화 시 잔여 포지션 발견: "
                f"{self._position['code']} → 내부 상태 초기화"
            )
            self._position = None

    def _next_screen(self) -> str:
        self._screen_counter = (self._screen_counter + 1) % 10
        return str(self._SCREEN_BASE + self._screen_counter)
