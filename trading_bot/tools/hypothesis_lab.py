# -*- coding: utf-8 -*-
"""가설 탐색 라이브러리 — 파라미터 미세조정(refine)을 넘어 '다른 종류의 전략'을 시험한다.

기존 탐색엔진(run_index_search)은 지표 그리드를 다 돈 뒤 벤치(BB29/1.0) 주변 국소탐색만
무한반복해 사실상 정체됐다. 여기서는 가설을 '패밀리' 단위로 정의한다:
  gate_*    일봉 방향게이트 교체(급락 지연 대응 등)       — 신호는 벤치 BB 그대로
  filter_*  벤치 신호에 진입필터 추가(과매도 숏 회피 등)   — 게이트·신호 그대로
  entry_*   새 진입 로직(ORB·일중모멘텀·반등 등)           — 벤치와 독립 전략
각 가설은 build(spec, ctx) → (trend_lookup, signal_fn, sim_kwargs)로 simulate_index에 들어가
라이브와 같은 현실모델(실제 ETF 시세·실측틱 슬리피지)로 채점된다.

판정은 run_hypothesis_explorer.evaluate()가 기존 선정기 기준(paired-t·홀드아웃·국면·코스닥
전이)을 그대로 쓴다. 이 모듈은 '가설 정의'만 담당한다."""
import indicator_lab as lab

BENCH_SPEC = {'kind': 'bench'}


# ─────────────────────────────────────────
# 컨텍스트 (유니버스 1개당 1회 계산)
# ─────────────────────────────────────────

class Ctx:
    def __init__(self, lev3, levd, inv3):
        self.bars, self.daily = lab.load_master_data(lev3, levd)
        self.short_lookup = lab.load_short_lookup(inv3)
        self.long_tick, self.short_tick = lab.detect_tick(lev3), lab.detect_tick(inv3)
        self.closes = [b['close'] for b in self.bars]
        self.dates = [d['date'] for d in self.daily]
        self.dcloses = [d['close'] for d in self.daily]
        # 일자별 인덱스·직전일 종가(3분봉 기준)·장중 누적 고저
        self.day_first, self.prev_close = {}, {}
        self.run_hi, self.run_lo = [0] * len(self.bars), [0] * len(self.bars)
        last_close, cur_d, hi, lo = None, None, 0, 0
        for i, b in enumerate(self.bars):
            if b['d'] != cur_d:
                if cur_d is not None:
                    last_close = self.bars[i - 1]['close']
                cur_d, hi, lo = b['d'], b['high'], b['low']
                self.day_first[b['d']] = i
                self.prev_close[b['d']] = last_close
            hi, lo = max(hi, b['high']), min(lo, b['low'])
            self.run_hi[i], self.run_lo[i] = hi, lo
        self.vwap = lab.vwap_session_series(self.bars)
        self._bb = {}
        self.paths = {'lev3': lev3, 'levd': levd, 'inv3': inv3}
        self._lazy = {}

    # ── 2026-10-24 추가 정보원 (지연 로드, 전부 '당일 미포함' 조회만 제공)
    def daily_ohlc(self):
        """일봉 CSV의 시고저종 {date: (o,h,l,c)} — load_master_data는 종가만 담아 별도 파싱."""
        if 'ohlc' not in self._lazy:
            import csv
            out = {}
            with open(self.paths['levd'], encoding='utf-8') as f:
                for r in csv.DictReader(f):
                    try:
                        out[r['date']] = (int(r['open']), int(r['high']), int(r['low']), int(r['close']))
                    except (KeyError, ValueError):
                        continue
            self._lazy['ohlc'] = out
        return self._lazy['ohlc']

    def peer(self):
        """다른 유니버스(지수↔코스닥) 데이터: {'daily', 'close_dt', 'prev_close'} 또는 None.
        같은 폴더의 index_*/kosdaq_* 파일명을 맞바꿔 찾는다(코스닥 전이게이트에선 지수가 피어)."""
        if 'peer' not in self._lazy:
            import os
            def swap(p):
                d, b = os.path.split(p)
                if b.startswith('index_'):
                    return os.path.join(d, 'kosdaq_' + b[len('index_'):])
                if b.startswith('kosdaq_'):
                    return os.path.join(d, 'index_' + b[len('kosdaq_'):])
                return None
            p3, pd_ = swap(self.paths['lev3']), swap(self.paths['levd'])
            if not (p3 and pd_ and os.path.exists(p3) and os.path.exists(pd_)):
                self._lazy['peer'] = None
            else:
                bars, daily = lab.load_master_data(p3, pd_)
                close_dt, prev_close, cur, last = {}, {}, None, None
                for b in bars:
                    if b['d'] != cur:
                        prev_close[b['d']] = last
                        cur = b['d']
                    close_dt[b['dt']] = b['close']
                    last = b['close']
                self._lazy['peer'] = {'daily': daily, 'close_dt': close_dt, 'prev_close': prev_close}
        return self._lazy['peer']

    def day_amounts(self):
        """일자별 거래대금(Σ close×volume, 3분봉) — {'lev': {d: amt}, 'inv': {d: amt}}."""
        if 'amt' not in self._lazy:
            import csv
            lev = {}
            for b in self.bars:
                lev[b['d']] = lev.get(b['d'], 0) + b['close'] * b['volume']
            inv = {}
            with open(self.paths['inv3'], encoding='utf-8') as f:
                for r in csv.DictReader(f):
                    try:
                        d = r['datetime'][:8]
                        inv[d] = inv.get(d, 0) + int(r['close']) * int(r.get('volume') or 0)
                    except (KeyError, ValueError):
                        continue
            self._lazy['amt'] = {'lev': lev, 'inv': inv}
        return self._lazy['amt']

    def trade_days(self):
        if 'days' not in self._lazy:
            self._lazy['days'] = sorted(self.day_first)
        return self._lazy['days']

    def bb(self, period=29, mult=1.0):
        k = (period, mult)
        if k not in self._bb:
            self._bb[k] = lab.bollinger_full_series(self.closes, period, mult)
        return self._bb[k]

    def prior_closes(self, d):
        """d일 '이전' 확정 일봉 종가 목록(라이브와 동일하게 당일 미포함)."""
        out = []
        for dt, c in zip(self.dates, self.dcloses):
            if dt >= d:
                break
            out.append(c)
        return out


