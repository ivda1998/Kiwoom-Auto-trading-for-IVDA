# nh_api.py - NH투자증권(나무) NHPLUG REST + WebSocket 래퍼
#
# KiwoomAPI와 같은 메서드·반환 형식을 제공해 config.BROKER=nh 로 바꿔 끼울 수 있게 한다.
# 명세 정본: https://www.nhplug.com/llms.txt · openapi-docs/krstock/openapi.json
#
# 서버 구성 (2026-10-22 실측):
#   - 토큰 발급은 운영서버 전용(api.nhplug.com). 발급 토큰은 운영·모의 양쪽에서 사용.
#     24시간 유효, 재발급마다 보안 알림 → 파일 캐시, 401일 때만 재발급.
#   - 모의서버(moapi)는 시세 REST를 전부 거부(IGW40023)하고, 모의 WS는 통보(d2·d3)만 허용.
#     → 시세(현재가·일봉·분봉·실시간 체결가)는 항상 운영서버에서 "조회만" 하고,
#       주문·잔고·체결통보만 IS_SIMULATION에 따라 모의/운영 서버로 보낸다.
#   - REST 초당 5회 수준(일일 한도 없음). WS 앱키당 동시 2세션, 세션당 30종목, 구독 초당 10건.
#   - 분봉 시각은 봉 "끝" 시각(09:03 봉 = 09:00~09:03). 봇은 키움처럼 "시작" 시각을 쓰므로
#     변환한다(종가 단일가 15:30 봉만 예외 — 키움도 153000으로 표기).
#   - 성공 판정은 rsp_cd 단일 비교 금지: 기대 블록에 값이 있는지 + rsp_msg로 판단.

import asyncio
import collections
import hashlib
import json
import logging
import os
import ssl
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import requests
import websockets

import config

logger = logging.getLogger(__name__)

LIVE_REST = "https://api.nhplug.com:8443"
MOCK_REST = "https://moapi.nhplug.com:8443"
LIVE_WS = "wss://api.nhplug.com:7070/websocket"
MOCK_WS = "wss://moapi.nhplug.com:17070/websocket"

_WS_MAX_KEYS = 30          # 세션당 실시간 등록 한도(서버 강제 — 넘으면 오류 없이 끊김)
_ORDER_PATHS = ("/krstock/order/",)


def _i(v) -> int:
    """'00000035550' · '-1,234' · '5.000000' 같은 숫자 문자열 → int (부호 무시)."""
    s = str(v if v is not None else "").replace(",", "").strip().lstrip("+-")
    if not s:
        return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


def _f(v) -> float:
    s = str(v if v is not None else "").replace(",", "").replace("%", "").strip()
    try:
        return float(s) if s else 0.0
    except ValueError:
        return 0.0


def _rows(data: dict, block: str) -> list:
    """응답 블록을 항상 리스트로 (블록은 데이터가 있을 때만 오고, 객체/배열 둘 다 가능)."""
    v = data.get(block) if isinstance(data, dict) else None
    if isinstance(v, list):
        return v
    if isinstance(v, dict) and v:
        return [v]
    return []


def _msg(data: dict) -> str:
    if not isinstance(data, dict):
        return str(data)[:200]
    m = data.get("message") or {}
    cd = data.get("rsp_cd") or m.get("msg_code") or ""
    txt = data.get("rsp_msg") or m.get("usr_msg") or ""
    return f"[{cd}] {txt}".strip()


def _clean_name(name: str) -> str:
    # 종목명 앞 1바이트 지수 마커(* KOSPI200 / # 코스닥150) 제거
    return str(name or "").strip().lstrip("*#").strip()


