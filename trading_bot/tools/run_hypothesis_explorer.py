# -*- coding: utf-8 -*-
"""가설 탐색 에이전트 — 매일 '새로운 종류의' 전략 가설을 채점하고 원장(ledger)에 누적한다.

run_index_search(16:20)가 데이터 갱신·벤치 채점을 끝낸 뒤 VM cron(16:40)으로 실행된다.
  1) 가설 카탈로그(hypothesis_lab.catalog) 중 미검증분 + 재검증 주기 도래분을 BATCH건 채점
  2) 판정: 교체형=벤치 대비 paired-t, 병행형=단독 t·벤치와 상관 → 1차 통과 시 코스닥 전이게이트
  3) 관찰/유망 가설은 이웃 파라미터로 자식 가설 생성(최대 2세대 — 데이터마이닝 억제)
  4) 원장 저장(data/research/hypothesis_ledger.json) + 텔레그램 요약(자동배포 없음)
실패 목록 보기: python tools/run_hypothesis_explorer.py --list
데이터는 갱신하지 않는다(토큰 사용 0) — 앞선 크론이 갱신한 CSV를 읽기만 한다."""
import argparse
import copy
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import indicator_lab as lab
import hypothesis_lab as hl
import run_indicator_search as sam
import run_index_search as ris

LEDGER_PATH = os.path.join(ris.DATA_DIR, "hypothesis_ledger.json")
BATCH = 12                 # 1회 채점 수(텔레그램 가독성·지속 탐색 페이스)
RETEST_DAYS = 7            # 관찰/유망 가설 재검증 주기(데이터가 쌓이면 판정이 바뀔 수 있음)
MAX_GENERATION = 2         # 이웃 확장 최대 세대
WATCH_T = 1.0              # 관찰 문턱
STATUS_ICON = {'promising': '🌟', 'watch': '👀', 'rejected': '❌', 'queued': '⏳', 'historical': '📜'}


# ─────────────────────────────────────────
# 원장
# ─────────────────────────────────────────

def load_ledger() -> dict:
    if os.path.exists(LEDGER_PATH):
        with open(LEDGER_PATH, encoding='utf-8') as f:
            return json.load(f)
    led = {'created': datetime.now().isoformat(), 'runs': 0, 'tested_total': 0, 'hypotheses': {}}
    for hid, title, verdict, note in hl.HISTORICAL:
        led['hypotheses'][hid] = {'id': hid, 'family': 'historical', 'title': title, 'why': '',
                                  'status': 'historical', 'verdict_note': f'{verdict} — {note}',
                                  'generation': 0, 'history': []}
    return led


def save_ledger(led: dict):
    tmp = LEDGER_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(led, f, ensure_ascii=False, indent=1)
    os.replace(tmp, LEDGER_PATH)


def sync_catalog(led: dict) -> int:
    added = 0
    for h in hl.catalog():
        if h['id'] not in led['hypotheses']:
            led['hypotheses'][h['id']] = {**h, 'status': 'queued', 'generation': 0, 'parent': None, 'history': []}
            added += 1
    return added


def pick_batch(led: dict, n: int) -> list:
    hs = led['hypotheses'].values()
    queued = sorted([h for h in hs if h['status'] == 'queued'], key=lambda h: (h['generation'], h['id']))
    cutoff = (datetime.now() - timedelta(days=RETEST_DAYS)).isoformat()
    due = [h for h in hs if h['status'] in ('watch', 'promising') and (h.get('last_tested') or '') < cutoff]
    return (queued + due)[:n]


# ─────────────────────────────────────────
# 채점
# ─────────────────────────────────────────

def _daily(trades):
    m = defaultdict(float)
    for t in trades:
        m[str(t['d'])] += t['ret']
    return m


def _corr(a: dict, b: dict, days):
    xs = [a.get(d, 0.0) for d in days]; ys = [b.get(d, 0.0) for d in days]
    n = len(xs)
    if n < 3:
        return 0.0
    mx, my = sum(xs) / n, sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sx = sum((x - mx) ** 2 for x in xs) ** .5; sy = sum((y - my) ** 2 for y in ys) ** .5
    return sxy / (sx * sy) if sx > 0 and sy > 0 else 0.0


def score(h: dict, ctx, bench_trades, days) -> dict:
    tr = hl.run_spec(h['spec'], ctx)
    hold = [t for t in tr if str(t['d']) >= lab.HOLDOUT_START]
    bhold = [t for t in bench_trades if str(t['d']) >= lab.HOLDOUT_START]
    s, sh = lab.trade_stats(tr), lab.trade_stats(hold)
    regs = lab.regime_stats(tr, lab.regime_boundaries(ctx.bars))
    rec = tr[-60:]
    out = {'n': s['n'], 'net': round(s['net_ret'], 4), 'win': round(s['win_rate'], 3), 't': round(s['t_stat'], 2),
           'hold_n': sh['n'], 'hold_net': round(sh['net_ret'], 4), 'hold_t': round(sh['t_stat'], 2),
           'rec60_net': round(sum(t['ret'] for t in rec), 4),
           'reg_pos': sum(1 for r in regs if r['net_ret'] > 0), 'reg_n': len(regs),
           'long_net': round(sum(t['ret'] for t in tr if t['dir'] == 'L'), 4),
           'short_net': round(sum(t['ret'] for t in tr if t['dir'] == 'S'), 4)}
    if h['role'] == 'replace':
        p, ph = lab.paired_diff_stats(tr, bench_trades), lab.paired_diff_stats(hold, bhold)
        out['paired_t'], out['paired_hold_t'] = round(p['t'], 2), round(ph['t'], 2)
        out['paired_day'] = round(p['mean_ret'], 5)
    else:
        out['corr_bench'] = round(_corr(_daily(tr), _daily(bench_trades), days), 2)
    return out


