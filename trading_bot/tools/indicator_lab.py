# -*- coding: utf-8 -*-
"""
레버리지 전략용 지표 연구 라이브러리.

라이브 코드(agents/leverage_agent.py)와 동일한 원칙을 지킨다:
  - 지표는 날짜 경계에서 리셋하지 않고 전체 누적 종가로 연속 계산
    (VWAP은 정의상 세션마다 리셋되는 게 정상이라 예외)
  - 일봉 추세필터(단기MA vs 장기MA)로 롱/숏 방향을 먼저 결정하고,
    그 방향과 일치하는 신호만 진입으로 채택
  - 청산은 손절 / 트레일링(peak 확보 후 되밀림) / EOD(15:20) 고정 —
    entry 시그널만 지표별로 교체해서 비교 (공정 비교를 위해 exit은 고정)

이 모듈은 순수 계산 로직만 담당한다 (SSH/텔레그램/상태파일 IO는 run_indicator_search.py).
"""
import csv
from collections import defaultdict
from datetime import datetime

FEE = 0.0003  # 왕복 수수료
CAPITAL = 2_000_000  # config.LEVERAGE_AMOUNT와 동일 — 손익금액(원) 환산 기준


# ─────────────────────────────────────────
# 데이터 로딩
# ─────────────────────────────────────────

def load_master_data(path_3min: str, path_daily: str):
    """3분봉 CSV + 일봉 CSV 로드.
    Returns: (all_bars: list[dict] 시간순 정렬, daily: list[dict] 날짜순 정렬)
    all_bars 항목: {'d': 'YYYYMMDD', 't': 'HHMM', 'open','high','low','close','volume'}
    """
    bars = []
    with open(path_3min, encoding='utf-8') as f:
        for r in csv.DictReader(f):
            dt_str = r['datetime']
            try:
                dt = datetime.strptime(dt_str[:14], '%Y%m%d%H%M%S')
            except ValueError:
                continue
            bars.append({
                'dt': dt, 'd': dt.strftime('%Y%m%d'), 't': dt.strftime('%H%M'),
                'open': int(r['open']), 'high': int(r['high']),
                'low': int(r['low']), 'close': int(r['close']),
                'volume': int(r.get('volume', 0) or 0),
            })
    bars.sort(key=lambda x: x['dt'])

    # 하루 최소 50봉 미만인 날(휴장일 파편 등)은 제외
    by_day = defaultdict(list)
    for b in bars:
        by_day[b['d']].append(b)
    full_days = {d for d, v in by_day.items() if len(v) >= 50}
    all_bars = compress_after_hours([b for b in bars if b['d'] in full_days])

    daily = []
    with open(path_daily, encoding='utf-8') as f:
        for r in csv.DictReader(f):
            daily.append({'date': r['date'], 'close': int(r['close'])})
    daily.sort(key=lambda x: x['date'])

    return all_bars, daily


REGULAR_CLOSE_HM = '1530'   # KRX 정규장 마감 — 이후는 장후 시간외/애프터마켓


def compress_after_hours(bars: list) -> list:
    """15:30 초과 봉(KRX 애프터마켓 16~20시)을 하루 1봉으로 압축.

    가격 정보는 남기되 봉 개수 가중치만 정상화한다. 애프터마켓은 삼성전자 거래량의
    4%인데 3분봉으로는 하루 208봉 중 80봉(38%)을 차지해, 압축하지 않으면 장 초반
    볼린저 창이 체결 불가능한(ETF는 애프터마켓 거래 대상 제외) 얇은 호가에 지배된다.
    2026-09-14 애프터마켓 개장 이전에는 이 구간이 하루 1봉(종가단일가)이었으므로
    압축이 곧 그 이전 이력과의 정의 연속성을 회복시킨다.
    """
    out, pend = [], []

    def flush():
        if not pend:
            return
        out.append({
            'dt': pend[0]['dt'], 'd': pend[0]['d'], 't': pend[0]['t'],
            'open': pend[0]['open'],
            'high': max(b['high'] for b in pend),
            'low': min(b['low'] for b in pend),
            'close': pend[-1]['close'],
            'volume': sum(b['volume'] for b in pend),
        })
        del pend[:]

    for b in bars:
        if b['t'] > REGULAR_CLOSE_HM:
            pend.append(b)
        else:
            flush()
            out.append(b)
    flush()
    return out


# ─────────────────────────────────────────
# 일봉 추세필터
# ─────────────────────────────────────────

def make_trend_lookup(daily: list, fast_ma: int, slow_ma: int):
    """날짜(YYYYMMDD) -> 'UP'/'DOWN'/None 조회 함수를 반환 (결과 캐시)."""
    cache = {}
    closes_by_date = [(d['date'], d['close']) for d in daily]

    def trend_for_day(target_date_str):
        if target_date_str in cache:
            return cache[target_date_str]
        closes = [c for dt, c in closes_by_date if dt < target_date_str]
        if len(closes) < slow_ma:
            cache[target_date_str] = None
            return None
        maf = sum(closes[-fast_ma:]) / fast_ma
        mas = sum(closes[-slow_ma:]) / slow_ma
        trend = 'UP' if maf > mas else ('DOWN' if maf < mas else None)
        cache[target_date_str] = trend
        return trend

    return trend_for_day


# ─────────────────────────────────────────
# 지표 계산 (전 구간 1회, 날짜 경계 리셋 없음 — VWAP만 예외)
# ─────────────────────────────────────────

def ema_series(vals, period):
    if len(vals) < period:
        return []
    k = 2 / (period + 1)
    e = [sum(vals[:period]) / period]
    for v in vals[period:]:
        e.append(v * k + e[-1] * (1 - k))
    return e


