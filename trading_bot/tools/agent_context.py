# -*- coding: utf-8 -*-
"""에이전트 개입 연구용 '장전 컨텍스트' — 거래일 d의 09:00 전에 실제로 알 수 있던 정보만.

  - 07:50 아침 리포트 (invest analysis_reports, 07:5x 생성)
  - 직전 거래일 15:30 ~ d 07:50 텔레그램 메시지 중 시장 전반 관련분 (invest messages)
  - d보다 앞선 날짜의 미국 지수 종가 수익률(S&P500·나스닥·SOX), 전일까지 원/달러
  - 일봉 MA10/20 갭·기울기 레짐 (d 당일 종가 미포함)

라이브(섀도)와 오프라인 백테스트가 같은 함수를 쓰도록 DB 경로만 바꿔 호출한다.
"""
import csv
import os
import re
import sqlite3
from datetime import datetime, timedelta

# 시장 전반 메시지 판별(개별 미국 종목 자동글·종목 리포트 제외용)
MACRO_KW = ["뉴욕증시", "미 증시", "미증시", "나스닥", "S&P", "다우", "코스피", "코스닥", "KOSPI", "지수",
            "환율", "원/달러", "달러", "금리", "국채", "연준", "Fed", "FOMC", "CPI", "고용", "유가", "WTI", "브렌트",
            "외국인", "기관", "선물", "옵션", "반도체", "필라델피아", "SOX", "엔비디아", "삼성전자", "SK하이닉스",
            "중국", "관세", "전쟁", "공습", "지정학", "리스크", "급락", "급등", "폭락", "폭등", "마감", "시황", "전략"]
_WS = re.compile(r"\s+")
SKIP_PAT = re.compile(r"\[AI시그널\]|상장폐지 통지|주당 .*배당|순손실|순이익 .*달러")

# 순수 규칙(R3·R4)용 어휘 — 방향성 단어만, 사전 고정(데이터 보고 고치지 말 것)
BEAR_KW = ["급락", "폭락", "하락", "약세", "매도세", "리스크오프", "risk-off", "패닉", "투매", "공포", "우려",
           "경계", "충격", "침체", "긴축", "매파", "금리 급등", "달러 강세", "환율 급등", "유가 급등", "지정학", "공습", "전쟁"]
BULL_KW = ["급등", "폭등", "상승", "강세", "반등", "랠리", "매수세", "리스크온", "risk-on", "사상 최고", "신고가",
           "호재", "완화", "비둘기", "금리 인하", "달러 약세", "순매수", "회복", "서프라이즈", "상향"]


def _ymd(d):
    return d if len(d) == 8 else d.replace('-', '')


def _dash(d):
    d = _ymd(d)
    return f"{d[:4]}-{d[4:6]}-{d[6:]}"


