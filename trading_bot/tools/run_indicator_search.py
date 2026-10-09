# -*- coding: utf-8 -*-
"""
레버리지 전략 지표 탐색 자동 루프.

매일 장마감 후 스케줄 작업이 이 스크립트를 호출한다.
1) VM에서 삼성전자 3분봉(최근 구간)/일봉 증분 데이터 조회 -> data/research 마스터 CSV에 병합
2) data/research/search_state.json 로드 (최초 실행이면 현재 라이브 배포판을 벤치마크로 초기화)
3) 현재 phase의 대기열에서 배치만큼 후보를 백테스트 -> 리더보드 갱신
4) 벤치마크 + 기존 최고기록을 유의미하게 넘는 새 후보가 나오면 텔레그램 상세 보고
   (배포는 절대 하지 않음 — data/research/search_state.json에 best_found로만 기록)
5) 오늘 진행상황을 짧게 텔레그램 요약
6) 현재 phase 대기열 소진 시 다음 phase 생성. 마지막 phase("refine")는 최고 후보
   주변 국소탐색을 매번 새로 생성하므로 무한 반복 가능 — 이 태스크는 계속 활성 상태로 둔다.

사용: python tools/run_indicator_search.py
"""
import csv
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_DIR, "tools"))
import indicator_lab as lab
import search_phases as sp

SSH_KEY = os.path.expanduser("~/.ssh/oracle_key")
VM_HOST = "ubuntu@168.107.18.129"
VM_DIR = "/home/ubuntu/autoTrade/trading_bot"
VM_PY = "/home/ubuntu/venv/bin/python"

DATA_DIR = os.path.join(REPO_DIR, "data", "research")
CSV_3MIN = os.path.join(DATA_DIR, "samsung_3min_master.csv")
CSV_DAILY = os.path.join(DATA_DIR, "samsung_daily_master.csv")
STATE_PATH = os.path.join(DATA_DIR, "search_state.json")

BATCH_SIZE = 300
MIN_TRADES_FOR_LEADER = 200
MIN_T_STAT = 2.0            # 전구간 거래당 수익률 t값 최소치(바닥)
MIN_HOLDOUT_TRADES = 60     # 홀드아웃 구간 최소 거래수
MIN_PAIRED_T = 2.0          # Phase1(2026-10-08): 벤치마크 대비 '일자별 차이' t값 최소치
RECENT_N = 60               # 벤치 건강검진: 최근창(벤치 거래) 표본 크기
BENCH_HEALTH_CSV = os.path.join(DATA_DIR, "bench_health_samsung.csv")
DEPLOYED_DATE = "2026-09-29"     # 현재 라이브 배포판 적용일 (벤치마크 라벨용)

# VM(.env + config.py)에 실제 배포된 값과 일치해야 함 — 어긋나면 탐색이 구버전 기준으로 비교하게 된다.
# 2026-09-29: 국면분리 관문 통과 확인 후 BB27/1.1/09:30 -> BB29/0.8/10:00로 교체 배포.
# 물량은 관찰기간 동안 축소(LEVERAGE_AMOUNT 300만->30만원, .env) — 정상화 시 벤치마크는 안 바뀜.
BENCHMARK_CANDIDATE = {
    'id': 'benchmark:live', 'kind': 'bollinger', 'params': {'period': 29, 'mult': 0.8},
    'fast_ma': 10, 'slow_ma': 20, 'entry_start': '10:00', 'sl': 0.025, 'trail_gap': 0.015,
    'label': f'현재 라이브 배포판 BOLLINGER ({DEPLOYED_DATE}~)',
}


ON_VM = os.path.abspath(REPO_DIR) == VM_DIR
"""VM에서 직접 실행 중인지. 이 경우 SSH/SCP는 로컬 실행·복사로 대체되어,
같은 스크립트가 로컬 PC(원격 조회)와 VM(직접 조회) 양쪽에서 그대로 동작한다."""


def ssh(cmd: str, timeout: int = 90) -> str:
    if ON_VM:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout)
        return r.stdout
    r = subprocess.run(["ssh", "-i", SSH_KEY, VM_HOST, cmd], capture_output=True, text=True, timeout=timeout)
    return r.stdout


def scp_from_vm(remote_path: str, local_path: str):
    if ON_VM:
        shutil.copyfile(remote_path, local_path)
        return
    subprocess.run(["scp", "-i", SSH_KEY, "-q", f"{VM_HOST}:{remote_path}", local_path],
                    check=True, timeout=60)