def primary_pass(h, m) -> bool:
    if m['n'] < 40:
        return False
    if h['role'] == 'replace':
        return m['paired_t'] >= sam.MIN_PAIRED_T and m['paired_hold_t'] > 0 and m['reg_pos'] >= m['reg_n'] - 1
    return m['t'] >= 2.0 and m['hold_net'] > 0 and m['corr_bench'] < 0.6 and m['reg_pos'] >= m['reg_n'] - 1


def watch_pass(h, m) -> bool:
    if m['n'] < 30:
        return False
    if h['role'] == 'replace':
        return m['paired_t'] >= WATCH_T and m['paired_hold_t'] > 0
    return m['t'] >= WATCH_T and m['hold_net'] > 0


_KQ = {}


def locked_gate(h) -> dict:
    """코스닥(탐색 미사용 독립 유니버스) 전이: 같은 spec이 거기서도 살아남는가."""
    if not all(os.path.exists(p) for p in (ris.LOCKED_LEV3, ris.LOCKED_LEVD, ris.LOCKED_INV3)):
        return {'available': False}
    if 'ctx' not in _KQ:
        _KQ['ctx'] = hl.Ctx(ris.LOCKED_LEV3, ris.LOCKED_LEVD, ris.LOCKED_INV3)
        _KQ['bench'] = hl.run_spec(hl.BENCH_SPEC, _KQ['ctx'])
    kc = _KQ['ctx']
    tr = hl.run_spec(h['spec'], kc)
    s = lab.trade_stats(tr)
    hn = sum(t['ret'] for t in tr if str(t['d']) >= lab.HOLDOUT_START)
    pt = lab.paired_diff_stats(tr, _KQ['bench'])['t'] if h['role'] == 'replace' else None
    ok = s['net_ret'] > 0 and hn > 0 and (pt is None or pt >= 0)
    return {'available': True, 'ok': bool(ok), 'net': round(s['net_ret'], 3), 'hold_net': round(hn, 3),
            'paired_t': None if pt is None else round(pt, 2), 'n': s['n']}


# ─────────────────────────────────────────
# 이웃 확장
# ─────────────────────────────────────────

