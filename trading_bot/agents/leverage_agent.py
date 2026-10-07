import logging
import time
from datetime import datetime
from typing import Optional, List

import config
from agents.base_agent import BaseAgent
from logger import TradeLogger

logger = logging.getLogger(__name__)


class LeverageInverseAgent(BaseAgent):
    """
    삼성전자 방향성 모멘텀 기반 레버리지/인버스 ETF 당일 매매 전략.

    신호원: 삼성전자(005930) 분봉 (주기: config.LEVERAGE_CANDLE_INTERVAL)

    LEVERAGE_STRATEGY="BOLLINGER" (현재 기본, 2026-09-22 지표탐색 루프에서 발굴):
      - 방향: 일봉 단기MA/장기MA 정배열 (LEVERAGE_TREND_FAST_MA/SLOW_MA)
      - 진입: 그 방향으로 3분봉 볼린저밴드(LEVERAGE_BB_PERIOD/MULT) 상단/하단 돌파 시
      - 청산: 봉 색깔/신호 무관 — 손절(SL_RATE)·트레일링(TRAIL_GAP, 틱 기준)·EOD 강제청산만
    LEVERAGE_STRATEGY="TREND_MACD" (이전 기본):
      - 방향: 일봉 단기MA/장기MA 정배열
      - 진입: 그 방향으로 3분봉 MACD 골드/데드 크로스 발생 시
      - 청산: BOLLINGER와 동일 (손절/트레일링/EOD만)
    LEVERAGE_STRATEGY="MACD_HA" (레거시): HA 봉 색깔+MACD 레벨 AND 진입, HA 색 전환 청산
    LEVERAGE_STRATEGY="BASIC" (레거시): 연속봉+5MA+거래량 진입, MA 역전 청산
    """

    LEVERAGE_CODE    = "0193W0"
    INVERSE_CODE     = "0193L0"
    SAMSUNG_CODE     = "005930"

    SIGNAL_MA_PERIOD = 5
    SIGNAL_VOL_RATIO = 1.5
    SIGNAL_CONSEC    = 2

    SL_RATE          = 0.015
    TP_RATE          = 0.030

    REGULAR_CLOSE_HM = "1530"   # KRX 정규장 마감 — 이후는 애프터마켓

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
        self._last_exit_time: Optional[datetime] = None  # 재진입 쿨다운용
        self._daily_trend_cache: dict  = {}  # {"date": "YYYY-MM-DD", "trend": "UP"/"DOWN"/None}

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
        entry_start = getattr(config, "LEVERAGE_ENTRY_START", self.ENTRY_START)
        entry_end   = getattr(config, "LEVERAGE_ENTRY_END",   self.ENTRY_END)

        # 시뮬 모드에서는 Samsung TICK이 없으므로 캔들 도착 시에도 EOD 체크
        self._check_force_exit(context)
        if self._force_exited_today:
            return

        # 포지션 보유 중: 전략별 청산 체크
        if self._position:
            direction = self._position["direction"]
            strategy  = getattr(config, "LEVERAGE_STRATEGY", "BASIC")
            if strategy in ("TREND_MACD", "BOLLINGER"):
                # 봉 색깔/신호 기반 청산 없음 — 손절/트레일링(틱)·EOD(시각)만으로 청산
                return
            elif strategy == "MACD_HA":
                closed = [c for c in candles if c.is_closed]
                if closed:
                    ha      = self._calc_heikin_ashi(closed)
                    last_ha = ha[-1]
                    body    = abs(last_ha["close"] - last_ha["open"])
                    rng     = last_ha["high"] - last_ha["low"]
                    doji_th = getattr(config, "LEVERAGE_DOJI_TH", 0.2)
                    is_doji = rng > 0 and body / rng < doji_th
                    if not is_doji:
                        ha_bear = last_ha["close"] < last_ha["open"]
                        ha_bull = last_ha["close"] > last_ha["open"]
                        flip_to = None
                        if direction == "LEVERAGE" and ha_bear:
                            self._exit("HA음봉전환(방향역전)", candle.close, context)
                            flip_to = "INVERSE"
                        elif direction == "INVERSE" and ha_bull:
                            self._exit("HA양봉전환(방향역전)", candle.close, context)
                            flip_to = "LEVERAGE"
                        # 색깔 전환 청산 성공 시 반대 방향 즉시 진입 (플립)
                        # LEVERAGE_FLIP_ENABLED=false면 비활성 — 일반 진입 경로(1봉 쿨다운 후)만 사용
                        if flip_to and not self._position and not self._force_exited_today:
                            if getattr(config, "LEVERAGE_FLIP_ENABLED", False):
                                if getattr(config, "LEVERAGE_ENABLED", True):
                                    if entry_start <= now_hm <= entry_end:
                                        if self._calc_signal(candles) == flip_to:
                                            self._enter(flip_to, context)
            else:
                ma = self._calc_ma(candles, self.SIGNAL_MA_PERIOD)
                if ma:
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
        if not (entry_start <= now_hm <= entry_end):
            return
        # 청산 직후 1캔들 쿨다운 — 연속 전환 매매 방지 (봉 주기 연동)
        cooldown_sec = getattr(config, "LEVERAGE_CANDLE_INTERVAL", 3) * 60
        if self._last_exit_time and (datetime.now() - self._last_exit_time).total_seconds() < cooldown_sec:
            return
        if len(candles) < self.SIGNAL_MA_PERIOD + self.SIGNAL_CONSEC:
            return

        direction = self._calc_signal(candles, harness)
        if direction:
            self._enter(direction, context)

    # ─────────────────────────────────────────
    # 신호 계산 (전략 디스패처)
    # ─────────────────────────────────────────

    def _calc_signal(self, candles: list, harness=None) -> Optional[str]:
        strategy = getattr(config, "LEVERAGE_STRATEGY", "BASIC")
        if strategy == "TREND_MACD":
            return self._calc_signal_trend_macd(candles, harness)
        if strategy == "BOLLINGER":
            return self._calc_signal_bollinger(candles, harness)
        if strategy == "MACD_HA":
            return self._calc_signal_macd_ha(candles)
        return self._calc_signal_basic(candles)

    # ── 기본 전략: 연속봉 + 5MA + 거래량 ───────────────────────────

    def _calc_signal_basic(self, candles: list) -> Optional[str]:
        closed = [c for c in candles if c.is_closed]
        ma = self._calc_ma(closed, self.SIGNAL_MA_PERIOD)
        if not ma or len(closed) < self.SIGNAL_CONSEC:
            return None

        last    = closed[-1]
        avg_vol = self._calc_avg_volume(closed, 10)
        vol_ok  = avg_vol > 0 and last.volume >= avg_vol * self.SIGNAL_VOL_RATIO

        recent   = closed[-self.SIGNAL_CONSEC:]
        all_bull = all(c.is_bullish for c in recent)
        all_bear = all(c.close < c.open for c in recent)

        if last.close > ma and all_bull and vol_ok:
            return "LEVERAGE"
        if last.close < ma and all_bear and vol_ok:
            return "INVERSE"
        return None

    # ── 일봉추세 + MACD 크로스 전략 ──────────────────────────────────
    # 방향: 일봉 5MA/20MA 정배열(상승=골드/하락=데드)이 허용 방향을 결정
    # 진입: 3분봉 MACD가 그 방향으로 골드/데드 크로스 하는 순간만
    # 청산: 봉 색깔/신호 무관, 손절·트레일링(틱)·EOD(시각)만 (_on_samsung_candle 참조)

    def _get_daily_trend(self, harness) -> Optional[str]:
        """삼성전자 일봉 단기MA vs 장기MA 정배열로 당일 허용 매매방향 판단. 하루 1회 조회 후 캐시."""
        fast_ma = getattr(config, "LEVERAGE_TREND_FAST_MA", 5)
        slow_ma = getattr(config, "LEVERAGE_TREND_SLOW_MA", 20)

        today = datetime.now().strftime("%Y-%m-%d")
        if self._daily_trend_cache.get("date") == today:
            return self._daily_trend_cache.get("trend")

        # ka10081은 max_retries=0이라 429 한 번에 None을 반환한다. 추세 판단은 하루 1회라
        # 지연에 민감하지 않으므로 몇 차례 재시도한다(조용한 None → 판단불가 오표시 방지).
        df = None
        for attempt in range(3):
            try:
                df = harness.kiwoom.get_daily_data(self.SAMSUNG_CODE, count=max(40, slow_ma + 15))
            except Exception as e:
                logger.warning(f"[LeverageAgent] 일봉 조회 예외(시도 {attempt+1}/3): {e}")
                df = None
            if df is not None and not df.empty:
                break
            time.sleep(1.5)

        if df is None or df.empty:
            logger.warning("[LeverageAgent] 일봉 조회 3회 실패 → 추세 판단불가 (이전 캐시 유지)")
            return self._daily_trend_cache.get("trend")  # 실패 시 이전 캐시 유지

        rows = df.to_dict("records")
        today_str = datetime.now().strftime("%Y%m%d")
        if rows and str(rows[-1].get("date", "")) == today_str:
            rows = rows[:-1]  # 오늘 형성 중인 봉 제외 — 전일까지 확정봉만 사용 (장중 노이즈로 추세 흔들림 방지)

        if len(rows) < slow_ma:
            logger.warning(f"[LeverageAgent] 일봉 데이터 부족({len(rows)}행/{slow_ma}, df원본 {len(df)}행) → 추세 판단 불가")
            return None

        closes = [r["close"] for r in rows]
        maf = sum(closes[-fast_ma:]) / fast_ma
        mas = sum(closes[-slow_ma:]) / slow_ma
        trend = "UP" if maf > mas else ("DOWN" if maf < mas else None)

        self._daily_trend_cache = {"date": today, "trend": trend}
        logger.info(f"[LeverageAgent] 일봉추세 갱신: MA{fast_ma}={maf:,.0f} MA{slow_ma}={mas:,.0f} → {trend}")
        return trend

    def _calc_signal_trend_macd(self, candles: list, harness) -> Optional[str]:
        min_bars = getattr(config, "LEVERAGE_MACD_SLOW", 26) + getattr(config, "LEVERAGE_MACD_SIGNAL", 9)
        closed = [c for c in candles if c.is_closed]
        closes = self._compressed_closes(closed)
        if len(closes) < min_bars or harness is None:
            return None

        trend = self._get_daily_trend(harness)
        if trend is None:
            return None

        fast = getattr(config, "LEVERAGE_MACD_FAST",   12)
        slow = getattr(config, "LEVERAGE_MACD_SLOW",   26)
        sig  = getattr(config, "LEVERAGE_MACD_SIGNAL",  9)

        macd_now, sig_now = self._calc_macd_values(closes, fast, slow, sig)
        macd_prev, sig_prev = self._calc_macd_values(closes[:-1], fast, slow, sig)
        if macd_now is None or macd_prev is None:
            return None

        golden = macd_prev <= sig_prev and macd_now > sig_now
        dead   = macd_prev >= sig_prev and macd_now < sig_now
        logger.debug(
            f"[LeverageAgent] 일봉추세={trend} MACD {macd_prev:+.4f}->{macd_now:+.4f} "
            f"Signal {sig_prev:+.4f}->{sig_now:+.4f} golden={golden} dead={dead}"
        )

        if trend == "UP" and golden:
            return "LEVERAGE"
        if trend == "DOWN" and dead:
            return "INVERSE"
        return None

    # ── 볼린저밴드 돌파 전략 (일봉추세 + 3분봉 상/하단 돌파) ──────────

    def _calc_signal_bollinger(self, candles: list, harness) -> Optional[str]:
        period = getattr(config, "LEVERAGE_BB_PERIOD", 20)
        mult   = getattr(config, "LEVERAGE_BB_MULT", 2.0)
        closed = [c for c in candles if c.is_closed]
        closes = self._compressed_closes(closed)
        if len(closes) < period + 1 or harness is None:
            return None

        trend = self._get_daily_trend(harness)
        if trend is None:
            return None

        _, upper_now, lower_now = self._calc_bollinger_values(closes, period, mult)
        _, upper_prev, lower_prev = self._calc_bollinger_values(closes[:-1], period, mult)
        if upper_now is None or upper_prev is None:
            return None

        close_now, close_prev = closes[-1], closes[-2]
        breakout_up   = close_prev <= upper_prev and close_now > upper_now
        breakout_down = close_prev >= lower_prev and close_now < lower_now
        logger.debug(
            f"[LeverageAgent] 일봉추세={trend} BB상단 {upper_prev:.0f}->{upper_now:.0f} "
            f"BB하단 {lower_prev:.0f}->{lower_now:.0f} close {close_prev}->{close_now} "
            f"up={breakout_up} down={breakout_down}"
        )

        if trend == "UP" and breakout_up:
            return "LEVERAGE"
        if trend == "DOWN" and breakout_down:
            return "INVERSE"
        return None

    def _compressed_closes(self, closed: list) -> list:
        """15:30 초과 봉(KRX 애프터마켓 16~20시)을 하루 1봉으로 압축한 종가 리스트.

        애프터마켓은 삼성전자 거래량의 4%인데 3분봉으로는 하루 208봉 중 80봉(38%)이라,
        압축하지 않으면 장 초반 지표 창이 체결 불가능한(ETF는 애프터마켓 거래 대상 제외)
        얇은 호가에 지배된다. 2026-09-14 애프터마켓 개장 이전 정의(하루 1봉)와도 일치한다.
        """
        closes, pend = [], None
        for c in closed:
            if c.datetime.strftime("%H%M") > self.REGULAR_CLOSE_HM:
                pend = c.close
            else:
                if pend is not None:
                    closes.append(pend)
                    pend = None
                closes.append(c.close)
        if pend is not None:
            closes.append(pend)
        return closes

    def _calc_bollinger_values(self, closes: list, period: int, mult: float):
        """(mid, upper, lower) — closes 마지막 시점 기준. 데이터 부족 시 (None, None, None)"""
        if len(closes) < period:
            return None, None, None
        window = closes[-period:]
        m = sum(window) / period
        var = sum((c - m) ** 2 for c in window) / period
        sd = var ** 0.5
        return m, m + mult * sd, m - mult * sd

    # ── MACD + 하이킨아시 전략 ──────────────────────────────────────

    def _calc_signal_macd_ha(self, candles: list) -> Optional[str]:
        closed = [c for c in candles if c.is_closed]
        if len(closed) < self.SIGNAL_MA_PERIOD:
            return None

        ha      = self._calc_heikin_ashi(closed)
        last_ha = ha[-1]

        # 도지 무시 (봉 범위 대비 몸통 비율 미만 — config LEVERAGE_DOJI_TH)
        body = abs(last_ha["close"] - last_ha["open"])
        rng  = last_ha["high"] - last_ha["low"]
        if rng > 0 and body / rng < getattr(config, "LEVERAGE_DOJI_TH", 0.2):
            return None

        ha_bull = last_ha["close"] > last_ha["open"]
        ha_bear = last_ha["close"] < last_ha["open"]

        # MACD 진입 조건 (HA 색깔과 함께 AND 조건)
        fast = getattr(config, "LEVERAGE_MACD_FAST",   12)
        slow = getattr(config, "LEVERAGE_MACD_SLOW",   26)
        sig  = getattr(config, "LEVERAGE_MACD_SIGNAL",  9)
        macd_bull = False
        macd_bear = False
        if len(closed) >= slow + sig - 1:
            macd_val, signal_val = self._calc_macd_values(
                [c.close for c in closed], fast, slow, sig
            )
            if macd_val is not None:
                macd_bull = macd_val > signal_val
                macd_bear = macd_val < signal_val
                trend = "상승" if macd_bull else "하락"
                logger.debug(
                    f"[LeverageAgent] MACD {macd_val:+.4f} / Signal {signal_val:+.4f} ({trend})"
                )

        # 추세필터: 종가가 SMA(LEVERAGE_MA_FILTER) 기준 신호 방향과 같은 쪽일 때만 진입 허용
        # (역추세 휩쏘 진입 차단 — 0/미설정이면 비활성)
        ma_period = getattr(config, "LEVERAGE_MA_FILTER", 0)
        ma_ok_long = ma_ok_short = True
        if ma_period and len(closed) >= ma_period:
            sma = sum(c.close for c in closed[-ma_period:]) / ma_period
            last_close = closed[-1].close
            ma_ok_long  = last_close > sma
            ma_ok_short = last_close < sma

        # HA 색깔 AND MACD 방향 AND 추세필터 일치 시 진입
        if ha_bull and macd_bull and ma_ok_long:
            return "LEVERAGE"
        if ha_bear and macd_bear and ma_ok_short:
            return "INVERSE"
        return None

    # ── HA / MACD 계산 헬퍼 ─────────────────────────────────────────

    def _calc_heikin_ashi(self, candles: list) -> list:
        """확정봉 리스트 → HA 딕셔너리 리스트 {open, high, low, close}"""
        ha = []
        for i, c in enumerate(candles):
            ha_close = (c.open + c.high + c.low + c.close) / 4
            ha_open  = (c.open + c.close) / 2 if i == 0 else (ha[i-1]["open"] + ha[i-1]["close"]) / 2
            ha.append({
                "open":  ha_open,
                "high":  max(c.high, ha_open, ha_close),
                "low":   min(c.low,  ha_open, ha_close),
                "close": ha_close,
            })
        return ha

    def _calc_ema(self, values: list, period: int) -> list:
        """지수이동평균(EMA). 데이터 부족 시 []"""
        if len(values) < period:
            return []
        k   = 2 / (period + 1)
        ema = [sum(values[:period]) / period]
        for v in values[period:]:
            ema.append(v * k + ema[-1] * (1 - k))
        return ema

    def _calc_macd_values(self, closes: list, fast: int, slow: int, signal_period: int):
        """(macd_최신값, signal_최신값) 또는 데이터 부족 시 (None, None)"""
        ema_fast = self._calc_ema(closes, fast)
        ema_slow = self._calc_ema(closes, slow)
        if not ema_fast or not ema_slow:
            return None, None
        diff          = len(ema_fast) - len(ema_slow)
        macd_series   = [f - s for f, s in zip(ema_fast[diff:], ema_slow)]
        signal_series = self._calc_ema(macd_series, signal_period)
        if not signal_series:
            return None, None
        return macd_series[-1], signal_series[-1]

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

        sl_rate = getattr(config, "LEVERAGE_SL_RATE", self.SL_RATE)
        tp_rate = getattr(config, "LEVERAGE_TP_RATE", self.TP_RATE)
        sl = round(price * (1 - sl_rate))
        tp = round(price * (1 + tp_rate))

        self._position = {
            "code":        etf_code,
            "name":        etf_name,
            "direction":   direction,
            "qty":         qty,
            "entry_price": price,
            "stop_loss":   sl,
            "take_profit": tp,
            "peak_ret":    0.0,   # 트레일링 스탑용 최고 favorable 수익률
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
                f"손절: {sl:,}원 (-{sl_rate:.1%}) | "
                f"익절: {tp:,}원 (+{tp_rate:.1%})"
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
            return  # 주문 실패 시 포지션 유지

        exit_price = self._last_etf_price.get(etf_code, 0) or ref_price
        pnl        = (exit_price - pos["entry_price"]) * qty
        pnl_rate   = (exit_price - pos["entry_price"]) / pos["entry_price"] if pos["entry_price"] else 0.0
        self._position     = None
        self._last_exit_time = datetime.now()  # 재진입 쿨다운 시작

        logger.info(
            f"[LeverageAgent] 청산({reason}): {etf_name}({etf_code}) "
            f"{qty}주 @ {exit_price:,}원 | PnL {pnl:+,.0f}원 ({pnl_rate:+.2%}) | ord={ord_no}"
        )
        self.trade_logger.log_trade(
            code=etf_code, name=etf_name, side="SELL",
            qty=qty, price=exit_price,
            pnl=pnl, pnl_rate=pnl_rate, reason=reason
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
        """2단계(계단식) 트레일링 스탑 (2026-10-01):
          peak_ret < STAGE1_RET              : 트레일링 비활성 — stop_loss(진입가 기준)만 유효
          STAGE1_RET <= peak_ret < STAGE2_RET : 수익 STAGE1_LOCK 마지노선 (밑으로 밀리면 청산)
          peak_ret >= STAGE2_RET              : 피크 대비 STAGE2_GAP 트레일링 (더 좁게 추적)
        peak_ret는 한 번 올라가면 내려가지 않으므로 단계도 한쪽으로만 진행(래칫).
        """
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

        stage1_ret  = getattr(config, "LEVERAGE_TRAIL_STAGE1_RET",  0.025)
        stage1_lock = getattr(config, "LEVERAGE_TRAIL_STAGE1_LOCK", 0.025)
        stage2_ret  = getattr(config, "LEVERAGE_TRAIL_STAGE2_RET",  0.035)
        stage2_gap  = getattr(config, "LEVERAGE_TRAIL_STAGE2_GAP",  0.01)

        if peak >= stage2_ret:
            if (peak - cur_ret) >= stage2_gap:
                self._exit("트레일링청산(2단계)", price, context)
        elif peak >= stage1_ret:
            if cur_ret <= stage1_lock:
                self._exit("수익보존청산(1단계)", price, context)

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
