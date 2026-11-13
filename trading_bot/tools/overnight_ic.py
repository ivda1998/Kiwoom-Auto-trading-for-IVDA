# -*- coding: utf-8 -*-
"""오버나잇(122630 종가→익일 시가) 지표별 상관분석 — 2단계 (연구 전용).

사전 등록 (2026-11-06):
- 목표 y: 122630 오버나잇 순수익 = 익일시가/당일종가 - 1 - 수수료 0.015%×2 (동시호가 체결 가정)
- 표본: 월~목 매수일만 (기준선 '금요일 제외 매일 오버나잇'의 우주)
- 결정 시각 15:20 → 해외 일봉은 '당일 이전 날짜'까지만, 국내는 당일 종가(10분 근사),
  월간·주간 지표는 macro_data.py의 보수적 사용 가능일(avail) 이후만.
- 기간: 학습 2014-08~2022-12 / 검증 2023~2024 / 홀드아웃 2025~ (선정에 안 씀, 보고만)
- 채택 기준: 학습 Newey-West |t| ≥ 2 AND 학습 연도별 IC 부호 일치 ≥ 6/9년 AND 검증 IC 같은 부호
  + 다중검정 참고용 BH-FDR(10%) 표시.
- 종합지표: 채택 지표의 '과거만 쓴' 백분위(직전 3년 창) × 부호 평균 → >0일 때만 보유. 기준선과 일자별 짝지은 차이 t.

실행: python tools/overnight_ic.py  → data/agent_lab/macro/ic_report.csv, composite.csv, 콘솔 요약
"""
import os, sys, warnings
import numpy as np
import pandas as pd

warnings.filterwarnings("ignore")
sys.stdout.reconfigure(encoding="utf-8")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
M = os.path.join(BASE, "data", "agent_lab", "macro")
FEE = 0.00015
TRAIN = ("2014-08-01", "2022-12-31"); VALID = ("2023-01-01", "2024-12-31"); HOLD = ("2025-01-01", "2030-12-31")


# ── 데이터 ─────────────────────────────────────────────────────────────────
def load():
    mk = pd.read_csv(os.path.join(M, "market_daily.csv"), index_col=0, parse_dates=True).sort_index()
    rel = pd.read_csv(os.path.join(M, "releases.csv"), parse_dates=["ref", "avail"])
    return mk, rel


def prior(series: pd.Series, days: pd.DatetimeIndex) -> pd.Series:
    """각 국내 거래일 t에 대해 'date < t'인 마지막 값 (해외 일봉: 15:20엔 전날 마감까지만 앎)."""
    s = series.dropna()
    s.index = s.index + pd.Timedelta(days=1)          # t-1 이하 → t에 사용 가능
    return s.reindex(days.union(s.index)).ffill().reindex(days)


def asof(rel: pd.DataFrame, name: str, col: str, days: pd.DatetimeIndex) -> pd.Series:
    """발표 기반 지표: avail ≤ t인 가장 최근 값."""
    r = rel[rel.name == name].sort_values("avail").drop_duplicates("avail", keep="last")
    s = pd.Series(r[col].values, index=r.avail)
    return s.reindex(days.union(s.index)).ffill().reindex(days)