def macd_full_series(closes, fast, slow, sig):
    ef, es = ema_series(closes, fast), ema_series(closes, slow)
    n = len(closes)
    if not ef or not es:
        return [None] * n, [None] * n
    d = len(ef) - len(es)
    m = [f - s for f, s in zip(ef[d:], es)]
    ss = ema_series(m, sig)

    macd_full = [None] * n
    off_m = slow - 1
    for idx, val in enumerate(m):
        if off_m + idx < n:
            macd_full[off_m + idx] = val

    sig_full = [None] * n
    off_s = off_m + (sig - 1)
    for idx, val in enumerate(ss):
        if off_s + idx < n:
            sig_full[off_s + idx] = val

    return macd_full, sig_full


def rsi_full_series(closes, period):
    n = len(closes)
    rsi = [None] * n
    if n < period + 1:
        return rsi
    gains = losses = 0.0
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        gains += max(diff, 0)
        losses += max(-diff, 0)
    avg_gain = gains / period
    avg_loss = losses / period
    rsi[period] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    for i in range(period + 1, n):
        diff = closes[i] - closes[i - 1]
        gain, loss = max(diff, 0), max(-diff, 0)
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        rsi[i] = 100.0 if avg_loss == 0 else 100 - 100 / (1 + avg_gain / avg_loss)
    return rsi


def bollinger_full_series(closes, period, mult):
    n = len(closes)
    mid, upper, lower = [None] * n, [None] * n, [None] * n
    window = []
    s = s2 = 0.0
    for i in range(n):
        window.append(closes[i])
        s += closes[i]
        s2 += closes[i] ** 2
        if len(window) > period:
            old = window.pop(0)
            s -= old
            s2 -= old ** 2
        if len(window) == period:
            m = s / period
            var = max(0.0, s2 / period - m ** 2)
            sd = var ** 0.5
            mid[i], upper[i], lower[i] = m, m + mult * sd, m - mult * sd
    return mid, upper, lower


def stochastic_full_series(highs, lows, closes, k_period, d_period):
    n = len(closes)
    k = [None] * n
    for i in range(k_period - 1, n):
        window_h = highs[i - k_period + 1:i + 1]
        window_l = lows[i - k_period + 1:i + 1]
        hh, ll = max(window_h), min(window_l)
        k[i] = 50.0 if hh == ll else (closes[i] - ll) / (hh - ll) * 100
    d = [None] * n
    window, s = [], 0.0
    for i in range(n):
        if k[i] is None:
            continue
        window.append(k[i])
        s += k[i]
        if len(window) > d_period:
            s -= window.pop(0)
        if len(window) == d_period:
            d[i] = s / d_period
    return k, d


def vwap_session_series(all_bars):
    """세션(일자)별로 리셋되는 거래량가중평균가. VWAP은 정의상 일중 지표라 예외적으로 날짜 리셋."""
    n = len(all_bars)
    vwap = [None] * n
    cum_pv = cum_v = 0.0
    prev_day = None
    for i, b in enumerate(all_bars):
        if b['d'] != prev_day:
            cum_pv = cum_v = 0.0
            prev_day = b['d']
        typical = (b['high'] + b['low'] + b['close']) / 3
        vol = b['volume'] or 1
        cum_pv += typical * vol
        cum_v += vol
        vwap[i] = cum_pv / cum_v if cum_v > 0 else b['close']
    return vwap


def volume_sma_series(volumes, period):
    n = len(volumes)
    out = [None] * n
    window, s = [], 0.0
    for i in range(n):
        window.append(volumes[i])
        s += volumes[i]
        if len(window) > period:
            s -= window.pop(0)
        if len(window) == period:
            out[i] = s / period
    return out


# ─────────────────────────────────────────
# 공용 백테스트 엔진 (entry signal만 교체 가능, exit은 고정)
# ─────────────────────────────────────────

def simulate(all_bars, trend_lookup, signal_fn,
             sl=0.025, trail_gap=0.015, entry_start='09:00', force_exit='1520',
             cooldown_bars=1, fee=FEE,
             time_stop_bars=None, breakeven_trigger=None, trail_activate=None):
    """signal_fn(i, trend) -> 'L' / 'S' / None"""
    trades = []
    pos = None
    last_exit_idx = -999
    es = entry_start.replace(':', '')

    for i, bar in enumerate(all_bars):
        d, t = bar['d'], bar['t']

        if pos:
            sign = 1 if pos['dir'] == 'L' else -1
            if i > pos['ei']:
                lo = sign * (bar['low'] - pos['p']) / pos['p'] * 2
                hi = sign * (bar['high'] - pos['p']) / pos['p'] * 2
                pos['peak'] = max(pos['peak'], hi)
                cur_ret = sign * (bar['close'] - pos['p']) / pos['p'] * 2
                eff_sl = -sl
                if breakeven_trigger is not None and pos['peak'] >= breakeven_trigger:
                    eff_sl = 0.0
                act = trail_activate if trail_activate is not None else sl
                if lo <= eff_sl:
                    trades.append({'d': d, 'ret': eff_sl - fee, 'r': 'SL', 'dir': pos['dir']})
                    pos = None
                    last_exit_idx = i
                    continue
                if trail_gap and pos['peak'] >= act and (pos['peak'] - cur_ret) >= trail_gap:
                    trades.append({'d': d, 'ret': cur_ret - fee, 'r': 'TRAIL', 'dir': pos['dir']})
                    pos = None
                    last_exit_idx = i
                    continue
                if time_stop_bars is not None and (i - pos['ei']) >= time_stop_bars:
                    trades.append({'d': d, 'ret': cur_ret - fee, 'r': 'TIME', 'dir': pos['dir']})
                    pos = None
                    last_exit_idx = i
                    continue
            if pos and t >= force_exit:
                ret = sign * (bar['close'] - pos['p']) / pos['p'] * 2 - fee
                trades.append({'d': d, 'ret': ret, 'r': 'EOD', 'dir': pos['dir']})
                pos = None
                last_exit_idx = i
                continue

        if not pos and es <= t <= '1500' and (i - last_exit_idx) > cooldown_bars:
            trend = trend_lookup(d)
            if trend:
                sig_dir = signal_fn(i, trend)
                if sig_dir:
                    pos = {'dir': sig_dir, 'p': bar['close'], 'ei': i, 'peak': 0.0}

    if pos:
        sign = 1 if pos['dir'] == 'L' else -1
        ret = sign * (all_bars[-1]['close'] - pos['p']) / pos['p'] * 2 - fee
        trades.append({'d': all_bars[-1]['d'], 'ret': ret, 'r': 'EOD', 'dir': pos['dir']})

    n = len(trades)
    if n == 0:
        return {'n': 0, 'win_rate': 0.0, 'net_ret': 0.0, 'net_pnl_krw': 0.0,
                'avg_pnl_krw': 0.0, 'trades': trades}
    wins = sum(1 for x in trades if x['ret'] > 0)
    net = sum(x['ret'] for x in trades)
    net_pnl_krw = net * CAPITAL   # 수수료는 이미 각 trade의 ret에 반영됨 (fee 파라미터로 매 청산마다 차감)
    return {
        'n': n, 'win_rate': wins / n, 'net_ret': net,
        'net_pnl_krw': net_pnl_krw, 'avg_pnl_krw': net_pnl_krw / n,
        'trades': trades,
    }


