# -*- coding: utf-8 -*-
"""지수 레버리지(dual-ETF) 지표 탐색 자동 루프 — 삼성 run_indicator_search의 지수 버전.

삼성과 완전 병행·격리(상태·마스터·벤치·리더보드 별도). 매일 장마감 후 cron이 호출한다.
1) 캐시토큰 재사용(재발급 0)으로 122630 3분봉/일봉 + 114800 3분봉 증분 조회 → 마스터 병합
2) data/research/index_search_state.json 로드 (최초엔 라이브 지수 배포판을 벤치마크로)
3) 현재 phase 큐에서 배치 백테스트(dual-ETF 현실모델, dt정합·실측틱·슬리피지) → 리더보드
4) 4관문(t·홀드아웃·국면·총익) 통과 새 후보 시 텔레그램 상세 (배포 안 함, best_found 기록만)
5) 진행 요약 + 벤치마크 롱숏 vs 롱온리 비교(숏 유지 가치) 텔레그램

2026-10-07 정합버그 수정 반영: indicator_lab.simulate_index(dt정합) 사용. 삼성 대칭 2x
모델과 분리. 숏레그 중립 드러남 → leg_mode phase로 롱온리 대안을 탐색에 포함.
"""
import json
import os
import sys
from datetime import datetime

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_DIR, "tools"))
import indicator_lab as lab
import index_search_phases as isp
import run_indicator_search as sam   # 재사용: ssh/scp/_merge_csv/텔레그램/게이트/리더보드/상수

LEV_CODE = "122630"   # 롱 ETF 겸 신호원 (config.LEVERAGE_INDEX_LEV_CODE와 일치)
INV_CODE = "114800"   # 숏 ETF (config.LEVERAGE_INDEX_INV_CODE와 일치)

DATA_DIR = os.path.join(REPO_DIR, "data", "research")
CSV_LEV3 = os.path.join(DATA_DIR, "index_lev_3min_master.csv")
CSV_LEVD = os.path.join(DATA_DIR, "index_lev_daily_master.csv")
CSV_INV3 = os.path.join(DATA_DIR, "index_inv_3min_master.csv")
STATE_PATH = os.path.join(DATA_DIR, "index_search_state.json")

# Phase4(2026-10-08): 유니버스-외 '잠긴' 최종 게이트 — 코스닥 dual-ETF(탐색이 건드리지 않는
# 독립 유니버스). read-only. 리더 후보가 나왔을 때만 GO/NO-GO로 평가(드물게).
LOCKED_LEV3 = os.path.join(DATA_DIR, "kosdaq_lev_3min_master.csv")   # 233740 코스닥150레버리지
LOCKED_INV3 = os.path.join(DATA_DIR, "kosdaq_inv_3min_master.csv")   # 251340 코스닥150선물인버스
LOCKED_LEVD = os.path.join(DATA_DIR, "kosdaq_lev_daily_master.csv")
BENCH_HEALTH_CSV = os.path.join(DATA_DIR, "bench_health_index.csv")   # 벤치 건강검진 시계열

BATCH_SIZE = 300
DEPLOYED_DATE = "2026-10-07"   # 지수 전략 모의 활성화일

# VM(.env)에 실제 배포된 라이브 지수 구성과 일치해야 함.
BENCHMARK_CANDIDATE = {
    'id': 'idx:benchmark:live', 'kind': 'bollinger', 'params': {'period': 29, 'mult': 1.0},
    'fast_ma': 10, 'slow_ma': 20, 'entry_start': '09:30', 'sl': 0.025, 'trail_gap': 0.015,
    'leg_mode': 'longshort', 'slip_ticks': isp.SLIP_TICKS,
    'label': f'라이브 지수 BB29/1.0 롱숏 롱{LEV_CODE}/숏{INV_CODE} ({DEPLOYED_DATE}~)',
}


# ─────────────────────────────────────────
# 1) 데이터 증분 갱신 (캐시토큰 재사용, 재발급 0)
# ─────────────────────────────────────────

