# -*- coding: utf-8 -*-
"""에이전트 개입 연구 — 벤치 거래마다 '사이즈 배수'를 매기는 개입을 평가한다.

개입 = 배수 {0, 0.5, 1, 1.5}. 평가는 배수를 평가구간 평균 1로 정규화(노출 동일)한 뒤
벤치와 일자별 paired-t (indicator_lab.paired_diff_stats). 크게 걸어서 이기는 착시 방지.

  python tools/agent_lab.py rules        # 규칙 에이전트 R1~R4 (사전등록 후 실행)
  python tools/agent_lab.py rules --kosdaq   # 코스닥 전이 확인

결과는 data/agent_lab/agent_ledger.json 에 사전등록 정보와 함께 누적 기록.
"""
import json
import os
import statistics
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import indicator_lab as lab          # noqa: E402
import hypothesis_lab as hl          # noqa: E402
import agent_context as ac           # noqa: E402

BASE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "agent_lab")
LEDGER = os.path.join(BASE, "agent_ledger.json")

# 평가 구간 (진입일 기준)
WINDOWS = {
    "full": ("00000000", "99999999"),
    "holdout": (lab.HOLDOUT_START, "99999999"),     # 2026-05-01~
    "msgs": ("20260406", "99999999"),               # 텔레그램 메시지 수집 시작 이후
    "clean": ("20260701", "99999999"),              # LLM 학습시점 이후(누설 없는 구간)
}

# 사전 등록 판정 기준 (결과 보기 전에 고정)
CRITERIA = ("채택 후보 = 주 판정구간 paired-t ≥ 2.0 AND 홀드아웃 paired 평균차 > 0 AND "
            "MDD(고정자본 누적) 벤치 대비 악화 ≤ 10%. 주 판정구간: R1·R2=full, R3·R4=msgs, LLM=clean.")


# ─────────────────────────────────────────
# 데이터
# ─────────────────────────────────────────

def load(universe="index"):
    p = lambda n: os.path.join(BASE, f"{universe}_{n}")
    ctx = hl.Ctx(p("lev_3min_master.csv"), p("lev_daily_master.csv"), p("inv_3min_master.csv"))
    trades = hl._shadow_bench(ctx)
    cb = ac.ContextBuilder(os.path.join(BASE, "messages.db"), os.path.join(BASE, "global_daily.csv"), ctx.dates)
    return ctx, trades, cb


def day_features(ctx, cb, days):
    """진입일별 피처(라이브와 같은 정보만): 레짐, 해외, 메시지·리포트 어휘점수."""
    feats = {}
    for d in sorted(set(days)):
        c = cb.build(d)
        feats[d] = {"regime": ac.regime(ctx.prior_closes(d)), "global": c["global"],
                    "msg_score": c["msg_score"] if c["n_messages"] else None,
                    "report_score": c["report_score"]}
    # 어휘점수는 수준 편향(리포트는 늘 긍정 단어가 많음)이 있어 직전 20일 중앙값 대비 편차로 쓴다
    for key in ("msg_score", "report_score"):
        hist = []
        for d in sorted(feats):
            v = feats[d][key]
            prior = [x for x in hist[-20:] if x is not None]
            feats[d][key + "_dev"] = (v - statistics.median(prior)) if (v is not None and len(prior) >= 5) else None
            hist.append(v)
    return feats


# 규칙 에이전트는 라이브 섀도와 공유하려고 agent_context.RULES로 옮김 (함수 시그니처: (direction, features))
SIZERS = ac.RULES


# ─────────────────────────────────────────
# 평가
# ─────────────────────────────────────────

def mdd(trades):
    """고정자본 기준 누적수익(단순합)의 최대낙폭(%p)."""
    cum = peak = dd = 0.0
    for t in sorted(trades, key=lambda x: (x["d"], x.get("et", ""))):
        cum += t["ret"]
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return dd