class ContextBuilder:
    def __init__(self, messages_db, global_csv=None, trade_days=None):
        """messages_db: invest messages.db 경로(읽기 전용으로 연다).
        global_csv: build_global_csv()로 만든 해외지표 CSV (없으면 해외지표 생략).
        trade_days: 오름차순 거래일 목록(YYYYMMDD) — 직전 거래일 계산용."""
        self.db = sqlite3.connect(f"file:{messages_db}?mode=ro", uri=True)
        self.trade_days = sorted(trade_days or [])
        self.glob = self._load_global(global_csv) if global_csv and os.path.exists(global_csv) else None

    # ── 원천 ──
    def prev_trade_day(self, d):
        d = _ymd(d)
        prev = [x for x in self.trade_days if x < d]
        if prev:
            return prev[-1]
        return (datetime.strptime(d, "%Y%m%d") - timedelta(days=1)).strftime("%Y%m%d")

    def report(self, d):
        r = self.db.execute("select market_report, created_at from analysis_reports where date=?", (_dash(d),)).fetchone()
        if not r or not r[0]:
            return ""
        # 09:00 이후 생성분은 장전 정보가 아님
        created = str(r[1] or "")
        if created[11:16] and created[11:16] > "09:00":
            return ""
        return r[0]

    def messages(self, d, cutoff="07:50:00"):
        """직전 거래일 15:30 ~ d cutoff 사이 메시지 (channel, time, text) — 시장 전반 관련만."""
        start = f"{_dash(self.prev_trade_day(d))} 15:30:00"
        end = f"{_dash(d)} {cutoff}"
        rows = self.db.execute("select channel, date, text from messages where date>? and date<=? order by date",
                               (start, end)).fetchall()
        out = []
        for ch, dt, tx in rows:
            if not tx or ch.startswith("sample_") or SKIP_PAT.search(tx):
                continue
            head = tx[:300]
            if any(k in head for k in MACRO_KW):
                out.append((ch, dt, tx))
        return out

    # ── 해외 ──
    @staticmethod
    def build_global_csv(path, start="2025-08-01"):
        """FinanceDataReader로 S&P500·나스닥·SOX·원달러 일봉 종가를 받아 CSV 저장(오프라인 1회)."""
        import FinanceDataReader as fdr
        cols = {"spx": "US500", "ndx": "IXIC", "sox": "^SOX", "usdkrw": "USD/KRW"}
        data = {}
        for k, sym in cols.items():
            df = fdr.DataReader(sym, start)
            for idx, v in df["Close"].items():
                data.setdefault(idx.strftime("%Y%m%d"), {})[k] = float(v)
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["date"] + list(cols))
            for d in sorted(data):
                w.writerow([d] + [data[d].get(k, "") for k in cols])
        return path

    @staticmethod
    def _load_global(path):
        rows = {}
        with open(path, encoding="utf-8") as f:
            for r in csv.DictReader(f):
                rows[r["date"]] = {k: (float(v) if v else None) for k, v in r.items() if k != "date"}
        return rows

    def global_moves(self, d):
        """d보다 앞선 마지막 두 관측치로 수익률(%) — 미국 d-1 종가는 한국 d 아침 전에 확정."""
        if not self.glob:
            return {}
        d = _ymd(d)
        out = {}
        for k in ("spx", "ndx", "sox", "usdkrw"):
            seq = [(x, v[k]) for x, v in sorted(self.glob.items()) if x < d and v.get(k)]
            if len(seq) >= 2:
                out[k] = round((seq[-1][1] / seq[-2][1] - 1) * 100, 2)
        return out

    # ── 점수(순수 규칙) ──
    @staticmethod
    def lexicon_score(texts):
        """(bull-bear)/(bull+bear) ∈ [-1,1], 단어 등장 횟수 기준. 단어 없으면 0."""
        bull = bear = 0
        for t in texts:
            bull += sum(t.count(k) for k in BULL_KW)
            bear += sum(t.count(k) for k in BEAR_KW)
        return 0.0 if bull + bear == 0 else (bull - bear) / (bull + bear)

    def build(self, d, max_msgs=40, msg_chars=220, report_chars=3500):
        msgs = self.messages(d)
        rep = self.report(d)
        return {
            "report": rep[:report_chars],
            "messages": ["[%s %s] %s" % (dt[11:16], ch, _WS.sub(" ", tx)[:msg_chars]) for ch, dt, tx in msgs[-max_msgs:]],
            "n_messages": len(msgs),
            "global": self.global_moves(d),
            "msg_score": round(self.lexicon_score([tx for _, _, tx in msgs]), 3),
            "report_score": round(self.lexicon_score([rep]), 3) if rep else None,
        }


def regime(daily_closes_before, fast=10, slow=20, slope_days=3):
    """d 전날까지의 일봉 종가 리스트 → {'gap','slope','trend','phase'}.
    trend L/S = MA10 >/< MA20, phase 강화(|갭| 확대)/역전중(축소)."""
    c = daily_closes_before
    if len(c) < slow + slope_days:
        return None

    def gap(k):  # k일 전 기준 갭(%)
        cc = c[:len(c) - k] if k else c
        f, s = sum(cc[-fast:]) / fast, sum(cc[-slow:]) / slow
        return (f - s) / s * 100
    g0, g3 = gap(0), gap(slope_days)
    trend = "L" if g0 > 0 else "S"
    s = g0 - g3   # 2026-10-13 레짐 분해(transition_perf.py)와 같은 정의: 롱은 s<0, 숏은 s>0이면 역전중
    phase = ("역전중" if s < 0 else "강화") if g0 > 0 else ("역전중" if s > 0 else "강화")
    return {"gap": round(g0, 3), "slope": round(g0 - g3, 3), "trend": trend, "phase": phase}