# ─────────────────────────────────────────
# 후보(candidate) 평가 — 지표별 signal_fn 빌더
# ─────────────────────────────────────────

def build_signal_fn(kind: str, params: dict, all_bars: list):
    """kind + params로 signal_fn(i, trend) 클로저를 만들어 반환.
    필요한 지표 배열은 여기서 한 번만 전구간 계산."""
    closes = [b['close'] for b in all_bars]
    highs  = [b['high']  for b in all_bars]
    lows   = [b['low']   for b in all_bars]
    vols   = [b['volume'] for b in all_bars]

    if kind == 'macd':
        fast, slow, sig = params['fast'], params['slow'], params['sig']
        macd_f, sig_f = macd_full_series(closes, fast, slow, sig)

        def fn(i, trend):
            if i < 1 or macd_f[i] is None or macd_f[i-1] is None or sig_f[i] is None or sig_f[i-1] is None:
                return None
            golden = macd_f[i-1] <= sig_f[i-1] and macd_f[i] > sig_f[i]
            dead   = macd_f[i-1] >= sig_f[i-1] and macd_f[i] < sig_f[i]
            if trend == 'UP' and golden: return 'L'
            if trend == 'DOWN' and dead: return 'S'
            return None
        return fn

    if kind == 'rsi':
        period, oversold, overbought = params['period'], params['oversold'], params['overbought']
        rsi = rsi_full_series(closes, period)

        def fn(i, trend):
            if i < 1 or rsi[i] is None or rsi[i-1] is None:
                return None
            if trend == 'UP' and rsi[i-1] < oversold <= rsi[i]:
                return 'L'
            if trend == 'DOWN' and rsi[i-1] > overbought >= rsi[i]:
                return 'S'
            return None
        return fn

    if kind == 'bollinger':
        period, mult = params['period'], params['mult']
        mid, upper, lower = bollinger_full_series(closes, period, mult)

        def fn(i, trend):
            if i < 1 or upper[i] is None or lower[i-1] is None:
                return None
            # 상단밴드 상향 돌파(추세추종 브레이크아웃) / 하단밴드 하향 돌파
            if trend == 'UP' and closes[i-1] <= upper[i-1] and closes[i] > upper[i]:
                return 'L'
            if trend == 'DOWN' and closes[i-1] >= lower[i-1] and closes[i] < lower[i]:
                return 'S'
            return None
        return fn

    if kind == 'bollinger_revert':
        period, mult = params['period'], params['mult']
        mid, upper, lower = bollinger_full_series(closes, period, mult)

        def fn(i, trend):
            if i < 1 or lower[i] is None or lower[i-1] is None:
                return None
            # 하단밴드 이탈 후 재진입(반등) / 상단밴드 이탈 후 재진입(반락)
            if trend == 'UP' and closes[i-1] < lower[i-1] and closes[i] >= lower[i]:
                return 'L'
            if trend == 'DOWN' and closes[i-1] > upper[i-1] and closes[i] <= upper[i]:
                return 'S'
            return None
        return fn

    if kind == 'stochastic':
        k_period, d_period = params['k_period'], params['d_period']
        low_th, high_th = params['low_th'], params['high_th']
        k, d = stochastic_full_series(highs, lows, closes, k_period, d_period)

        def fn(i, trend):
            if i < 1 or k[i] is None or d[i] is None or k[i-1] is None or d[i-1] is None:
                return None
            golden = k[i-1] <= d[i-1] and k[i] > d[i] and k[i] < low_th
            dead   = k[i-1] >= d[i-1] and k[i] < d[i] and k[i] > high_th
            if trend == 'UP' and golden: return 'L'
            if trend == 'DOWN' and dead: return 'S'
            return None
        return fn

    if kind == 'vwap':
        dev_th = params['dev_th']
        vwap = vwap_session_series(all_bars)

        def fn(i, trend):
            if i < 1 or vwap[i] is None or vwap[i-1] is None:
                return None
            prev_dev = (closes[i-1] - vwap[i-1]) / vwap[i-1]
            cur_dev  = (closes[i]   - vwap[i])   / vwap[i]
            # VWAP 아래로 dev_th 이상 괴리됐다가 다시 VWAP 위로 회귀 -> 롱 (평균회귀)
            if trend == 'UP' and prev_dev <= -dev_th and cur_dev > -dev_th:
                return 'L'
            if trend == 'DOWN' and prev_dev >= dev_th and cur_dev < dev_th:
                return 'S'
            return None
        return fn

    if kind == 'bollinger_vol':
        # 볼린저 상/하단 돌파 + 거래량 확인(직전 N봉 평균 대비 배수 이상).
        # 추세장에서만 터지는 '진짜 돌파'를 거르고 눌림장 가짜돌파를 줄이는 게 목적
        # (워크포워드상 순수 볼린저 돌파가 눌림국면에서 손실을 낸 약점을 겨냥).
        period, mult = params['period'], params['mult']
        vol_mult, vol_period = params['vol_mult'], params['vol_period']
        mid, upper, lower = bollinger_full_series(closes, period, mult)
        vol_avg = volume_sma_series(vols, vol_period)

        def fn(i, trend):
            if i < 1 or upper[i] is None or lower[i-1] is None:
                return None
            if vol_avg[i] is None or vol_avg[i] <= 0 or vols[i] < vol_avg[i] * vol_mult:
                return None
            if trend == 'UP' and closes[i-1] <= upper[i-1] and closes[i] > upper[i]:
                return 'L'
            if trend == 'DOWN' and closes[i-1] >= lower[i-1] and closes[i] < lower[i]:
                return 'S'
            return None
        return fn

    if kind == 'donchian':
        # 돈치안 채널 돌파: 직전 N봉 최고가 상향 돌파 시 롱 / 최저가 하향 돌파 시 숏.
        # 표준편차 기반(볼린저)이 아니라 실제 고저 기반이라 변동성 축소구간에서
        # 밴드가 과도하게 좁아지는 볼린저의 약점이 없다.
        period = params['period']

        def fn(i, trend):
            if i < period:
                return None
            prior_high = max(highs[i - period:i])
            prior_low  = min(lows[i - period:i])
            if trend == 'UP' and closes[i] > prior_high:
                return 'L'
            if trend == 'DOWN' and closes[i] < prior_low:
                return 'S'
            return None
        return fn

    if kind == 'macd_vol':
        # MACD 크로스 + 거래량 확인 필터 (직전 N봉 평균 대비 배수 이상일 때만 채택)
        fast, slow, sig, vol_mult, vol_period = (
            params['fast'], params['slow'], params['sig'], params['vol_mult'], params['vol_period']
        )
        macd_f, sig_f = macd_full_series(closes, fast, slow, sig)
        vol_avg = volume_sma_series(vols, vol_period)

        def fn(i, trend):
            if i < 1 or macd_f[i] is None or macd_f[i-1] is None or sig_f[i] is None or sig_f[i-1] is None:
                return None
            if vol_avg[i] is None or vol_avg[i] <= 0 or vols[i] < vol_avg[i] * vol_mult:
                return None
            golden = macd_f[i-1] <= sig_f[i-1] and macd_f[i] > sig_f[i]
            dead   = macd_f[i-1] >= sig_f[i-1] and macd_f[i] < sig_f[i]
            if trend == 'UP' and golden: return 'L'
            if trend == 'DOWN' and dead: return 'S'
            return None
        return fn

    raise ValueError(f'unknown signal kind: {kind}')


