# -*- coding: utf-8 -*-
"""
레버리지-인버스 전략 파라미터 자동 튜닝 도구.

매일 장마감 후 스케줄 작업이 이 스크립트를 호출한다.
1) VM에서 삼성전자 3분봉(최근 ~15거래일) 조회 → 여러 봉 주기로 리샘플
2) VM 거래이력 DB에서 오늘/누적 실거래 승패 집계
3) 파라미터 그리드로 백테스트 → 현재 설정과 비교해 최적 조합 도출
4) data/leverage_tuning_state.json 갱신 (제안은 저장만 하고 배포는 하지 않음)
5) 텔레그램으로 리포트 전송

사용:
    python tools/tune_leverage.py
"""
import csv
import json
import os
import subprocess
import sys
from collections import defaultdict
from datetime import datetime, timedelta

SSH_KEY  = os.path.expanduser("~/.ssh/oracle_key")
VM_HOST  = "ubuntu@168.107.18.129"
VM_DIR   = "/home/ubuntu/autoTrade/trading_bot"
VM_PY    = "/home/ubuntu/venv/bin/python"

REPO_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(REPO_DIR, "data", "leverage_tuning_state.json")

GOAL_WIN_RATE     = 0.60
GOAL_WEEKLY_RETURN = 0.10   # 이번 주(월~오늘) 거래금액 가중평균 수익률
MAX_DAYS      = 10
SLIPPAGE      = 0.004   # 왕복 슬리피지 가정 (최근 실거래 SL 미끄러짐 관측 반영)
FEE           = 0.0003  # 왕복 수수료

# ── 탐색 그리드 (기존 배포판을 기준점으로 주변 탐색) ──
GRID = {
    "candle_interval": [5, 10, 15, 20, 30],
    "sl":              [0.015, 0.020, 0.025],
    "tp":              [0.030, 0.040, 0.050],
    "doji_th":         [0.15, 0.20, 0.25],
    "entry_start":     ["09:00", "09:30", "10:00"],
    "flip":            [False],   # 사용자 요청으로 당분간 고정 비활성 (재탐색 원하면 True 추가)
    # None=off. 값이 있으면 고정 TP 대신 트레일링 사용
    # (peak 수익이 sl 이상 확보된 뒤 peak 대비 이 폭만큼 되밀리면 청산)
    "trail_gap":       [None, 0.015, 0.020],
    # None=off. 값이 있으면 진입 시 종가가 이 기간 SMA 기준 추세방향과
    # 일치할 때만 진입 허용 (역추세 진입 차단)
    "ma_filter":       [None, 20],
}

# 현재 VM 배포판 설정 (백테스트 비교 기준선)
# 2026-07-14 배포: SL 2.0%->2.5%, 고정TP->트레일링(1.5%), 도지 0.20->0.15,
# 진입시작 09:00->10:00, MA추세필터(20봉) 추가 (백테스트: 승률 34.9%->60.0%, 순수익 -13.7%->+10.1%)
# 2026-07-25 사용자 요청: 진입시작 10:00->09:30 (수동 배포, 백테스트 재검증 없이 적용)
CURRENT_PROD = {
    "candle_interval": 10, "sl": 0.025, "tp": None, "doji_th": 0.15,
    "entry_start": "09:30", "flip": False, "trail_gap": 0.015, "ma_filter": 20,
}


def ssh(cmd: str) -> str:
    full = ["ssh", "-i", SSH_KEY, VM_HOST, cmd]
    r = subprocess.run(full, capture_output=True, text=True, timeout=60)
    return r.stdout


def scp_from_vm(remote_path: str, local_path: str):
    subprocess.run(["scp", "-i", SSH_KEY, "-q", f"{VM_HOST}:{remote_path}", local_path],
                    check=True, timeout=60)


# ─────────────────────────────────────────
# 1) 데이터 수집
# ─────────────────────────────────────────