def _ma(xs, n):
    return sum(xs[-n:]) / n if len(xs) >= n else None


def _cached(fn):
    cache = {}
    def g(d):
        if d not in cache:
            cache[d] = fn(d)
        return cache[d]
    return g


# ─────────────────────────────────────────
# 게이트 (일봉 방향)
# ─────────────────────────────────────────

def gate_ma(ctx, fast=10, slow=20):
    return lab.make_trend_lookup(ctx.daily, fast, slow)


def gate_close_ma(ctx, n=10):
    def f(d):
        c = ctx.prior_closes(d)
        m = _ma(c, n)
        if m is None:
            return None
        return 'UP' if c[-1] > m else 'DOWN'
    return _cached(f)


def gate_crash_override(ctx, d1=0.04, d3=0.08, sym=True):
    """MA10/20 게이트 + 급변 오버라이드: 전일 −d1 이하 또는 3일 −d3 이하 → DOWN 강제
    (sym이면 반대로 급등 → UP 강제). 10-07~12 급락에서 게이트가 4일간 UP에 머문 지연 대응."""
    base = lab.make_trend_lookup(ctx.daily, 10, 20)
    def f(d):
        c = ctx.prior_closes(d)
        t = base(d)
        if len(c) < 4:
            return t
        r1, r3 = c[-1] / c[-2] - 1, c[-1] / c[-4] - 1
        if r1 <= -d1 or r3 <= -d3:
            return 'DOWN'
        if sym and (r1 >= d1 or r3 >= d3):
            return 'UP'
        return t
    return _cached(f)


def gate_ma_confirm(ctx, n=5):
    """MA10/20 방향과 '전일종가 vs MA(n)'이 일치할 때만 거래(불일치=쉬는 날)."""
    base = lab.make_trend_lookup(ctx.daily, 10, 20)
    def f(d):
        t = base(d)
        c = ctx.prior_closes(d)
        m = _ma(c, n)
        if t is None or m is None:
            return None
        return t if (('UP' if c[-1] > m else 'DOWN') == t) else None
    return _cached(f)


# ─────────────────────────────────────────
# 벤치 신호 + 필터
# ─────────────────────────────────────────

def bench_signal(ctx, period=29, mult=1.0):
    return lab.build_signal_fn('bollinger', {'period': period, 'mult': mult}, ctx.bars)


def with_filter(ctx, base_fn, keep):
    """keep(i, trend, sig) False면 신호 버림."""
    def fn(i, trend):
        s = base_fn(i, trend)
        if s and not keep(i, trend, s):
            return None
        return s
    return fn


def filt_stretch(ctx, k=0.05, n=10):
    """과매도 숏·과매수 롱 회피: 현재가가 전일까지 일봉 MA(n) 대비 k 이상 아래면 숏 금지,
    k 이상 위면 롱 금지(급락 후 반등장에서 바닥 숏이 털리는 현상 겨냥)."""
    mcache = _cached(lambda d: _ma(ctx.prior_closes(d), n))
    def keep(i, trend, s):
        m = mcache(ctx.bars[i]['d'])
        if m is None:
            return True
        dev = ctx.closes[i] / m - 1
        return not ((s == 'S' and dev <= -k) or (s == 'L' and dev >= k))
    return keep


