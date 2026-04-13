# config.py - 전략 설정 및 환경 변수 관리

import os as _os
_BASE_DIR = _os.path.dirname(_os.path.abspath(__file__))

# ─────────────────────────────────────────
# 키움 REST API 인증
# ─────────────────────────────────────────
APP_KEY    = _os.environ.get("KIWOOM_APP_KEY",    "YOUR_APP_KEY")
APP_SECRET = _os.environ.get("KIWOOM_APP_SECRET", "YOUR_APP_SECRET")
ACCOUNT_NUMBER = _os.environ.get("KIWOOM_ACCOUNT", "81202949")
IS_SIMULATION = _os.environ.get("IS_SIMULATION", "true").lower() == "true"
EXCLUDE_SPAC_ETN_ETF = _os.environ.get("EXCLUDE_SPAC_ETN_ETF", "false").lower() == "true"

# 모의투자 / 실서버 도메인 자동 선택 (키움 REST API)
API_BASE_URL = (
    "https://mockapi.kiwoom.com"   # 모의투자
    if IS_SIMULATION else
    "https://api.kiwoom.com"       # 실전투자
)

# WebSocket 엔드포인트 (ka10171/172/173 조건식 및 실시간 틱)
WS_BASE_URL = (
    "wss://mockapi.kiwoom.com:10000"   # 모의투자
    if IS_SIMULATION else
    "wss://api.kiwoom.com:10000"       # 실전투자
)

# ─────────────────────────────────────────
# 매매 시간 설정
# ─────────────────────────────────────────
TRADE_START_TIME = "09:00"
BUY_END_TIME     = "15:10"
TRADE_END_TIME   = "15:30"
LUNCH_START      = "11:50"
LUNCH_END        = "13:00"
CANDLE_INTERVAL  = 3          # 분봉 단위

# ─────────────────────────────────────────
# 수동 입력 최우선 감시 종목 (독립 매매 전략)
# ─────────────────────────────────────────
# 조건식이나 돌파 로직을 무시하고, 지정된 매수 범위 내에 들어오면 즉시 진입.
# 강제 청산 시간(15:19)에도 해당 종목들은 판매되지 않고 계속 오버나잇(보유) 됨.
# 종목명 또는 종목코드("6자리")를 키(key)로 사용.
CUSTOM_TARGETS = {
    # 예시:
    # "삼성전자": {
    #     "buy_min": 185000,
    #     "buy_max": 188000,
    #     "stop_loss": 180000,
    #     "take_profit": 195000
    # }
}

import os
import json
import logging
from datetime import datetime

TELEGRAM_PICKS_FILE = os.path.join(os.path.dirname(__file__), "data", "telegram_picks.json")

def get_custom_targets(filter_today: bool = False) -> dict:
    """
    CUSTOM_TARGETS와 telegram_picks.json의 내용을 병합하여 실시간 반환
    filter_today=True: telegram_picks 중 created_at이 오늘 날짜인 것만 포함
    """
    targets = dict(CUSTOM_TARGETS)
    today_str = datetime.now().strftime("%Y-%m-%d")

    try:
        if os.path.exists(TELEGRAM_PICKS_FILE):
            with open(TELEGRAM_PICKS_FILE, "r", encoding="utf-8") as f:
                picks = json.load(f)
                if isinstance(picks, dict):
                    for k, v in picks.items():
                        # 기존 수동매수 종목(CUSTOM_TARGETS)과 중복 시 패스 (다음 순위 종목 추가 기회 제공)
                        if k in targets:
                            continue

                        if filter_today:
                            created_at = v.get("created_at", "")
                            if created_at != today_str:
                                continue # 오늘 날짜가 아니면 제외
                        targets[k] = v
    except Exception as e:
        logging.getLogger(__name__).debug(f"텔레그램 연동 JSON 읽기 실패 (무시됨): {e}")
    return targets

# ─────────────────────────────────────────
# 종목 스캔 조건 (일봉 기준)
# ─────────────────────────────────────────
HIGH_PROXIMITY_RATE   = 0.03              # 최고가 근접 허용 범위 (±3%)
MIN_DAILY_VOLUME      = 10_000_000_000   # 최소 5일 평균 거래대금 (100억)
SCAN_HIGH_DAYS        = 20
SCAN_HIGH_DAYS_LONG   = 60
MAX_CANDIDATES        = 10                # 매매 대상 종목 수 10개로 제한
MAX_CUSTOM_TARGETS    = 3                 # 전체 대상(10개) 중 최대 3개까지만 수동 지정 허용
SCAN_UPDATE_INTERVAL  = 180               # 조건식 재검색 간격 (초 단위, 3분)