def _perturb(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return []
    if isinstance(v, int):
        return sorted({max(1, round(v * 0.75)), round(v * 1.25)} - {v})
    return sorted({round(v * 0.75, 4), round(v * 1.25, 4)} - {v})


def expand(led: dict, h: dict) -> int:
    if h['generation'] >= MAX_GENERATION:
        return 0
    added = 0
    for part in ('gate', 'entry'):
        if h['spec'].get(part):
            name, params = h['spec'][part][0], (h['spec'][part][1] if len(h['spec'][part]) > 1 else {})
            for k, v in params.items():
                for nv in _perturb(v):
                    spec = copy.deepcopy(h['spec']); spec[part] = [name, {**params, k: nv}]
                    added += _add_child(led, h, spec, f'{k}={nv}')
    for fi, f in enumerate(h['spec'].get('filters') or []):
        params = f[1] if len(f) > 1 else {}
        for k, v in params.items():
            for nv in _perturb(v):
                spec = copy.deepcopy(h['spec']); spec['filters'][fi] = [f[0], {**params, k: nv}]
                added += _add_child(led, h, spec, f'{k}={nv}')
    return added


def _add_child(led, parent, spec, label) -> int:
    cid = f"{parent['id']}~{label}"
    if cid in led['hypotheses'] or any(x.get('spec') == spec for x in led['hypotheses'].values()):
        return 0
    led['hypotheses'][cid] = {**{k: parent[k] for k in ('family', 'why', 'role')},
                              'id': cid, 'title': f"{parent['title']} [{label}]", 'spec': spec,
                              'status': 'queued', 'generation': parent['generation'] + 1,
                              'parent': parent['id'], 'history': []}
    return 1


# ─────────────────────────────────────────
# 출력
# ─────────────────────────────────────────

def fmt_metric(h, m) -> str:
    if h['role'] == 'replace':
        core = f"벤치대비 paired-t {m['paired_t']:+.2f}(홀드 {m['paired_hold_t']:+.2f})"
    else:
        core = f"단독 t {m['t']:+.2f} 홀드 {m['hold_net']*100:+.0f}% 벤치상관 {m['corr_bench']:+.2f}"
    return f"{core} | {m['n']}건 net{m['net']*100:+.0f}% 최근60 {m['rec60_net']*100:+.0f}%"


def print_list(led: dict):
    groups = defaultdict(list)
    for h in led['hypotheses'].values():
        groups[h['status']].append(h)
    for st in ('promising', 'watch', 'queued', 'rejected', 'historical'):
        hs = groups.get(st) or []
        if not hs:
            continue
        print(f"\n{STATUS_ICON[st]} {st} ({len(hs)})")
        for h in sorted(hs, key=lambda x: x['id']):
            last = (h.get('history') or [{}])[-1].get('m')
            tail = fmt_metric(h, last) if last else h.get('verdict_note', '')
            print(f"  - {h['title']}  ·  {tail}")


# ─────────────────────────────────────────
# main
# ─────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--list', action='store_true', help='원장 출력만')
    ap.add_argument('--all', action='store_true', help='큐 전체를 한 번에 채점')
    ap.add_argument('--no-telegram', action='store_true')
    ap.add_argument('--data-dir', help='CSV 위치 재지정(로컬 검증용)')
    args = ap.parse_args()

    global LEDGER_PATH
    if args.data_dir:
        for k in ('CSV_LEV3', 'CSV_LEVD', 'CSV_INV3', 'LOCKED_LEV3', 'LOCKED_LEVD', 'LOCKED_INV3'):
            setattr(ris, k, os.path.join(args.data_dir, os.path.basename(getattr(ris, k))))
        LEDGER_PATH = os.path.join(args.data_dir, os.path.basename(LEDGER_PATH))

    led = load_ledger()
    if args.list:
        print_list(led)
        return
    added = sync_catalog(led)

    ctx = hl.Ctx(ris.CSV_LEV3, ris.CSV_LEVD, ris.CSV_INV3)
    bench = hl.run_spec(hl.BENCH_SPEC, ctx)
    days = sorted({b['d'] for b in ctx.bars})
    data_end = ctx.bars[-1]['d']
    print(f"[가설탐색] 데이터 ~{data_end} | 벤치 {len(bench)}건 | 카탈로그 신규 {added}건")

    batch = pick_batch(led, 10**6 if args.all else BATCH)
    results, expanded = [], 0
    for h in batch:
        try:
            m = score(h, ctx, bench, days)
        except Exception as e:
            print(f"  ! {h['id']} 실패: {e}", file=sys.stderr)
            continue
        lk = None
        if primary_pass(h, m):
            lk = locked_gate(h)
            st = 'promising' if (not lk.get('available') or lk.get('ok')) else 'watch'
        elif watch_pass(h, m):
            st = 'watch'
        else:
            st = 'rejected'
        h['status'] = st
        h['last_tested'] = datetime.now().isoformat()
        h['history'].append({'at': h['last_tested'], 'data_end': data_end, 'm': m, 'locked': lk})
        h['history'] = h['history'][-12:]
        led['tested_total'] += 1
        if st in ('watch', 'promising'):
            expanded += expand(led, h)
        results.append((h, m, lk))
        print(f"  {STATUS_ICON[st]} {h['title']} | {fmt_metric(h, m)}")

    led['runs'] += 1
    led['last_run_at'] = datetime.now().isoformat()
    save_ledger(led)

    cnt = defaultdict(int)
    for h in led['hypotheses'].values():
        cnt[h['status']] += 1
    lines = [
        "🧪 <b>가설 탐색 에이전트</b>",
        f"데이터 ~{data_end} | 이번 {len(results)}건 · 누적 {led['tested_total']}건 | 이웃확장 +{expanded}",
        f"원장: 🌟유망 {cnt['promising']} · 👀관찰 {cnt['watch']} · ❌기각 {cnt['rejected']} · ⏳대기 {cnt['queued']}"
        f" · 📜과거기각 {cnt['historical']}",
    ]
    order = {'promising': 0, 'watch': 1, 'rejected': 2}
    for h, m, lk in sorted(results, key=lambda r: order[r[0]['status']])[:12]:
        line = f"{STATUS_ICON[h['status']]} {h['title']}\n   └ {fmt_metric(h, m)}"
        if lk and lk.get('available'):
            line += f"\n   └ 🔒코스닥 {'통과' if lk['ok'] else '실패'} net{lk['net']*100:+.0f}% 홀드{lk['hold_net']*100:+.0f}%"
        lines.append(line)
    if any(h['status'] == 'promising' for h, _, _ in results):
        lines.append("🌟는 후보일 뿐 — 워크포워드·모의 검증 전 적용 금지(자동배포 없음).")
    if not batch:
        lines.append("⚠️ 대기 가설 고갈 — hypothesis_lab.catalog()에 새 패밀리 추가 필요.")
    lines.append(f"(다중검정 주의: 누적 {led['tested_total']}건이면 우연한 t≥2가 ~{led['tested_total']*0.023:.0f}건 나올 수 있음)")
    msg = "\n".join(lines)
    print(msg)
    if not args.no_telegram:
        try:
            sam.send_telegram(msg)
        except Exception as e:
            print(f"텔레그램 실패: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
