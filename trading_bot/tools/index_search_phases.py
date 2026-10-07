# -*- coding: utf-8 -*-
"""지수 레버리지(dual-ETF) 지표 탐색 후보 생성기.

삼성 search_phases.py의 지표 generator를 재사용하되(신호 계산은 지표 무관),
지수 특성에 맞게 phase 순서를 조정하고 모든 후보에 leg_mode(롱숏/롱온리)·slip_ticks를
스탬프한다. run_index_search.py가 phase 이름으로 이 모듈을 호출한다.

2026-10-07: 정합 버그 수정 후 숏레그가 사실상 중립으로 드러나(=실질 롱온리),
'leg_mode' phase에서 현재 최고 후보를 롱숏/롱온리 두 모드로 직접 비교해 숏 유지 가치를
지속 재검증한다.
"""
import copy
import search_phases as sp

SLIP_TICKS = 0.5   # 측당 슬리피지(틱) — 지수 분석 전체의 현실 기준

PHASES = [
    'bollinger_grid',          # 지수는 저변동 → 볼린저 우선 (mult 1.5~2.5)
    'bollinger_lowmult_grid',  # 안정형 BB29/1.0 주변 (mult 0.5~1.3)
    'donchian_grid',           # 지수 추세구간 채널돌파
    'macd_grid',
    'rsi_grid',
    'vwap_grid',
    'macd_vol_grid',
    'bollinger_vol_grid',
    'exit_grid_index',         # 지수 벤치/공격형 base로 SL·트레일 탐색(trailNone 포함)
    'leg_mode',                # 롱숏 vs 롱온리 — 숏 유지 가치 검증
    'refine',                  # 최고 후보 주변 무한 국소탐색
]


def _stamp(cands, leg_mode='longshort'):
    for c in cands:
        c['leg_mode'] = leg_mode
        c['slip_ticks'] = SLIP_TICKS
        c['id'] = f"idx:{c['id']}:{leg_mode}"
    return cands


def gen_exit_grid_index():
    """지수 벤치(BB29/1.0)·공격형(BB40/1.6) base로 SL/트레일 전수 탐색."""
    bases = [
        ('bollinger', {'period': 29, 'mult': 1.0}, 10, 20, '09:30'),  # 라이브 벤치
        ('bollinger', {'period': 40, 'mult': 1.6}, 10, 20, '09:30'),  # 공격형 후보
    ]
    out = []
    for kind, params, fm, sm, es in bases:
        for sl in [0.02, 0.025, 0.03, 0.035, 0.04]:
            for trail in [0.01, 0.015, 0.02, 0.025, 0.03, None]:
                out.append(sp._wrap(kind, params, fm, sm, es, 'xidx', sl=sl, trail_gap=trail))
    return out


def gen_leg_mode(best_candidate):
    """현재 최고 후보를 롱숏/롱온리 두 모드로 비교(숏 레그가 수익에 기여하는지)."""
    base = best_candidate or sp._wrap('bollinger', {'period': 29, 'mult': 1.0}, 10, 20, '09:30', 'seed')
    out = []
    for lm in ('longshort', 'longonly'):
        c = copy.deepcopy(base)
        c['leg_mode'] = lm
        c['slip_ticks'] = SLIP_TICKS
        c['id'] = f"idx:legmode:{c.get('kind')}{c.get('params')}:{c.get('entry_start')}:{lm}"
        out.append(c)
    return out


def generate_phase_candidates(phase_name: str, best_candidate: dict = None):
    if phase_name == 'refine':
        base = best_candidate or sp._wrap('bollinger', {'period': 29, 'mult': 1.0}, 10, 20, '09:30', 'seed')
        lm = best_candidate.get('leg_mode', 'longshort') if best_candidate else 'longshort'
        return _stamp(sp.gen_refine_batch(base, n=80), leg_mode=lm)
    if phase_name == 'exit_grid_index':
        return _stamp(gen_exit_grid_index(), leg_mode='longshort')
    if phase_name == 'leg_mode':
        return gen_leg_mode(best_candidate)   # 이미 leg_mode/slip 스탬프됨
    # 나머지 지표 phase는 삼성 generator 재사용 (전부 롱숏으로 스탬프)
    cands = sp.generate_phase_candidates(phase_name, best_candidate=best_candidate)
    return _stamp(cands, leg_mode='longshort')