def refresh_master_data() -> bool:
    script = f"""
import sys, logging
sys.path.insert(0, '{sam.VM_DIR}')
import os
os.chdir('{sam.VM_DIR}')
logging.disable(logging.CRITICAL)
from datetime import datetime
from kiwoom_api import KiwoomAPI
k = KiwoomAPI()
# 캐시토큰 재사용만 — 유효 토큰 없으면 조회 실패로 끝내고 재발급은 절대 안 한다.
if not (getattr(k, '_token', None) and datetime.now() < k._token_expires):
    print('NO_TOKEN'); sys.exit(0)
d = k.get_minute_data('{LEV_CODE}', tick_range=3, count=100, paginate=True, max_pages=8)
if d is not None:
    d.to_csv('/tmp/_idx_lev3.csv', index=False); print('LEV3_OK', len(d))
else: print('LEV3_FAIL')
dd = k.get_daily_data('{LEV_CODE}', count=250)
if dd is not None:
    dd.to_csv('/tmp/_idx_levd.csv', index=False); print('LEVD_OK', len(dd))
else: print('LEVD_FAIL')
iv = k.get_minute_data('{INV_CODE}', tick_range=3, count=100, paginate=True, max_pages=8)
if iv is not None:
    iv.to_csv('/tmp/_idx_inv3.csv', index=False); print('INV3_OK', len(iv))
else: print('INV3_FAIL')
"""
    local_tmp = os.path.join(REPO_DIR, "_tmp_idx_fetch.py")
    with open(local_tmp, "w", encoding="utf-8") as f:
        f.write(script)
    try:
        sam.scp_to_vm(local_tmp, "/tmp/_idx_fetch.py")
        out = sam.ssh(f"{sam.VM_PY} /tmp/_idx_fetch.py", timeout=180)
        oks = {}
        for tag, remote, master, key in [
            ("LEV3_OK", "/tmp/_idx_lev3.csv", CSV_LEV3, "datetime"),
            ("LEVD_OK", "/tmp/_idx_levd.csv", CSV_LEVD, "date"),
            ("INV3_OK", "/tmp/_idx_inv3.csv", CSV_INV3, "datetime"),
        ]:
            if tag in out:
                tmp_local = os.path.join(REPO_DIR, f"_tmp_{tag}.csv")
                sam.scp_from_vm(remote, tmp_local)
                sam._merge_csv(tmp_local, master, key_col=key)
                os.remove(tmp_local)
                oks[tag] = True
        return len(oks) == 3
    except Exception as e:
        print(f"[refresh] 실패: {e}", file=sys.stderr)
        return False
    finally:
        if os.path.exists(local_tmp):
            os.remove(local_tmp)


# ─────────────────────────────────────────
# 2) 상태 관리
# ─────────────────────────────────────────

def load_state(long_bars, short_lookup, daily, long_tick, short_tick) -> dict:
    bench_result = lab.evaluate_index_candidate(BENCHMARK_CANDIDATE, long_bars, short_lookup, daily,
                                                long_tick=long_tick, short_tick=short_tick)
    benchmark = {**BENCHMARK_CANDIDATE, "result": bench_result}
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            state = json.load(f)
        state["benchmark"] = benchmark
        state["phases"] = isp.PHASES
        return state
    return {
        "created_at": datetime.now().isoformat(), "last_run_at": None,
        "run_count": 0, "tested_count": 0,
        "benchmark": benchmark, "best_found": None,
        "phase_index": 0, "phases": isp.PHASES, "queue": [], "leaderboard": [],
    }


def current_phase(state):
    return state["phases"][min(state["phase_index"], len(state["phases"]) - 1)]


def refill_queue_if_empty(state):
    if state["queue"]:
        return
    best = state["best_found"]["candidate"] if state["best_found"] else state["benchmark"]
    state["queue"] = isp.generate_phase_candidates(current_phase(state), best_candidate=best)


def advance_phase_if_needed(state):
    if not state["queue"] and state["phase_index"] < len(state["phases"]) - 1:
        state["phase_index"] += 1