HOLDOUT_START = '20260501'
"""홀드아웃 시작일. 이 날짜 이후 거래는 후보를 '고르는' 데 쓰지 않고 검증용으로만 본다.
그리드서치로 수천 개 후보를 같은 구간에 채점하면 상위권이 다중검정 우연으로 채워지므로,
최근 구간에서도 벤치마크를 이기는지 따로 확인해야 실제 엣지와 과적합이 구분된다."""


def trade_stats(trades: list) -> dict:
    """거래 리스트 → 요약통계. t_stat은 거래당 수익률의 t값(mean/(sd/√n))으로,
    거래건수와 편차를 함께 반영하므로 '건수 적은데 승률만 높은 후보'가 자동으로 걸러진다."""
    n = len(trades)
    if n == 0:
        return {'n': 0, 'win_rate': 0.0, 'net_ret': 0.0,
                'net_pnl_krw': 0.0, 'avg_pnl_krw': 0.0, 't_stat': 0.0}
    rets = [t['ret'] for t in trades]
    mean = sum(rets) / n
    sd = (sum((r - mean) ** 2 for r in rets) / (n - 1)) ** 0.5 if n > 1 else 0.0
    net = sum(rets)
    return {
        'n': n,
        'win_rate': sum(1 for r in rets if r > 0) / n,
        'net_ret': net,
        'net_pnl_krw': net * CAPITAL,
        'avg_pnl_krw': net * CAPITAL / n,
        't_stat': mean / (sd / (n ** 0.5)) if sd > 0 else 0.0,
    }


def paired_diff_stats(a_trades: list, b_trades: list) -> dict:
    """두 전략의 '일자별 수익 차이' 검정 (a=후보, b=벤치마크).
    같은 종목·세션을 거래해 수익이 강하게 상관되므로, 절대 총손익 비교보다
    '후보가 벤치마크보다 하루당 더 버는가'를 짝지은 차이(diff=a-b)의 t값으로 보는 게
    훨씬 민감하고 공정하다. t>0 이면 후보 우위, t가 그 유의성(노이즈 대비 마진)."""
    da, db = defaultdict(float), defaultdict(float)
    for t in a_trades:
        da[t['d']] += t['ret']
    for t in b_trades:
        db[t['d']] += t['ret']
    days = sorted(set(da) | set(db))
    diffs = [da[d] - db[d] for d in days]
    n = len(diffs)
    if n < 2:
        return {'n': n, 'mean_ret': 0.0, 'mean_krw': 0.0, 't': 0.0}
    mean = sum(diffs) / n
    sd = (sum((x - mean) ** 2 for x in diffs) / (n - 1)) ** 0.5
    return {'n': n, 'mean_ret': mean, 'mean_krw': mean * CAPITAL,
            't': mean / (sd / (n ** 0.5)) if sd > 0 else 0.0}