def filt_vwap(ctx):
    def keep(i, trend, s):
        v = ctx.vwap[i]
        return v is None or (s == 'L' and ctx.closes[i] > v) or (s == 'S' and ctx.closes[i] < v)
    return keep


def filt_dayret(ctx):
    """당일 방향 일치: 롱은 전일종가 위, 숏은 전일종가 아래에서만."""
    def keep(i, trend, s):
        pc = ctx.prev_close.get(ctx.bars[i]['d'])
        if not pc:
            return True
        return (s == 'L' and ctx.closes[i] > pc) or (s == 'S' and ctx.closes[i] < pc)
    return keep


def filt_window(ctx, end='1500'):
    def keep(i, trend, s):
        return ctx.bars[i]['t'] <= end
    return keep


def filt_rangepos(ctx, th=0.5):
    """장중 위치: 롱은 당일 고저범위 상위 th 이상, 숏은 하위 th 이하에서만."""
    def keep(i, trend, s):
        hi, lo = ctx.run_hi[i], ctx.run_lo[i]
        if hi <= lo:
            return True
        pos = (ctx.closes[i] - lo) / (hi - lo)
        return (s == 'L' and pos >= th) or (s == 'S' and pos <= 1 - th)
    return keep


# ─────────────────────────────────────────
# 새 진입 패밀리 (벤치와 독립 전략)
# ─────────────────────────────────────────

def entry_orb(ctx, minutes=30, use_gate=True):
    """오프닝레인지 돌파: 09:00~09:00+minutes 고저를 장중 첫 돌파 시 1일 1회 진입."""
    end_t = f"{9 + (minutes // 60):02d}{minutes % 60:02d}"
    rng = {}
    for d, i0 in ctx.day_first.items():
        hi = lo = None
        j = i0
        while j < len(ctx.bars) and ctx.bars[j]['d'] == d and ctx.bars[j]['t'] < end_t:
            hi = max(hi or 0, ctx.bars[j]['high']); lo = min(lo or 10**12, ctx.bars[j]['low'])
            j += 1
        rng[d] = (hi, lo)
    used = set()
    def fn(i, trend):
        b = ctx.bars[i]; d = b['d']
        if d in used or b['t'] < end_t:
            return None
        hi, lo = rng.get(d, (None, None))
        if hi is None:
            return None
        sig = 'L' if ctx.closes[i] > hi else ('S' if ctx.closes[i] < lo else None)
        if sig and use_gate and not ((sig == 'L' and trend == 'UP') or (sig == 'S' and trend == 'DOWN')):
            return None
        if sig:
            used.add(d)
        return sig
    return fn


def entry_pdhl(ctx, use_gate=True):
    """전일 고/저(3분봉 기준) 돌파 — 1일 1회."""
    dhl, cur, hi, lo = {}, None, 0, 0
    prev = {}
    for b in ctx.bars:
        if b['d'] != cur:
            if cur is not None:
                dhl[cur] = (hi, lo)
            prev[b['d']] = dhl.get(cur) if cur else None
            cur, hi, lo = b['d'], b['high'], b['low']
        hi, lo = max(hi, b['high']), min(lo, b['low'])
    used = set()
    def fn(i, trend):
        d = ctx.bars[i]['d']
        if d in used or not prev.get(d):
            return None
        phi, plo = prev[d]
        sig = 'L' if ctx.closes[i] > phi else ('S' if ctx.closes[i] < plo else None)
        if sig and use_gate and not ((sig == 'L' and trend == 'UP') or (sig == 'S' and trend == 'DOWN')):
            return None
        if sig:
            used.add(d)
        return sig
    return fn


def entry_tsmom(ctx, at='1400', th=0.01, use_gate=False):
    """장중 시계열 모멘텀: at 시각 기준 당일 수익률(전일종가 대비)이 +th 이상이면 롱,
    −th 이하면 숏 → EOD 청산(장 초반 흐름이 장 후반까지 이어진다는 가설)."""
    used = set()
    def fn(i, trend):
        b = ctx.bars[i]; d = b['d']
        if d in used or b['t'] < at:
            return None
        used.add(d)   # 판정은 at 이후 첫 봉에서 1회
        pc = ctx.prev_close.get(d)
        if not pc:
            return None
        r = ctx.closes[i] / pc - 1
        sig = 'L' if r >= th else ('S' if r <= -th else None)
        if sig and use_gate and not ((sig == 'L' and trend == 'UP') or (sig == 'S' and trend == 'DOWN')):
            return None
        return sig
    return fn