# ─────────────────────────────────────────
# LLM 에이전트 공용(오프라인·라이브 섀도가 같은 문장을 보냄)
# ─────────────────────────────────────────

LLM_MODEL = "claude-sonnet-5-5"

SYSTEM = """당신은 한국 지수 레버리지 ETF 단타 전략의 리스크 감독 에이전트입니다.

전략(벤치): KOSPI200 2배 레버리지 ETF의 3분봉 볼린저밴드(29, 1.0) 돌파에 진입하고, 일봉 MA10/20 추세 방향으로만 매매합니다.
- 롱(L) = 레버리지 ETF 매수(지수 상승 베팅), 숏(S) = 인버스 1배 ETF 매수(지수 하락 베팅)
- 청산: 손절 -2.5%, 이익 2.5% 도달 후 고점 대비 1.5% 하락 시 트레일, 15:20 강제청산(당일 매매)
- 과거 1년 성적: 건당 평균 +0.2% 안팎의 얇은 양(+)의 기대값, 승률 약 54%. 큰 수익은 추세가 이어지는 날에 몰립니다.

당신의 일: 신호가 난 지금, 이 거래에 걸 금액 배수를 정하세요.
- "1.5" 평소보다 확신이 높을 때 / "1" 평소대로 / "0.5" 줄여야 할 뚜렷한 이유가 있을 때 / "0" 건너뛰어야 할 강한 이유가 있을 때
- 목표는 승률이 아니라 기대수익입니다. 거래를 빼거나 줄이면 그 거래의 기대이익도 잃습니다. 근거가 약하면 "1"을 고르세요.
- 이미 장 시작 갭과 오전 흐름에 반영된 뉴스는 새 정보가 아닙니다. 장전 정보가 '앞으로의' 장중 방향을 바꿀 근거가 되는지 판단하세요.
- 자료에는 날짜가 지워져 있습니다. 특정 날짜를 추측하지 말고 주어진 정보로만 판단하세요.

출력: JSON {"size": "0"|"0.5"|"1"|"1.5", "reason": 한국어 한두 문장}"""

SCHEMA = {
    "type": "object",
    "properties": {"size": {"type": "string", "enum": ["0", "0.5", "1", "1.5"]},
                   "reason": {"type": "string"}},
    "required": ["size", "reason"],
    "additionalProperties": False,
}

_DATE_PATS = [
    re.compile(r"https?://\S+|www\.\S+|\b[\w.-]+\.(?:com|net|co\.kr|kr)/\S*"),   # 링크(날짜가 박힌 경우 많음)
    re.compile(r"(?<!\d)20\d{2}[-./년]\s?\d{1,2}[-./월]\s?\d{1,2}일?"),
    re.compile(r"(?<!\d)\d{2}/\d{1,2}/\d{1,2}(?!\d)"),
    re.compile(r"(?<![\d,.])\d{1,2}/\d{1,2}(?![\d,.])"),
    re.compile(r"\d{1,2}월\s?\d{1,2}일"),
    re.compile(r"(?<![\d,.])(?:19|20)\d{2}(?![\d,.])"),          # 한글·한자가 붙은 연도 포함(2026년·2026회계연도·2026年)
    re.compile(r"(?:CY|FY)?[1-4][QH]\d{2}(?!\d)|\bFY\s?\d{2,4}\b|\b[1-4]Q\s?\d{2}\b"),
    re.compile(r"\(\s?[월화수목금토일]\s?\)"),
]


def scrub(text):
    for p in _DATE_PATS:
        text = p.sub("[날짜]", text)
    return text