def fmt_candidate(c):
    lm = c.get('leg_mode', 'longshort')
    return (f"{c['kind']}{c['params']} 추세MA{c['fast_ma']}/{c['slow_ma']} 진입{c['entry_start']} "
            f"SL{c['sl']*100:.1f}%/트레일{(c['trail_gap'] or 0)*100:.1f}% [{lm}]")


def passes_locked_gate(candidate: dict) -> dict:
    """Phase4: 유니버스-외(코스닥) 잠금 최종 게이트. 코스피 탐색의 다중검정에 한 번도
    오염되지 않은 독립 유니버스에서 후보가 '전이(transfer)'되는지 GO/NO-GO로만 본다.
    코스닥은 변동성이 커 t가 낮으므로 '벤치 초과'가 아니라 '방향·생존'으로 보정 판정:
      전구간 net>0 + 홀드아웃 net>0 + 국면 과반 양(+) + 코스닥 벤치 대비 paired-t≥0.
    read-only(탐색·튜닝에 절대 사용 안 함). 리더 후보일 때만 호출(드물게)."""
    if not (os.path.exists(LOCKED_LEV3) and os.path.exists(LOCKED_INV3) and os.path.exists(LOCKED_LEVD)):
        return {"available": False}
    kl, kd = lab.load_master_data(LOCKED_LEV3, LOCKED_LEVD)
    ksl = lab.load_short_lookup(LOCKED_INV3)
    klt, kst = lab.detect_tick(LOCKED_LEV3), lab.detect_tick(LOCKED_INV3)
    r = lab.evaluate_index_candidate(candidate, kl, ksl, kd, long_tick=klt, short_tick=kst)
    kb = lab.evaluate_index_candidate(BENCHMARK_CANDIDATE, kl, ksl, kd, long_tick=klt, short_tick=kst)
    pd = lab.paired_diff_stats(r.get("trades") or [], kb.get("trades") or [])
    regs = r.get("regimes") or []
    reg_pos = sum(1 for x in regs if x.get("net_pnl_krw", 0.0) > 0)
    hnet = (r.get("holdout") or {}).get("net_ret", 0.0)
    ok = (r.get("net_ret", 0.0) > 0 and hnet > 0
          and reg_pos >= (len(regs) - 1 if regs else 1) and pd["t"] >= 0.0)
    return {"available": True, "ok": bool(ok), "net": r.get("net_ret", 0.0),
            "t": r.get("t_stat", 0.0), "hold_net": hnet,
            "reg_pos": reg_pos, "reg_n": len(regs), "paired_t": pd["t"]}


# ─────────────────────────────────────────
# main
# ─────────────────────────────────────────

