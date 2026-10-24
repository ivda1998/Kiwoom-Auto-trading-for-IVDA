# -*- coding: utf-8 -*-
"""섀도 에이전트 채점 — agent_shadow.csv의 배수를 실제 지수 거래 수익에 곱해 '그랬다면'을 계산.

  python tools/score_agent_shadow.py [shadow_csv] [trade_db]
  (기본: data/agent_shadow.csv, data/trade_history.db)

짝짓기: 섀도 신호(날짜·방향·봉시각) → 같은 날 같은 ETF(롱 122630/숏 114800)의
'지수레버리지전략_*' 매수 중 봉시각~+10분 안의 첫 건 → 그다음 같은 종목 매도의 pnl_rate.
평가는 agent_lab과 같음: 배수를 평균 1로 정규화(노출 동일) 후 일자별 paired-t.
사전등록 판정(2026-10-25): 최소 8주 또는 신호 40건 이상에서, 오프라인 clean paired-t≥2 이고
섀도 평균차 > 0 인 에이전트만 '채택 후보'. 40건은 검정력이 낮음을 감안할 것.
"""
import csv
import os
import sqlite3
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import indicator_lab as lab   # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CODES = {"L": "122630", "S": "114800"}


def load_trades(db):
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = c.execute("select id, trade_date, trade_time, code, side, pnl_rate, reason from trades "
                     "where code in ('122630','114800') order by trade_date, trade_time, id").fetchall()
    out, open_buy = [], {}
    for _id, d, t, code, side, rate, reason in rows:
        if side == "BUY" and str(reason).startswith("지수레버리지전략_"):
            open_buy[code] = {"d": d.replace("-", ""), "t": t, "code": code}
        elif side == "SELL" and code in open_buy:
            b = open_buy.pop(code)
            b["ret"] = float(rate or 0)
            b["exit"] = reason
            out.append(b)
    return out


def match(signals, trades):
    used, pairs = set(), []
    for s in signals:
        st = datetime.strptime(s["date"] + s["time"], "%Y%m%d%H%M")
        for k, tr in enumerate(trades):
            if k in used or tr["d"] != s["date"] or tr["code"] != CODES[s["direction"]]:
                continue
            bt = datetime.strptime(tr["d"] + tr["t"].replace(":", "")[:6], "%Y%m%d%H%M%S")
            if st <= bt <= st + timedelta(minutes=10):
                used.add(k)
                pairs.append((s, tr))
                break
    return pairs


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    csv_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "data", "agent_shadow.csv")
    db = sys.argv[2] if len(sys.argv) > 2 else os.path.join(ROOT, "data", "trade_history.db")
    rows = list(csv.DictReader(open(csv_path, encoding="utf-8")))
    sigs = {}
    for r in rows:
        s = sigs.setdefault(r["signal_id"], {"id": r["signal_id"], "date": r["date"], "time": r["time"],
                                             "direction": r["direction"], "sizes": {}})
        if r["size"] not in ("", None):
            s["sizes"][r["agent"]] = float(r["size"])
    trades = load_trades(db)
    pairs = match(sorted(sigs.values(), key=lambda x: x["id"]), trades)
    print(f"섀도 신호 {len(sigs)}건, 실제 지수 거래 {len(trades)}건, 짝지음 {len(pairs)}건")
    if not pairs:
        return
    bench = [{"d": tr["d"], "ret": tr["ret"]} for _, tr in pairs]
    agents = sorted({a for s, _ in pairs for a in s["sizes"]})
    print(f"{'에이전트':14s}{'n':>4s}{'평균배수':>8s}{'pairT':>7s}{'실제net':>9s}{'가정net':>9s}{'평균차/건':>10s}")
    for a in agents:
        w = [s["sizes"].get(a, 1.0) for s, _ in pairs]
        mw = sum(w) / len(w)
        if mw <= 0:
            continue
        cand = [{"d": tr["d"], "ret": tr["ret"] * x / mw} for (s, tr), x in zip(pairs, w)]
        pd = lab.paired_diff_stats(cand, bench)
        print(f"{a:14s}{len(w):>4d}{mw:>8.2f}{pd['t']:>7.2f}{sum(b['ret'] for b in bench) * 100:>8.1f}%"
              f"{sum(c['ret'] for c in cand) * 100:>8.1f}%{pd['mean_ret'] * 100:>+9.3f}%")
    llm = [(s["sizes"].get("LLM"), tr["ret"]) for s, tr in pairs if "LLM" in s["sizes"]]
    for v in sorted({x for x, _ in llm}):
        rs = [r for x, r in llm if x == v]
        print(f"  LLM 배수 {v}: {len(rs)}건, 건당 실제 {sum(rs) / len(rs) * 100:+.2f}%")
    days = sorted({tr["d"] for _, tr in pairs})
    print(f"기간 {days[0]}~{days[-1]} ({len(days)}거래일). 판정 기준: 8주 또는 40건 이상 + 오프라인 clean t≥2.")


if __name__ == "__main__":
    main()