def entry_bounce(ctx, drop=0.03, period=29, mult=1.0):
    """급락 반등 롱(역추세): 전일종가 대비 −drop 이상 빠진 날, 볼린저 하단 아래에서
    하단 위로 재진입하면 게이트와 무관하게 롱(반등 시도장에서 숏 대신 반등을 먹는 가설)."""
    _, _, lower = ctx.bb(period, mult)
    def fn(i, trend):
        if i < 1 or lower[i] is None or lower[i - 1] is None:
            return None
        pc = ctx.prev_close.get(ctx.bars[i]['d'])
        if not pc or ctx.run_lo[i] / pc - 1 > -drop:
            return None
        if ctx.closes[i - 1] < lower[i - 1] and ctx.closes[i] >= lower[i]:
            return 'L'
        return None
    return fn


def entry_gap(ctx, g=0.02, mode='follow'):
    """갭 매매: 첫 판정봉에서 갭(시가/전일종가) ≥ g면 follow(같은 방향)/fade(반대) 1회."""
    used = set()
    def fn(i, trend):
        d = ctx.bars[i]['d']
        if d in used:
            return None
        used.add(d)
        pc = ctx.prev_close.get(d)
        o = ctx.bars[ctx.day_first[d]]['open']
        if not pc:
            return None
        gp = o / pc - 1
        if abs(gp) < g:
            return None
        up = gp > 0
        if mode == 'fade':
            up = not up
        return 'L' if up else 'S'
    return fn


def entry_state_short(ctx, period=29, mult=1.0):
    """(10-15 보류후보) 벤치 + 첫 판정봉에서 이미 하단 밖이면 숏(교차 없이)."""
    base = bench_signal(ctx, period, mult)
    _, _, lower = ctx.bb(period, mult)
    first_eval = {}
    for j, b in enumerate(ctx.bars):
        if b['t'] >= '0930' and b['d'] not in first_eval:
            first_eval[b['d']] = j
    def fn(i, trend):
        s = base(i, trend)
        if s:
            return s
        if first_eval.get(ctx.bars[i]['d']) == i and trend == 'DOWN' \
                and lower[i] is not None and ctx.closes[i] < lower[i]:
            return 'S'
        return None
    return fn


# ─────────────────────────────────────────
# 2026-10-24 새 정보원 패밀리 (캘린더·일봉 OHLC·수급·크로스마켓·메타)
# ─────────────────────────────────────────

def _expiry_days(ctx):
    """KOSPI200 월물 옵션만기 = 매월 둘째 목요일(휴장이면 직전 거래일). 거래일 달력은 사전 공지."""
    from datetime import date, timedelta
    days = ctx.trade_days()
    dset, out = set(days), set()
    months = sorted({d[:6] for d in days})
    for ym in months:
        y, m = int(ym[:4]), int(ym[4:])
        first = date(y, m, 1)
        thu = first + timedelta(days=(3 - first.weekday()) % 7 + 7)
        c = thu
        while c.month == m and c.strftime('%Y%m%d') not in dset:
            c -= timedelta(days=1)
        if c.month == m:
            out.add(c.strftime('%Y%m%d'))
    return out


def filt_expiry(ctx, mode='skip'):
    """옵션만기일: skip=신규진입 금지(핀닝·마감 프로그램 매물로 추세 왜곡 가설)."""
    ex = _expiry_days(ctx)
    def keep(i, trend, s):
        return ctx.bars[i]['d'] not in ex
    return keep


def filt_tom(ctx, last=1, first=3, block='S'):
    """월말·월초(월말 last + 월초 first 거래일): 연기금·적립식 자금 유입으로 상승 편향 → 숏 금지."""
    days = ctx.trade_days()
    tom = set()
    for k, d in enumerate(days):
        # 월초: 같은 달의 앞선 거래일 수
        j = k
        while j > 0 and days[j - 1][:6] == d[:6]:
            j -= 1
        if k - j < first:
            tom.add(d)
        # 월말: 같은 달의 뒤 거래일 수
        j = k
        while j + 1 < len(days) and days[j + 1][:6] == d[:6]:
            j += 1
        if j - k < last and j + 1 < len(days):   # 데이터 끝 달은 월말 미확정 → 제외
            tom.add(d)
    def keep(i, trend, s):
        return not (s == block and ctx.bars[i]['d'] in tom)
    return keep


def filt_prior_clv(ctx, th=0.25):
    """전일 일봉 종가위치 CLV=(C−L)/(H−L): 하위 th로 마감(매도우위)한 다음날 롱 금지,
    상위 th로 마감(매수우위)한 다음날 숏 금지 — 일봉 고저 정보(지금까지 미사용)."""
    ohlc = ctx.daily_ohlc()
    ds = sorted(ohlc)
    import bisect
    def clv(d):
        k = bisect.bisect_left(ds, d) - 1
        if k < 0:
            return None
        o, h, l, c = ohlc[ds[k]]
        return None if h <= l else (c - l) / (h - l)
    cc = _cached(clv)
    def keep(i, trend, s):
        v = cc(ctx.bars[i]['d'])
        if v is None:
            return True
        return not ((s == 'L' and v <= th) or (s == 'S' and v >= 1 - th))
    return keep