def fetch_samsung_3min(count: int = 2000) -> list:
    """VM에서 ka10080으로 삼성전자 3분봉 조회 → [{'dt':datetime,'o','h','l','c','v'}]"""
    script = f"""
import sys, os, logging
sys.path.insert(0, '{VM_DIR}')
os.chdir('{VM_DIR}')
logging.disable(logging.CRITICAL)
from kiwoom_api import KiwoomAPI
api = KiwoomAPI()
df = api.get_minute_data('005930', tick_range=3, count={count})
if df is None or df.empty:
    print('ERROR')
else:
    df.to_csv('/tmp/tune_samsung_3min.csv', index=False)
    print('OK', len(df))
"""
    remote_script = "/tmp/_tune_fetch.py"
    with open(os.path.join(REPO_DIR, "_tmp_fetch.py"), "w", encoding="utf-8") as f:
        f.write(script)
    subprocess.run(["scp", "-i", SSH_KEY, "-q",
                    os.path.join(REPO_DIR, "_tmp_fetch.py"), f"{VM_HOST}:{remote_script}"],
                    check=True, timeout=30)
    os.remove(os.path.join(REPO_DIR, "_tmp_fetch.py"))
    out = ssh(f"{VM_PY} {remote_script}")
    if "OK" not in out:
        raise RuntimeError(f"삼성전자 3분봉 조회 실패: {out}")

    local_csv = os.path.join(REPO_DIR, "tools", "_samsung_3min.csv")
    scp_from_vm("/tmp/tune_samsung_3min.csv", local_csv)

    bars = []
    with open(local_csv, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            dt = r["datetime"]
            bars.append({
                "dt": datetime.strptime(dt[:14], "%Y%m%d%H%M%S"),
                "o": int(r["open"]), "h": int(r["high"]),
                "l": int(r["low"]), "c": int(r["close"]), "v": int(r["volume"]),
            })
    os.remove(local_csv)
    return bars


def resample(bars_3min: list, interval_min: int) -> dict:
    """3분봉 리스트 → interval_min 단위로 리샘플, 날짜별 딕셔너리 반환"""
    k = interval_min // 3
    if k < 1:
        k = 1
    by_day = defaultdict(list)
    for b in bars_3min:
        by_day[b["dt"].strftime("%Y%m%d")].append(b)

    out = {}
    for day, bs in by_day.items():
        bs = sorted(bs, key=lambda x: x["dt"])
        merged = []
        for i in range(0, len(bs), k):
            chunk = bs[i:i + k]
            if not chunk:
                continue
            merged.append({
                "time": chunk[0]["dt"].strftime("%H%M"),
                "open": chunk[0]["o"], "high": max(c["h"] for c in chunk),
                "low": min(c["l"] for c in chunk), "close": chunk[-1]["c"],
            })
        out[day] = merged
    return out


def fetch_live_trade_stats(since_date: str) -> dict:
    """VM DB에서 since_date 이후 레버리지 ETF 거래의 승패 + 손익금액 집계 (일자별 + 누적 + 이번주)"""
    script = f"""
import sqlite3
conn = sqlite3.connect('{VM_DIR}/data/trade_history.db')
rows = conn.execute(
    "SELECT trade_date, side, pnl, amount, reason FROM trades "
    "WHERE code IN ('0193W0','0193L0') AND side='SELL' AND trade_date>=? "
    "ORDER BY trade_date, trade_time",
    ('{since_date}',)
).fetchall()
for r in rows:
    print(r[0], r[1], r[2], r[3])
"""
    remote_script = "/tmp/_tune_stats.py"
    local_tmp = os.path.join(REPO_DIR, "_tmp_stats.py")
    with open(local_tmp, "w", encoding="utf-8") as f:
        f.write(script)
    subprocess.run(["scp", "-i", SSH_KEY, "-q", local_tmp, f"{VM_HOST}:{remote_script}"],
                    check=True, timeout=30)
    os.remove(local_tmp)
    out = ssh(f"{VM_PY} {remote_script}")
    by_day = defaultdict(lambda: {"n": 0, "wins": 0})
    total = {"n": 0, "wins": 0}
    today_str = datetime.now().strftime("%Y-%m-%d")
    week_start = (datetime.now() - timedelta(days=datetime.now().weekday())).strftime("%Y-%m-%d")
    week_pnl, week_amount, week_n, week_wins = 0.0, 0.0, 0, 0
    for line in out.strip().splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        d, _side, pnl, amount = parts[0], parts[1], float(parts[2]), float(parts[3])
        by_day[d]["n"] += 1
        total["n"] += 1
        if pnl > 0:
            by_day[d]["wins"] += 1
            total["wins"] += 1
        if d >= week_start:
            week_n += 1
            week_pnl += pnl
            week_amount += amount
            if pnl > 0:
                week_wins += 1
    today = by_day.get(today_str, {"n": 0, "wins": 0})
    return {
        "today_date": today_str,
        "today_n": today["n"], "today_wins": today["wins"],
        "today_win_rate": (today["wins"] / today["n"]) if today["n"] else None,
        "cum_n": total["n"], "cum_wins": total["wins"],
        "cum_win_rate": (total["wins"] / total["n"]) if total["n"] else None,
        "week_start": week_start,
        "week_n": week_n, "week_wins": week_wins,
        "week_win_rate": (week_wins / week_n) if week_n else None,
        "week_return": (week_pnl / week_amount) if week_amount else None,
        "by_day": dict(by_day),
    }


# ─────────────────────────────────────────
# 2) 신호 계산 (leverage_agent.py와 동일 공식)
# ─────────────────────────────────────────

def heikin_ashi(cs):
    ha = []
    for i, c in enumerate(cs):
        hc = (c["open"] + c["high"] + c["low"] + c["close"]) / 4
        ho = (c["open"] + c["close"]) / 2 if i == 0 else (ha[i - 1]["open"] + ha[i - 1]["close"]) / 2
        ha.append({"open": ho, "high": max(c["high"], ho, hc),
                    "low": min(c["low"], ho, hc), "close": hc})
    return ha


def ema_series(vals, period):
    if len(vals) < period:
        return []
    k = 2 / (period + 1)
    e = [sum(vals[:period]) / period]
    for v in vals[period:]:
        e.append(v * k + e[-1] * (1 - k))
    return e


def macd_at(closes, fast=12, slow=26, sig=9):
    ef, es = ema_series(closes, fast), ema_series(closes, slow)
    if not ef or not es:
        return None, None
    d = len(ef) - len(es)
    m = [f - s for f, s in zip(ef[d:], es)]
    ss = ema_series(m, sig)
    if not ss:
        return None, None
    return m[-1], ss[-1]


# ─────────────────────────────────────────
# 3) 백테스트 시뮬레이션
# ─────────────────────────────────────────

def simulate(days: dict, sl, tp, doji_th, entry_start, flip, force_exit="1520",
             trail_gap=None, ma_filter=None):
    """days: {yyyymmdd: [{'time':HHMM,'open','high','low','close'}, ...]}

    trail_gap: None이면 고정 tp 사용. 값이 있으면 peak favorable 수익이 sl
        이상 확보된 뒤 peak 대비 이 폭만큼 되밀리면 청산 (고정 tp 대신).
        봉 단위 OHLC만 사용하는 근사치이며, 같은 봉 안에서는 SL을 먼저
        체크한 뒤 peak/트레일링을 체크하는 보수적 순서를 따른다.
    ma_filter: None이면 미사용. 정수 period면 진입 시 종가가 해당 기간
        SMA 기준 신호 방향과 같은 쪽에 있을 때만 진입 허용 (역추세 진입 차단).
    """
    cost = FEE + SLIPPAGE
    trades = []
    day_list = sorted(days)
    for di, d in enumerate(day_list):
        warm = days[day_list[di - 1]] if di > 0 else []
        seq = warm + days[d]
        w = len(warm)
        closes = [c["close"] for c in seq]
        ha = heikin_ashi(seq)

        def color(i):
            h = ha[i]
            body = abs(h["close"] - h["open"])
            rng = h["high"] - h["low"]
            if rng > 0 and body / rng < doji_th:
                return "D"
            return "B" if h["close"] > h["open"] else ("R" if h["close"] < h["open"] else "D")

        def ma_ok(i, direction):
            if not ma_filter or i + 1 < ma_filter:
                return True
            ma = sum(closes[i + 1 - ma_filter:i + 1]) / ma_filter
            price = closes[i]
            return price > ma if direction == "L" else price < ma

        def entry_sig(i):
            c = color(i)
            m, s = macd_at(closes[:i + 1])
            if m is None:
                return None
            if c == "B" and m > s and ma_ok(i, "L"):
                return "L"
            if c == "R" and m < s and ma_ok(i, "S"):
                return "S"
            return None

        pos = None
        for i in range(w, len(seq)):
            t = seq[i]["time"]
            if pos:
                sign = 1 if pos["dir"] == "L" else -1
                if i > pos["ei"]:
                    lo = sign * (seq[i]["low"] - pos["p"]) / pos["p"] * 2
                    hi = sign * (seq[i]["high"] - pos["p"]) / pos["p"] * 2
                    if min(lo, hi) <= -sl:
                        trades.append({"d": d, "ret": -sl - cost, "r": "SL"}); pos = None; continue
                    if trail_gap is not None:
                        pos["peak"] = max(pos.get("peak", 0.0), hi)
                        if pos["peak"] >= sl and (pos["peak"] - lo) >= trail_gap:
                            exit_ret = pos["peak"] - trail_gap - cost
                            trades.append({"d": d, "ret": exit_ret, "r": "TRAIL"}); pos = None; continue
                    elif max(lo, hi) >= tp:
                        trades.append({"d": d, "ret": tp - cost, "r": "TP"}); pos = None; continue
                if t >= force_exit:
                    ret = sign * (seq[i]["close"] - pos["p"]) / pos["p"] * 2 - cost
                    trades.append({"d": d, "ret": ret, "r": "EOD"}); pos = None; continue
                c = color(i)
                if (pos["dir"] == "L" and c == "R") or (pos["dir"] == "S" and c == "B"):
                    ret = sign * (seq[i]["close"] - pos["p"]) / pos["p"] * 2 - cost
                    trades.append({"d": d, "ret": ret, "r": "REV"}); pos = None
                    if flip and entry_start.replace(":", "") <= t <= "1500":
                        nd = entry_sig(i)
                        if nd:
                            pos = {"dir": nd, "p": seq[i]["close"], "ei": i, "peak": 0.0}
                    continue
            if not pos and entry_start.replace(":", "") <= t <= "1500":
                nd = entry_sig(i)
                if nd:
                    pos = {"dir": nd, "p": seq[i]["close"], "ei": i, "peak": 0.0}
        if pos:
            sign = 1 if pos["dir"] == "L" else -1
            ret = sign * (seq[-1]["close"] - pos["p"]) / pos["p"] * 2 - cost
            trades.append({"d": d, "ret": ret, "r": "EOD"})

    n = len(trades)
    if n == 0:
        return {"n": 0, "win_rate": 0.0, "net_ret": 0.0}
    wins = sum(1 for t in trades if t["ret"] > 0)
    net = sum(t["ret"] for t in trades)
    return {"n": n, "win_rate": wins / n, "net_ret": net}


def grid_search(bars_3min: list):
    resampled_cache = {}
    results = []
    for interval in GRID["candle_interval"]:
        if interval not in resampled_cache:
            resampled_cache[interval] = resample(bars_3min, interval)
        days = resampled_cache[interval]
        for sl in GRID["sl"]:
            for doji in GRID["doji_th"]:
                for start in GRID["entry_start"]:
                    for flip in GRID["flip"]:
                        for ma_filter in GRID["ma_filter"]:
                            for trail_gap in GRID["trail_gap"]:
                                # trail_gap 사용 시 고정 tp는 무시되므로 tp 그리드를 반복하지 않음
                                tp_options = [None] if trail_gap is not None else GRID["tp"]
                                for tp in tp_options:
                                    res = simulate(days, sl, tp, doji, start, flip,
                                                    trail_gap=trail_gap, ma_filter=ma_filter)
                                    if res["n"] < 10:  # 표본 너무 적으면 제외
                                        continue
                                    results.append({
                                        "candle_interval": interval, "sl": sl, "tp": tp,
                                        "doji_th": doji, "entry_start": start, "flip": flip,
                                        "trail_gap": trail_gap, "ma_filter": ma_filter,
                                        **res,
                                    })
    results.sort(key=lambda r: (r["win_rate"], r["net_ret"]), reverse=True)
    return results


def backtest_current_prod(bars_3min: list) -> dict:
    """비교 기준선: 현재 VM 배포판 파라미터로 백테스트."""
    days = resample(bars_3min, CURRENT_PROD["candle_interval"])
    res = simulate(
        days, CURRENT_PROD["sl"], CURRENT_PROD["tp"], CURRENT_PROD["doji_th"],
        CURRENT_PROD["entry_start"], CURRENT_PROD["flip"],
        trail_gap=CURRENT_PROD["trail_gap"], ma_filter=CURRENT_PROD["ma_filter"],
    )
    return {**CURRENT_PROD, **res}


# ─────────────────────────────────────────
# 4) 상태 파일 + 텔레그램
# ─────────────────────────────────────────

def load_state() -> dict:
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    return {
        "goal_win_rate": GOAL_WIN_RATE, "goal_weekly_return": GOAL_WEEKLY_RETURN,
        "max_days": MAX_DAYS,
        "start_date": datetime.now().strftime("%Y-%m-%d"),
        "day_count": 0, "status": "in_progress",
        "history": [],
    }


def save_state(state: dict):
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def send_telegram(text: str):
    script = (
        "import sys; sys.path.insert(0,'.');"
        "import config;"
        "from notifier import TelegramNotifier;"
        "n=TelegramNotifier(config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID);"
        f"n.send({json.dumps(text)})"
    )
    ssh(f"cd {VM_DIR} && {VM_PY} -c \"{script}\"")


# ─────────────────────────────────────────
# main
# ─────────────────────────────────────────

def main():
    state = load_state()
    state["day_count"] += 1
    day_n = state["day_count"]

    stats = fetch_live_trade_stats(state["start_date"])
    bars = fetch_samsung_3min(count=2000)
    results = grid_search(bars)
    best = results[0] if results else None

    entry = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "day": day_n,
        "today_win_rate": stats["today_win_rate"],
        "today_n": stats["today_n"],
        "cum_win_rate": stats["cum_win_rate"],
        "cum_n": stats["cum_n"],
        "week_win_rate": stats["week_win_rate"],
        "week_n": stats["week_n"],
        "week_return": stats["week_return"],
        "proposal": best,
        "applied": False,
    }
    state["history"].append(entry)

    goal_weekly_return = state.get("goal_weekly_return", GOAL_WEEKLY_RETURN)
    win_rate_hit = stats["cum_win_rate"] is not None and stats["cum_win_rate"] >= state["goal_win_rate"] and stats["cum_n"] >= 10
    weekly_return_hit = stats["week_return"] is not None and stats["week_return"] >= goal_weekly_return and stats["week_n"] >= 1
    goal_hit = win_rate_hit and weekly_return_hit
    max_days_hit = day_n >= state["max_days"]

    lines = [f"📐 <b>레버리지 전략 자동튜닝 D-{day_n}/{state['max_days']}</b>"]
    if stats["today_n"]:
        lines.append(f"오늘 실거래: {stats['today_n']}건 승률 {stats['today_win_rate']:.1%}")
    else:
        lines.append("오늘 실거래: 없음")
    if stats["cum_n"]:
        lines.append(f"누적({state['start_date']}~): {stats['cum_n']}건 승률 {stats['cum_win_rate']:.1%} (목표 {state['goal_win_rate']:.0%})")
    if stats["week_n"]:
        lines.append(f"이번주({stats['week_start']}~): {stats['week_n']}건 승률 {stats['week_win_rate']:.1%} 이익률 {stats['week_return']:+.1%} (목표 {goal_weekly_return:+.0%})")
    else:
        lines.append(f"이번주({stats['week_start']}~): 거래 없음 (목표 이익률 {goal_weekly_return:+.0%})")

    if best:
        lines.append(
            f"\n🔬 백테스트 최적안 (봉{best['candle_interval']}분/SL{best['sl']:.1%}/TP{best['tp']:.1%}/"
            f"도지{best['doji_th']:.0%}/진입{best['entry_start']}/플립{'ON' if best['flip'] else 'OFF'})\n"
            f"백테스트 승률 {best['win_rate']:.1%} | 순수익률 {best['net_ret']*100:+.2f}% | {best['n']}건 (슬리피지 {SLIPPAGE:.1%} 가정)"
        )

    if goal_hit:
        state["status"] = "goal_reached"
        lines.append(f"\n🎉 목표(승률 {state['goal_win_rate']:.0%} + 주간 이익률 {goal_weekly_return:+.0%}) 달성! 자동튜닝 루프를 종료합니다.")
    elif max_days_hit:
        state["status"] = "max_days_reached"
        lines.append(f"\n🛑 최대 시도일수({state['max_days']}일) 도달. 목표 미달성 — 자동튜닝 루프를 종료합니다. 수동 검토가 필요합니다.")
    else:
        lines.append(f"\n✅ 적용하시려면 Claude Code 대화에서 '적용해줘'라고 말씀해주세요.")

    save_state(state)
    send_telegram("\n".join(lines))
    report = "\n".join(lines)
    sys.stdout.buffer.write(report.encode("utf-8", errors="replace"))
    sys.stdout.buffer.write(b"\n")
    print(f"\nstatus={state['status']} stop={goal_hit or max_days_hit}")


if __name__ == "__main__":
    main()
