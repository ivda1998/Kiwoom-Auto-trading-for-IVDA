# -*- coding: utf-8 -*-
"""지수 레버리지 '5분 BB17/1.0' 섀도 기록 — 실시간 봇은 건드리지 않는 장후 전진 기록.

매 거래일 장 마감 후 1회:
  1) KIS(alpha-intraday 읽기 전용 클라이언트)로 그날 122630·114800 1분봉을 받아 누적 CSV에 덧붙인다
     (하루 약 8회 호출, 키움·나무 토큰·호출한도와 무관).
  2) 일봉 종가는 기존 일봉 마스터 + 이후 날짜는 1분봉 15:30 종가(=공식 종가)로 잇는다.
  3) 실행은 전부 1분봉, 신호만 다르게 두 전략을 같은 조건으로 시뮬레이션:
       벤치  = 3분 BB29/1.0 (라이브와 같은 신호)
       섀도  = 5분 BB17/1.0 (2026-10-25 '유망 관찰' 판정, 파라미터 고정·재튜닝 금지)
  4) 원장(shadow_5m_trades.csv)에 '아직 기록 안 된 날짜'의 거래만 덧붙인다 — 이미 쓴 행은 바꾸지 않는다.
  5) 전진 구간(FORWARD_START 이후) 누적 비교를 출력한다.

사전등록(2026-10-25): 전진 구간에서 거래일 40일 이상 쌓이면 5분 BB17 vs 벤치를
  paired-t > 0, 최대낙폭 ≤ 벤치, 수익 ≥ 벤치로 판단한다. 2개월 뒤 백테스트 재검증(A·B·C)과 함께 본다.

  python tools/shadow_5m_index.py            # 수집 + 기록 + 요약
  python tools/shadow_5m_index.py --no-fetch # 수집 없이 기록·요약만
"""
import csv
import os
import sys
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import indicator_lab as lab      # noqa: E402
import hypothesis_lab as hl      # noqa: E402

BASE = os.path.join(os.path.dirname(HERE), "data", "agent_lab")
LEV1, INV1 = os.path.join(BASE, "index_lev_1min.csv"), os.path.join(BASE, "index_inv_1min.csv")
DAILY_MASTER = os.path.join(BASE, "index_lev_daily_master.csv")
DAILY_SHADOW = os.path.join(BASE, "index_lev_daily_shadow.csv")
LEDGER = os.path.join(BASE, "shadow_5m_trades.csv")
CODES = {"122630": LEV1, "114800": INV1}
KIS_REPO = r"G:\alpha-intraday"
FORWARD_START = "20261026"
STRATS = {"bench_3m_bb29": (3, 29, 1.0), "shadow_5m_bb17": (5, 17, 1.0)}