def filt_flow(ctx, q=0.2, lb=20, mode='contra'):
    """개인 수급 쏠림: 전일 레버/인버스 거래대금 비율이 직전 lb일 중 상위 q면 '롱 과열',
    하위 q면 '숏 과열'. contra=과열 방향 신호 금지(역발상), follow=반대 방향 신호 금지(쏠림 추종)."""
    import math
    amt = ctx.day_amounts()
    days = ctx.trade_days()
    lr = {d: math.log(amt['lev'][d] / amt['inv'][d]) for d in days
          if amt['lev'].get(d) and amt['inv'].get(d)}
    state = {}
    for k, d in enumerate(days):
        hist = [lr[x] for x in days[max(0, k - lb):k] if x in lr]   # d 미포함
        if len(hist) < lb // 2 or days[k - 1] not in lr:
            continue
        y = lr[days[k - 1]]
        rank = sum(1 for v in hist if v < y) / len(hist)
        state[d] = 'HOT_L' if rank >= 1 - q else ('HOT_S' if rank <= q else None)
    def keep(i, trend, s):
        st = state.get(ctx.bars[i]['d'])
        if not st:
            return True
        hot = 'L' if st == 'HOT_L' else 'S'
        return s != hot if mode == 'contra' else s == hot
    return keep


def filt_peer_intraday(ctx):
    """크로스마켓 확인: 신호 시점 피어(지수↔코스닥) 레버 ETF의 당일 수익률 부호가 신호와 일치할 때만."""
    p = ctx.peer()
    def keep(i, trend, s):
        if not p:
            return True
        b = ctx.bars[i]
        pc, c = p['prev_close'].get(b['d']), p['close_dt'].get(b['dt'])
        if not pc or not c:
            return True
        r = c / pc - 1
        return (s == 'L' and r > 0) or (s == 'S' and r < 0)
    return keep


def gate_peer_agree(ctx, fast=10, slow=20):
    """자기 MA10/20 게이트 + 피어 유니버스 같은 게이트가 같은 방향인 날만(시장 폭 확인)."""
    base = lab.make_trend_lookup(ctx.daily, fast, slow)
    p = ctx.peer()
    if not p:
        return base
    pt = lab.make_trend_lookup(p['daily'], fast, slow)
    def f(d):
        t = base(d)
        return t if t and pt(d) == t else None
    return _cached(f)


def _shadow_bench(ctx):
    if 'shadow' not in ctx._lazy:
        tr, sig, sim = gate_ma(ctx), bench_signal(ctx), {'sl': 0.025, 'trail_gap': 0.015, 'entry_start': '09:30'}
        ctx._lazy['shadow'] = lab.simulate_index(ctx.bars, ctx.short_lookup, tr, sig, slip_ticks=0.5,
                                                 long_tick=ctx.long_tick, short_tick=ctx.short_tick,
                                                 **sim)['trades']
    return ctx._lazy['shadow']


def gate_meta(ctx, mode='streak', k=3, rest=1, lb=20):
    """메타 규칙(벤치 섀도 거래 기반, d일 이전 청산분만 사용):
    streak = 직전 k건 연속 손실이면 다음 rest 거래일 휴식(연속 손절=레짐 불일치 신호),
    curve  = 섀도 최근 lb건 누적수익 < 0이면 휴식(에쿼티커브 필터)."""
    base = lab.make_trend_lookup(ctx.daily, 10, 20)
    sh = _shadow_bench(ctx)
    days = ctx.trade_days()
    off = set()
    import bisect
    if mode == 'streak':
        run = 0
        for t in sh:
            run = run + 1 if t['ret'] < 0 else 0
            if run >= k:
                k0 = bisect.bisect_right(days, str(t['d']))
                off.update(days[k0:k0 + rest])
    def f(d):
        t = base(d)
        if not t or d in off:
            return None
        if mode == 'curve':
            past = [x['ret'] for x in sh if str(x['d']) < d][-lb:]
            if len(past) >= lb and sum(past) < 0:
                return None
        return t
    return _cached(f)


# ─────────────────────────────────────────
# 빌더: spec → (trend_lookup, signal_fn, sim_kwargs)
# ─────────────────────────────────────────

GATES = {'ma': gate_ma, 'close_ma': gate_close_ma, 'crash': gate_crash_override,
         'confirm': gate_ma_confirm, 'peer_agree': gate_peer_agree, 'meta': gate_meta}
FILTERS = {'stretch': filt_stretch, 'vwap': filt_vwap, 'dayret': filt_dayret,
           'window': filt_window, 'rangepos': filt_rangepos,
           'expiry': filt_expiry, 'tom': filt_tom, 'prior_clv': filt_prior_clv, 'flow': filt_flow,
           'peer_intraday': filt_peer_intraday}
