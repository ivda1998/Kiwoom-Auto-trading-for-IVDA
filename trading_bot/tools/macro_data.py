# -*- coding: utf-8 -*-
"""오버나잇 연구용 매크로·시장 데이터 수집 (연구 전용, 라이브 코드와 무관).

출력: data/agent_lab/macro/
  - market_daily.csv : 국내외 일봉 종가(및 국내 OHLC) — FinanceDataReader
  - fred_*.csv / ecos_*.csv : 월간·주간·일간 지표 원자료
  - releases.csv     : 지표별 '사용 가능 시작일'(발표일 보수적 근사) 붙인 long 테이블

발표일 원칙: 월간 값은 기준월이 아니라 '발표 후'부터만 쓴다. 정확한 발표일 이력이 없어
보수적으로 늦춘 규칙을 쓴다(실제 발표보다 늦게 반영 → 미래정보 누설 없음, 대신 신선도 손해).
FRED 값은 사후 수정치(빈티지 아님)라 고용 등은 약간의 수정 편향이 남는다 — 보고서에 명시.

실행: python tools/macro_data.py   (ECOS_API_KEY는 .env에서 읽음)
"""
import os, sys, time, warnings
from datetime import timedelta
import pandas as pd
import requests

warnings.filterwarnings("ignore")
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "data", "agent_lab", "macro")
START = "2013-01-01"

# 일봉 (국내는 OHLC 필요)
DAILY = {
    "KS200": "KS200", "KS11": "KS11", "ETF_LEV": "122630", "ETF_INV": "114800",
    "SPX": "US500", "NDX": "IXIC", "SOX": "SOXX", "ES": "ES=F", "NQ": "NQ=F",
    "VIX": "VIX", "USDKRW": "USD/KRW", "USDJPY": "USD/JPY", "DXY": "DX-Y.NYB",
    "TNX": "^TNX", "WTI": "CL=F", "HSI": "HSI", "N225": "N225", "SSEC": "SSEC",
}
FRED = {   # 이름: (코드, 주기, 사용 가능 규칙)
    "UMCSENT": ("UMCSENT", "M", "m1d1"),          # 미시간 소비자심리 — 기준월 말 확정 → 다음달 1일부터
    "PHILLY": ("GACDFSA066MSFRBPHI", "M", "m1d1"),  # 필라델피아 연준 제조업 — 기준월 셋째 목 → 다음달 1일부터
    "PAYEMS": ("PAYEMS", "M", "m1d8"),            # 비농업고용 — 다음달 첫 금 → 다음달 8일부터
    "CPI": ("CPIAUCSL", "M", "m1d20"),            # CPI — 다음달 중순 → 다음달 20일부터
    "INDPRO": ("INDPRO", "M", "m1d20"),           # 산업생산 — 다음달 중순 → 20일부터
    "ICSA": ("ICSA", "W", "w6"),                  # 신규실업수당 — 주말(토) 기준, 다음 목 발표 → +6일(금)부터
    "T10Y2Y": ("T10Y2Y", "D", "d1"),              # 장단기 금리차 — 미국 날짜 → 다음 날부터
}
ECOS = {   # 이름: (통계표, 주기, 항목코드들, 사용 가능 규칙)
    "KR_EXPORT": ("901Y118", "M", ["T002"], "m1d2"),     # 수출금액 — 다음달 1일 발표 → 2일부터
    "KR_IMPORT": ("901Y118", "M", ["T004"], "m1d2"),
    "KR_CCSI": ("511Y002", "M", ["FME", "99988"], "m1d1"),  # 소비자심리지수 — 당월 하순 발표 → 다음달 1일
    "KR_ESI": ("513Y001", "M", ["E2000"], "m1d1"),       # 경제심리지수(순환변동치) — 당월 말 → 다음달 1일
    "KR_CLI": ("901Y067", "M", ["I16E"], "m2d1"),        # 선행지수 순환변동치 — 다음달 말 발표 → 다다음달 1일
    "KR_NSI": ("523Y001", "D", ["A001"], "d8"),          # 뉴스심리지수(일) — 주간 공표 → 8일 지연
}