def _last_day(path):
    last = ""
    with open(path, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            last = max(last, r["datetime"][:8])
    return last


def fetch_new_days():
    """마지막 저장일 다음 평일부터 오늘(15:40 이후면 오늘 포함)까지 1분봉을 받는다. 빈 날(휴장)은 건너뜀."""
    sys.path.insert(0, KIS_REPO)
    from src.ingestion.kis_client import KisClient          # noqa: E402
    from src.ingestion.minute import fetch_day, MinuteError  # noqa: E402
    now = datetime.now()
    end = now.date() if now.strftime("%H%M") >= "1540" else now.date() - timedelta(days=1)
    cli = None
    added = {}
    for code, path in CODES.items():
        d = datetime.strptime(_last_day(path), "%Y%m%d").date() + timedelta(days=1)
        n = 0
        while d <= end:
            if d.weekday() < 5:
                cli = cli or KisClient(delay=1.1)
                ds = d.strftime("%Y%m%d")
                try:
                    res = fetch_day(cli, code, ds)
                except MinuteError as e:
                    print(f"[수집] {code} {ds} 실패: {e}")
                    break   # 이후 날짜를 건너뛰면 구멍이 생기므로 다음 실행에서 다시
                if res["bars"]:
                    with open(path, "a", newline="", encoding="utf-8") as f:
                        w = csv.writer(f)
                        for b in sorted(res["bars"], key=lambda x: x["time"]):
                            w.writerow([ds + b["time"], int(b["open"]), int(b["high"]), int(b["low"]),
                                        int(b["close"]), int(b.get("volume") or 0), res["status"]])
                    n += 1
            d += timedelta(days=1)
        added[code] = n
    return added


def build_daily():
    """일봉 마스터 + 마스터 이후 날짜는 1분봉 마지막(15:30) 종가."""
    rows = {}
    with open(DAILY_MASTER, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows[r["date"]] = int(r["close"])
    last_master = max(rows)
    closes = {}
    with open(LEV1, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            d, t = r["datetime"][:8], r["datetime"][8:12]
            if d > last_master and t <= "1530":
                closes[d] = int(r["close"])   # 시간순 저장이므로 마지막 값 = 15:30 종가
    rows.update(closes)
    with open(DAILY_SHADOW, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["date", "close"])
        for d in sorted(rows):
            w.writerow([d, rows[d]])
    return len(closes)


def sig_m(ctx, M, period, mult):
    """M분 버킷 마지막 1분봉에서만, 버킷 종가 기준 볼린저 돌파 신호."""
    bars = ctx.bars
    key = lambda b: (b['d'], (int(b['t'][:2]) * 60 + int(b['t'][2:]) - 540) // M)
    last_of = {}
    for i, b in enumerate(bars):
        last_of[key(b)] = i
    ends = sorted(last_of.values())
    closes = [bars[i]['close'] for i in ends]
    _, up, lo = lab.bollinger_full_series(closes, period, mult)
    pos = {i: j for j, i in enumerate(ends)}

    def fn(i, trend):
        j = pos.get(i)
        if j is None or j < 1 or up[j] is None or lo[j - 1] is None:
            return None
        if trend == 'UP' and closes[j - 1] <= up[j - 1] and closes[j] > up[j]:
            return 'L'
        if trend == 'DOWN' and closes[j - 1] >= lo[j - 1] and closes[j] < lo[j]:
            return 'S'
        return None
    return fn


def simulate():
    ctx = hl.Ctx(LEV1, DAILY_SHADOW, INV1)
    out = {}
    for name, (M, per, mult) in STRATS.items():
        out[name] = lab.simulate_index(ctx.bars, ctx.short_lookup, hl.gate_ma(ctx), sig_m(ctx, M, per, mult),
                                       slip_ticks=0.5, long_tick=ctx.long_tick, short_tick=ctx.short_tick,
                                       cooldown_bars=3, sl=0.025, trail_gap=0.015,
                                       entry_start='09:30')['trades']
    return ctx, out


def append_ledger(ctx, out):
    """원장에 없는 날짜(FORWARD_START 이후, 마지막 데이터일 이전 완결일)만 덧붙인다."""
    have = set()
    if os.path.exists(LEDGER):
        with open(LEDGER, encoding="utf-8") as f:
            have = {r["date"] for r in csv.DictReader(f)}
    days = sorted({b['d'] for b in ctx.bars if b['d'] >= FORWARD_START and b['d'] not in have})
    if not days:
        return 0
    new = not os.path.exists(LEDGER)
    n = 0
    with open(LEDGER, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["date", "strategy", "dir", "entry_time", "ret", "exit", "recorded_at"])
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        for d in days:
            for name, trades in out.items():
                day_tr = [t for t in trades if str(t['d']) == d]
                if not day_tr:
                    w.writerow([d, name, "", "", "", "거래없음", stamp])
                for t in day_tr:
                    w.writerow([d, name, t['dir'], t['et'], round(t['ret'], 6), t['r'], stamp]); n += 1
    return len(days)


def summary():
    if not os.path.exists(LEDGER):
        print("원장 없음 — 전진 구간 데이터가 아직 없다")
        return
    rows = list(csv.DictReader(open(LEDGER, encoding="utf-8")))
    days = sorted({r["date"] for r in rows})
    tr = {k: [{'d': r["date"], 'ret': float(r["ret"])} for r in rows if r["strategy"] == k and r["ret"]]
          for k in STRATS}
    print(f"전진 섀도 {days[0]}~{days[-1]} ({len(days)}거래일, 판단 기준 40일)")
    for k, v in tr.items():
        s = lab.trade_stats(v)
        cum = peak = dd = 0.0
        for t in v:
            cum += t['ret']; peak = max(peak, cum); dd = max(dd, peak - cum)
        print(f"  {k:16s} 거래 {s['n']:3d}  승률 {s['win_rate']*100:5.1f}%  누적 {s['net_ret']*100:+6.2f}%  MDD {dd*100:5.2f}%")
    if tr["shadow_5m_bb17"] or tr["bench_3m_bb29"]:
        p = lab.paired_diff_stats(tr["shadow_5m_bb17"], tr["bench_3m_bb29"])
        print(f"  5분−벤치 일자별 paired-t {p['t']:+.2f} (일평균 차이 {p['mean_ret']*100:+.3f}%)")
    last = days[-1]
    for k in STRATS:
        dl = [r for r in rows if r["date"] == last and r["strategy"] == k and r["ret"]]
        txt = ", ".join(f"{r['dir']} {r['entry_time']} {float(r['ret'])*100:+.2f}%" for r in dl) or "거래없음"
        print(f"  {last} {k}: {txt}")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    if "--no-fetch" not in sys.argv:
        print("[수집] 새 거래일:", fetch_new_days())
    print("[일봉] 1분봉에서 이은 날짜:", build_daily())
    ctx, out = simulate()
    print(f"[기록] 데이터 마지막일 {ctx.bars[-1]['d']}, 원장에 새로 쓴 날짜 {append_ledger(ctx, out)}일")
    summary()


if __name__ == "__main__":
    main()