def format_snapshot(direction, entry_hhmm, reg, daily_closes6, path, ctx_built):
    """LLM에 보낼 스냅샷 문장. direction 'L'/'S', entry_hhmm 'HHMM',
    reg=regime() 결과, daily_closes6=직전 6일 종가(5일 등락 계산), path=[(HHMM, 전일종가대비%)],
    ctx_built=ContextBuilder.build() 결과."""
    reg = reg or {}
    dc = daily_closes6 or []
    d5 = " ".join(f"{(dc[k] / dc[k - 1] - 1) * 100:+.1f}%" for k in range(1, len(dc)))
    p = " ".join(f"{t[:2]}:{t[2:]} {v:+.2f}" for t, v in path)
    g = ctx_built.get("global") or {}
    lines = [
        f"[신호] 방향 {'롱(지수 상승 베팅)' if direction == 'L' else '숏(지수 하락 베팅)'}, 진입 시각 {entry_hhmm[:2]}:{entry_hhmm[2:]}",
        f"[일봉 추세] MA10-MA20 갭 {reg.get('gap', 0):+.2f}%, 3일 변화 {reg.get('slope', 0):+.2f}%p ({reg.get('phase', '?')}), 최근 5일 등락(2x ETF): {d5}",
        f"[오늘 장중, 2x ETF 전일종가 대비 %] {p}",
        f"[전일 해외] S&P500 {g.get('spx', 'NA')}%, 나스닥 {g.get('ndx', 'NA')}%, 필라델피아반도체 {g.get('sox', 'NA')}%, 원/달러 {g.get('usdkrw', 'NA')}%",
    ]
    if ctx_built.get("report"):
        lines.append("[07:50 투자 분석 리포트 — 텔레그램 채널 요약 AI 리포트]\n" + scrub(ctx_built["report"]))
    if ctx_built.get("messages"):
        lines.append(f"[장전 시장 메시지 {len(ctx_built['messages'])}건(전체 {ctx_built['n_messages']}건 중 최근분)]\n"
                     + scrub("\n".join(ctx_built["messages"])))
    else:
        lines.append("[장전 시장 메시지] 없음")
    return "\n\n".join(lines)


# ─────────────────────────────────────────
# 규칙 에이전트 (2026-10-25 사전등록 — 임계값 데이터 보고 바꾸지 말 것)
#   f = {'regime': regime(), 'global': {...}, 'msg_score_dev': float|None, 'report_score_dev': float|None}
# ─────────────────────────────────────────

def _agree(direction, x, band):
    """신호 방향과 지표 x의 일치: 1.5 / 반대: 0.5 / 중립대역 안: 1.0"""
    if x is None or abs(x) < band:
        return 1.0
    return 1.5 if (direction == "L") == (x > 0) else 0.5


def r1_regime(direction, f):
    r = f.get("regime")
    if not r:
        return 1.0
    if r["phase"] == "강화":
        return 1.5
    return 0.5 if r["trend"] == "S" else 1.0      # 숏-역전중만 0.5


def r2_overnight(direction, f):
    return _agree(direction, (f.get("global") or {}).get("ndx"), 0.3)


def r3_messages(direction, f):
    return _agree(direction, f.get("msg_score_dev"), 0.1)


def r4_report(direction, f):
    return _agree(direction, f.get("report_score_dev"), 0.1)


RULES = {
    "R1_regime": (r1_regime, "full", "레짐: 강화 1.5 / 역전중 1.0 / 숏-역전중 0.5"),
    "R2_overnight": (r2_overnight, "full", "전일 나스닥 방향 일치 1.5 / 반대 0.5 / ±0.3% 이내 1.0"),
    "R3_messages": (r3_messages, "msgs", "장전 메시지 어휘점수(20일 중앙값 대비) 방향 일치 1.5 / 반대 0.5 / ±0.1 이내 1.0"),
    "R4_report": (r4_report, "msgs", "07:50 리포트 어휘점수(20일 중앙값 대비) 일치 1.5 / 반대 0.5 / ±0.1 이내 1.0"),
}


def score_deviation(today_score, prior_scores):
    """어휘점수 수준편향 제거: 직전 20일 중앙값 대비 편차(직전 5일 이상 있을 때만)."""
    import statistics
    prior = [x for x in (prior_scores or [])[-20:] if x is not None]
    if today_score is None or len(prior) < 5:
        return None
    return today_score - statistics.median(prior)