# ─────────────────────────────────────────
# 조건식 기반 스캔 (ka10171/ka10172)
# ─────────────────────────────────────────
# 영웅문 HTS에서 만든 조건식 이름 (서버 저장한 이름과 정확히 일치해야 함)
# 빈 문자열이면 CONDITION_SEQ를 사용, 둘 다 비어있으면 ka10099 방식으로 폴백
CONDITION_NAME     = "#매물대돌파"  # 영웅문4 서버 저장 조건식 이름 (1순위)
CONDITION_SEQ      = ""        # CONDITION_NAME 없을 때만 사용 (ka10171 응답 seq 값)
CONDITION_STEX_TP  = "K"       # K=KRX, N=NXT
CONDITION_FALLBACK = True      # True: 모든 조건식 실패 시 ka10099 방식으로 자동 전환

# ── 런타임 선택 (봇 시작 시 대화형 선택으로 덮어씀, 코드에서 직접 수정 금지) ──
# True  : 시작 시 조건식 목록을 출력하고 사용자가 번호를 직접 선택
# False : CONDITION_NAME / CONDITION_SEQ 값을 자동 사용
CONDITION_INTERACTIVE = True

# 조건식 결과 종목이 없을 때 순차적으로 시도할 후보 조건식 이름 목록
# (CONDITION_NAME 먼저 시도 후 결과 없으면 아래 순서대로 재시도)
CONDITION_FALLBACK_NAMES = [
    "#매물대돌파 신고가",   # seq=19
    "&3분봉 돌파매매",      # seq=39
    "이평선 정배열(5/20/50)", # seq=23
    "수급MACD",            # seq=24
]

# ─────────────────────────────────────────
# 돌파 조건 (3분봉)
# ─────────────────────────────────────────
BREAKOUT_VOLUME_RATIO = 2.0
BREAKOUT_LOOKBACK     = 5

# ─────────────────────────────────────────
# 눌림 진입 조건
# ─────────────────────────────────────────
PULLBACK_MAX_CANDLES = 3
PULLBACK_PROXIMITY   = 0.01
WATCHING_TIMEOUT_CANDLES = 20  # WATCHING 상태 최대 봉 수 (3분봉 기준 = 60분)
NEAR_HIGH_BUY_THRESHOLD  = 0.01  # 신고가(20일/60일) ±1% 이내 즉시 매수
ENTRY_STRATEGY_TYPE      = 3     # 진입 전략 (1: 20/60일 돌파/눌림, 2: 전일 종가 돌파 시초가, 3: 돌파 후 20MA 눌림)

# ─────────────────────────────────────────
# 청산 조건
# ─────────────────────────────────────────
MA_EXIT_PERIOD = 5            # 익절: 3분봉 N개 이동평균 기울기 음전환 시 청산
STOP_LOSS_RATE = 0.03         # 손절: 진입가 대비 -3% 이탈 시 청산
TARGET_PROFIT_RATE = 0.07     # 익절: 전략 3 기준 7% 자동 청산 (기본 5%)
TRAILING_STOP_RATE = 0.03     # 트레일링: 최고가 대비 3% 하락 시 청산
CLEAR_TIME         = "15:20"  # 시간청산: 15시 20분 동시호가 주문 (15시 30분 종가 청산)

# ─────────────────────────────────────────
# 리스크 관리
# ─────────────────────────────────────────
POSITION_RATIO       = 0.10   # 종목당 자본 10%
MAX_POSITIONS        = 10     # 동시 최대 보유 종목 10개
MAX_DAILY_LOSS_RATE  = 0.02
MAX_TRADES_PER_STOCK = 5      # 전략 3 등 중복(재) 매수 허용 (기본 1 -> 5로 확장)

# ─────────────────────────────────────────
# 시가총액 필터
# ─────────────────────────────────────────
MIN_MARKET_CAP = 100_000_000_000
MAX_MARKET_CAP = 2_000_000_000_000

# ─────────────────────────────────────────
# 시장 필터 (코스닥 지수)
# ─────────────────────────────────────────
MARKET_FILTER_CODE = "Q"
MARKET_FILTER_MA   = 5

# ─────────────────────────────────────────
# 로그 / DB
# ─────────────────────────────────────────
LOG_DIR          = _os.path.join(_BASE_DIR, "logs")
LOG_LEVEL        = "INFO"
DB_PATH          = _os.path.join(_BASE_DIR, "data", "trade_history.db")
BACKTEST_DB_PATH = _os.path.join(_BASE_DIR, "backtest_data", "backtest_results.db")