def evaluate(trades, weights, window):
    lo, hi = WINDOWS[window]
    idx = [i for i, t in enumerate(trades) if lo <= t["ed"] <= hi]
    if len(idx) < 10:
        return None
    w = [weights[i] for i in idx]
    mw = sum(w) / len(w)
    if mw <= 0:
        return None
    bench = [trades[i] for i in idx]
    cand = [dict(trades[i], ret=trades[i]["ret"] * weights[i] / mw) for i in idx]
    pd = lab.paired_diff_stats(cand, bench)
    sb, sc = lab.trade_stats(bench), lab.trade_stats(cand)
    return {"n": len(idx), "paired_t": round(pd["t"], 2), "paired_mean": pd["mean_ret"],
            "bench_net": round(sb["net_ret"], 4), "cand_net": round(sc["net_ret"], 4),
            "bench_t": round(sb.get("t_stat", 0), 2), "cand_t": round(sc.get("t_stat", 0), 2),
            "bench_mdd": round(mdd(bench), 4), "cand_mdd": round(mdd(cand), 4),
            "share_up": round(sum(1 for x in w if x > 1) / len(w), 2),
            "share_down": round(sum(1 for x in w if x < 1) / len(w), 2)}


def verdict(res, main_window):
    m, h = res.get(main_window), res.get("holdout")
    if not m or not h:
        return "판정불가"
    ok = (m["paired_t"] >= 2.0 and h["paired_mean"] > 0
          and m["cand_mdd"] <= m["bench_mdd"] * 1.10 + 1e-9)
    if ok:
        return "채택후보"
    return "관찰" if m["paired_t"] >= 1.0 and h["paired_mean"] > 0 else "기각"


def ledger_append(entry):
    L = json.load(open(LEDGER, encoding="utf-8")) if os.path.exists(LEDGER) else {"runs": []}
    L["runs"].append(entry)
    json.dump(L, open(LEDGER, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


def run_rules(universe="index"):
    ledger_append({"at": datetime.now().isoformat(timespec="seconds"), "kind": "preregister",
                   "universe": universe, "criteria": CRITERIA,
                   "sizers": {k: v[2] for k, v in SIZERS.items()}})
    ctx, trades, cb = load(universe)
    feats = day_features(ctx, cb, [t["ed"] for t in trades])
    rows = {}
    for name, (fn, main_w, desc) in SIZERS.items():
        weights = [fn(t["dir"], feats[t["ed"]]) for t in trades]
        res = {w: evaluate(trades, weights, w) for w in WINDOWS}
        rows[name] = {"desc": desc, "main": main_w, "verdict": verdict(res, main_w), "res": res}
    ledger_append({"at": datetime.now().isoformat(timespec="seconds"), "kind": "result",
                   "universe": universe, "n_bench": len(trades), "rows": rows})
    return trades, rows


def print_rows(rows):
    print(f"{'규칙':14s}{'구간':>7s}{'n':>5s}{'pairT':>7s}{'벤치net':>9s}{'후보net':>9s}{'벤치MDD':>9s}{'후보MDD':>9s}{'↑비중':>6s}{'↓비중':>6s}")
    for name, r in rows.items():
        for w, x in r["res"].items():
            if not x:
                continue
            mark = "◀" if w == r["main"] else ""
            print(f"{name:14s}{w:>7s}{x['n']:>5d}{x['paired_t']:>7.2f}{x['bench_net']*100:>8.1f}%{x['cand_net']*100:>8.1f}%"
                  f"{x['bench_mdd']*100:>8.1f}%{x['cand_mdd']*100:>8.1f}%{x['share_up']:>6.2f}{x['share_down']:>6.2f} {mark}")
        print(f"{'':14s}→ {r['verdict']}  ({r['desc']})")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    if len(sys.argv) > 1 and sys.argv[1] == "rules":
        uni = "kosdaq" if "--kosdaq" in sys.argv else "index"
        _, rows = run_rules(uni)
        print(f"[{uni}]")
        print_rows(rows)
    else:
        print(__doc__)
