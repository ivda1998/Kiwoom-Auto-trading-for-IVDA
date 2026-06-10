# kiwoom_api.py - 키움 REST + WebSocket API 래퍼
#
# 키움 API 핵심 규칙:
#   REST (주문/일봉/잔고/현재가):
#     - 모든 요청은 POST
#     - 모의투자: https://mockapi.kiwoom.com
#     - 실전투자: https://api.kiwoom.com
#     - 헤더: api-id (TR코드), authorization (Bearer 토큰)
#
#   WebSocket (조건식/실시간틱):
#     - 엔드포인트: wss://[mockapi|api].kiwoom.com:10000/api/dostk/websocket
#     - 연결 후 반드시 LOGIN TR 먼저 전송: {"trnm":"LOGIN","token":"<token>"}
#     - LOGIN 응답(return_code=0) 확인 후 나머지 TR 전송 가능
#     - ka10171(CNSRLST): 조건식 목록 조회
#     - ka10172(CNSRREQ search_type=0): 조건검색 결과
#     - ka10173(CNSRREQ search_type=1): 조건식 실시간 등록 (push)
#     - REG: 실시간 체결 틱 구독

import asyncio
import json
import os
import threading
import time
import logging
import requests
import websockets
import pandas as pd
from datetime import datetime, timedelta
from typing import Optional

import config

logger = logging.getLogger(__name__)