class NHAPI:
    """NH투자증권 NHPLUG 래퍼 (KiwoomAPI와 동일 인터페이스)."""

    BROKER_NAME = "나무"

    def __init__(self):
        self._app_key = config.NH_APP_KEY
        self._app_secret = config.NH_APP_SECRET
        self._account = "".join(ch for ch in str(config.NH_ACCOUNT) if ch.isdigit())
        self._quote_base = LIVE_REST                      # 시세는 항상 운영서버(조회 전용)
        self._trade_base = MOCK_REST if config.IS_SIMULATION else LIVE_REST
        self._notice_ws = MOCK_WS if config.IS_SIMULATION else LIVE_WS
        self._token_cache = os.path.join(config.DATA_DIR, ".nh_token_cache.json")
        self._key_hash = hashlib.sha256(self._app_key.encode()).hexdigest()[:16]

        # ── REST ──
        self._token: Optional[str] = None
        self._token_expires: datetime = datetime.min
        self._token_lock = threading.Lock()
        self._session = requests.Session()
        # 서버별 초당 호출 상한 — 운영 5회 수준(4로 여유), 모의는 초당 2회에서도 429 실측 → 1
        live_rate = max(1, min(5, int(getattr(config, "NH_RATE_LIMIT", 4))))
        self._rates = {LIVE_REST: live_rate, MOCK_REST: 1}
        self._rate_lock = threading.Lock()
        self._call_ts = {LIVE_REST: collections.deque(), MOCK_REST: collections.deque()}
        self._load_token_cache()

        # ── 호출 수 집계 (일일 한도는 없지만 원인 추적용으로 유지) ──
        self._rest_call_count = 0
        self._rest_call_date = None
        self._api_counts: dict = {}

        # ── WebSocket ──
        self._ws_loop: Optional[asyncio.AbstractEventLoop] = None
        self._ws_thread: Optional[threading.Thread] = None
        self._ws_stop_requested = False
        self._quote_ws = None
        self._quote_task = None
        self._notice_task = None
        self._subscribed_codes: set = set()
        self._tick_callbacks: list = []
        self._chejan_callbacks: list = []
        self._condition_callbacks: list = []
        self._last_tick_ts = 0.0

    # ─────────────────────────────────────────
    # 토큰
    # ─────────────────────────────────────────
    def login(self) -> bool:
        ok = self._ensure_token()
        if ok:
            self._ws_stop_requested = False
            self._start_ws_thread()
            if getattr(config, "NH_FILL_NOTICE", True):
                self._start_notice_ws()
        return ok

    def _ensure_token(self, force: bool = False) -> bool:
        with self._token_lock:
            if not force and self._token and datetime.now() < self._token_expires:
                return True
            return self._issue_token()

    def _issue_token(self) -> bool:
        try:
            resp = requests.post(
                LIVE_REST + "/oauth2/token",
                params={"appkey": self._app_key, "appsecretkey": self._app_secret,
                        "grant_type": "client_credentials", "scope": "oob"},
                headers={"content-type": "application/x-www-form-urlencoded"},
                timeout=10,
            )
            data = resp.json()
            token = data.get("access_token")
            if resp.status_code != 200 or not token:
                logger.error(f"[NHAPI] 토큰 발급 실패 HTTP {resp.status_code} {_msg(data)}")
                return False
            self._token = token
            # 만료 5분 전을 기준으로 재발급
            self._token_expires = datetime.now() + timedelta(seconds=int(data.get("expires_in", 86400)) - 300)
            self._save_token_cache()
            logger.info("[NHAPI] 토큰 발급 성공 (만료 %s)", self._token_expires.strftime("%m-%d %H:%M"))
            return True
        except Exception as e:
            logger.error(f"[NHAPI] 토큰 발급 오류: {e}")
            return False

    def _save_token_cache(self):
        try:
            os.makedirs(os.path.dirname(self._token_cache), exist_ok=True)
            with open(self._token_cache, "w") as f:
                json.dump({"token": self._token,
                           "expires": self._token_expires.strftime("%Y%m%d%H%M%S"),
                           "key_hash": self._key_hash}, f)
        except Exception as e:
            logger.debug(f"[NHAPI] 토큰 캐시 저장 실패: {e}")

    def _load_token_cache(self):
        try:
            if not os.path.exists(self._token_cache):
                return
            with open(self._token_cache) as f:
                data = json.load(f)
            if data.get("key_hash") != self._key_hash:
                return
            expires = datetime.strptime(data["expires"], "%Y%m%d%H%M%S")
            if datetime.now() >= expires:
                return
            self._token, self._token_expires = data["token"], expires
            logger.info("[NHAPI] 토큰 캐시 복원 (만료 %s)", expires.strftime("%m-%d %H:%M"))
        except Exception as e:
            logger.debug(f"[NHAPI] 토큰 캐시 로드 실패: {e}")

    # ─────────────────────────────────────────
    # 공통 POST
    # ─────────────────────────────────────────
    def _throttle(self, base: str):
        """서버별 슬라이딩 1초 창 — 초당 상한 이하 (스레드 안전)."""
        q, rate = self._call_ts[base], self._rates[base]
        while True:
            with self._rate_lock:
                now = time.time()
                while q and now - q[0] >= 1.0:
                    q.popleft()
                if len(q) < rate:
                    q.append(now)
                    return
                wait = 1.0 - (now - q[0]) + 0.01
            time.sleep(max(wait, 0.01))

    def _count(self, path: str):
        today = datetime.now().date()
        if self._rest_call_date != today:
            self._rest_call_date, self._rest_call_count, self._api_counts = today, 0, {}
        if path.startswith(_ORDER_PATHS):
            return
        self._rest_call_count += 1
        key = path.rsplit("/", 1)[-1]
        self._api_counts[key] = self._api_counts.get(key, 0) + 1

    def _post(self, base: str, path: str, inp: dict, cts: str = None, cts_flag: str = None,
              max_429: int = 3, timeout: float = 10.0):
        """반환: (data, cts, cts_flag). HTTP 200이 아니어도 본문(JSON)을 그대로 돌려준다 —
        업무 성공 판정은 호출측이 블록 존재 + rsp_msg로 한다.
        재시도는 '서버가 처리하지 않은' 경우만: 401(토큰 재발급 1회)·429(유량, 최대 max_429회).
        네트워크 오류·타임아웃은 재전송하지 않는다(주문 중복 방지)."""
        if not self._ensure_token():
            raise RuntimeError("[NHAPI] 토큰 없음")
        self._count(path)
        body = json.dumps({"Input_0": inp or {}})
        resp = None
        retried_auth = False
        n429 = 0
        while True:
            headers = {"authorization": f"Bearer {self._token}",
                       "content-type": "application/json; charset=UTF-8"}
            if cts:
                headers["cts"] = cts
            if cts_flag:
                headers["cts_flag"] = cts_flag
            self._throttle(base)
            resp = self._session.post(base + path, headers=headers, data=body, timeout=timeout)
            if resp.status_code == 401 and not retried_auth:
                # 토큰 무효 → 1회만 재발급 (429 등 다른 오류에는 재발급 금지)
                retried_auth = True
                logger.warning(f"[NHAPI] 401 토큰 무효 → 재발급 후 재시도 ({path})")
                self._ensure_token(force=True)
                continue
            if resp.status_code == 429 and n429 < max_429:
                n429 += 1
                logger.warning(f"[NHAPI] 429 유량 초과 ({path}) → {0.5 * n429:.1f}초 후 재시도")
                time.sleep(0.5 * n429)
                continue
            break
        try:
            data = resp.json()
        except Exception:
            data = {"_raw": resp.text[:300] if resp is not None else ""}
        h = {k.lower(): v for k, v in resp.headers.items()} if resp is not None else {}
        if resp is not None and resp.status_code != 200:
            logger.warning(f"[NHAPI] HTTP {resp.status_code} {path} {_msg(data)}")
        return data, (h.get("cts") or "").strip() or None, (h.get("cts_flag") or "").strip() or None

    def get_api_usage(self) -> dict:
        """REST 호출 수 (주문 제외). 나무는 일일 한도가 없으므로 limit=None."""
        today = datetime.now().date()
        is_today = self._rest_call_date == today
        return {
            "count": self._rest_call_count if is_today else 0,
            "limit": None,
            "remaining": None,
            "pct": 0.0,
            "date": str(today),
            "by_api": dict(self._api_counts) if is_today else {},
        }

    # ─────────────────────────────────────────
    # 시세 (운영서버 조회 전용)
    # ─────────────────────────────────────────
    def get_stock_info(self, code: str, fast: bool = False) -> dict:
        empty = {"code": code, "name": "", "current_price": 0, "market_cap": 0,
                 "flu_rt": 0.0, "acc_trd_vol": 0}
        try:
            data, _, _ = self._post(self._quote_base, "/krstock/quote/v1/currentPrice",
                                    {"market_cd": "KRX", "iem_cd": code},
                                    max_429=0 if fast else 3)
            o = (_rows(data, "Output_0") or [{}])[0]
            price = _i(o.get("stck_prpr"))
            if not price:
                logger.debug(f"[NHAPI] {code} 현재가 없음 {_msg(data)}")
                return empty
            # 등락률: prdy_ctrt는 부호가 없고 장 마감 후엔 등락부호도 비어 온다(실측) →
            # 전일종가로 직접 계산. 없으면 부호 코드(4·8 하한, 5·9 하락)로 보정.
            prev = _i(o.get("stck_prdy_clpr"))
            if prev > 0:
                flu = round((price - prev) / prev * 100, 2)
            else:
                flu = abs(_f(o.get("prdy_ctrt")))
                if str(o.get("prdy_vrss_sign", "")).strip() in ("4", "5", "8", "9"):
                    flu = -flu
            return {
                "code": code,
                "name": _clean_name(o.get("iem_nm")),
                "current_price": price,
                "market_cap": _i(o.get("hts_avls")) * 100_000_000,
                "flu_rt": flu,
                "acc_trd_vol": _i(o.get("acml_vol")),
            }
        except Exception as e:
            logger.debug(f"[NHAPI] {code} 현재가 실패: {e}")
            return empty

    def get_investor_data(self, code: str) -> dict:
        try:
            data, _, _ = self._post(self._quote_base, "/krstock/quote/v1/currentInvestor",
                                    {"market_cd": "KRX", "iem_cd": code, "array_cnt": "1"})
            rows = _rows(data, "Output_0")
            if not rows:
                raise ValueError(_msg(data))
            r = rows[0]

            def _signed(v):
                s = str(v or "0").replace(",", "").strip()
                try:
                    return int(float(s))
                except ValueError:
                    return 0
            return {"individual": _signed(r.get("person")),
                    "institution": _signed(r.get("gigwan")),
                    "foreign": _signed(r.get("frgn_ntby_qty"))}
        except Exception as e:
            logger.debug(f"[NHAPI] {code} 수급 조회 실패: {e}")
            return {"individual": None, "institution": None, "foreign": None}

    def _period(self, code: str, gubun: str, xtick: str, count: int,
                paginate: bool = False, max_pages: int = 1) -> list:
        """기간별시세(일=1, 분=5). 최신순으로 받은 Output_1 행을 모아 반환."""
        rows, cts, flag = [], None, None
        seen = set()
        for _ in range(max(1, max_pages)):
            data, cts2, flag2 = self._post(self._quote_base, "/krstock/quote/v1/period", {
                "market_cd": "KRX", "iem_cd": code, "mrkt_div_cls_code": "1",
                "edate": datetime.now().strftime("%Y%m%d"), "array_cnt": str(min(max(count, 1), 900)),
                "maxavg": "0", "gubun": gubun, "xtick": xtick, "today_cls_code": "0",
                "fake_tick": "1", "sur_flag": "0", "sur_gb_day_cnt": "", "sur_bf_end_time": "",
                "out1_scale_change": "", "out2_scale_change": "", "view_main_yn": "Y",
            }, cts=cts, cts_flag=flag)
            page = _rows(data, "Output_1")
            if not page:
                if not rows:
                    logger.warning(f"[NHAPI] {code} 기간별시세(gubun={gubun}) 응답 없음 {_msg(data)}")
                break
            rows.extend(page)
            if (not paginate or not cts2 or cts2 in seen
                    or (flag2 or "").upper() == "N"):
                break
            seen.add(cts2)
            cts, flag = cts2, flag2
        return rows

    def get_daily_data(self, code: str, count: int = 70) -> Optional[pd.DataFrame]:
        try:
            rows = self._period(code, "1", "", count)
            if not rows:
                return None
            out = [{
                "date": str(r.get("bsop_date", "")).strip(),
                "open": _i(r.get("stck_oprc")),
                "high": _i(r.get("stck_hgpr")),
                "low": _i(r.get("stck_lwpr")),
                "close": _i(r.get("stck_prpr")),
                "volume": _i(r.get("vol")),
                "amount": _i(r.get("tr_pbmn")),
            } for r in rows if r.get("bsop_date")]
            df = pd.DataFrame(out).drop_duplicates("date")
            return df.sort_values("date").tail(count).reset_index(drop=True)
        except Exception as e:
            logger.debug(f"[NHAPI] {code} 일봉 실패: {e}")
            return None

    @staticmethod
    def _bar_start(date: str, hhmmss: str, minutes: int) -> str:
        """봉 끝 시각 → 시작 시각(키움 표기). 15:30 종가 단일가 봉은 그대로."""
        hhmmss = str(hhmmss).strip().zfill(6)
        if hhmmss == "153000":
            return date + hhmmss
        end = datetime.strptime(date + hhmmss, "%Y%m%d%H%M%S")
        return (end - timedelta(minutes=minutes)).strftime("%Y%m%d%H%M%S")

    def get_minute_data(self, code: str, tick_range: int = 3, count: int = 100,
                        paginate: bool = False, max_pages: int = 10) -> Optional[pd.DataFrame]:
        try:
            # 형성 중인 봉(필터로 제외됨)까지 감안해 1개 더 받는다
            rows = self._period(code, "5", str(tick_range), count + 1,
                                paginate=paginate, max_pages=max_pages if paginate else 1)
            if not rows:
                return None
            out = []
            for r in rows:
                d, t = str(r.get("bsop_date", "")).strip(), str(r.get("bsop_time", "")).strip()
                if len(d) != 8 or not t:
                    continue
                out.append({
                    "datetime": self._bar_start(d, t, int(tick_range)),
                    "open": _i(r.get("stck_oprc")),
                    "high": _i(r.get("stck_hgpr")),
                    "low": _i(r.get("stck_lwpr")),
                    "close": _i(r.get("stck_prpr")),
                    "volume": _i(r.get("vol")),
                })
            if not out:
                return None
            df = pd.DataFrame(out).drop_duplicates("datetime").sort_values("datetime")
            if not paginate:
                df = df.tail(count + 1)
            return df.reset_index(drop=True)
        except Exception as e:
            logger.warning(f"[NHAPI] {code} 분봉 실패: {e}")
            return None

    # ─────────────────────────────────────────
    # 나무 API에 없는 기능 (빈 결과 — 돌파 스캔·/movers 전용 기능)
    # ─────────────────────────────────────────
    def get_all_stocks(self, market: str = "10") -> list:
        logger.warning("[NHAPI] 전종목 목록 API 없음(종목마스터 파일 필요) — 빈 목록 반환")
        return []

    def get_top_change_rate(self, *args, **kwargs) -> list:
        logger.warning("[NHAPI] 등락률 순위 API 없음 — 빈 목록 반환")
        return []

    def get_volume_surge(self, *args, **kwargs) -> list:
        logger.warning("[NHAPI] 거래량 급증 순위 API 없음 — 빈 목록 반환")
        return []

    def get_condition_list(self) -> list:
        logger.info("[NHAPI] 조건검색 미지원 — 빈 목록")
        return []

    def search_by_condition(self, seq: str) -> list:
        return []

    def register_condition_realtime(self, seq: str):
        logger.info("[NHAPI] 조건검색 실시간 등록 미지원 — 생략")

    def add_condition_callback(self, callback):
        self._condition_callbacks.append(callback)

    # ─────────────────────────────────────────
    # 계좌 (IS_SIMULATION → 모의서버)
    # ─────────────────────────────────────────
    def _balance_raw(self) -> dict:
        # get_balance·get_positions가 연달아 불리는 경우가 많아(모의서버 429 실측) 2초 캐시
        cached = getattr(self, "_bal_cache", None)
        if cached and time.time() - cached[0] < 2.0:
            return cached[1]
        data = self._balance_fetch()
        self._bal_cache = (time.time(), data)
        return data

    def _balance_fetch(self) -> dict:
        data, _, _ = self._post(self._trade_base, "/krstock/inquiry/v1/balance", {
            "act_no": self._account, "bnc_bse_cd": "1", "ltg_aot_dit_cd": "9",
            "aet_bse": "2", "qut_dit_cd": "KRX", "aly_qut_cd": "1",
        })
        if not _rows(data, "Output_0"):
            raise RuntimeError(f"잔고 응답 없음 {_msg(data)}")
        return data

    def get_positions(self) -> list:
        try:
            data = self._balance_raw()
            out = []
            for p in _rows(data, "Output_1"):
                qty = _i(p.get("itg_bnc_qty"))
                if qty <= 0:
                    continue
                code = str(p.get("iem_cd", "")).strip()
                if len(code) == 7 and code[0] == "A":
                    code = code[1:]
                out.append({"code": code, "name": str(p.get("iem_nm", "")).strip(),
                            "qty": qty, "entry_price": float(_i(p.get("phs_pr")))})
            return out
        except Exception as e:
            logger.error(f"[NHAPI] 보유 포지션 조회 실패: {e}")
            return []

    def get_balance(self) -> dict:
        """키움과 같은 의미로 반환: 모의=예수금/주문가능, 실전=총평가/순자산.
        NH_BALANCE_CAP(>0)이면 상한 적용 — 모의 예수금(50억)이 키움 모의와 달라
        자본 비례 사이징이 어긋나는 것을 막는다."""
        try:
            o = _rows(self._balance_raw(), "Output_0")[0]
            if config.IS_SIMULATION:
                # orr_pbl_amt2~4는 증거금률 기준 미수 포함 금액(예수금의 2.5~5배) — 현금 매매엔
                # D+2 예수금(결제 반영 후 실제 쓸 수 있는 현금)을 쓴다.
                total = _i(o.get("dca"))
                avail = _i(o.get("nxt2_dd_dca")) or _i(o.get("drn_pbl_amt")) or total
            else:
                total = _i(o.get("tot_eal_amt"))
                avail = _i(o.get("nas_amt"))
            cap = int(getattr(config, "NH_BALANCE_CAP", 0) or 0)
            if cap > 0:
                total, avail = min(total, cap), min(avail, cap)
            return {"total": total, "available": avail}
        except Exception as e:
            logger.error(f"[NHAPI] 잔고 조회 실패: {e}")
            return {"total": 0, "available": 0}

    # ─────────────────────────────────────────
    # 주문 (IS_SIMULATION → 모의서버)
    # ─────────────────────────────────────────
    def send_order(self, order_name: str, screen_no: str, code: str, qty: int, price: int,
                   order_type: int, hoga_type: str = "03") -> tuple:
        """order_type 1=매수 2=매도, hoga_type "00"=지정가 "03"=시장가. 반환 (0, 주문번호) / (-1, "")."""
        path = "/krstock/order/v1/cashBuy" if order_type == 1 else "/krstock/order/v1/cashSell"
        market = hoga_type == "03"
        # 수량·가격은 숫자 타입(문자열이면 IGW40011). 시장가는 가격 필드를 빼고, 금액·정지가격도 넣지 않음 (SDK 예제와 동일)
        inp = {
            "act_no": self._account, "iem_cd": code, "orr_qty": int(qty),
            "nmn_pr_tp_cd": "05" if market else "01", "orr_cnd_dit_cd": "00",
            "ssl_nmn_pr_dit_cd": "00",
            "rmt_mkt_cd": getattr(config, "NH_ORDER_MARKET", "KRX"), "sor_mkt_sli_yn": "N",
        }
        if not market:
            inp["orr_pr"] = int(price)
        try:
            # 주문: 401·429(서버 미처리)만 재시도 — 타임아웃 등은 재전송 안 함(중복 주문 방지)
            data, _, _ = self._post(self._trade_base, path, inp)
            o = (_rows(data, "Output_0") or [{}])[0]
            ord_no = str(o.get("mkt_orr_no") or o.get("anw_cld_mkt_orr_no1") or "").strip()
            if ord_no:
                logger.info(f"[NHAPI] 주문 성공: {order_name} {code} {qty}주 ord_no={ord_no} {_msg(data)}")
                return (0, ord_no)
            logger.error(f"[NHAPI] 주문 실패: {order_name} {code} {qty}주 @{price} {_msg(data)}")
            return (-1, "")
        except Exception as e:
            logger.error(f"[NHAPI] 주문 오류: {e}")
            return (-1, "")

    def cancel_order(self, org_ord_no: str, code: str, qty: int) -> int:
        try:
            data, _, _ = self._post(self._trade_base, "/krstock/order/v1/cancel", {
                "act_no": self._account, "org_mkt_orr_no": str(org_ord_no),
                "all_pat_dit_cd": "1", "iem_cd": code,   # 전량 취소 — 수량(cor_qty, 숫자)은 일부 취소 때만
            })
            o = (_rows(data, "Output_0") or [{}])[0]
            if str(o.get("mkt_orr_no") or "").strip():
                logger.info(f"[NHAPI] 주문 취소 성공: {code} org={org_ord_no} {_msg(data)}")
                return 0
            logger.error(f"[NHAPI] 주문 취소 실패: {code} org={org_ord_no} {_msg(data)}")
            return -1
        except Exception as e:
            logger.error(f"[NHAPI] 주문 취소 오류: {e}")
            return -1

    # ─────────────────────────────────────────
    # 실시간 (WebSocket)
    # ─────────────────────────────────────────
    def add_tick_callback(self, callback):
        self._tick_callbacks.append(callback)

    def add_chejan_callback(self, callback):
        self._chejan_callbacks.append(callback)

    def _start_ws_thread(self):
        if self._ws_loop and self._ws_thread and self._ws_thread.is_alive():
            return
        self._ws_loop = asyncio.new_event_loop()
        self._ws_thread = threading.Thread(target=self._ws_loop.run_forever, daemon=True, name="NHWS")
        self._ws_thread.start()

    @staticmethod
    def _ssl_ctx(url: str):
        if not url.startswith("wss://"):
            return None
        try:
            import truststore   # 운영 WS는 중간 CA를 안 보내 OS 저장소가 필요할 수 있음
            return truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        except ImportError:
            return ssl.create_default_context()

    def _sub_msg(self, tr_cd: str, key: str, tr_type: str = "1") -> str:
        return json.dumps({"header": {"token": self._token, "tr_type": tr_type},
                           "body": {"tr_cd": tr_cd, "tr_key": key}})

    async def _ws_session(self, url: str, label: str, on_open, on_msg):
        """연결 → on_open(구독) → 수신 루프. 끊기면 3초 후 재연결(정상 종료 요청 시 중단)."""
        ping = int(getattr(config, "NH_WS_PING", 30)) or None
        while not self._ws_stop_requested:
            try:
                async with websockets.connect(url, ssl=self._ssl_ctx(url), open_timeout=10,
                                              ping_interval=ping, ping_timeout=20) as ws:
                    logger.info(f"[NHWS] {label} 연결: {url}")
                    await on_open(ws)
                    async for raw in ws:
                        try:
                            on_msg(json.loads(raw))
                        except Exception as e:
                            logger.debug(f"[NHWS] {label} 메시지 처리 오류: {e} raw={str(raw)[:200]}")
            except Exception as e:
                logger.warning(f"[NHWS] {label} 연결 오류: {type(e).__name__} {e}")
            finally:
                if label == "시세":
                    self._quote_ws = None
            if self._ws_stop_requested:
                break
            self._ensure_token()
            await asyncio.sleep(3)
        logger.info(f"[NHWS] {label} 세션 종료")

    def _ensure_quote_ws(self):
        if self._quote_task and not self._quote_task.done():
            return
        self._start_ws_thread()

        async def on_open(ws):
            self._quote_ws = ws
            for code in sorted(self._subscribed_codes):
                await ws.send(self._sub_msg("mc", code))
                await asyncio.sleep(0.12)          # 구독 초당 10건 한도
            if self._subscribed_codes:
                logger.info(f"[NHWS] 시세 구독 {len(self._subscribed_codes)}종목: {sorted(self._subscribed_codes)}")

        self._quote_task = asyncio.run_coroutine_threadsafe(
            self._ws_session(LIVE_WS, "시세", on_open, self._on_quote_msg), self._ws_loop)

    def _start_notice_ws(self):
        if self._notice_task and not self._notice_task.done():
            return
        self._start_ws_thread()

        async def on_open(ws):
            await ws.send(self._sub_msg("d2", ""))   # 체결통보 (tr_key=빈 값)

        self._notice_task = asyncio.run_coroutine_threadsafe(
            self._ws_session(self._notice_ws, "체결통보", on_open, self._on_notice_msg), self._ws_loop)

    def subscribe_realtime(self, code: str, screen_no: str = ""):
        if code in self._subscribed_codes:
            return
        if len(self._subscribed_codes) >= _WS_MAX_KEYS:
            logger.error(f"[NHAPI] 실시간 구독 한도({_WS_MAX_KEYS}) 초과 — {code} 구독 불가")
            return
        if not self._ensure_token():
            return
        self._ws_stop_requested = False
        self._subscribed_codes.add(code)
        ws = self._quote_ws
        if ws is None:
            self._ensure_quote_ws()     # 연결 시 on_open이 전체 목록을 구독
            return
        try:
            asyncio.run_coroutine_threadsafe(ws.send(self._sub_msg("mc", code)), self._ws_loop).result(timeout=5)
            logger.debug(f"[NHAPI] 실시간 구독 등록: {code}")
        except Exception as e:
            logger.warning(f"[NHAPI] 실시간 구독 전송 실패({code}) — 재연결 시 복원: {e}")

    def unsubscribe_realtime(self, screen_no: str = ""):
        """장 종료: 구독 반납 + 연결 종료(재연결 안 함)."""
        self._ws_stop_requested = True
        ws, codes = self._quote_ws, sorted(self._subscribed_codes)
        self._subscribed_codes.clear()
        if ws is not None and self._ws_loop:
            async def _close():
                for c in codes:
                    try:
                        await ws.send(self._sub_msg("mc", c, tr_type="2"))
                    except Exception:
                        break
                await ws.close()
            try:
                asyncio.run_coroutine_threadsafe(_close(), self._ws_loop).result(timeout=10)
            except Exception:
                pass
        for t in (self._quote_task, self._notice_task):
            if t:
                t.cancel()
        self._quote_ws = None
        logger.info("[NHAPI] 실시간 구독 해제 완료")

    def _on_quote_msg(self, msg: dict):
        h, b = msg.get("header") or {}, msg.get("body") or {}
        if h.get("tr_type") or h.get("rsp_cd"):       # 구독 응답(ACK)
            rc = str(h.get("rsp_cd", ""))
            if rc and rc != "00000":
                logger.warning(f"[NHWS] 구독 응답 {h.get('tr_cd')} {rc} {h.get('rsp_msg')}")
            return
        if h.get("tr_cd") != "mc" or not isinstance(b, dict):
            return
        code = str(b.get("code") or h.get("tr_key") or "").strip()
        price = _i(b.get("price"))
        if not code or not price:
            return
        self._last_tick_ts = time.time()
        tick = {"code": code, "price": price, "volume": _i(b.get("movolume")),
                "time": str(b.get("time", "")).replace(":", "").strip()}
        for cb in self._tick_callbacks:
            try:
                cb(tick)
            except Exception as e:
                logger.error(f"[NHWS] tick callback 오류: {e}")

    def _on_notice_msg(self, msg: dict):
        h, b = msg.get("header") or {}, msg.get("body") or {}
        if h.get("tr_type") or h.get("rsp_cd"):
            rc = str(h.get("rsp_cd", ""))
            if rc and rc != "00000":
                logger.warning(f"[NHWS] 체결통보 구독 응답 {rc} {h.get('rsp_msg')}")
            return
        if h.get("tr_cd") != "d2" or not isinstance(b, dict):
            return
        fill = {"order_no": str(b.get("orderno", "")).lstrip("0"),
                "code": str(b.get("issuecd", "")).strip(),
                "name": str(b.get("issue_nm", "")).strip(),
                "side": "SELL" if str(b.get("slbygb")) == "1" else "BUY",
                "qty": _i(b.get("concgty")), "price": _i(b.get("concprc")),
                "time": str(b.get("conctime", "")).strip(),
                "rejected": str(b.get("rejgb", "0")) not in ("", "0")}
        logger.info(f"[NHWS] 체결통보: {fill}")
        for cb in self._chejan_callbacks:
            try:
                cb(fill)
            except Exception as e:
                logger.error(f"[NHWS] chejan callback 오류: {e}")