def scp_to_vm(local_path: str, remote_path: str):
    if ON_VM:
        shutil.copyfile(local_path, remote_path)
        return
    subprocess.run(["scp", "-i", SSH_KEY, "-q", local_path, f"{VM_HOST}:{remote_path}"],
                    check=True, timeout=60)


# ─────────────────────────────────────────
# 1) 데이터 증분 갱신
# ─────────────────────────────────────────

def refresh_master_data() -> bool:
    """VM에서 최근 구간 데이터를 받아 기존 마스터 CSV에 병합. 실패해도 예외 없이 False 반환."""
    script = f"""
import sys, logging
sys.path.insert(0, '{VM_DIR}')
import os
os.chdir('{VM_DIR}')
logging.disable(logging.CRITICAL)
from kiwoom_api import KiwoomAPI
k = KiwoomAPI()
df = k.get_minute_data('005930', tick_range=3, count=100, paginate=True, max_pages=8)
if df is not None:
    df.to_csv('/tmp/_search_3min.csv', index=False)
    print('3MIN_OK', len(df))
else:
    print('3MIN_FAIL')
daily = k.get_daily_data('005930', count=250)
if daily is not None:
    daily.to_csv('/tmp/_search_daily.csv', index=False)
    print('DAILY_OK', len(daily))
else:
    print('DAILY_FAIL')
"""
    local_tmp = os.path.join(REPO_DIR, "_tmp_search_fetch.py")
    with open(local_tmp, "w", encoding="utf-8") as f:
        f.write(script)
    try:
        scp_to_vm(local_tmp, "/tmp/_search_fetch.py")
        out = ssh(f"{VM_PY} /tmp/_search_fetch.py", timeout=120)
        ok3 = "3MIN_OK" in out
        okd = "DAILY_OK" in out
        if ok3:
            tmp_local_3 = os.path.join(REPO_DIR, "_tmp_new_3min.csv")
            scp_from_vm("/tmp/_search_3min.csv", tmp_local_3)
            _merge_csv(tmp_local_3, CSV_3MIN, key_col="datetime")
            os.remove(tmp_local_3)
        if okd:
            tmp_local_d = os.path.join(REPO_DIR, "_tmp_new_daily.csv")
            scp_from_vm("/tmp/_search_daily.csv", tmp_local_d)
            _merge_csv(tmp_local_d, CSV_DAILY, key_col="date")
            os.remove(tmp_local_d)
        return ok3 and okd
    except Exception as e:
        print(f"[refresh] 실패: {e}", file=sys.stderr)
        return False
    finally:
        if os.path.exists(local_tmp):
            os.remove(local_tmp)