ENTRIES = {'orb': entry_orb, 'pdhl': entry_pdhl, 'tsmom': entry_tsmom, 'bounce': entry_bounce,
           'gap': entry_gap, 'state_short': entry_state_short}


def build(spec: dict, ctx: Ctx):
    """spec 예: {'gate': ['crash', {'d1':.04}], 'filters': [['stretch', {'k':.05}]],
               'entry': ['orb', {'minutes':30}], 'sim': {'sl':.025, 'entry_start':'09:30'}}
    생략 시 벤치 기본값(MA10/20 게이트, BB29/1.0 신호, SL2.5/트레일1.5, 09:30~)."""
    g = spec.get('gate')
    trend = GATES[g[0]](ctx, **(g[1] if len(g) > 1 else {})) if g else gate_ma(ctx)
    e = spec.get('entry')
    sig = ENTRIES[e[0]](ctx, **(e[1] if len(e) > 1 else {})) if e else bench_signal(ctx)
    for f in spec.get('filters') or []:
        sig = with_filter(ctx, sig, FILTERS[f[0]](ctx, **(f[1] if len(f) > 1 else {})))
    sim = {'sl': 0.025, 'trail_gap': 0.015, 'entry_start': '09:30'}
    sim.update(spec.get('sim') or {})
    return trend, sig, sim


def run_spec(spec: dict, ctx: Ctx, leg_mode='longshort'):
    trend, sig, sim = build(spec, ctx)
    return lab.simulate_index(ctx.bars, ctx.short_lookup, trend, sig,
                              slip_ticks=0.5, long_tick=ctx.long_tick, short_tick=ctx.short_tick,
                              leg_mode=leg_mode, **sim)['trades']


# ─────────────────────────────────────────
# 가설 카탈로그 (패밀리 × 파라미터) — 새 아이디어는 여기에 추가
# ─────────────────────────────────────────

def _h(hid, family, title, why, spec, role='replace'):
    """role: replace(벤치 교체 후보 → 벤치 대비 paired-t로 판정)
             additive(독립 전략 → 단독 t·벤치와 상관으로 판정, 벤치와 병행 운용 후보)"""
    return {'id': hid, 'family': family, 'title': title, 'why': why, 'spec': spec, 'role': role}