def main():
    print("[1/5] 데이터 갱신 중...")
    refreshed = refresh_master_data()
    print(f"  -> {'성공' if refreshed else '실패/부분(기존 데이터로 진행)'}")

    if not (os.path.exists(CSV_LEV3) and os.path.exists(CSV_LEVD) and os.path.exists(CSV_INV3)):
        print("status=error stop=false  (지수 마스터 데이터 없음 — 최초 1회 수동 확보 필요)")
        return

    long_bars, daily = lab.load_master_data(CSV_LEV3, CSV_LEVD)
    short_lookup = lab.load_short_lookup(CSV_INV3)
    long_tick = lab.detect_tick(CSV_LEV3)
    short_tick = lab.detect_tick(CSV_INV3)
    print(f"[2/5] 롱봉 {len(long_bars)}개 ({long_bars[0]['d']}~{long_bars[-1]['d']}), 일봉 {len(daily)}, "
          f"숏조회 {len(short_lookup)} | 틱 롱{long_tick}/숏{short_tick}")

    state = load_state(long_bars, short_lookup, daily, long_tick, short_tick)
    state["run_count"] += 1

    # Phase1: 인컴번트(기존 리더)를 현재 데이터로 재채점 → 벤치마크와 같은 선상에서 paired 비교.
    # 재채점 후 새 기준(paired-t)을 못 넘으면 강등(노이즈 크라운으로 판명).
    demoted = None
    if state.get("best_found"):
        try:
            state["best_found"]["result"] = lab.evaluate_index_candidate(
                state["best_found"]["candidate"], long_bars, short_lookup, daily,
                long_tick=long_tick, short_tick=short_tick)
            if not sam.passes_core_gates(state, state["best_found"]["result"]):
                demoted = state["best_found"]
                state["best_found"] = None
        except Exception:
            pass

    print(f"[3/5] phase={current_phase(state)} 큐={len(state['queue'])}건, 누적테스트={state['tested_count']}건")
    refill_queue_if_empty(state)

    batch = state["queue"][:BATCH_SIZE]
    state["queue"] = state["queue"][BATCH_SIZE:]

    tested_today = 0
    new_leader = None
    locked_reject = None   # Phase1 관문은 통과했으나 유니버스-외 전이에 실패해 보류된 후보
    for cand in batch:
        try:
            result = lab.evaluate_index_candidate(cand, long_bars, short_lookup, daily,
                                                  long_tick=long_tick, short_tick=short_tick)
        except Exception:
            continue
        tested_today += 1
        state["tested_count"] += 1
        sam.update_leaderboard(state, cand, result)
        if sam.is_new_leader(state, result):
            # Phase4: 유니버스-외(코스닥) 잠금 게이트 — 리더 후보일 때만 평가(드물게).
            locked = passes_locked_gate(cand)
            if locked.get("available"):
                state["locked_gate_queries"] = state.get("locked_gate_queries", 0) + 1
            if locked.get("available") and not locked.get("ok"):
                locked_reject = {"candidate": cand, "result": result, "locked": locked}
                continue   # 코스닥 전이 실패 → 리더로 승격하지 않음
            state["best_found"] = {"candidate": cand, "result": result,
                                   "found_at": datetime.now().isoformat(), "reported": False,
                                   "locked_gate": locked}
            new_leader = state["best_found"]

    advance_phase_if_needed(state)
    state["last_run_at"] = datetime.now().isoformat()
    # 벤치 절대성능 건강검진 — strip(=trades 제거) 전에 계산해야 최근창 산출 가능.
    health = lab.bench_health(state["benchmark"]["result"], recent_n=sam.RECENT_N, min_full_t=sam.MIN_T_STAT)
    sam.append_bench_health_csv(BENCH_HEALTH_CSV, health, long_bars[-1]['d'])
    sam.strip_trades_for_persist(state)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    print(f"[4/5] 이번 회차 테스트 {tested_today}건, 누적 {state['tested_count']}건")

    bench = state["benchmark"]["result"]
    bh = bench.get("holdout") or {}
    bl = bench.get("long") or {}
    bs = bench.get("short") or {}
    # 벤치마크를 롱온리로도 평가 — 숏 유지 가치 지속 점검
    bench_lo = lab.evaluate_index_candidate({**BENCHMARK_CANDIDATE, 'leg_mode': 'longonly'},
                                            long_bars, short_lookup, daily,
                                            long_tick=long_tick, short_tick=short_tick)

    lines = [
        "🔬 <b>지수 레버리지 지표탐색 루프</b>",
        f"phase: {current_phase(state)} | 이번회차: {tested_today}건 | 누적: {state['tested_count']}건",
        f"벤치(롱숏): {bench['n']}건 승률{bench['win_rate']*100:.1f}% "
        f"순익{bench['net_ret']*100:+.1f}%({bench['net_pnl_krw']:+,.0f}원) t={bench.get('t_stat',0):.2f}",
        f"└ 레그분해: 롱 {bl.get('n',0)}건 {bl.get('net_ret',0)*100:+.1f}% / "
        f"숏 {bs.get('n',0)}건 {bs.get('net_ret',0)*100:+.1f}%(승률{bs.get('win_rate',0)*100:.0f}%)",
        f"└ 홀드아웃: {bh.get('n',0)}건 {bh.get('net_pnl_krw',0):+,.0f}원 t={bh.get('t_stat',0):.2f}",
        f"🆚 롱온리 벤치: {bench_lo['n']}건 순익{bench_lo['net_ret']*100:+.1f}%"
        f"({bench_lo['net_pnl_krw']:+,.0f}원) t={bench_lo.get('t_stat',0):.2f} "
        f"→ {'숏이 기여' if bench['net_pnl_krw']>bench_lo['net_pnl_krw'] else '롱온리가 우위(숏 불필요)'}",
    ]
    htag = "⚠️" if health['alerts'] else "✅"
    lines.append(
        f"{htag} 벤치 건강검진: 최근{health['rec_n']}건 net{health['rec_net']*100:+.1f}%·t{health['rec_t']:.2f}"
        f" | 전구간 t{health['full_t']:.2f} | 홀드 t{health['hold_t']:.2f}"
    )
    if health['alerts']:
        lines.append("   └ 🔻<b>벤치 쇠퇴경보</b>: " + " · ".join(health['alerts'])
                     + " (교체/중단은 사람 판단 — 자동개입 안 함)")
    if new_leader:
        c, r = new_leader["candidate"], new_leader["result"]
        rh = r.get("holdout") or {}
        pv = r.get("paired_vs_bench") or {}
        pvh = r.get("paired_vs_bench_hold") or {}
        lk = new_leader.get("locked_gate") or {}
        lines += [
            "", "🎉 <b>벤치마크를 넘는 새 지수 후보!</b>", fmt_candidate(c),
            f"→ 전구간 {r['n']}건 승률{r['win_rate']*100:.1f}% 순익{r['net_ret']*100:+.1f}%"
            f"({r['net_pnl_krw']:+,.0f}원) t={r.get('t_stat',0):.2f}",
            f"→ 홀드아웃 {rh.get('n',0)}건 {rh.get('net_pnl_krw',0):+,.0f}원 t={rh.get('t_stat',0):.2f}",
            f"→ 벤치 대비 짝지은차이: 전구간 t={pv.get('t',0):.2f} / 홀드 t={pvh.get('t',0):.2f} "
            f"(일당 {pv.get('mean_krw',0):+,.0f}원)",
        ]
        if lk.get("available"):
            lines.append(f"→ 🔒 코스닥 전이게이트 통과: net{lk.get('net',0)*100:+.0f}% 홀드{lk.get('hold_net',0)*100:+.0f}% "
                         f"국면{lk.get('reg_pos',0)}/{lk.get('reg_n',0)} paired-t{lk.get('paired_t',0):+.2f}")
        else:
            lines.append("→ 🔒 코스닥 전이게이트: 데이터 없음(미평가) — 승격은 했으나 수동 확인 요망")
        lines.append("적용하려면 세션에서 \"적용해줘\"라고 하세요 (자동배포 안 함).")
    elif locked_reject:
        c, lk = locked_reject["candidate"], locked_reject["locked"]
        lines += [
            "", "⚠️ <b>Phase1 관문 통과했으나 코스닥 전이 실패로 보류</b>", fmt_candidate(c),
            f"→ 코스닥: net{lk.get('net',0)*100:+.0f}% 홀드{lk.get('hold_net',0)*100:+.0f}% "
            f"국면{lk.get('reg_pos',0)}/{lk.get('reg_n',0)} paired-t{lk.get('paired_t',0):+.2f} "
            f"→ 유니버스-외 전이 안 됨(과적합 의심), 리더 승격 안 함.",
        ]
    if demoted:
        dp = (demoted.get("result") or {}).get("paired_vs_bench") or {}
        lines += [
            "", "📉 <b>기존 리더 강등</b> — 새 기준(paired-t)에서 벤치마크 대비 유의성 상실",
            f"{fmt_candidate(demoted['candidate'])} → 벤치 대비 paired-t={dp.get('t',0):.2f}(<{sam.MIN_PAIRED_T}) "
            f"= 고원 위 노이즈로 판명, 현행 벤치 유지.",
        ]

    print("[5/5] 텔레그램 전송 중...")
    try:
        sam.send_telegram("\n".join(lines))
        print("  -> 성공")
    except Exception as e:
        print(f"  -> 실패: {e}", file=sys.stderr)

    if new_leader:
        state["best_found"]["reported"] = True
        sam.strip_trades_for_persist(state)
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)

    print(f"status=in_progress stop=false leader={'yes' if new_leader else 'no'}")


if __name__ == "__main__":
    main()