def _merge_csv(new_path: str, master_path: str, key_col: str):
    """new_path의 행들을 master_path에 병합 (key_col 기준 중복 제거, 시간/날짜순 정렬)."""
    rows = {}
    if os.path.exists(master_path):
        with open(master_path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames
            for r in reader:
                rows[r[key_col]] = r
    else:
        with open(new_path, encoding="utf-8") as f:
            fieldnames = csv.DictReader(f).fieldnames

    with open(new_path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows[r[key_col]] = r

    with open(master_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for k in sorted(rows.keys()):
            w.writerow(rows[k])


# ─────────────────────────────────────────
# 2) 상태 관리
# ─────────────────────────────────────────

def load_state(all_bars, daily) -> dict:
    # 벤치마크는 매 실행마다 새로 계산한다 (evaluate_candidate 1건이라 비용이 작음).
    # 예전엔 "필드 없으면만 재계산" 식으로 마이그레이션했는데, evaluate_candidate에
    # 필드가 추가될 때마다(t_stat, holdout, regimes...) 매번 그 가드를 갱신해야 해서
    # 2026-09-29에 실제로 놓쳤다(벤치마크에 regimes가 없어 국면분리 관문이 무조건
    # 통과로 새는 버그) — 아예 매번 재계산해 이 부류의 버그를 원천 차단.
    bench_result = lab.evaluate_candidate(BENCHMARK_CANDIDATE, all_bars, daily)
    benchmark = {**BENCHMARK_CANDIDATE, "result": bench_result}

    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            state = json.load(f)
        state["benchmark"] = benchmark
        # phases는 search_phases.py(계획)를 단일 진실원으로 삼는다. 과거엔 상태파일에
        # 고정 복사돼 있어서, 계획에 새 phase를 추가해도 기존 상태파일이 옛 목록을
        # 그대로 들고 있어 반영되지 않는 함정이 있었다(2026-10-04). 새 phase는 항상
        # 기존 phase_index 뒤에 추가하므로, 인덱스를 보존하면 진행 지점이 유지된다.
        state["phases"] = sp.PHASES
        return state

    return {
        "created_at": datetime.now().isoformat(),
        "last_run_at": None,
        "run_count": 0,
        "tested_count": 0,
        "benchmark": benchmark,
        "best_found": None,   # 벤치마크를 넘은 최고 후보 (없으면 None)
        "phase_index": 0,
        "phases": sp.PHASES,
        "queue": [],
        "leaderboard": [],    # 상위 10개 (win_rate, net_ret, candidate) 계속 갱신
    }


def strip_trades_for_persist(state: dict):
    """상태 JSON 비대화 방지 — 결과의 원시 거래리스트(paired-diff용)는 매 실행 재계산되므로
    영속화하지 않는다. 벤치마크·best_found·리더보드 결과에서 모두 제거."""
    def _strip(res):
        if isinstance(res, dict):
            res.pop("trades", None)
    _strip((state.get("benchmark") or {}).get("result"))
    bf = state.get("best_found")
    if bf:
        _strip(bf.get("result"))
    for e in (state.get("leaderboard") or []):
        _strip(e.get("result"))


def save_state(state: dict):
    strip_trades_for_persist(state)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def append_bench_health_csv(path: str, health: dict, data_end: str):
    """벤치 건강검진 시계열 1행 append (대시보드 쇠퇴추세용). 실패해도 탐색은 계속."""
    cols = ["ts", "data_end", "full_n", "full_t", "full_net",
            "hold_n", "hold_t", "hold_net", "rec_n", "rec_t", "rec_net", "alerts"]
    try:
        new = not os.path.exists(path)
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new:
                w.writerow(cols)
            w.writerow([
                datetime.now().isoformat(timespec="seconds"), data_end,
                health['full_n'], f"{health['full_t']:.4f}", f"{health['full_net']:.6f}",
                health['hold_n'], f"{health['hold_t']:.4f}", f"{health['hold_net']:.6f}",
                health['rec_n'], f"{health['rec_t']:.4f}", f"{health['rec_net']:.6f}",
                ";".join(health['alerts']),
            ])
    except Exception as e:
        print(f"  (bench_health csv 실패: {e})", file=sys.stderr)


def current_phase(state: dict) -> str:
    idx = state["phase_index"]
    phases = state["phases"]
    return phases[min(idx, len(phases) - 1)]


def refill_queue_if_empty(state: dict):
    if state["queue"]:
        return
    phase = current_phase(state)
    best = state["best_found"]["candidate"] if state["best_found"] else state["benchmark"]
    candidates = sp.generate_phase_candidates(phase, best_candidate=best)
    state["queue"] = candidates
    if phase != "refine" and state["phase_index"] < len(state["phases"]) - 1:
        # 이 phase가 소진되면 다음 phase로 넘어가도록 인덱스는 큐가 다시 비었을 때 증가
        pass


def advance_phase_if_needed(state: dict):
    if not state["queue"] and state["phase_index"] < len(state["phases"]) - 1:
        state["phase_index"] += 1


MIN_TRADES_FOR_LEADERBOARD = 100  # 리더보드는 참고용 순위표 — 소표본(운) 노이즈 배제


def update_leaderboard(state: dict, candidate: dict, result: dict):
    if result["n"] < MIN_TRADES_FOR_LEADERBOARD:
        return
    entry = {"candidate": candidate, "result": result}
    board = state["leaderboard"]
    board.append(entry)
    # 승률만 높고 총손익(수수료 반영 원화금액)은 작은 후보가 상위로 오는 걸 막기 위해
    # 정렬 1순위는 net_pnl_krw(수수료 차감 후 총손익금액). 승률은 별도 최저기준으로만 사용.
    board.sort(key=lambda e: e["result"]["net_pnl_krw"], reverse=True)
    state["leaderboard"] = board[:10]


def regime_majority_check(result: dict, bench_result: dict) -> bool:
    """국면분리 관문 (2026-09-29 추가).

    t값+홀드아웃 두 관문을 통과한 후보 2개(BB29/0.8·10:00, BB34/0.7·10:00)가 연달아
    '최근 국면에만 의존'해서 기각됐다 — 전체 4구간 중 2구간에서는 벤치마크가 이겼는데
    우위가 최근 구간에 몰려있어 전구간·홀드아웃 숫자만으로는 구분이 안 됐다.
    같은 HOLDOUT_START 구간에 매일 수백 개 후보를 부딪히면 그 구간 자체에 서서히
    과적합되므로(워크포워드 전환 전까지의 임시 방어), 구간 과반 승리를 요구한다.
    """
    reg = result.get("regimes") or []
    breg = bench_result.get("regimes") or []
    if not reg or not breg or len(reg) != len(breg):
        return True  # 구간 데이터가 없으면(초기 등) 다른 관문에 맡기고 통과
    wins = sum(1 for r, b in zip(reg, breg) if r.get("net_pnl_krw", 0.0) > b.get("net_pnl_krw", 0.0))
    return wins >= len(reg) // 2 + 1  # 과반(동률은 불충분 — 4구간 중 2승은 탈락해야 함)


def is_new_leader(state: dict, result: dict) -> bool:
    """관문 판정 (Phase1 개편 2026-10-08).

    구 기준은 절대 총손익(net_pnl_krw)으로 '벤치마크 초과'를 strict `>`로만 봤다.
    그러나 (1) 마진 없는 `>`는 0.01% 운-우위도 통과시키고, (2) 고원(plateau) 위에서
    절대 총손익 argmax를 뽑으면 노이즈 좌표를 리더로 크라운한다. 또 (3) 리더는 발견시점
    스냅샷으로 고정되는데 벤치는 매 실행 재채점돼 비교가 비대칭이었다.

    개편: '벤치마크와의 짝지은 차이 검정(paired_diff_stats)'으로 교체. 후보·벤치마크는
    같은 종목·세션을 거래해 수익이 강상관이므로, 일자별 차이(diff=후보-벤치)의 t값이
    '신뢰할 만큼 더 버는가'를 훨씬 민감·공정하게 판정한다. t≥2 요구가 곧 노이즈 대비 마진.
    인컴번트도 '현재 데이터로 재채점'된 뒤 paired-t로 비교(드라이버가 재채점해 둠).
    기본 관문(표본수·t바닥·홀드아웃 표본·국면 과반)은 유지. 유니버스-외 최종게이트(Phase4)는
    드라이버 쪽에서 리더 후보가 나왔을 때만 별도로 건다.
    """
    if not passes_core_gates(state, result):
        return False
    # 인컴번트(현재 데이터로 재채점됨)보다 벤치 대비 우위가 더 커야 교체.
    cur_best = state.get("best_found")
    if cur_best:
        bench_tr = state["benchmark"]["result"].get("trades") or []
        cur_tr = (cur_best.get("result") or {}).get("trades") or []
        cur_pd = lab.paired_diff_stats(cur_tr, bench_tr)
        if cur_pd["t"] >= result["paired_vs_bench"]["t"]:
            return False
    return True


def passes_core_gates(state: dict, result: dict) -> bool:
    """인컴번트 비교를 뺀 '벤치마크 대비 자격' 관문. is_new_leader와 '기존 리더 강등'
    판정에 공용으로 쓴다. 통과 시 result에 paired_vs_bench(_hold)를 채워 넣는다."""
    bench = state["benchmark"]["result"]
    bench_tr = bench.get("trades") or []
    cand_tr = result.get("trades") or []
    h = result.get("holdout") or {}
    if result["n"] < MIN_TRADES_FOR_LEADER:
        return False
    if result.get("t_stat", 0.0) < MIN_T_STAT:
        return False
    if h.get("n", 0) < MIN_HOLDOUT_TRADES:
        return False
    if not regime_majority_check(result, bench):
        return False
    # Phase1 핵심: 전구간, 벤치마크 대비 짝지은 차이가 유의하게 양(+)이어야 한다.
    pd = lab.paired_diff_stats(cand_tr, bench_tr)
    result["paired_vs_bench"] = pd  # 보고용
    if pd["t"] < MIN_PAIRED_T:
        return False
    # 홀드아웃 구간에서도 벤치마크에 뒤지지 않을 것(paired-t ≥ 0).
    hc = [t for t in cand_tr if str(t["d"]) >= lab.HOLDOUT_START]
    hb = [t for t in bench_tr if str(t["d"]) >= lab.HOLDOUT_START]
    pdh = lab.paired_diff_stats(hc, hb)
    result["paired_vs_bench_hold"] = pdh
    if pdh["t"] < 0.0:
        return False
    return True


# ─────────────────────────────────────────
# 3) 실거래 성적 집계 (참고용, 리포트에만 사용)
# ─────────────────────────────────────────

def fetch_live_stats_since(since_date: str) -> dict:
    script = f"""
import sqlite3
conn = sqlite3.connect('{VM_DIR}/data/trade_history.db')
rows = conn.execute(
    "SELECT trade_date, pnl FROM trades "
    "WHERE code IN ('0193W0','0193L0') AND side='SELL' AND trade_date>=? "
    "ORDER BY trade_date", ('{since_date}',)
).fetchall()
for r in rows:
    print(r[0], r[1])
"""
    local_tmp = os.path.join(REPO_DIR, "_tmp_search_stats.py")
    with open(local_tmp, "w", encoding="utf-8") as f:
        f.write(script)
    try:
        scp_to_vm(local_tmp, "/tmp/_search_stats.py")
        out = ssh(f"{VM_PY} /tmp/_search_stats.py")
    except Exception:
        return {"n": 0, "wins": 0}
    finally:
        if os.path.exists(local_tmp):
            os.remove(local_tmp)
    n = wins = 0
    for line in out.strip().splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        n += 1
        if float(parts[1]) > 0:
            wins += 1
    return {"n": n, "wins": wins}


# ─────────────────────────────────────────
# 4) 텔레그램
# ─────────────────────────────────────────

def send_telegram(text: str):
    script = (
        "import sys; sys.path.insert(0,'.');"
        "import config;"
        "from notifier import TelegramNotifier;"
        "n=TelegramNotifier(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID);"
        f"n.send({json.dumps(text)})"
    )
    ssh(f"cd {VM_DIR} && {VM_PY} -c \"{script}\"")


def fmt_candidate(c: dict) -> str:
    return f"{c['kind']}{c['params']} 추세MA{c['fast_ma']}/{c['slow_ma']} 진입{c['entry_start']} SL{c['sl']*100:.1f}%/트레일{(c['trail_gap'] or 0)*100:.1f}%"


# ─────────────────────────────────────────
# main
# ─────────────────────────────────────────

def main():
    print("[1/5] 데이터 갱신 중...")
    refreshed = refresh_master_data()
    print(f"  -> {'성공' if refreshed else '실패(기존 데이터로 진행)'}")

    if not os.path.exists(CSV_3MIN) or not os.path.exists(CSV_DAILY):
        print("status=error stop=false  (마스터 데이터 없음, 최초 1회는 수동으로 데이터를 확보해야 함)")
        return

    all_bars, daily = lab.load_master_data(CSV_3MIN, CSV_DAILY)
    print(f"[2/5] 데이터 로드: 3분봉 {len(all_bars)}개 ({all_bars[0]['d']}~{all_bars[-1]['d']}), 일봉 {len(daily)}개")

    state = load_state(all_bars, daily)
    state["run_count"] += 1

    # Phase1: 인컴번트(기존 리더)를 현재 데이터로 재채점 → 벤치마크와 같은 선상에서 paired 비교.
    # 재채점 후 새 기준(paired-t)을 못 넘으면 강등(노이즈 크라운으로 판명).
    if state.get("best_found"):
        try:
            state["best_found"]["result"] = lab.evaluate_candidate(
                state["best_found"]["candidate"], all_bars, daily)
            if not passes_core_gates(state, state["best_found"]["result"]):
                state["best_found"] = None
        except Exception:
            pass

    print(f"[3/5] phase={current_phase(state)} 큐={len(state['queue'])}건, 누적테스트={state['tested_count']}건")
    refill_queue_if_empty(state)

    batch = state["queue"][:BATCH_SIZE]
    state["queue"] = state["queue"][BATCH_SIZE:]

    tested_today = 0
    new_leader_this_run = None
    for cand in batch:
        try:
            result = lab.evaluate_candidate(cand, all_bars, daily)
        except Exception as e:
            continue
        tested_today += 1
        state["tested_count"] += 1
        update_leaderboard(state, cand, result)
        if is_new_leader(state, result):
            state["best_found"] = {
                "candidate": cand, "result": result,
                "found_at": datetime.now().isoformat(), "reported": False,
            }
            new_leader_this_run = state["best_found"]

    # 벤치 절대성능 건강검진 — 선정기 사각지대(벤치 자체 쇠퇴) 감시.
    # save_state 전에 호출해야 bench trades(최근창 계산용)가 아직 붙어 있다.
    health = lab.bench_health(state["benchmark"]["result"], recent_n=RECENT_N, min_full_t=MIN_T_STAT)
    append_bench_health_csv(BENCH_HEALTH_CSV, health, all_bars[-1]['d'])

    advance_phase_if_needed(state)
    state["last_run_at"] = datetime.now().isoformat()
    save_state(state)

    print(f"[4/5] 이번 회차 테스트 {tested_today}건, 누적 {state['tested_count']}건")

    bench = state["benchmark"]["result"]
    bench_h = bench.get("holdout") or {}
    live_stats = fetch_live_stats_since(DEPLOYED_DATE)
    live_wr = (live_stats["wins"] / live_stats["n"] * 100) if live_stats["n"] else None

    lines = [
        "🔬 <b>레버리지 지표탐색 루프 진행보고</b>",
        f"phase: {current_phase(state)} | 이번회차 테스트: {tested_today}건 | 누적: {state['tested_count']}건",
        f"벤치마크(현재 라이브): {bench['n']}건 승률{bench['win_rate']*100:.1f}% "
        f"순익{bench['net_ret']*100:+.1f}%({bench['net_pnl_krw']:+,.0f}원, 건당{bench['avg_pnl_krw']:+,.0f}원) "
        f"t={bench.get('t_stat', 0):.2f} ({state['benchmark']['label']})",
        f"└ 홀드아웃({lab.HOLDOUT_START}~): {bench_h.get('n', 0)}건 "
        f"승률{bench_h.get('win_rate', 0)*100:.1f}% {bench_h.get('net_pnl_krw', 0):+,.0f}원 "
        f"t={bench_h.get('t_stat', 0):.2f}",
    ]
    htag = "⚠️" if health['alerts'] else "✅"
    lines.append(
        f"{htag} 벤치 건강검진: 최근{health['rec_n']}건 net{health['rec_net']*100:+.1f}%·t{health['rec_t']:.2f}"
        f" | 전구간 t{health['full_t']:.2f} | 홀드 t{health['hold_t']:.2f}"
    )
    if health['alerts']:
        lines.append("   └ 🔻<b>벤치 쇠퇴경보</b>: " + " · ".join(health['alerts'])
                     + " (교체/중단은 사람 판단 — 자동개입 안 함)")
    if live_wr is not None:
        lines.append(f"실거래 성적({DEPLOYED_DATE}~): {live_stats['n']}건 중 승률{live_wr:.1f}%")

    if new_leader_this_run:
        c, r = new_leader_this_run["candidate"], new_leader_this_run["result"]
        lines.append("")
        lines.append("🎉 <b>벤치마크를 넘는 새 후보 발견!</b>")
        lines.append(fmt_candidate(c))
        rh = r.get("holdout") or {}
        lines.append(
            f"→ 전구간 {r['n']}건 | 승률{r['win_rate']*100:.1f}% | "
            f"순익{r['net_ret']*100:+.1f}%({r['net_pnl_krw']:+,.0f}원) | t={r.get('t_stat', 0):.2f}"
        )
        lines.append(
            f"→ 홀드아웃 {rh.get('n', 0)}건 | 승률{rh.get('win_rate', 0)*100:.1f}% | "
            f"{rh.get('net_pnl_krw', 0):+,.0f}원 | t={rh.get('t_stat', 0):.2f} "
            f"(벤치마크 대비 {rh.get('net_pnl_krw', 0)-bench_h.get('net_pnl_krw', 0):+,.0f}원)"
        )
        lines.append(
            f"※ 전구간 t≥{MIN_T_STAT} + 홀드아웃에서도 벤치마크 초과를 모두 통과한 후보입니다."
        )
        lines.append("적용하려면 대화 세션에서 \"적용해줘\"라고 말씀해주세요 (자동배포 안 함).")
    elif state["best_found"] and not state["best_found"]["reported"]:
        pass  # 다음 회차에서 처리 (이 분기는 이론상 도달 안 함)

    print("[5/5] 텔레그램 전송 중...")
    try:
        send_telegram("\n".join(lines))
        print("  -> 성공")
    except Exception as e:
        print(f"  -> 실패: {e}", file=sys.stderr)

    if new_leader_this_run:
        state["best_found"]["reported"] = True
        save_state(state)

    print(f"status=in_progress stop=false leader={'yes' if new_leader_this_run else 'no'}")


if __name__ == "__main__":
    main()