def catalog() -> list:
    H = []
    # ── A. 게이트: 급락 지연(10-07~12 롱 6연속 손절, 게이트 4일 UP 유지) 대응
    for f, s in [(5, 10), (3, 10), (5, 20)]:
        H.append(_h(f'gate_ma_{f}_{s}', 'gate', f'빠른 일봉게이트 MA{f}/{s}',
                    '급락 시 MA10/20 전환이 늦어 역방향 매매 누적 → 빠른 게이트', {'gate': ['ma', {'fast': f, 'slow': s}]}))
    for n in (5, 10, 20):
        H.append(_h(f'gate_close_ma{n}', 'gate', f'전일종가 vs MA{n} 게이트',
                    '크로스보다 즉각적인 위치 기반 방향 판정', {'gate': ['close_ma', {'n': n}]}))
    for d1, d3 in [(0.03, 0.06), (0.04, 0.08), (0.05, 0.10)]:
        for sym in (True, False):
            H.append(_h(f'gate_crash_{int(d1*100)}_{int(d3*100)}_{"sym" if sym else "dn"}', 'gate',
                        f'급변 오버라이드 1일{d1:.0%}/3일{d3:.0%}' + (' 양방향' if sym else ' 하락만'),
                        'MA 게이트 유지 + 급락(급등) 직후엔 방향 강제 전환',
                        {'gate': ['crash', {'d1': d1, 'd3': d3, 'sym': sym}]}))
    for n in (5, 3):
        H.append(_h(f'gate_confirm_ma{n}', 'gate', f'MA10/20 + 종가vsMA{n} 일치일만',
                    '방향 불일치(전환기) 날은 쉬기', {'gate': ['confirm', {'n': n}]}))
    # ── B. 필터: 급락 후 반등 시도장에서 바닥 숏 손절 대응
    for k in (0.03, 0.05, 0.08, 0.12):
        H.append(_h(f'filt_stretch_{int(k*100)}', 'filter', f'과매도숏·과매수롱 회피(MA10 ±{k:.0%})',
                    '가격이 일봉 MA10에서 크게 벌어진 상태의 추격 진입이 반등에 털림',
                    {'filters': [['stretch', {'k': k}]]}))
    H.append(_h('filt_vwap', 'filter', 'VWAP 방향 일치(롱>VWAP, 숏<VWAP)', '일중 수급 방향과 맞는 돌파만',
                {'filters': [['vwap']]}))
    H.append(_h('filt_dayret', 'filter', '당일 방향 일치(롱>전일종가, 숏<전일종가)', '당일 흐름 역행 돌파 배제',
                {'filters': [['dayret']]}))
    for th in (0.5, 0.7):
        H.append(_h(f'filt_rangepos_{int(th*100)}', 'filter', f'장중 고저범위 위치 {th:.0%}',
                    '당일 범위 상단(하단)에서의 돌파만 — 추세 날 선별', {'filters': [['rangepos', {'th': th}]]}))
    for end in ('1100', '1300'):
        H.append(_h(f'filt_window_{end}', 'filter', f'진입 마감 {end[:2]}:{end[2:]}',
                    '오후 돌파는 EOD까지 시간이 짧아 기대값 낮을 수 있음', {'filters': [['window', {'end': end}]]}))
    H.append(_h('entry_state_short', 'filter', '갭다운 숏 상태진입(10-15 보류후보 재검증)',
                '09:30 첫 판정봉에서 이미 하단 밖이면 숏 — 데이터 추가로 재확인', {'entry': ['state_short']}))
    # ── C. 새 진입 패밀리 (독립 전략 → 벤치와 병행 가능성)
    for m in (15, 30, 60):
        for g in (True, False):
            H.append(_h(f'entry_orb{m}_{"gate" if g else "nogate"}', 'entry',
                        f'오프닝레인지 {m}분 돌파' + (' +게이트' if g else ' 게이트없음'),
                        '장 초반 범위 이탈 방향으로 추세가 이어짐(1일 1회)',
                        {'entry': ['orb', {'minutes': m, 'use_gate': g}],
                         'sim': {'entry_start': f"{9 + m // 60:02d}:{m % 60:02d}"}}, role='additive'))
    for g in (True, False):
        H.append(_h(f'entry_pdhl_{"gate" if g else "nogate"}', 'entry', '전일 고/저 돌파' + (' +게이트' if g else ''),
                    '전일 범위 이탈 = 새 정보 반영', {'entry': ['pdhl', {'use_gate': g}],
                                                    'sim': {'entry_start': '09:15'}}, role='additive'))
    for at in ('1300', '1400'):
        for th in (0.01, 0.02):
            H.append(_h(f'entry_tsmom_{at}_{int(th*100)}', 'entry', f'장중 모멘텀 {at[:2]}시 ±{th:.0%} 추종',
                        '당일 오전 흐름이 장 마감까지 이어지는 일중 모멘텀(학계 보고 효과)',
                        {'entry': ['tsmom', {'at': at, 'th': th}],
                         'sim': {'entry_start': f'{at[:2]}:{at[2:]}', 'trail_gap': None}}, role='additive'))
    for dr in (0.02, 0.03, 0.05):
        H.append(_h(f'entry_bounce_{int(dr*100)}', 'entry', f'급락일 반등 롱(전일비 −{dr:.0%} 후 하단 재진입)',
                    '급락 후 반등 시도장에서는 숏 대신 반등을 노림(역추세)',
                    {'entry': ['bounce', {'drop': dr}]}, role='additive'))
    for g in (0.015, 0.03):
        for mode in ('follow', 'fade'):
            H.append(_h(f'entry_gap_{int(g*1000)}_{mode}', 'entry', f'갭 {g:.1%} {"추종" if mode=="follow" else "역행"}',
                        '시가 갭의 연속/되돌림', {'entry': ['gap', {'g': g, 'mode': mode}]}, role='additive'))
    # ── D. 2026-10-24 주간 패밀리: 가격 파생이 아닌 '새 정보원'(캘린더·일봉 고저·수급·크로스마켓·메타)
    H.append(_h('cal_expiry_skip', 'calendar', '옵션만기일(둘째 목) 신규진입 금지',
                '만기일은 핀닝·마감 차익/프로그램 매물로 일중 추세가 왜곡돼 돌파가 휩쏘되기 쉬움',
                {'filters': [['expiry']]}))
    for last, first in [(1, 3), (2, 2)]:
        H.append(_h(f'cal_tom_noshort_{last}_{first}', 'calendar', f'월말{last}·월초{first}거래일 숏 금지',
                    '월 전환기 연기금·적립식 자금 유입으로 상승 편향(turn-of-month 효과) → 숏 기대값 낮음',
                    {'filters': [['tom', {'last': last, 'first': first}]]}))
    for th in (0.2, 0.33):
        H.append(_h(f'daily_clv_{int(th*100)}', 'daily_pattern', f'전일 종가위치(CLV) 역방향 차단 {th:.0%}',
                    '전일 고저 중 어디서 마감했는지(일봉 고저=미사용 정보)가 다음날 수급 방향을 예고',
                    {'filters': [['prior_clv', {'th': th}]]}))
    for q, mode in [(0.2, 'contra'), (0.1, 'contra'), (0.2, 'follow')]:
        H.append(_h(f'flow_ratio_{mode}_{int(q*100)}', 'flow',
                    f'레버/인버스 거래대금비 쏠림 {"역발상" if mode == "contra" else "추종"}(상하위 {q:.0%})',
                    '개인 레버·인버스 매매대금 비율 = 군중 포지셔닝(가격 외 정보원), 극단 쏠림 방향은 소진',
                    {'filters': [['flow', {'q': q, 'mode': mode}]]}))
    H.append(_h('peer_gate_agree', 'cross_market', '지수·코스닥 일봉게이트 동방향일만',
                '두 시장 추세가 일치해야 광범위한 추세(시장 폭) — 불일치일은 섹터 로테이션 휩쏘',
                {'gate': ['peer_agree']}))
    H.append(_h('peer_intraday_confirm', 'cross_market', '코스닥 당일 방향 동조 시에만 진입',
                '한 시장만의 돌파는 수급 국지적 → 피어 시장 동조 시 추세 지속성 높음(자기 dayret과 다른 정보)',
                {'filters': [['peer_intraday']]}))
    for k in (3, 4):
        H.append(_h(f'meta_streak_{k}', 'meta', f'벤치 섀도 {k}연속 손실 후 1거래일 휴식',
                    '연속 손절 = 현재 레짐이 전략과 불일치 → 하루 쉬어 휩쏘 꼬리 회피(10-07~12 롱 6연패 계열)',
                    {'gate': ['meta', {'mode': 'streak', 'k': k, 'rest': 1}]}))
    H.append(_h('meta_curve_20', 'meta', '에쿼티커브 필터(섀도 최근20건 누적<0이면 휴식)',
                '전략 자체 성과의 자기상관(엣지 레짐 지속성)을 이용 — 가격지표가 아닌 메타 정보',
                {'gate': ['meta', {'mode': 'curve', 'lb': 20}]}))
    return H


