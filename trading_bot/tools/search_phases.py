# -*- coding: utf-8 -*-
"""
지표 탐색 루프의 후보(candidate) 생성기.
run_indicator_search.py가 phase 이름을 받아 이 모듈에서 그리드를 생성한다.
각 phase는 고정된 그리드 목록이고, 마지막 phase("refine")만 현재 최고 후보 주변을
매번 새로 생성하는 국소 탐색(hill-climbing)이라 무한히 반복 가능하다.
"""
import random

TREND_MA_GRID = [(5, 15), (5, 20), (10, 30), (10, 20), (3, 15)]
ENTRY_GRID = ['09:00', '09:30', '10:00']

PHASES = [
    'macd_grid',
    'rsi_grid',
    'bollinger_grid',
    'bollinger_revert_grid',
    'stochastic_grid',
    'vwap_grid',
    'macd_vol_grid',
    'refine',
]


def _wrap(kind, params, fast_ma, slow_ma, entry_start, cid_prefix):
    cid = f"{cid_prefix}:{kind}:{params}:{fast_ma}-{slow_ma}:{entry_start}"
    return {
        'id': cid, 'kind': kind, 'params': params,
        'fast_ma': fast_ma, 'slow_ma': slow_ma, 'entry_start': entry_start,
        'sl': 0.025, 'trail_gap': 0.015,
    }


def gen_macd_grid():
    out = []
    for f, s, g in [(12, 26, 9), (8, 17, 9), (6, 14, 6), (5, 13, 6), (5, 13, 3), (4, 10, 4)]:
        for fm, sm in TREND_MA_GRID:
            for es in ENTRY_GRID:
                out.append(_wrap('macd', {'fast': f, 'slow': s, 'sig': g}, fm, sm, es, 'macd'))
    return out


def gen_rsi_grid():
    out = []
    for period in [7, 9, 14, 21]:
        for oversold, overbought in [(20, 80), (25, 75), (30, 70), (35, 65)]:
            for fm, sm in TREND_MA_GRID:
                for es in ENTRY_GRID:
                    out.append(_wrap('rsi', {'period': period, 'oversold': oversold, 'overbought': overbought},
                                      fm, sm, es, 'rsi'))
    return out


def gen_bollinger_grid():
    out = []
    for period in [10, 20, 30]:
        for mult in [1.5, 2.0, 2.5]:
            for fm, sm in TREND_MA_GRID:
                for es in ENTRY_GRID:
                    out.append(_wrap('bollinger', {'period': period, 'mult': mult}, fm, sm, es, 'bb'))
    return out


def gen_bollinger_revert_grid():
    out = []
    for period in [10, 20, 30]:
        for mult in [1.5, 2.0, 2.5]:
            for fm, sm in TREND_MA_GRID:
                for es in ENTRY_GRID:
                    out.append(_wrap('bollinger_revert', {'period': period, 'mult': mult}, fm, sm, es, 'bbr'))
    return out


def gen_stochastic_grid():
    out = []
    for k_period in [5, 9, 14, 21]:
        for d_period in [3, 5]:
            for low_th, high_th in [(15, 85), (20, 80), (25, 75)]:
                for fm, sm in TREND_MA_GRID:
                    for es in ENTRY_GRID:
                        out.append(_wrap('stochastic',
                                          {'k_period': k_period, 'd_period': d_period,
                                           'low_th': low_th, 'high_th': high_th},
                                          fm, sm, es, 'stoch'))
    return out


def gen_vwap_grid():
    out = []
    for dev_th in [0.002, 0.003, 0.005, 0.008, 0.012]:
        for fm, sm in TREND_MA_GRID:
            for es in ENTRY_GRID:
                out.append(_wrap('vwap', {'dev_th': dev_th}, fm, sm, es, 'vwap'))
    return out