def build():
    mk, rel = load()
    k = mk[["KS200_Open", "KS200_High", "KS200_Low", "KS200_Close"]].dropna()
    k.columns = ["O", "H", "L", "C"]
    e = mk[["ETF_LEV_Open", "ETF_LEV_Close", "ETF_LEV_Volume"]].dropna()
    e = e[(e.ETF_LEV_Open > 0) & (e.ETF_LEV_Close > 0)]
    days = e.index.intersection(k.index)
    e, k = e.loc[days], k.loc[days]
    y = (e.ETF_LEV_Open.shift(-1) / e.ETF_LEV_Close - 1 - 2 * FEE)
    F = pd.DataFrame(index=days)
    C = k.C; r1 = C.pct_change()
    # 국내 (당일 종가까지)
    F["d_intraday"] = k.C / k.O - 1
    F["d_ret1"] = r1
    F["d_gap"] = k.O / C.shift(1) - 1
    F["d_ret5"] = C.pct_change(5)
    F["d_ret20"] = C.pct_change(20)
    F["d_ma20"] = C / C.rolling(20).mean() - 1
    F["d_ma60"] = C / C.rolling(60).mean() - 1
    F["d_ma120"] = C / C.rolling(120).mean() - 1
    F["d_ma10_20"] = C.rolling(10).mean() / C.rolling(20).mean() - 1
    F["d_clv"] = ((k.C - k.L) / (k.H - k.L)).replace([np.inf, -np.inf], np.nan)
    F["d_vol20"] = r1.rolling(20).std()
    F["d_volratio"] = r1.rolling(20).std() / r1.rolling(60).std()
    F["d_etfvol"] = e.ETF_LEV_Volume / e.ETF_LEV_Volume.rolling(20).mean()
    F["d_dd250"] = C / C.rolling(250, min_periods=120).max() - 1
    # 해외·시장 (당일 이전 날짜까지)
    def P(col): return prior(mk[col], days)
    def Pr(col, n=1):
        s = mk[col].dropna(); return prior(s.pct_change(n), days)
    F["f_spx1"] = Pr("SPX_Close"); F["f_ndx1"] = Pr("NDX_Close"); F["f_sox1"] = Pr("SOX_Close")
    F["f_spx5"] = Pr("SPX_Close", 5); F["f_sox20"] = Pr("SOX_Close", 20)
    spx = mk.SPX_Close.dropna()
    F["f_spx_ma50"] = prior(spx / spx.rolling(50).mean() - 1, days)
    F["f_spx_ma200"] = prior(spx / spx.rolling(200).mean() - 1, days)
    F["f_es1"] = Pr("ES_Close")
    F["f_vix"] = P("VIX_Close")
    F["f_vix_chg5"] = Pr("VIX_Close", 5)
    F["f_krw1"] = Pr("USDKRW_Close"); F["f_krw20"] = Pr("USDKRW_Close", 20)
    krw = mk.USDKRW_Close.dropna(); F["f_krw_ma60"] = prior(krw / krw.rolling(60).mean() - 1, days)
    F["f_jpy20"] = Pr("USDJPY_Close", 20); F["f_dxy20"] = Pr("DXY_Close", 20)
    F["f_tnx5"] = prior(mk.TNX_Close.dropna().diff(5), days)
    F["f_wti20"] = Pr("WTI_Close", 20)
    F["f_hsi1"] = Pr("HSI_Close"); F["f_n2251"] = Pr("N225_Close"); F["f_ssec1"] = Pr("SSEC_Close")
    # 발표 지표 (기준월별 파생값 계산 후 avail 기준 asof)
    def derive(name, how):
        r = rel[rel.name == name].sort_values("ref").copy()
        v = r.value
        if how == "chg": r["x"] = v.diff()
        elif how == "level": r["x"] = v
        elif how == "z12": r["x"] = (v - v.rolling(12).mean()) / v.rolling(12).std()
        elif how == "yoy": r["x"] = v / v.shift(12) - 1
        elif how == "yoy_chg": yy = v / v.shift(12) - 1; r["x"] = yy.diff()
        elif how == "chg3": r["x"] = v.diff().rolling(3).mean()
        elif how == "w4": a = v.rolling(4).mean(); r["x"] = a / a.shift(4) - 1
        elif how == "chg20": r["x"] = v - v.shift(20)
        elif how == "d": r["x"] = v
        rel.loc[r.index, "x"] = r.x
        return asof(rel.loc[r.index], name, "x", days)
    rel["x"] = np.nan
    spec = [
        ("m_umcsent_chg", "UMCSENT", "chg"), ("m_umcsent_z12", "UMCSENT", "z12"),
        ("m_philly", "PHILLY", "level"), ("m_philly_chg", "PHILLY", "chg"),
        ("m_payems_chg", "PAYEMS", "chg"), ("m_payems_3m", "PAYEMS", "chg3"),
        ("m_cpi_yoy", "CPI", "yoy"), ("m_cpi_yoy_chg", "CPI", "yoy_chg"),
        ("m_indpro_yoy", "INDPRO", "yoy"), ("m_icsa_4w", "ICSA", "w4"),
        ("m_t10y2y", "T10Y2Y", "d"),
        ("k_export_yoy", "KR_EXPORT", "yoy"), ("k_export_yoy_chg", "KR_EXPORT", "yoy_chg"),
        ("k_import_yoy", "KR_IMPORT", "yoy"),
        ("k_ccsi", "KR_CCSI", "level"), ("k_ccsi_chg", "KR_CCSI", "chg"),
        ("k_esi", "KR_ESI", "level"), ("k_esi_chg", "KR_ESI", "chg"),
        ("k_cli", "KR_CLI", "level"), ("k_cli_chg", "KR_CLI", "chg"),
        ("k_nsi", "KR_NSI", "d"), ("k_nsi_chg20", "KR_NSI", "chg20"),
    ]
    for col, nm, how in spec:
        F[col] = derive(nm, how)
    F["y"] = y
    F["wd"] = F.index.dayofweek
    return F