# 과거 세션에서 이미 검증·기각된 가설(메모리·연구로그 기준) — 원장 초기화 시 기록만 한다.
HISTORICAL = [
    ('hist_vol_gate', '변동성 레짐 게이트(저변동만 진입)', '기각', '고변동 구간=수익추세 구간이라 엣지 훼손 (2026-10-09)'),
    ('hist_htf_gate', '상위TF 추세정렬 게이트(MA20/60·10/40·5/20 추가정렬)', '기각', '거래만 줄고 개선 없음 (2026-10-09)'),
    ('hist_vol_sizing', '역변동성 사이징(vol 타게팅)', '기각', 'MDD↑·칼마↓ 역효과 (2026-10-09)'),
    ('hist_exit_grid', '청산 그리드(time-stop·breakeven·트레일 문턱·SL/트레일 폭)', '기각', '고원 — SL2.5/트레일1.5 견고 (2026-10-09)'),
    ('hist_entry_grid', '진입지표 그리드(볼린저·돈치안·MACD·RSI·VWAP·거래량필터)', '기각', '벤치 대비 paired-t<2, 리더 없음 (누적 1,561건)'),
    ('hist_ha_scalp', 'HA 플립 초단타(1분봉)', '기각', '비용0에도 음수, 방향정보 없음 (2026-10-08)'),
    ('hist_gate_scalp', '게이트+1분 스캘프', '기각', '비용이 엣지 잠식 (2026-10-08)'),
    ('hist_intraday_slope', '일중 3분 MA10 기울기 게이트', '기각', '휩쏘·과매매, 전구간 최하위 (2026-10-08)'),
    ('hist_ichimoku', '일목 전환/기준 게이트', '동급', 'MA10/20과 거의 동급, 개선 아님 (2026-10-08)'),
    ('hist_up_strengthen', '일봉 UP 정의 강화(MA20 상승·종가>MA10·MA10 상승·완전정배열)', '기각', '롱 건당 수익 악화 (2026-09-29)'),
    ('hist_bb34', 'BB34/0.7/09:00 챌린저', '기각', '월별 워크포워드에서 최근 국면 의존 (2026-10-04)'),
    ('hist_bb32', '구 지수 리더 BB32/0.9', '강등', '벤치 대비 paired-t 0.81 (2026-10-09)'),
    ('hist_bb29_11', 'BB29/1.1', '기각', '코스닥 전이 실패 (2026-10-09)'),
    ('hist_short_inverting', '숏-역전중(MA 재수렴) 숏 차단', '기각', 'paired-t +0.52, 홀드아웃 0.00 (2026-10-19)'),
    ('hist_gap_state_both', '갭 상태진입 롱+숏', '기각', 'paired-t −0.04, 롱 쪽 손해 (2026-10-15)'),
]