def bench_health(bench_result: dict, recent_n: int = 60, min_full_t: float = 2.0) -> dict:
    """벤치마크 '절대성능' 건강검진 — 선정기(벤치 대비 상대비교)의 사각지대인
    '벤치 자체의 쇠퇴'를 감시한다. 최근창 열화(벤치 거래의 최근 recent_n건) + 절대바닥
    (전구간 t, 홀드아웃 t)을 함께 본다. alerts가 비어있지 않으면 쇠퇴 경보.
    라이브 자동개입은 하지 않는다(보고·대시보드·사람 판단용).
    bench_result는 trades가 아직 붙어 있어야 한다(strip_trades 이전에 호출)."""
    full_n = bench_result.get('n', 0)
    full_t = bench_result.get('t_stat', 0.0)
    full_net = bench_result.get('net_ret', 0.0)
    h = bench_result.get('holdout') or {}
    hold_n, hold_t, hold_net = h.get('n', 0), h.get('t_stat', 0.0), h.get('net_ret', 0.0)
    trs = sorted((bench_result.get('trades') or []), key=lambda t: str(t['d']))
    rs = trade_stats(trs[-recent_n:])
    rec_n, rec_t, rec_net = rs['n'], rs['t_stat'], rs['net_ret']
    alerts = []
    if full_t < min_full_t:
        alerts.append(f"전구간t {full_t:.2f}<{min_full_t:g}")
    if hold_t < 0.0:
        alerts.append(f"홀드아웃t {hold_t:.2f}<0")
    if rec_net <= 0.0:
        alerts.append(f"최근{rec_n}건 net{rec_net * 100:+.1f}%≤0")
    else:
        # 최근창이 아직 +지만 '건당수익률'이 전구간의 절반 미만이면 상대 열화 경보.
        # (t값은 분산 0일 때 0이 되는 약점이 있어, 더 견고한 건당수익 비율로 판정)
        full_avg = full_net / full_n if full_n else 0.0
        rec_avg = rec_net / rec_n if rec_n else 0.0
        if full_avg > 0 and rec_avg < full_avg * 0.5:
            alerts.append(f"최근 건당{rec_avg * 100:+.2f}%≪전구간 {full_avg * 100:+.2f}%(절반↓)")
    return {
        'full_n': full_n, 'full_t': full_t, 'full_net': full_net,
        'hold_n': hold_n, 'hold_t': hold_t, 'hold_net': hold_net,
        'rec_n': rec_n, 'rec_t': rec_t, 'rec_net': rec_net,
        'alerts': alerts, 'ok': not alerts,
    }


def equity_stats(rets: list) -> dict:
    """거래(또는 베팅) 수익 시퀀스 → 자산곡선 지표(가산식 누적합 기준).
    사이징 평가용 — total(총수익률), mdd(최대낙폭), sharpe(거래당×√n=t스케일), calmar(total/mdd)."""
    n = len(rets)
    if n == 0:
        return {'n': 0, 'total': 0.0, 'mdd': 0.0, 'sharpe': 0.0, 'calmar': 0.0}
    eq = peak = mdd = 0.0
    for r in rets:
        eq += r
        if eq > peak:
            peak = eq
        if peak - eq > mdd:
            mdd = peak - eq
    mean = sum(rets) / n
    var = sum((r - mean) ** 2 for r in rets) / n
    sd = var ** 0.5
    sharpe = (mean / sd) * (n ** 0.5) if sd > 0 else 0.0
    return {'n': n, 'total': eq, 'mdd': mdd, 'sharpe': sharpe,
            'calmar': (eq / mdd) if mdd > 0 else 0.0}