class KiwoomAPI:
    """키움 OpenAPI 래퍼 (REST + WebSocket)"""

    @property
    def BASE_URL(self) -> str:
        return config.API_BASE_URL

    @property
    def WS_URI(self) -> str:
        return config.WS_BASE_URL + "/api/dostk/websocket"

    @property
    def TOKEN_URL(self) -> str:
        return f"{self.BASE_URL}/oauth2/token"

    # 키움 REST API 일일 한도
    _DAILY_LIMIT = 1700
    _WARN_THRESHOLDS = {1500, 1600, 1650, 1680}

    # 토큰 캐시 파일 경로 (재시작 시 재사용)
    _TOKEN_CACHE = os.path.join(os.path.dirname(__file__), "data", ".kiwoom_token_cache.json")

    def __init__(self):
        # ── REST ──────────────────────────────────────
        self._token: Optional[str] = None
        self._token_expires: datetime = datetime.min
        self._session = requests.Session()
        self._session.headers.update({
            "Content-Type": "application/json;charset=UTF-8",
        })
        # 재시작 시 캐시된 토큰 복원 시도
        self._load_token_cache()

        # ── API 요청 횟수 추적 (일일 1700회 한도) ──────
        self._rest_call_count: int = 0
        self._rest_call_date: Optional[object] = None   # datetime.date

        # ── REST 호출 간격 스로틀 (초당 최대 ~4.5회) ────
        self._last_rest_call: float = 0.0
        self._rest_min_interval: float = 0.22   # 최소 0.22초 간격

        # ── 차트 API 전용 스로틀 (ka10081/ka10080 전용, ≥1초/call) ──
        self._last_chart_call: float = 0.0
        self._chart_min_interval: float = 1.1   # 최소 1.1초 간격 (429 방지용)

        # ── WebSocket ──────────────────────────────────
        self._ws_loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws_thread: Optional[threading.Thread] = None
        self._ws_conn = None                    # websockets 연결 객체
        self._ws_ready = threading.Event()      # WS 연결 완료 신호
        self._ws_connecting: bool = False       # _ws_connect() 코루틴 실행 중 여부 (중복 방지)

        # 응답 대기용 Future (각 동기 조회 메서드에서 사용)
        self._cnsrlst_future: Optional[asyncio.Future] = None
        self._cnsrreq_future: Optional[asyncio.Future] = None

        # 콜백
        self._tick_callbacks: list = []
        self._condition_callbacks: list = []    # callback(code, name, action)
        self._subscribed_codes: set = set()

    # ─────────────────────────────────────────
    # 토큰 관리 (REST)
    # ─────────────────────────────────────────
    def login(self) -> bool:
        # 캐시된 토큰이 아직 유효하면 au10001 발급 없이 재사용
        if self._token and datetime.now() < self._token_expires:
            logger.info("[KiwoomAPI] 기존 토큰 유효 — 재발급 생략 (만료: %s)",
                        self._token_expires.strftime("%H:%M:%S"))
            self._start_ws_thread()
            return True
        ok = self._refresh_token()
        if ok:
            self._start_ws_thread()
        return ok

    def _refresh_token(self) -> bool:
        try:
            resp = requests.post(self.TOKEN_URL, json={
                "grant_type": "client_credentials",
                "appkey":     config.APP_KEY,
                "secretkey":  config.APP_SECRET,
            }, headers={"Content-Type": "application/json;charset=UTF-8"},
               timeout=15)

            data = resp.json()
            logger.debug(f"[KiwoomAPI] 토큰응답: {data}")

            if data.get("return_code", -1) != 0:
                logger.error(f"[KiwoomAPI] 토큰 오류: {data.get('return_msg')}")
                return False

            self._token = data["token"]
            expires_dt = data.get("expires_dt", "")
            if expires_dt and len(expires_dt) == 14:
                self._token_expires = (
                    datetime.strptime(expires_dt, "%Y%m%d%H%M%S")
                    - timedelta(minutes=5)
                )
            else:
                self._token_expires = datetime.now() + timedelta(hours=23)

            self._session.headers.update({
                "authorization": f"Bearer {self._token}",
            })
            logger.info("[KiwoomAPI] 토큰 발급 성공")
            self._save_token_cache()   # 파일에 저장 → 재시작 시 재사용
            return True

        except Exception as e:
            logger.error(f"[KiwoomAPI] 토큰 발급 실패: {e}")
            return False

    def _ensure_token(self):
        if datetime.now() >= self._token_expires:
            self._refresh_token()

    def _save_token_cache(self):
        """토큰과 만료 시각을 파일에 저장."""
        try:
            import json as _j
            os.makedirs(os.path.dirname(self._TOKEN_CACHE), exist_ok=True)
            with open(self._TOKEN_CACHE, "w") as f:
                _j.dump({
                    "token":   self._token,
                    "expires": self._token_expires.strftime("%Y%m%d%H%M%S"),
                    "app_key": config.APP_KEY,   # 키 바뀌면 캐시 무효화
                }, f)
        except Exception as e:
            logger.debug(f"[KiwoomAPI] 토큰 캐시 저장 실패: {e}")

    def _load_token_cache(self):
        """재시작 시 파일에서 토큰 복원. 만료됐거나 키가 다르면 무시."""
        try:
            import json as _j
            if not os.path.exists(self._TOKEN_CACHE):
                return
            with open(self._TOKEN_CACHE) as f:
                data = _j.load(f)
            # 앱 키 불일치 → 캐시 무효
            if data.get("app_key") != config.APP_KEY:
                return
            expires = datetime.strptime(data["expires"], "%Y%m%d%H%M%S")
            if datetime.now() >= expires:
                return   # 이미 만료
            self._token = data["token"]
            self._token_expires = expires
            self._session.headers.update({
                "authorization": f"Bearer {self._token}",
            })
            logger.info("[KiwoomAPI] 토큰 캐시 복원 (만료: %s)",
                        expires.strftime("%H:%M:%S"))
        except Exception as e:
            logger.debug(f"[KiwoomAPI] 토큰 캐시 로드 실패: {e}")

    # ─────────────────────────────────────────
    # 공통 POST (키움 REST는 모두 POST)
    # ─────────────────────────────────────────
    def _post(self, path: str, body: dict, api_id: str,
              cont_yn: str = "N", next_key: str = "",
              max_retries: int = 3) -> dict:
        self._ensure_token()

        # ── 일일 요청 횟수 추적 ──────────────────────
        today = datetime.now().date()
        if self._rest_call_date != today:
            self._rest_call_date  = today
            self._rest_call_count = 0

        self._rest_call_count += 1
        cnt = self._rest_call_count

        if cnt in self._WARN_THRESHOLDS:
            logger.warning(
                f"[KiwoomAPI] ⚠️  오늘 REST 요청 {cnt}/{self._DAILY_LIMIT}회 "
                f"— 한도 초과 시 당일 모든 API 중단"
            )
        elif cnt >= self._DAILY_LIMIT:
            logger.error(
                f"[KiwoomAPI] 🚫 일일 REST 요청 한도({self._DAILY_LIMIT}회) 초과! "
                f"({api_id}) → 오늘 더 이상 API 호출 불가"
            )
            raise RuntimeError(
                f"[KiwoomAPI] 일일 요청 한도({self._DAILY_LIMIT}회) 초과 — "
                f"봇을 내일 재시작하세요."
            )

        url = self.BASE_URL + path
        headers = {
            "api-id":   api_id,
            "cont-yn":  cont_yn,
            "next-key": next_key,
        }

        # 429 Too Many Requests 시 최대 max_retries회 재시도
        # max_retries=0 이면 429 즉시 예외 발생 (폴링 호출용 — 블로킹 방지)
        for attempt in range(max(1, max_retries)):
            # ── 최소 호출 간격 스로틀 (매 시도 직전 적용) ──
            _now_ts  = time.time()
            _elapsed = _now_ts - self._last_rest_call
            if _elapsed < self._rest_min_interval:
                time.sleep(self._rest_min_interval - _elapsed)
            self._last_rest_call = time.time()

            resp = self._session.post(url, json=body, headers=headers, timeout=15)

            if resp.status_code == 429:
                if max_retries == 0:
                    raise RuntimeError(f"429 {api_id} — 즉시 스킵")
                wait = [5, 20, 60][min(attempt, 2)]
                logger.warning(
                    f"[KiwoomAPI] 429 요청 한도 초과 ({api_id}) "
                    f"→ {wait}초 대기 후 재시도 ({attempt+1}/{max_retries})"
                )
                time.sleep(wait)
                continue

            if not resp.ok:
                logger.error(
                    f"[KiwoomAPI] HTTP {resp.status_code} | "
                    f"api-id={api_id} | body={resp.text[:300]}"
                )
                resp.raise_for_status()

            return resp.json()

        # 3회 모두 실패
        logger.error(f"[KiwoomAPI] {api_id} 3회 재시도 후 최종 실패")
        resp.raise_for_status()
        return {}

    # ─────────────────────────────────────────
    # WebSocket 이벤트 루프 스레드
    # ─────────────────────────────────────────
    def _start_ws_thread(self):
        """asyncio 이벤트 루프를 별도 daemon 스레드에서 실행."""
        self._ws_loop = asyncio.new_event_loop()
        self._ws_thread = threading.Thread(
            target=self._ws_loop.run_forever,
            daemon=True,
            name="KiwoomWS"
        )
        self._ws_thread.start()
        logger.debug("[KiwoomWS] 이벤트 루프 스레드 시작")

    async def _ws_connect(self):
        """
        WebSocket에 연결하고 수신 루프를 실행.
        연결 성공 시 _ws_ready 이벤트 세트.
        수신 메시지 → _dispatch_ws_message() 로 분기.

        키움 WS 인증 순서:
          1. wss://... 연결 (additional_headers 불필요, 또는 유지해도 무방)
          2. {"trnm": "LOGIN", "token": "<token>"} 전송
          3. {"trnm": "LOGIN", "return_code": 0} 응답 확인
          4. 이후 CNSRLST / CNSRREQ / REG 전송 가능
        """
        self._ws_connecting = True  # 코루틴 실행 시작 표시 (중복 방지용)
        try:
            async with websockets.connect(
                self.WS_URI,
                additional_headers={"authorization": f"Bearer {self._token}"},
                ping_interval=20,   # 20초마다 WS 프레임 PING 전송 (유휴 연결 유지)
                ping_timeout=10,    # PONG 미수신 시 10초 후 재연결
            ) as ws:
                self._ws_conn = ws # ── WS 로그인 인증 (필수: 다른 TR 전에 반드시 먼저 전송) ──
                await ws.send(json.dumps({"trnm": "LOGIN", "token": self._token}))
                try:
                    login_raw  = await asyncio.wait_for(ws.recv(), timeout=10)
                    login_resp = json.loads(login_raw)
                    rc = login_resp.get("return_code", -1)
                    if rc != 0:
                        raise Exception(
                            f"WS 로그인 실패: rc={rc} "
                            f"msg={login_resp.get('return_msg', '')}"
                        )
                    logger.info("[KiwoomWS] WebSocket 로그인 성공")
                except asyncio.TimeoutError:
                    raise Exception("[KiwoomWS] WS 로그인 응답 타임아웃 (10초)")

                self._ws_conn = ws
                self._ws_ready.set()
                logger.info(f"[KiwoomWS] WebSocket 연결 및 인증 완료: {self.WS_URI}")

                # 재연결 시 기존 구독 복원 (초기 연결에서는 _subscribed_codes가 비어있음)
                if self._subscribed_codes:
                    for code in list(self._subscribed_codes):
                        resubscribe_req = json.dumps({
                            "trnm": "REG", "grp_no": "0001", "refresh": "1",
                            "data": [{"item_cd": code, "item_tp": "J"}],
                        })
                        await ws.send(resubscribe_req)
                    logger.info(f"[KiwoomWS] 재연결 구독 복원: {len(self._subscribed_codes)}종목")

                async for raw in ws:
                    try:
                        msg = json.loads(raw)
                        self._dispatch_ws_message(msg)
                    except Exception as e:
                        logger.debug(f"[KiwoomWS] 메시지 파싱 오류: {e} | raw={raw[:200]}")

        except Exception as e:
            logger.error(f"[KiwoomWS] 연결 오류: {e}")
        finally:
            self._ws_conn = None
            self._ws_ready.clear()
            logger.info("[KiwoomWS] WebSocket 연결 종료")
            # 비의도적 연결 종료 시 자동 재연결
            # unsubscribe_realtime()이 _subscribed_codes.clear() + _ws_stop_requested = True를
            # 설정하므로, 장 종료 / 정상 종료 시에는 재연결하지 않음
            if getattr(self, "_ws_stop_requested", False):
                self._ws_connecting = False  # 정상 종료 — 재연결 불필요
            else:
                # 비의도적 종료 (서버 타임아웃, 네트워크 오류 등) → 무조건 재연결
                self._ensure_token()
                logger.warning("[KiwoomWS] 비의도적 연결 종료 → 3초 후 재연결 시도...")
                await asyncio.sleep(3)
                asyncio.ensure_future(self._ws_connect())

    def _ensure_ws(self, timeout: float = 15.0):
        """WS 연결이 없으면 연결을 시작하고 ready 이벤트를 기다림."""
        if self._ws_conn is not None:
            return
        if self._ws_loop is None:
            self._start_ws_thread()
        if not self._ws_connecting:
            # 이미 _ws_connect() 코루틴이 실행 중(또는 예약됨)이 아닐 때만 새로 시작
            # → finally 블록의 asyncio.ensure_future(_ws_connect())와 중복 방지
            asyncio.run_coroutine_threadsafe(
                self._ws_connect(), self._ws_loop
            )
        if not self._ws_ready.wait(timeout=timeout):
            raise TimeoutError("[KiwoomWS] WebSocket 연결 타임아웃")

    # ─────────────────────────────────────────
    # WebSocket 메시지 디스패처
    # ─────────────────────────────────────────
    def _dispatch_ws_message(self, msg: dict):
        """
        trnm 필드로 분기:
          CNSRLST  → get_condition_list() 응답 future 처리
          CNSRREQ  → search_by_condition() 응답 또는 실시간 편입 콜백
          CNSROUT  → 조건식 이탈 콜백
          S3_* / REAL → 실시간 체결 틱 처리
        """
        trnm = msg.get("trnm", "")
        rc = msg.get("return_code")
        # 오류가 너무 많으면 로깅 줄이거나 필터링 가능
        if rc and rc != 0 and rc != '0' and trnm != "PING":
            logger.debug(f"[KiwoomWS] msg_log: trnm={trnm} rc={rc} msg={msg.get('return_msg')}")

        if trnm == "PING":
            # Kiwoom WS requires application-level PONG response to stay alive
            if self._ws_conn and self._ws_loop:
                pong_msg = json.dumps({"trnm": "PONG"})
                asyncio.run_coroutine_threadsafe(self._ws_conn.send(pong_msg), self._ws_loop)

        elif trnm == "CNSRLST":
            fut = self._cnsrlst_future
            if fut is not None and not fut.done():
                self._ws_loop.call_soon_threadsafe(fut.set_result, msg)

        elif trnm == "CNSRREQ":
            fut = self._cnsrreq_future
            if fut is not None and not fut.done():
                # 일회성 조회 응답
                self._ws_loop.call_soon_threadsafe(fut.set_result, msg)
            else:
                # 실시간 편입 이벤트
                self._handle_condition_realtime(msg, action="IN")

        elif trnm == "CNSROUT":
            self._handle_condition_realtime(msg, action="OUT")

        elif trnm.startswith("S3_") or trnm in ("REAL", "H0STCNT0"):
            self._handle_realtime_tick(msg)

    # ─────────────────────────────────────────
    # 종목 목록 (코스닥 전종목) - REST
    # ─────────────────────────────────────────
    def get_all_stocks(self, market: str = "10") -> list:
        """
        ka10099 - 시장별 전종목 기본정보 리스트
        market: "0"=코스피, "10"=코스닥
        """
        if not hasattr(self, "_all_stocks_cache"):
            self._all_stocks_cache = {}
            
        if market in self._all_stocks_cache:
            return self._all_stocks_cache[market]

        stocks = []
        cont_yn  = "N"
        next_key = ""

        while True:
            try:
                data = self._post(
                    "/api/dostk/stkinfo",
                    body={"mrkt_tp": market},
                    api_id="ka10099",
                    cont_yn=cont_yn,
                    next_key=next_key,
                )
                items = data.get("list", [])
                for item in items:
                    cd = item.get("code", "")
                    if not cd:
                        continue
                    cd = cd.lstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ").strip()
                    raw_price = str(item.get("lastPrice", "0")).lstrip("0") or "0"
                    try:
                        last_price = int(raw_price)
                    except ValueError:
                        last_price = 0

                    stocks.append({
                        "code":       cd,
                        "name":       item.get("name", ""),
                        "last_price": last_price,
                        "list_count": int(item.get("listCount", "0") or "0"),
                    })

                cont_yn  = data.get("cont_yn",  "N")
                next_key = data.get("next_key", "")
                if cont_yn != "Y" or not next_key:
                    break

                time.sleep(0.3)

            except Exception as e:
                logger.error(f"[KiwoomAPI] 종목목록 조회 실패: {e}")
                break

        logger.info(f"[KiwoomAPI] 종목목록: {len(stocks)}개 ({market})")
        self._all_stocks_cache[market] = stocks
        return stocks

    # ─────────────────────────────────────────
    # 현재가 + 시가총액 - REST
    # ─────────────────────────────────────────
    def get_stock_info(self, code: str, fast: bool = False) -> dict:
        """ka10001 - 주식 현재가 시세 (현재가·등락률·거래량 포함)
        fast=True: 429 시 재시도 없이 즉시 예외 발생 (폴링용)
        """
        try:
            data = self._post(
                "/api/dostk/stkinfo",
                body={"stk_cd": code},
                api_id="ka10001",
                max_retries=0 if fast else 3,
            )
            out     = data
            name    = out.get("stk_nm", "")
            current = abs(int(str(out.get("cur_prc", "0"))
                           .replace(",", "").lstrip("+-") or "0"))
            mktcap  = int(str(out.get("mac", "0"))
                          .replace(",", "") or "0") * 100_000_000
            # 등락률 (±X.XX% 형태 or 숫자 문자열)
            flu_rt_raw = str(out.get("flu_rt", "0")).replace(",", "").replace("%", "").strip()
            try:
                flu_rt = float(flu_rt_raw)
            except ValueError:
                flu_rt = 0.0
            # 누적 거래량
            vol_raw = str(out.get("acc_trd_vol", "0")).replace(",", "").strip()
            try:
                acc_trd_vol = int(vol_raw)
            except ValueError:
                acc_trd_vol = 0
            return {
                "code":          code,
                "name":          name,
                "current_price": current,
                "market_cap":    mktcap,
                "flu_rt":        flu_rt,        # 등락률 (float, %)
                "acc_trd_vol":   acc_trd_vol,   # 누적거래량 (int, 주)
            }
        except Exception as e:
            logger.debug(f"[KiwoomAPI] {code} 현재가 실패: {e}")
            return {"code": code, "name": "", "current_price": 0, "market_cap": 0,
                    "flu_rt": 0.0, "acc_trd_vol": 0}

    def get_investor_data(self, code: str) -> dict:
        """
        ka10060 - 주식 투자자별 순매수 현황 (당일 개인/기관/외국인).
        반환: {"individual": int, "institution": int, "foreign": int}  (단위: 주)
        실패 시 모두 None 반환.
        """
        try:
            data = self._post(
                "/api/dostk/stkinfo",
                body={"stk_cd": code},
                api_id="ka10060",
            )
            def _qty(key):
                raw = str(data.get(key, "0")).replace(",", "").lstrip("+-").strip()
                try:
                    return int(raw)
                except ValueError:
                    return 0
            # 개인: ind_*, 기관: orgn_*, 외국인: frgn_*
            # 필드명이 API마다 다를 수 있어 복수 후보 시도
            indv = (_qty("ind_buy_qty")  or _qty("ind_netbuy")
                    or _qty("indvdl_netbuy_qty") or 0)
            orgn = (_qty("orgn_buy_qty") or _qty("orgn_netbuy")
                    or _qty("instttn_netbuy_qty") or 0)
            frgn = (_qty("frgn_buy_qty") or _qty("frgn_netbuy")
                    or _qty("frgn_netbuy_qty") or 0)
            return {"individual": indv, "institution": orgn, "foreign": frgn}
        except Exception as e:
            logger.debug(f"[KiwoomAPI] {code} 수급 조회 실패: {e}")
            return {"individual": None, "institution": None, "foreign": None}

    # ─────────────────────────────────────────
    # 일봉 데이터 - REST
    # ─────────────────────────────────────────
    def get_daily_data(self, code: str, count: int = 70) -> Optional[pd.DataFrame]:
        """ka10081 - 주식 일봉차트"""
        # ── 차트 API 전용 스로틀 (ka10081 rate limit 보호, 최소 1.1초/call) ──
        _now = time.time()
        _wait = self._chart_min_interval - (_now - self._last_chart_call)
        if _wait > 0:
            time.sleep(_wait)
        self._last_chart_call = time.time()

        try:
            data = self._post(
                "/api/dostk/chart",
                body={
                    "stk_cd":       code,
                    "base_dt":      "00000000",
                    "upd_stkpc_tp": "1",
                },
                api_id="ka10081",
            )
            items = data.get("stk_dt_pole_chart_qry", [])
            if not items:
                logger.debug(f"[KiwoomAPI] {code} 일봉 데이터 없음: {data}")
                return None

            rows = []
            for item in items[:count]:
                def _int(v):
                    return abs(int(str(v).replace(",", "").lstrip("+-") or "0"))
                rows.append({
                    "date":   item.get("dt", ""),
                    "open":   _int(item.get("open_pric",  0)),
                    "high":   _int(item.get("high_pric",  0)),
                    "low":    _int(item.get("low_pric",   0)),
                    "close":  _int(item.get("cur_prc",    0)),
                    "volume": _int(item.get("trde_qty",   0)),
                    "amount": _int(item.get("trde_prica", 0)) * 1_000_000,
                })

            df = pd.DataFrame(rows)
            return df.iloc[::-1].reset_index(drop=True)

        except Exception as e:
            logger.debug(f"[KiwoomAPI] {code} 일봉 실패: {e}")
            return None

    # ─────────────────────────────────────────
    # 분봉 데이터 - REST
    # ─────────────────────────────────────────
    def get_minute_data(self, code: str,
                        tick_range: int = 3,
                        count: int = 100) -> Optional[pd.DataFrame]:
        """ka10080 - 주식 분봉차트"""
        # ── 차트 API 전용 스로틀 (ka10080 rate limit 보호, 최소 1.1초/call) ──
        _now = time.time()
        _wait = self._chart_min_interval - (_now - self._last_chart_call)
        if _wait > 0:
            time.sleep(_wait)
        self._last_chart_call = time.time()

        try:
            data = self._post(
                "/api/dostk/chart",
                body={
                    "stk_cd":       code,
                    "tic_scope":    str(tick_range),
                    "upd_stkpc_tp": "1",
                },
                api_id="ka10080",
            )
            items = data.get("stk_min_pole_chart_qry", [])
            if not items:
                return None

            rows = []
            for item in items[:count]:
                def _int(v):
                    return abs(int(str(v).replace(",", "").lstrip("+-") or "0"))
                rows.append({
                    "datetime": item.get("cntr_tm", ""),
                    "open":     _int(item.get("opn_prc",  0)),
                    "high":     _int(item.get("high_prc", 0)),
                    "low":      _int(item.get("low_prc",  0)),
                    "close":    _int(item.get("cur_prc",  0)),
                    "volume":   _int(item.get("trde_qty", 0)),
                })

            df = pd.DataFrame(rows)
            return df.iloc[::-1].reset_index(drop=True)

        except Exception as e:
            logger.debug(f"[KiwoomAPI] {code} 분봉 실패: {e}")
            return None

    # ─────────────────────────────────────────
    # 조건식 기반 종목 검색 - WebSocket
    # ─────────────────────────────────────────
    def get_condition_list(self) -> list:
        """
        ka10171 - WebSocket으로 조건식 목록 조회.
        응답 data: [["0","조건1"], ["1","조건2"], ...]  (리스트 of 리스트)

        Returns: [{"seq": "0", "name": "조건1"}, ...]
        """
        try:
            self._ensure_ws()
        except TimeoutError as e:
            logger.error(f"[KiwoomAPI] 조건식 목록 조회 중 WS 타임아웃: {e}")
            return []

        # 루프 스레드에서 Future 생성
        future_holder = []

        def _make_future():
            f = self._ws_loop.create_future()
            future_holder.append(f)

        self._ws_loop.call_soon_threadsafe(_make_future)
        # future_holder에 담길 때까지 짧게 대기
        deadline = time.time() + 2.0
        while not future_holder and time.time() < deadline:
            time.sleep(0.01)
        if not future_holder:
            logger.error("[KiwoomAPI] Future 생성 실패")
            return []

        self._cnsrlst_future = future_holder[0]

        # 요청 전송
        req = json.dumps({"trnm": "CNSRLST"})
        try:
            asyncio.run_coroutine_threadsafe(
                self._ws_conn.send(req), self._ws_loop
            ).result(timeout=5)
        except Exception as e:
            logger.error(f"[KiwoomAPI] CNSRLST 전송 실패: {e}")
            return []

        # 응답 대기 (최대 12초)
        try:
            concurrent_future = asyncio.run_coroutine_threadsafe(
                asyncio.wait_for(
                    asyncio.shield(self._cnsrlst_future), timeout=10
                ),
                self._ws_loop
            )
            msg = concurrent_future.result(timeout=12)
        except Exception as e:
            logger.error(f"[KiwoomAPI] 조건식 목록 응답 대기 실패: {e}")
            return []
        finally:
            self._cnsrlst_future = None

        logger.debug(f"[KiwoomAPI] ka10171 raw: {msg}")
        rc = msg.get("return_code", -1)
        if rc != 0:
            logger.warning(
                f"[KiwoomAPI] 조건식 목록 오류 응답: rc={rc} "
                f"msg={msg.get('return_msg', '')}"
            )
            return []
        raw = msg.get("data") or []
        result = []
        for item in raw:
            if isinstance(item, list) and len(item) >= 2:
                result.append({
                    "seq":  str(item[0]).strip(),
                    "name": str(item[1]).strip(),
                })
            elif isinstance(item, dict):
                seq  = str(item.get("seq", item.get("cond_no", ""))).strip()
                name = str(item.get("cond_nm", item.get("cond_name", ""))).strip()
                if seq:
                    result.append({"seq": seq, "name": name})

        logger.info(f"[KiwoomAPI] 조건식 목록: {len(result)}개")
        return result

    def search_by_condition(self, seq: str) -> list:
        """
        ka10172 - WebSocket으로 조건검색 종목 조회 (search_type=0, 일회성).

        Returns: [{"code": "000020", "name": "동화약품"}, ...]
        """
        try:
            self._ensure_ws()
        except TimeoutError as e:
            logger.error(f"[KiwoomAPI] 조건검색 중 WS 타임아웃: {e}")
            return []

        future_holder = []

        def _make_future():
            f = self._ws_loop.create_future()
            future_holder.append(f)

        self._ws_loop.call_soon_threadsafe(_make_future)
        deadline = time.time() + 2.0
        while not future_holder and time.time() < deadline:
            time.sleep(0.01)
        if not future_holder:
            logger.error("[KiwoomAPI] Future 생성 실패 (search_by_condition)")
            return []

        self._cnsrreq_future = future_holder[0]

        req = json.dumps({
            "trnm":        "CNSRREQ",
            "seq":         seq,
            "search_type": "0",
            "stex_tp":     config.CONDITION_STEX_TP,
        })
        try:
            asyncio.run_coroutine_threadsafe(
                self._ws_conn.send(req), self._ws_loop
            ).result(timeout=5)
        except Exception as e:
            logger.error(f"[KiwoomAPI] CNSRREQ 전송 실패: {e}")
            self._cnsrreq_future = None
            return []

        try:
            concurrent_future = asyncio.run_coroutine_threadsafe(
                asyncio.wait_for(
                    asyncio.shield(self._cnsrreq_future), timeout=15
                ),
                self._ws_loop
            )
            msg = concurrent_future.result(timeout=17)
        except Exception as e:
            logger.error(f"[KiwoomAPI] 조건검색 응답 대기 실패 (seq={seq}): {e}")
            return []
        finally:
            self._cnsrreq_future = None

        logger.debug(f"[KiwoomAPI] ka10172 raw: {msg}")
        rc = msg.get("return_code", -1)
        if rc != 0:
            logger.warning(
                f"[KiwoomAPI] 조건검색 오류 응답: rc={rc} "
                f"msg={msg.get('return_msg', '')} (seq={seq})"
            )
            return []
        raw = msg.get("data") or []
        stocks = []
        for item in raw:
            if isinstance(item, list) and len(item) >= 2:
                stocks.append({
                    "code": str(item[0]).strip(),
                    "name": str(item[1]).strip(),
                })
            elif isinstance(item, dict):
                # 실제 응답: {"9001": "A000400", "302": "종목명", ...}
                # "9001" 필드: 종목코드 (앞에 "A" 접두어 포함)
                # "302"  필드: 종목명
                raw_code = (
                    item.get("9001") or          # ka10172 실제 필드명
                    item.get("code")  or
                    item.get("stk_cd") or
                    ""
                ).strip()
                # "A000400", "Q610071" → "000400", "610071" (영문 접두사 제거)
                code = raw_code.lstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ").strip() if raw_code else ""
                name = (
                    item.get("302")   or          # ka10172 실제 필드명
                    item.get("name")  or
                    item.get("stk_nm", "")
                ).strip()
                if code:
                    stocks.append({"code": code, "name": name})

        logger.info(f"[KiwoomAPI] 조건검색({seq}) 결과: {len(stocks)}개")
        return stocks

    def register_condition_realtime(self, seq: str):
        """
        ka10173 - 조건식 실시간 등록 (search_type=1, WebSocket push).
        이후 편입/이탈 발생 시 _condition_callbacks 가 호출됨.
        """
        try:
            self._ensure_ws()
        except TimeoutError as e:
            logger.error(f"[KiwoomAPI] 조건식 실시간 등록 중 WS 타임아웃: {e}")
            return
        req = json.dumps({
            "trnm":        "CNSRREQ",
            "seq":         seq,
            "search_type": "1",
            "stex_tp":     config.CONDITION_STEX_TP,
        })
        try:
            asyncio.run_coroutine_threadsafe(
                self._ws_conn.send(req), self._ws_loop
            ).result(timeout=5)
            logger.info(f"[KiwoomAPI] 조건식 실시간 등록 완료 (seq={seq})")
        except Exception as e:
            logger.error(f"[KiwoomAPI] 조건식 실시간 등록 실패: {e}")

    def add_condition_callback(self, callback):
        """조건식 편입/이탈 콜백 등록. callback(code, name, action) 형태."""
        self._condition_callbacks.append(callback)

    def _handle_condition_realtime(self, msg: dict, action: str):
        """조건식 실시간 편입(IN) / 이탈(OUT) 메시지 처리 → 콜백 호출."""
        data = msg.get("data", [])
        if not isinstance(data, list):
            data = [data] if data else []

        for item in data:
            if isinstance(item, list) and len(item) >= 2:
                code = str(item[0]).strip()
                name = str(item[1]).strip()
            elif isinstance(item, dict):
                code = (item.get("code") or item.get("stk_cd") or "").strip()
                name = (item.get("name") or item.get("stk_nm", "")).strip()
            else:
                continue

            if not code:
                continue
                
            code = code.lstrip("ABCDEFGHIJKLMNOPQRSTUVWXYZ").strip()

            for cb in self._condition_callbacks:
                try:
                    cb(code, name, action)
                except Exception as e:
                    logger.error(f"[KiwoomWS] condition callback 오류: {e}")

    # ─────────────────────────────────────────
    # 계좌 잔고 및 보유 종목 - REST
    # ─────────────────────────────────────────
    def get_positions(self) -> list:
        """kt00018 - 계좌평가잔고내역 기반의 보유 종목 반환"""
        try:
            data = self._post(
                "/api/dostk/acnt",
                body={
                    "acnt_no": config.ACCOUNT_NUMBER,
                    "qry_tp": "2",  # 일반조회
                    "dmst_stex_tp": "KRX",
                },
                api_id="kt00018",
            )
            positions = []
            res_list = data.get("acnt_evlt_remn_indv_tot", [])
            for p in res_list:
                qty = int(p.get("rmnd_qty", "0"))
                if qty > 0:
                    positions.append({
                        "code": p.get("stk_cd", "").replace("A", ""),
                        "name": p.get("stk_nm", ""),
                        "qty": qty,
                        "entry_price": float(p.get("pur_pric", "0"))
                    })
            return positions
        except Exception as e:
            logger.error(f"[KiwoomAPI] 보유 포지션 조회 실패: {e}")
            return []

    def get_balance(self) -> dict:
        """kt00001 - 예수금조회 / kt00018 - 계좌평가잔고내역"""
        # 모의투자는 kt00018을 지원하지 않으므로 kt00001(예수금조회) 사용
        if config.IS_SIMULATION:
            api_id = "kt00001"
            body = {
                "acnt_no": config.ACCOUNT_NUMBER,
                "qry_tp": "3",  # 추정조회
                "dmst_stex_tp": "KRX",
            }
        else:
            api_id = "kt00018"
            body = {
                "qry_tp": "1",
                "dmst_stex_tp": "KRX",
            }
            
        try:
            data = self._post(
                "/api/dostk/acnt",
                body=body,
                api_id=api_id,
            )
            def _i(v): return int(str(v).replace(",","").lstrip("0") or "0")
            
            if config.IS_SIMULATION:
                return {
                    "total":     _i(data.get("entr", "0")),         # 예수금
                    "available": _i(data.get("ord_alow_amt", "0")), # 주문가능금액
                }
            else:
                return {
                    "total":     _i(data.get("tot_evlt_amt",        "0")),
                    "available": _i(data.get("prsm_dpst_aset_amt",  "0")),
                }
        except Exception as e:
            logger.error(f"[KiwoomAPI] 잔고 조회 실패: {e}")
            return {"total": 0, "available": 0}

    # ─────────────────────────────────────────
    # 주문 - REST
    # ─────────────────────────────────────────
    def send_order(self, order_name: str, screen_no: str,
                   code: str, qty: int, price: int,
                   order_type: int, hoga_type: str = "03") -> int:
        """
        REST 모의/실전 통합 주문API (kt10000 / kt10001)
        매수: api-id = kt10000
        매도: api-id = kt10001
        hoga_type: "00"=지정가, "03"=시장가
        """
        api_id  = "kt10000" if order_type == 1 else "kt10001"

        try:
            data = self._post(
                "/api/dostk/ordr",
                body={
                    "acnt_no":  config.ACCOUNT_NUMBER,
                    "stk_cd":   code,
                    "ord_qty":  str(qty),
                    "ord_prc":  "0" if hoga_type == "03" else str(price),
                    "trde_tp":  hoga_type,
                    "dmst_stex_tp": "KRX",
                },
                api_id=api_id,
                max_retries=1,  # 주문 429 시 1회만 재시도 (5초 대기) — 블로킹 방지
            )
            rt = data.get("return_code", -1)
            if rt == 0:
                logger.info(f"[KiwoomAPI] 주문 성공: {order_name} {code} {qty}주")
                return 0
            else:
                logger.error(f"[KiwoomAPI] 주문 실패: {data.get('return_msg')}")
                return -1
        except Exception as e:
            logger.error(f"[KiwoomAPI] 주문 오류: {e}")
            return -1

    # ─────────────────────────────────────────
    # 실시간 체결 구독 - WebSocket
    # ─────────────────────────────────────────
    def add_tick_callback(self, callback):
        """실시간 틱 콜백 등록. callback(tick: dict) 형태."""
        self._tick_callbacks.append(callback)

    def add_chejan_callback(self, callback):
        """체결잔고 콜백 등록 (미구현, 인터페이스 유지용)."""
        pass

    def subscribe_realtime(self, code: str, screen_no: str = ""):
        """
        특정 종목의 실시간 체결/호가 데이터를 웹소켓으로 수신 요청.
        모의투자의 경우 REG 실시간 체결 등록이 지원되지 않거나 105111 에러가 발생하므로 스킵 가능.
        """
        if config.IS_SIMULATION:
            logger.debug(f"[KiwoomAPI] 모의투자에서는 실시간(REG) 구독이 불가하여 생략합니다: {code}")
            return
            
        """
        실시간 체결 구독 - WebSocket으로 등록 요청.
        trnm="REG", item_tp="J" (주식 체결)
        """
        try:
            self._ensure_ws()  # WS 미연결 시 먼저 연결 (race condition 방지)
        except Exception as e:
            logger.error(f"[KiwoomAPI] WS 연결 실패, 구독 불가 ({code}): {e}")
            return

        req = json.dumps({
            "trnm":    "REG",
            "grp_no":  screen_no if screen_no else "0001",
            "refresh": "1",
            "data": [{"item_cd": code, "item_tp": "J"}],
        })
        try:
            asyncio.run_coroutine_threadsafe(
                self._ws_conn.send(req), self._ws_loop
            ).result(timeout=5)
            self._subscribed_codes.add(code)  # 전송 성공 후에만 추가 (재연결 재구독 기준)
            logger.debug(f"[KiwoomAPI] 실시간 구독 등록: {code}")
        except Exception as e:
            logger.error(f"[KiwoomAPI] 실시간 구독 등록 실패 ({code}): {e}")

    def unsubscribe_realtime(self, screen_no: str = ""):
        """장 종료 시 구독 해제 + WS 연결 종료."""
        self._ws_stop_requested = True   # 재연결 방지 플래그
        self._subscribed_codes.clear()
        if self._ws_conn is not None:
            try:
                asyncio.run_coroutine_threadsafe(
                    self._ws_conn.close(), self._ws_loop
                ).result(timeout=5)
            except Exception:
                pass
            self._ws_conn = None
            self._ws_ready.clear()
        logger.info("[KiwoomAPI] 실시간 구독 해제 완료")

    def _handle_realtime_tick(self, msg: dict):
        """
        실시간 체결 메시지 → tick dict 변환 → _tick_callbacks 호출.
        tick = {"code": str, "price": int, "volume": int, "time": str}
        """
        data = msg.get("data", {})
        # data가 리스트면 첫 번째 요소 사용
        if isinstance(data, list):
            if not data:
                return
            data = data[0]
        if not isinstance(data, dict):
            return

        def _i(v):
            return abs(int(str(v).replace(",", "").lstrip("+-") or "0"))

        code   = str(data.get("stk_cd",  data.get("code",   ""))).strip()
        price  = _i(data.get("cur_prc",  data.get("price",  0)))
        volume = _i(data.get("trde_qty", data.get("volume", 0)))
        time_s = str(data.get("cntr_tm", data.get("time",   ""))).strip()

        if not code or not price:
            return

        tick = {"code": code, "price": price, "volume": volume, "time": time_s}
        for cb in self._tick_callbacks:
            try:
                cb(tick)
            except Exception as e:
                logger.error(f"[KiwoomWS] tick callback 오류: {e}")

    # ─────────────────────────────────────────
    # 유틸
    # ─────────────────────────────────────────
    def _throttle(self, seconds: float = 0.25):
        time.sleep(seconds)