def gen_macd_vol_grid():
    out = []
    for f, s, g in [(5, 13, 6), (8, 17, 9)]:
        for vol_mult in [1.2, 1.5, 2.0]:
            for vol_period in [10, 20]:
                for fm, sm in TREND_MA_GRID:
                    for es in ENTRY_GRID:
                        out.append(_wrap('macd_vol',
                                          {'fast': f, 'slow': s, 'sig': g,
                                           'vol_mult': vol_mult, 'vol_period': vol_period},
                                          fm, sm, es, 'macdvol'))
    return out


_GENERATORS = {
    'macd_grid': gen_macd_grid,
    'rsi_grid': gen_rsi_grid,
    'bollinger_grid': gen_bollinger_grid,
    'bollinger_revert_grid': gen_bollinger_revert_grid,
    'stochastic_grid': gen_stochastic_grid,
    'vwap_grid': gen_vwap_grid,
    'macd_vol_grid': gen_macd_vol_grid,
}


def _perturb_params(kind, params):
    """kind별 파라미터를 소폭 랜덤 변형 (hill-climbing 이웃 생성)."""
    p = dict(params)
    if kind in ('macd', 'macd_vol'):
        p['fast'] = max(2, p['fast'] + random.choice([-2, -1, 0, 1, 2]))
        p['slow'] = max(p['fast'] + 2, p['slow'] + random.choice([-3, -1, 0, 1, 3]))
        p['sig'] = max(2, p['sig'] + random.choice([-2, -1, 0, 1, 2]))
        if kind == 'macd_vol':
            p['vol_mult'] = round(max(1.0, p['vol_mult'] + random.choice([-0.3, -0.1, 0, 0.1, 0.3])), 2)
    elif kind == 'rsi':
        p['period'] = max(3, p['period'] + random.choice([-3, -1, 0, 1, 3]))
        p['oversold'] = max(5, min(45, p['oversold'] + random.choice([-5, -2, 0, 2, 5])))
        p['overbought'] = max(55, min(95, p['overbought'] + random.choice([-5, -2, 0, 2, 5])))
    elif kind in ('bollinger', 'bollinger_revert'):
        p['period'] = max(5, p['period'] + random.choice([-5, -2, 0, 2, 5]))
        p['mult'] = round(max(0.5, p['mult'] + random.choice([-0.3, -0.1, 0, 0.1, 0.3])), 2)
    elif kind == 'stochastic':
        p['k_period'] = max(3, p['k_period'] + random.choice([-3, -1, 0, 1, 3]))
        p['low_th'] = max(5, min(45, p['low_th'] + random.choice([-5, -2, 0, 2, 5])))
        p['high_th'] = max(55, min(95, p['high_th'] + random.choice([-5, -2, 0, 2, 5])))
    elif kind == 'vwap':
        p['dev_th'] = round(max(0.0005, p['dev_th'] + random.choice([-0.002, -0.001, 0, 0.001, 0.002])), 5)
    return p


def gen_refine_batch(best_candidate: dict, n: int = 60, seed: int = None):
    """현재 최고 후보 주변을 랜덤 국소탐색 — 매번 호출할 때마다 새 이웃집합을 생성하므로
    무한히 반복 호출 가능 (지속 탐색)."""
    if seed is not None:
        random.seed(seed)
    out = []
    kind = best_candidate['kind']
    for _ in range(n):
        params = _perturb_params(kind, best_candidate['params'])
        fm, sm = random.choice(TREND_MA_GRID)
        es = random.choice(ENTRY_GRID)
        out.append(_wrap(kind, params, fm, sm, es, 'refine'))
    return out


def generate_phase_candidates(phase_name: str, best_candidate: dict = None):
    if phase_name == 'refine':
        base = best_candidate or _wrap('macd', {'fast': 5, 'slow': 13, 'sig': 6}, 10, 30, '09:00', 'seed')
        return gen_refine_batch(base, n=80)
    gen = _GENERATORS.get(phase_name)
    if gen is None:
        raise ValueError(f'unknown phase: {phase_name}')
    return gen()