# ── 통계 ───────────────────────────────────────────────────────────────────
def nw_t(x, y, lags=5):
    """y ~ a + b·x 의 b에 대한 Newey-West t (x는 순위 표준화)."""
    x = (x - x.mean()) / x.std(); X = np.column_stack([np.ones(len(x)), x]); Y = y.values
    b = np.linalg.lstsq(X, Y, rcond=None)[0]; u = Y - X @ b
    XtX_inv = np.linalg.inv(X.T @ X)
    S = (X * u[:, None]).T @ (X * u[:, None])
    for L in range(1, lags + 1):
        w = 1 - L / (lags + 1)
        G = (X[L:] * u[L:, None]).T @ (X[:-L] * u[:-L, None])
        S += w * (G + G.T)
    V = XtX_inv @ S @ XtX_inv
    return b[1] / np.sqrt(V[1, 1])


def ic(a, b):
    return a.rank().corr(b.rank())


def bh(p, q=0.10):
    p = np.asarray(p); n = len(p); o = np.argsort(p); ok = np.zeros(n, bool)
    thr = q * (np.arange(1, n + 1)) / n
    passed = np.where(p[o] <= thr)[0]
    if len(passed): ok[o[:passed.max() + 1]] = True
    return ok


def main():
    from scipy import stats
    F = build()
    F.to_csv(os.path.join(M, "features.csv"))
    S = F[(F.wd != 4) & F.y.notna()]
    feats = [c for c in F.columns if c[:2] in ("d_", "f_", "m_", "k_")]
    tr, va, ho = S[TRAIN[0]:TRAIN[1]], S[VALID[0]:VALID[1]], S[HOLD[0]:HOLD[1]]
    rows = []
    for f in feats:
        a = tr[[f, "y"]].dropna()
        if len(a) < 300:
            rows.append(dict(feature=f, n=len(a))); continue
        icv = ic(a[f], a.y); t = nw_t(a[f].rank(), a.y)
        yrs = [ic(g[f], g.y) for _, g in a.groupby(a.index.year) if len(g) > 60]
        same = sum(np.sign(v) == np.sign(icv) for v in yrs)
        qs = pd.qcut(a[f].rank(method="first"), 5, labels=False)
        q51 = (a.y[qs == 4].mean() - a.y[qs == 0].mean()) * 100
        v = va[[f, "y"]].dropna(); h = ho[[f, "y"]].dropna()
        # 하락장(2018·2022) 유리 절반 vs 불리 절반 (학습 중앙값 기준)
        med = a[f].median(); bear = S[S.index.year.isin([2018, 2022])][[f, "y"]].dropna()
        fav = (bear[f] > med) if icv > 0 else (bear[f] < med)
        rows.append(dict(feature=f, n=len(a), ic_train=icv, nw_t=t, p=2 * (1 - stats.norm.cdf(abs(t))),
                         yrs_same=f"{same}/{len(yrs)}", same=same, nyrs=len(yrs), q5_q1=q51,
                         ic_valid=ic(v[f], v.y) if len(v) > 50 else np.nan,
                         ic_hold=ic(h[f], h.y) if len(h) > 50 else np.nan,
                         bear_fav=bear.y[fav].mean() * 100, bear_unfav=bear.y[~fav].mean() * 100))
    R = pd.DataFrame(rows)
    ok = R.p.notna()
    R.loc[ok, "bh10"] = bh(R.loc[ok, "p"].values)
    R["valid_same"] = np.sign(R.ic_valid) == np.sign(R.ic_train)
    R["pass"] = (R.nw_t.abs() >= 2) & (R.same >= 6) & R.valid_same
    R = R.sort_values("nw_t", key=lambda s: -s.abs())
    R.to_csv(os.path.join(M, "ic_report.csv"), index=False, encoding="utf-8-sig")

    print(f"표본(월~목): 학습 {len(tr)} / 검증 {len(va)} / 홀드아웃 {len(ho)}일 | 학습 y 평균 {tr.y.mean()*100:+.3f}%")
    print(f"{'지표':18s} {'IC학습':>7s} {'NW t':>6s} {'연도일치':>6s} {'Q5-Q1':>7s} {'IC검증':>7s} {'IC홀드':>7s} {'하락장 유리/불리':>16s} BH 채택")
    for _, r in R.iterrows():
        if pd.isna(r.get("ic_train")): print(f"{r.feature:18s} 표본부족 n={r.n}"); continue
        print(f"{r.feature:18s} {r.ic_train:+7.3f} {r.nw_t:+6.2f} {r.yrs_same:>6s} {r.q5_q1:+6.3f}% {r.ic_valid:+7.3f} {r.ic_hold:+7.3f} "
              f"{r.bear_fav:+7.3f}/{r.bear_unfav:+7.3f}  {'*' if r.bh10 else ' '}  {'◎' if r['pass'] else ''}")

    sel = R[R["pass"]]
    print(f"\n채택 {len(sel)}개: {list(sel.feature)}")
    if len(sel) == 0:
        return
    # 종합지표: 직전 3년(756일) 창 백분위 (당일 포함 과거만) × 부호 → 평균
    comp = []
    for _, r in sel.iterrows():
        x = F[r.feature]
        pct = x.rolling(756, min_periods=250).apply(lambda w: (w[:-1] < w[-1]).mean() if not np.isnan(w[-1]) else np.nan, raw=True)
        comp.append((pct - 0.5) * np.sign(r.ic_train))
    F["composite"] = pd.concat(comp, axis=1).mean(axis=1, skipna=False)
    F[["composite", "y", "wd"]].to_csv(os.path.join(M, "composite.csv"))
    S2 = F[(F.wd != 4) & F.y.notna() & F.composite.notna()]
    print("\n[종합지표 → 오버나잇 보유 (월~목), 기준선 = 월~목 매일]")
    for nm, (lo, hi) in [("학습", TRAIN), ("검증", VALID), ("홀드아웃", HOLD), ("2018", ("2018", "2018")), ("2022", ("2022", "2022"))]:
        g = S2[lo:hi]
        if len(g) < 20: continue
        base = g.y; hold = g.y.where(g.composite > 0, 0.0); diff = hold - base
        t = diff.mean() / diff.std() * np.sqrt(len(diff))
        def cg(x):
            eq = (1 + x).cumprod(); yrs = max((x.index[-1] - x.index[0]).days / 365.25, 0.1)
            return (eq.iloc[-1] ** (1 / yrs) - 1) * 100, (eq / eq.cummax() - 1).min() * 100
        cb, db = cg(base); ch, dh = cg(hold)
        print(f"  {nm:6s} IC {ic(g.composite, g.y):+.3f} | 보유비율 {(g.composite>0).mean()*100:3.0f}% | "
              f"기준선 CAGR {cb:+6.1f}% MDD {db:6.1f}% → 종합 CAGR {ch:+6.1f}% MDD {dh:6.1f}% | 짝지은 차이 t {t:+.2f}")


if __name__ == "__main__":
    main()