def vol_target_sizes(trades: list, vol_prev: dict, target=None, lo=0.3, hi=2.0) -> list:
    """변동성 타게팅 사이즈(리스크관리). 각 거래에 size=clip(target/전일변동성, lo, hi).
    저변동성일 때 크게·고변동성일 때 작게 → 거래별 리스크 기여 균등화(낙폭 완화 목적).
    target 미지정 시 전일변동성 중앙값(평균 사이즈≈1 → 총수익 비교 가능). 룩어헤드 없음."""
    vs = [vol_prev.get(t['d']) for t in trades]
    valid = sorted(v for v in vs if v and v > 0)
    if not valid:
        return [1.0] * len(trades)
    if target is None:
        m = len(valid)
        target = valid[m // 2] if m % 2 else (valid[m // 2 - 1] + valid[m // 2]) / 2
    out = []
    for v in vs:
        out.append(1.0 if (not v or v <= 0) else max(lo, min(hi, target / v)))
    return out


N_REGIMES = 4
"""국면분리 검증에 쓸 구간 수. 2026-09-29: 총손익+t값+홀드아웃 세 관문을 통과한 후보 2개
(BB29/0.8·10:00, BB34/0.7·10:00)가 모두 '최근 국면에만 의존'해서 기각됐다 — 4구간 중
2구간에서는 벤치마크가 이겼는데 우위가 최근 구간에 몰려있어 전구간·홀드아웃 숫자가 좋아
보인 것. 매번 사람이 수작업으로 나눠 확인하는 대신 자동 관문으로 편입."""


def regime_boundaries(all_bars: list, n_regimes: int = N_REGIMES) -> list:
    """전체 거래일을 n_regimes개 구간으로 균등분할하는 경계 날짜 목록(길이 n_regimes+1)을
    반환. all_bars가 매일 갱신되므로 하드코딩 날짜 대신 매번 동적으로 계산한다."""
    days = sorted(set(b['d'] for b in all_bars))
    if len(days) < n_regimes * 2:
        return [days[0], days[-1] + '1'] if days else []
    chunk = len(days) / n_regimes
    bounds = [days[int(round(i * chunk))] for i in range(n_regimes)]
    bounds.append(days[-1] + '1')  # 마지막 구간을 닫는 센티넬(모든 실제 날짜보다 큼)
    return bounds


def regime_stats(trades: list, boundaries: list) -> list:
    """boundaries(길이 n+1)로 정의된 n개의 반열린구간([lo, hi))별 trade_stats 목록."""
    n_regimes = max(0, len(boundaries) - 1)
    buckets = [[] for _ in range(n_regimes)]
    for t in trades:
        d = str(t['d'])
        for i in range(n_regimes):
            if boundaries[i] <= d < boundaries[i + 1]:
                buckets[i].append(t)
                break
    return [trade_stats(b) for b in buckets]


def evaluate_candidate(candidate: dict, all_bars: list, daily: list):
    """candidate = {'kind':..., 'params':{...}, 'fast_ma':int, 'slow_ma':int,
                     'entry_start':str, 'sl':float, 'trail_gap':float or None}
    반환값에는 전구간 통계 + 'holdout'(HOLDOUT_START 이후) + 'regimes'(N_REGIMES 등분
    구간별 통계 목록, 국면 의존성 확인용) 키가 함께 담긴다.
    """
    trend_lookup = make_trend_lookup(daily, candidate.get('fast_ma', 10), candidate.get('slow_ma', 30))
    signal_fn = build_signal_fn(candidate['kind'], candidate['params'], all_bars)
    res = simulate(
        all_bars, trend_lookup, signal_fn,
        sl=candidate.get('sl', 0.025),
        trail_gap=candidate.get('trail_gap', 0.015),
        entry_start=candidate.get('entry_start', '09:00'),
        time_stop_bars=candidate.get('time_stop_bars'),
        breakeven_trigger=candidate.get('breakeven_trigger'),
        trail_activate=candidate.get('trail_activate'),
    )
    out = trade_stats(res['trades'])
    out['holdout'] = trade_stats([t for t in res['trades'] if str(t['d']) >= HOLDOUT_START])
    out['regimes'] = regime_stats(res['trades'], regime_boundaries(all_bars))
    out['trades'] = res['trades']  # paired_diff_stats용 (영속화 전 strip_trades_for_persist로 제거)
    return out


# ─────────────────────────────────────────
# 지수 레버리지(dual-ETF) 현실 백테스트 — 삼성 대칭 2x 모델과 분리
#   삼성 simulate()는 005930에 sign×ret×2 대칭모델이라 숏을 낙관한다
#   (실측 252670 상관 −0.776, 슬리피지로 엣지 증발). 지수는 신호를 롱 ETF(122630)에서
#   만들되, 롱은 122630·숏은 114800 '실제 ETF 시세 ×1'로 각각 체결하고 KRX 틱
#   슬리피지를 반영한다(scratchpad index_sim/part3_full의 simulate_real 정식 편입).
# ─────────────────────────────────────────

def detect_tick(path_3min: str) -> int:
    """CSV 전 구간 OHLC에서 관측된 최소 가격증분 = 실제 호가단위(원).
    ETF 호가단위는 주식 스케줄과 달라(저가 ETF는 1~5원) 종목별 실측이 정확하다 —
    지수 전략 분석 전체가 이 실측틱 기반(scratchpad index_sim.detect_tick)이라 일치시킨다."""
    vals = set()
    with open(path_3min, encoding='utf-8') as f:
        for r in csv.DictReader(f):
            for k in ('open', 'high', 'low', 'close'):
                try:
                    vals.add(int(float(r[k])))
                except (ValueError, KeyError):
                    pass
    vals = sorted(vals)
    diffs = sorted({b - a for a, b in zip(vals, vals[1:]) if b - a > 0})
    return diffs[0] if diffs else 1


def load_short_lookup(path_3min: str) -> dict:
    """숏 ETF(예: 114800) 3분봉 CSV를 dt→{low,high,close} 딕셔너리로 로드.
    롱 ETF 봉의 'dt'로 조회해 숏 레그 체결가를 맞춘다."""
    out = {}
    with open(path_3min, encoding='utf-8') as f:
        for r in csv.DictReader(f):
            try:
                dt = datetime.strptime(r['datetime'][:14], '%Y%m%d%H%M%S')
            except (ValueError, KeyError):
                continue
            out[dt] = {'low': int(r['low']), 'high': int(r['high']), 'close': int(r['close'])}
    return out


def simulate_index(long_bars, short_lookup, trend_lookup, signal_fn,
                   sl=0.025, trail_gap=0.015, entry_start='09:30', force_exit='1520',
                   cooldown_bars=1, slip_ticks=0.5, long_tick=5, short_tick=5, fee=FEE,
                   leg_mode='longshort',
                   time_stop_bars=None, breakeven_trigger=None, trail_activate=None):
    """지수 dual-ETF 현실모델. signal_fn(i, trend)->'L'/'S'/None (신호원=long_bars=122630).
      롱('L') → long_bars 자기 시세로 진입/청산 (ETF가 2x 내장, ×1 체결)
      숏('S') → short_lookup[dt] 시세로 진입/청산 (ETF가 1x인버스 내장, ×1 체결)
    청산: 손절(진입가 -sl)·단일단계 트레일(peak≥sl 후 peak-cur≥trail_gap)·EOD. 방향 무관
    (둘 다 '매수'라 보유 ETF 상승이 favorable). 슬리피지=slip_ticks틱/측(진입+청산),
    틱은 종목별 실측치(long_tick/short_tick, detect_tick으로 구함)."""
    trades = []
    pos = None
    last_exit_idx = -999
    es = entry_start.replace(':', '')

    for i, bar in enumerate(long_bars):
        d, t, dt = bar['d'], bar['t'], bar['dt']

        if pos:
            xbar = bar if pos['dir'] == 'L' else short_lookup.get(dt)
            if xbar is not None:
                ep = pos['p']
                exitpx = None
                if i > pos['ei']:
                    lo = (xbar['low'] - ep) / ep
                    hi = (xbar['high'] - ep) / ep
                    pos['peak'] = max(pos['peak'], hi)
                    cur = (xbar['close'] - ep) / ep
                    # 손절선 — breakeven 상향(peak가 트리거 넘으면 손절선을 본전=0으로)
                    eff_sl = -sl
                    if breakeven_trigger is not None and pos['peak'] >= breakeven_trigger:
                        eff_sl = 0.0
                    act = trail_activate if trail_activate is not None else sl
                    if lo <= eff_sl:
                        exitpx = ep * (1 + eff_sl)
                    elif trail_gap and pos['peak'] >= act and (pos['peak'] - cur) >= trail_gap:
                        exitpx = xbar['close']
                    elif time_stop_bars is not None and (i - pos['ei']) >= time_stop_bars:
                        exitpx = xbar['close']   # 최대보유 초과 → 종가 청산
                if exitpx is None and t >= force_exit:
                    exitpx = xbar['close']
                if exitpx is not None:
                    tk = long_tick if pos['dir'] == 'L' else short_tick
                    slip = slip_ticks * tk * (1 / ep + 1 / max(exitpx, 1e-9))
                    ret = (exitpx - ep) / ep - fee - slip
                    trades.append({'d': d, 'ret': ret, 'r': 'EXIT', 'dir': pos['dir']})
                    pos = None
                    last_exit_idx = i
                    continue

        if not pos and es <= t <= '1500' and (i - last_exit_idx) > cooldown_bars:
            trend = trend_lookup(d)
            if trend:
                sig_dir = signal_fn(i, trend)
                if sig_dir == 'S' and leg_mode == 'longonly':
                    sig_dir = None   # 롱온리: 숏 신호 무시
                if sig_dir == 'S':
                    ib = short_lookup.get(dt)
                    if ib is not None:
                        pos = {'dir': 'S', 'p': ib['close'], 'ei': i, 'peak': 0.0}
                elif sig_dir == 'L':
                    pos = {'dir': 'L', 'p': bar['close'], 'ei': i, 'peak': 0.0}

    if pos:
        ep = pos['p']
        xbar = long_bars[-1] if pos['dir'] == 'L' else short_lookup.get(long_bars[-1]['dt'])
        if xbar is not None:
            exitpx = xbar['close']
            tk = long_tick if pos['dir'] == 'L' else short_tick
            slip = slip_ticks * tk * (1 / ep + 1 / max(exitpx, 1e-9))
            trades.append({'d': long_bars[-1]['d'],
                           'ret': (exitpx - ep) / ep - fee - slip, 'r': 'EOD', 'dir': pos['dir']})

    return {'trades': trades}


def evaluate_index_candidate(candidate: dict, long_bars: list, short_lookup: dict, daily: list,
                             long_tick: int = 5, short_tick: int = 5):
    """지수 후보 평가 — evaluate_candidate의 dual-ETF 버전.
    전구간 통계 + holdout + regimes + 레그별(long/short) 분해를 반환.
    long_tick/short_tick은 detect_tick으로 구한 종목별 실측 호가단위."""
    trend_lookup = make_trend_lookup(daily, candidate.get('fast_ma', 10), candidate.get('slow_ma', 20))
    signal_fn = build_signal_fn(candidate['kind'], candidate['params'], long_bars)
    res = simulate_index(
        long_bars, short_lookup, trend_lookup, signal_fn,
        sl=candidate.get('sl', 0.025),
        trail_gap=candidate.get('trail_gap', 0.015),
        entry_start=candidate.get('entry_start', '09:30'),
        slip_ticks=candidate.get('slip_ticks', 0.5),
        long_tick=long_tick, short_tick=short_tick,
        leg_mode=candidate.get('leg_mode', 'longshort'),
        time_stop_bars=candidate.get('time_stop_bars'),
        breakeven_trigger=candidate.get('breakeven_trigger'),
        trail_activate=candidate.get('trail_activate'),
    )
    trades = res['trades']
    out = trade_stats(trades)
    out['holdout'] = trade_stats([t for t in trades if str(t['d']) >= HOLDOUT_START])
    out['regimes'] = regime_stats(trades, regime_boundaries(long_bars))
    out['long'] = trade_stats([t for t in trades if t['dir'] == 'L'])
    out['short'] = trade_stats([t for t in trades if t['dir'] == 'S'])
    out['trades'] = trades  # paired_diff_stats용 (영속화 전 strip_trades_for_persist로 제거)
    return out


# ─────────────────────────────────────────
# 공격형 HA 플립 초단타(스캘프) 엔진 — 별도 전략(ScalpFlip)
#   신호원(롱 ETF, 예 233740 코스닥150레버리지)의 1분봉 하이킨아시 색 전환으로
#   롱/숏을 플립. 추세게이트 없음(양방향 자유). 타이트 SL/TP + 반대전환 플립 + EOD.
#   롱→롱ETF, 숏→숏ETF(예 251340) 실제 시세 ×1 체결, dt정합·실측틱 슬리피지.
# ─────────────────────────────────────────

def heikin_ashi(bars: list) -> list:
    """bars(list[dict] open/high/low/close) → HA dict 리스트(ha_open/ha_close/color).
    color: +1 양봉(ha_close>ha_open) / -1 음봉. 첫 봉은 (O+C)/2로 시드."""
    out = []
    prev_o = prev_c = None
    for b in bars:
        ha_c = (b['open'] + b['high'] + b['low'] + b['close']) / 4.0
        if prev_o is None:
            ha_o = (b['open'] + b['close']) / 2.0
        else:
            ha_o = (prev_o + prev_c) / 2.0
        color = 1 if ha_c > ha_o else (-1 if ha_c < ha_o else 0)
        out.append({'ha_open': ha_o, 'ha_close': ha_c, 'color': color})
        prev_o, prev_c = ha_o, ha_c
    return out


def simulate_scalp(long_bars, short_lookup, sl=0.004, tp=0.006, trail_gap=None,
                   flip_confirm_n=1, entry_start='09:05', force_exit='1520',
                   slip_ticks=0.5, long_tick=5, short_tick=5, fee=FEE,
                   use_trend=False, trend_lookup=None):
    """HA 색전환 플립 스캘퍼. long_bars=신호원(롱 ETF) 1분봉.
      - 새 색이 flip_confirm_n봉 연속되면 그 방향으로 진입/플립(반대보유 시 청산 후 반대진입).
      - 청산: 타이트 SL(-sl)·TP(+tp)·선택적 트레일·반대플립·EOD. 방향 무관(둘 다 매수).
      - use_trend=True면 trend_lookup(d)로 방향 게이트(기본 False=게이트 없음, 공격형).
    반환 {'trades':[{d,ret,dir,r}]}. 비용=fee+slip_ticks틱/측."""
    ha = heikin_ashi(long_bars)
    trades = []
    pos = None           # {'dir','p','ei','peak'}
    run_color = 0        # 현재까지 연속된 색
    run_len = 0
    es = entry_start.replace(':', '')

    def tick_of(dirn):
        return long_tick if dirn == 'L' else short_tick

    def do_exit(i, bar, reason):
        nonlocal pos
        ep = pos['p']
        xbar = bar if pos['dir'] == 'L' else short_lookup.get(long_bars[i]['dt'])
        if xbar is None:
            return False
        px = reason_px if (reason_px := {'SL': ep * (1 - sl), 'TP': ep * (1 + tp)}.get(reason)) else xbar['close']
        tk = tick_of(pos['dir'])
        slip = slip_ticks * tk * (1 / ep + 1 / max(px, 1e-9))
        trades.append({'d': bar['d'], 'ret': (px - ep) / ep - fee - slip, 'dir': pos['dir'], 'r': reason})
        pos = None
        return True

    for i, bar in enumerate(long_bars):
        d, t, dt = bar['d'], bar['t'], bar['dt']
        c = ha[i]['color']
        if c != 0 and c == run_color:
            run_len += 1
        elif c != 0:
            run_color = c; run_len = 1

        # 보유 중 청산 체크 (SL/TP/트레일/EOD) — 진입봉 이후
        if pos and i > pos['ei']:
            xbar = bar if pos['dir'] == 'L' else short_lookup.get(dt)
            if xbar is not None:
                ep = pos['p']
                lo = (xbar['low'] - ep) / ep; hi = (xbar['high'] - ep) / ep
                pos['peak'] = max(pos['peak'], hi)
                if lo <= -sl:
                    do_exit(i, bar, 'SL'); 
                elif tp and hi >= tp:
                    do_exit(i, bar, 'TP')
                elif trail_gap and pos['peak'] >= tp and (pos['peak'] - (xbar['close'] - ep) / ep) >= trail_gap:
                    do_exit(i, bar, 'TRAIL')
        # EOD
        if pos and t >= force_exit:
            do_exit(i, bar, 'EOD'); 
            continue

        # 플립/진입: 색이 flip_confirm_n 연속 충족 & 진입시간대
        if es <= t <= '1500' and run_len >= flip_confirm_n and run_color != 0:
            desired = 'L' if run_color == 1 else 'S'
            if use_trend and trend_lookup is not None:
                tr = trend_lookup(d)
                if (desired == 'L' and tr != 'UP') or (desired == 'S' and tr != 'DOWN'):
                    desired = None
            if desired and (pos is None or pos['dir'] != desired):
                if pos is not None:
                    do_exit(i, bar, 'FLIP')
                entry_bar = bar if desired == 'L' else short_lookup.get(dt)
                if entry_bar is not None and t < force_exit:
                    pos = {'dir': desired, 'p': entry_bar['close'], 'ei': i, 'peak': 0.0}

    if pos is not None:
        do_exit(len(long_bars) - 1, long_bars[-1], 'EOD')
    return {'trades': trades}


def evaluate_scalp_candidate(candidate: dict, long_bars: list, short_lookup: dict,
                             long_tick: int = 5, short_tick: int = 5, daily: list = None):
    """HA 스캘프 후보 평가 — 전구간+holdout+regimes+레그분해 + 청산사유 분포."""
    tl = None
    if candidate.get('use_trend') and daily is not None:
        tl = make_trend_lookup(daily, candidate.get('fast_ma', 10), candidate.get('slow_ma', 20))
    res = simulate_scalp(
        long_bars, short_lookup,
        sl=candidate.get('sl', 0.004), tp=candidate.get('tp', 0.006),
        trail_gap=candidate.get('trail_gap'), flip_confirm_n=candidate.get('flip_confirm_n', 1),
        entry_start=candidate.get('entry_start', '09:05'),
        slip_ticks=candidate.get('slip_ticks', 0.5),
        long_tick=long_tick, short_tick=short_tick,
        use_trend=candidate.get('use_trend', False), trend_lookup=tl,
    )
    trades = res['trades']
    out = trade_stats(trades)
    out['holdout'] = trade_stats([t for t in trades if str(t['d']) >= HOLDOUT_START])
    out['regimes'] = regime_stats(trades, regime_boundaries(long_bars)) if long_bars else []
    out['long'] = trade_stats([t for t in trades if t['dir'] == 'L'])
    out['short'] = trade_stats([t for t in trades if t['dir'] == 'S'])
    rc = defaultdict(int)
    for t in trades:
        rc[t['r']] += 1
    out['exit_reasons'] = dict(rc)
    return out