def avail_date(ref: pd.Timestamp, rule: str) -> pd.Timestamp:
    """기준 시점 → 보수적 사용 가능 시작일."""
    if rule.startswith("m"):
        months, day = int(rule[1]), int(rule.split("d")[1])
        first = (ref.to_period("M") + months).to_timestamp()
        return first + timedelta(days=day - 1)
    if rule.startswith("w"):
        return ref + timedelta(days=int(rule[1:]))
    if rule.startswith("d"):
        return ref + timedelta(days=int(rule[1:]))
    raise ValueError(rule)


def fetch_daily():
    import FinanceDataReader as fdr
    frames = {}
    for nm, sym in DAILY.items():
        for i in range(3):
            try:
                d = fdr.DataReader(sym, START)
                break
            except Exception as e:
                print(f"  재시도 {nm}: {e}"); time.sleep(2)
        else:
            raise RuntimeError(f"{nm} 수집 실패")
        cols = ["Open", "High", "Low", "Close", "Volume"] if nm in ("KS200", "KS11", "ETF_LEV", "ETF_INV") else ["Close"]
        cols = [c for c in cols if c in d.columns]
        frames[nm] = d[cols].rename(columns={c: f"{nm}_{c}" for c in cols})
        print(f"  {nm:8s} {len(d):5d}행 {d.index[0].date()}~{d.index[-1].date()}")
    return frames


def fetch_fred():
    import FinanceDataReader as fdr
    rows = []
    for nm, (code, cyc, rule) in FRED.items():
        d = fdr.DataReader(f"FRED:{code}", START).dropna()
        for ref, v in d.iloc[:, 0].items():
            rows.append((nm, cyc, ref, avail_date(pd.Timestamp(ref), rule), float(v)))
        print(f"  FRED {nm:8s} {len(d):5d}행 ~{d.index[-1].date()}")
    return rows


def fetch_ecos(key):
    rows = []
    for nm, (tbl, cyc, items, rule) in ECOS.items():
        s, e = ("20130101", "20301231") if cyc == "D" else ("201301", "203012")
        path = "/".join(items)
        out = []
        for page in range(1, 20):
            a, b = (page - 1) * 10000 + 1, page * 10000
            url = f"https://ecos.bok.or.kr/api/StatisticSearch/{key}/json/kr/{a}/{b}/{tbl}/{cyc}/{s}/{e}/{path}"
            r = requests.get(url, timeout=60).json()
            got = r.get("StatisticSearch", {}).get("row", [])
            out += got
            if len(got) < 10000:
                break
        for x in out:
            t = x["TIME"]
            ref = pd.Timestamp(f"{t[:4]}-{t[4:6]}-01") if cyc == "M" else pd.Timestamp(f"{t[:4]}-{t[4:6]}-{t[6:8]}")
            rows.append((nm, cyc, ref, avail_date(ref, rule), float(x["DATA_VALUE"])))
        print(f"  ECOS {nm:9s} {len(out):5d}행 ~{out[-1]['TIME'] if out else '-'}")
    return rows


def main():
    os.makedirs(OUT, exist_ok=True)
    try:
        from dotenv import dotenv_values
        key = (dotenv_values(os.path.join(BASE, ".env")).get("ECOS_API_KEY") or "").strip()
    except Exception:
        key = os.environ.get("ECOS_API_KEY", "").strip()
    print("[일봉]")
    fr = fetch_daily()
    pd.concat(fr.values(), axis=1).sort_index().to_csv(os.path.join(OUT, "market_daily.csv"))
    print("[FRED]")
    rows = fetch_fred()
    if key:
        print("[ECOS]")
        rows += fetch_ecos(key)
    else:
        print("[ECOS] ECOS_API_KEY 없음 — 한국 지표 생략")
    rel = pd.DataFrame(rows, columns=["name", "cycle", "ref", "avail", "value"])
    rel.to_csv(os.path.join(OUT, "releases.csv"), index=False)
    print(f"저장: {OUT}")


if __name__ == "__main__":
    main()
