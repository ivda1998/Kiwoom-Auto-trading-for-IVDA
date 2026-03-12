# scanner.py - 장 시작 전 후보 종목 스캔

import time
import logging
from datetime import datetime
from typing import Optional, List, Dict

import numpy as np
import pandas as pd

import config

logger = logging.getLogger(__name__)


class StockScanner:
    """
    일봉 데이터 기반 후보 종목 필터링
    전략: 20/60일 고점 근접 + 거래대금 + 추세 조건

    [스캔 우선순위]
    1. 조건식 경로 (ka10171 → ka10172 → ka10081)
       config.CONDITION_NAME 또는 CONDITION_SEQ가 설정되어 있을 때 사용
       API 호출: ~15~35회 (1700회 한도 대비 매우 안전)
    2. 기존 경로 (ka10099 → ka10081) - 폴백
       조건식 미설정·실패 시 자동 사용
       API 호출: ~300~500회
    """

    def __init__(self, kiwoom):
        self.kiwoom = kiwoom
        self._candidates = []

    # ─────────────────────────────────────────
    # 메인 스캔 (장 시작 전 실행)
    # ─────────────────────────────────────────
    def run_scan(self, market: str = "10") -> list:
        """
        조건식 기반 스캔 우선 시도, 실패/미설정 시 기존 ka10099 방식으로 폴백.

        Returns: [{"code": ..., "name": ..., "reason": ...}, ...]
        """
        # 수동 지정 종목 및 Telegram 신호 중 "오늘 날짜"인 것만 추출 + 기존 보유종목 제외
        executor_positions = list(getattr(self.kiwoom, "get_all_positions", lambda: {})().keys())
        if hasattr(self, "_get_executor_positions_hook"):
            executor_positions = self._get_executor_positions_hook()
            
        custom_targets = self._get_custom_targets(existing_positions=executor_positions)
        
        use_condition = bool(
            getattr(config, "CONDITION_NAME", "").strip()
            or getattr(config, "CONDITION_SEQ",  "").strip()
        )

        condition_results = []
        if use_condition:
            logger.info("[Scanner] 조건식 기반 스캔 시도")
            result = self._run_condition_scan()

            if result is not None:
                condition_results = result
                logger.info(f"[Scanner] 조건식 스캔 1차 완료: {len(condition_results)}개")
            else:
                # 조건식 스캔 실패
                if not getattr(config, "CONDITION_FALLBACK", True):
                    logger.error("[Scanner] 조건식 스캔 실패, 폴백 비활성화 → 수동 종목만 반환")
                    self._candidates = custom_targets[:config.MAX_CANDIDATES]
                    return self._candidates
                
                logger.warning("[Scanner] 조건식 스캔 실패 → 기존 방식(ka10099)으로 폴백")
                condition_results = self._run_legacy_scan(market)
        else:
            condition_results = self._run_legacy_scan(market)

        # 수동 지정 종목과 조건 검색 종목을 병합 (수동 지정 종목 우선)
        self._candidates = self._merge_candidates(custom_targets, condition_results)
        self._candidates = self._candidates[:config.MAX_CANDIDATES]
        logger.info(f"[Scanner] 최종 후보군 스캔 완료: {len(self._candidates)}개 (수동 지정 포함)")
        
        return self._candidates

    # ─────────────────────────────────────────
    def _get_custom_targets(self, existing_positions: list = None) -> list:
        if existing_positions is None:
            existing_positions = []
            
        targets_conf = config.get_custom_targets(filter_today=True)
        if not targets_conf:
            return []

        logger.info(f"[Scanner] 수동 지정 CUSTOM_TARGETS 확인: {list(targets_conf.keys())}")
        custom_candidates = []
        for term in targets_conf.keys():
            term_str = str(term).strip()
            if not term_str:
                continue
                
            # 종목코드 6자리인지 이름인지 판단
            if term_str.isdigit() and len(term_str) == 6:
                code = term_str
                if not hasattr(self, "_custom_target_names"):
                    self._custom_target_names = {}
                
                if code in self._custom_target_names:
                    name = self._custom_target_names[code]
                else:
                    info = self.kiwoom.get_stock_info(code)
                    name = info.get("name", "") if info else ""
                    if name:
                        self._custom_target_names[code] = name
                        
                if not name:
                    logger.warning(f"[Scanner] 수동 종목코드 '{code}'의 이름을 찾을 수 없습니다.")
                    continue
            else:
                name = term_str
                code = self._get_code_by_name(name)
                
                if not code:
                    logger.warning(f"[Scanner] 수동 종목명 '{name}'의 코드를 찾을 수 없습니다.")
                    continue

            # 이미 보유 중인 종목이라면 신규 매수 감시 대상에서 제외
            if code in existing_positions or name in existing_positions:
                logger.debug(f"[Scanner] 수동 후보 '{name}({code})' 이미 보유 중이므로 감시대상 스킵")
                continue

            # _evaluate_stock 호출 (check_filters=False로 기본 고점 데이터만 수집)
            try:
                result = self._evaluate_stock(code, name, check_filters=False)
                if result:
                    # CUSTOM_TARGETS의 경우 무조건 우선순위이므로 강제 점수 부여
                    result["score"] = 9999999.0
                    custom_candidates.append(result)
                    logger.info(f"[Scanner] 수동 후보 등록 성공: {name}({code})")
                else:
                    logger.warning(f"[Scanner] 수동 후보 '{name}({code})' 평가 불가 (일봉 부족 등)")
            except Exception as e:
                logger.error(f"[Scanner] 수동 후보 '{name}({code})' 처리 중 에러: {e}")

        # 전체 매매 대상 종목의 50% 제한 적용 (MAX_CUSTOM_TARGETS)
        if len(custom_candidates) > config.MAX_CUSTOM_TARGETS:
            logger.warning(f"[Scanner] 수동 후보 초과! {len(custom_candidates)}명 중 {config.MAX_CUSTOM_TARGETS}명만 선착순 반영")
            custom_candidates = custom_candidates[:config.MAX_CUSTOM_TARGETS]

        return custom_candidates

    def _get_code_by_name(self, name: str) -> Optional[str]:
        if not getattr(self, "_name_to_code", None):
            self._name_to_code = {}
            for i, market in enumerate(["10", "3", "8"]):
                if i > 0:
                    time.sleep(1.0) # API 요청 한도(429) 방지를 위한 대기
                try:
                    stocks = self.kiwoom.get_all_stocks(market)
                    for s in stocks:
                        self._name_to_code[s["name"]] = s["code"]
                except Exception as e:
                    logger.debug(f"[Scanner] 이름 변환용 시장({market}) 조회 실패: {e}")
        return self._name_to_code.get(name)

    def _merge_candidates(self, custom: list, generated: list) -> list:
        merged = list(custom)
        custom_codes = {c["code"] for c in custom}
        
        # 생성된 후보 리스트를 추가 (중복 제외)
        generated_sorted = self._sort_by_score(generated)
        for c in generated_sorted:
            if c["code"] not in custom_codes:
                merged.append(c)
                
        return merged

    # ─────────────────────────────────────────
    # 조건식 경로 (ka10171 → ka10172 → ka10081)
    # ─────────────────────────────────────────
    def _run_condition_scan(self) -> Optional[list]:
        """
        1. ka10171: 조건식 목록 조회 (1회)
        2. ka10172: 조건검색 결과 종목 조회 (1~N회)
        3. ka10081: 일봉 세부 검증 (결과 종목 수만큼)

        Returns: candidates 리스트(성공) 또는 None(실패)
        """
        # ── 1단계: 조건식 목록 조회 (ka10171) ──
        cond_list = self.kiwoom.get_condition_list()
        if not cond_list:
            logger.warning(
                "[Scanner] 조건식 목록 없음. "
                "영웅문4 > 조건검색 > 조건식 작성 후 '서버 저장' 필요"
            )
            return None

        # ── 2단계: 사용할 조건식 seq 탐색 (결과 없으면 후보 목록 순차 시도) ──
        raw_stocks = self._try_condition_list(cond_list)
        if raw_stocks is None:
            return None   # seq 자체를 못 찾은 경우
        if not raw_stocks:
            # 대화형 선택: [] 반환 → run_scan()에서 레거시 스캔 없이 종료
            # 자동 선택:   None 반환 → run_scan()에서 레거시 스캔으로 폴백
            if getattr(config, "_interactive_selected", False):
                logger.warning("[Scanner] 선택 조건식 결과 없음 → 빈 후보로 시작")
                return []
            return None   # 모든 조건식 결과 없음 → 레거시 스캔 폴백

        logger.info(f"[Scanner] 조건식 결과 {len(raw_stocks)}개 종목 → ka10081 세부 검증")

        # ── 3단계: 일봉 기본 정보 수집 (ka10081, 필터 없음) ──
        # 조건식이 이미 종목을 선별했으므로 추가 필터 없이 고점/거래대금 정보만 수집
        candidates = []
        for i, stock in enumerate(raw_stocks):
            code = stock["code"]
            name = stock["name"]
            try:
                result = self._evaluate_stock(code, name, check_filters=False)
                if result:
                    candidates.append(result)
                    logger.info(f"[Scanner] 조건식 후보: {name}({code})")

                if len(candidates) >= config.MAX_CANDIDATES:
                    break

            except Exception as e:
                logger.debug(f"[Scanner] {code} 평가 실패: {e}")
                continue

        return candidates

    def _try_condition_list(self, cond_list: list) -> Optional[list]:
        """
        1순위(CONDITION_NAME) → 결과 없으면 CONDITION_FALLBACK_NAMES 순서로 재시도.

        대화형 선택(_interactive_selected=True)인 경우:
          선택한 조건식만 시도. CONDITION_FALLBACK_NAMES 사용 안 함.
          (_choose_condition에서 이미 결과 확인 후 사용자가 계속 진행을 선택한 것이므로)

        Returns:
          - 종목 리스트 (len >= 1): 성공
          - []                    : 조건식 결과 없음
          - None                  : seq 자체를 찾지 못함
        """
        primary       = getattr(config, "CONDITION_NAME", "").strip()
        user_selected = getattr(config, "_interactive_selected", False)

        if user_selected:
            # 대화형: 선택한 조건식만 사용 (폴백 이름 목록 없음)
            candidates_names = [primary] if primary else []
        else:
            # 자동: 1순위 + CONDITION_FALLBACK_NAMES (기존 동작)
            fallback_names = list(getattr(config, "CONDITION_FALLBACK_NAMES", []))
            candidates_names = ([primary] if primary else []) + [
                n for n in fallback_names if n != primary
            ]

        # CONDITION_SEQ 직접 지정 모드 (이름 목록이 비어있을 때)
        if not candidates_names:
            seq = getattr(config, "CONDITION_SEQ", "").strip()
            if not seq:
                return None
            stocks = self.kiwoom.search_by_condition(seq)
            if stocks:
                logger.info(f"[Scanner] 조건식 seq={seq} 결과: {len(stocks)}개")
            else:
                logger.warning(f"[Scanner] 조건식 seq={seq} 결과 없음")
            return stocks

        # 이름 목록 순서대로 시도
        name_to_seq = {c["name"]: c["seq"] for c in cond_list}
        for name in candidates_names:
            seq = name_to_seq.get(name, "")
            if not seq:
                logger.debug(f"[Scanner] 조건식 '{name}' 목록에 없음, 건너뜀")
                continue

            logger.info(f"[Scanner] 조건식 시도: '{name}' (seq={seq})")
            stocks = self.kiwoom.search_by_condition(seq)
            if stocks:
                logger.info(f"[Scanner] 조건식 '{name}'(seq={seq}) 결과: {len(stocks)}개")
                return stocks
            logger.warning(f"[Scanner] 조건식 '{name}'(seq={seq}) 결과 없음, 다음 시도")

        logger.warning(f"[Scanner] 모든 조건식 결과 없음 → ka10099 폴백")
        return []

    def _find_condition_seq(self, cond_list: list) -> str:
        """
        config의 CONDITION_NAME 또는 CONDITION_SEQ를 사용하여
        ka10171 결과에서 대상 조건식의 seq를 반환.

        우선순위: CONDITION_NAME (이름 일치) > CONDITION_SEQ (직접 지정)
        """
        target_name = getattr(config, "CONDITION_NAME", "").strip()
        target_seq  = getattr(config, "CONDITION_SEQ",  "").strip()

        if target_name:
            for cond in cond_list:
                if cond["name"] == target_name:
                    logger.info(
                        f"[Scanner] 조건식 발견: '{target_name}' (seq={cond['seq']})"
                    )
                    return cond["seq"]

            # 이름 불일치 → 사용 가능한 목록 출력 (디버깅 지원)
            available = [f"'{c['name']}'(seq={c['seq']})" for c in cond_list]
            logger.warning(
                f"[Scanner] 조건식 이름 '{target_name}' 없음. "
                f"사용 가능한 조건식: {', '.join(available) if available else '없음'}"
            )
            return ""

        if target_seq:
            # seq 직접 지정 모드: 목록에 있는지 검증
            for cond in cond_list:
                if cond["seq"] == target_seq:
                    logger.info(f"[Scanner] 조건식 seq={target_seq} 확인됨 ('{cond['name']}')")
                    return target_seq
            # 목록에 없어도 일단 시도 (모의투자 등 목록이 다를 수 있음)
            logger.warning(f"[Scanner] 조건식 seq={target_seq} 목록에 없음, 직접 시도")
            return target_seq

        return ""

    # ─────────────────────────────────────────
    # 기존 경로 (ka10099 → ka10081) - 폴백
    # ─────────────────────────────────────────
    def _run_legacy_scan(self, market: str = "10") -> list:
        """
        ka10099(전종목) + 가격/시총 로컬 필터 + ka10081(일봉 검증)
        조건식 미설정 또는 조건식 스캔 실패 시 자동 사용.
        """
        logger.info("[Scanner] 기존 방식(ka10099) 종목 스캔 시작")
        all_stocks = self.kiwoom.get_all_stocks(market)
        logger.info(f"[Scanner] 전체 종목 수: {len(all_stocks)}")

        # ── 1차 필터: 가격 + 시총 추정 ──
        # 시총 추정 = list_count(상장주수) × last_price
        price_filtered = []
        for s in all_stocks:
            price = s["last_price"]
            if not (1_000 <= price <= 100_000):   # 1000원~10만원
                continue
            list_count = s["list_count"]
            est_mktcap = list_count * price        # 추정 시총 (원)
            if list_count > 0 and not (
                config.MIN_MARKET_CAP <= est_mktcap <= config.MAX_MARKET_CAP
            ):
                continue
            s["est_mktcap"] = est_mktcap
            price_filtered.append(s)

        logger.info(f"[Scanner] 가격/시총 필터 통과: {len(price_filtered)}개")

        candidates = []
        for i, stock in enumerate(price_filtered):
            code       = stock["code"]
            name       = stock["name"]

            try:
                result = self._evaluate_stock(code, name)
                if result:
                    candidates.append(result)
                    logger.info(f"[Scanner] 후보 추가: {name}({code})")

                if len(candidates) >= config.MAX_CANDIDATES:
                    break

            except Exception as e:
                logger.debug(f"[Scanner] {code} 평가 실패: {e}")
                continue

        candidates = self._sort_by_score(candidates)
        self._candidates = candidates[:config.MAX_CANDIDATES]
        logger.info(f"[Scanner] 최종 후보: {len(self._candidates)}개")
        return self._candidates

    # ─────────────────────────────────────────
    # 개별 종목 평가 (ka10081 일봉 사용)
    # ─────────────────────────────────────────
    def _evaluate_stock(self, code: str, name: str = "",
                        check_filters: bool = True) -> Optional[dict]:
        """
        ka10081 일봉 데이터로 종목 정보 수집 및 (선택적) 전략 조건 평가.

        check_filters=True  : 기존 ka10099 레거시 스캔 경로 — 6가지 조건 필터 적용
        check_filters=False : 조건식 결과 종목 경로 — 조건식이 이미 선별했으므로
                              필터 없이 고점/거래대금 기본 정보만 수집
        """
        # 일봉 데이터 (ka10081)
        if not hasattr(self, "_daily_data_cache"):
            self._daily_data_cache = {}

        if code in self._daily_data_cache:
            daily = self._daily_data_cache[code]
        else:
            daily = self.kiwoom.get_daily_data(code, count=70)
            if daily is not None:
                self._daily_data_cache[code] = daily

        if daily is None or len(daily) < 61: # 오늘 포함 과거 60일치 조회를 위해 최소 61개 필요
            return None

        close  = daily["close"].values
        high   = daily["high"].values
        low    = daily["low"].values
        volume = daily["volume"].values if "volume" in daily.columns else daily["amount"].values
        amount = daily["amount"].values

        current = close[-1]
        if current <= 0:
            return None

        # 신고가 기준: 금일 제외, 과거의 '종가/고가'
        close_prev = close[:-1]
        
        # 진입 전략(1번/2번)에 따른 기준가 설정
        if getattr(config, "ENTRY_STRATEGY_TYPE", 1) == 2:
            # 2번 전략: 전일 종가를 기준으로 돌파 매매
            high_20    = close_prev[-1]
            high_60    = close_prev[-1]
        else:
            # 1번 전략: 기존 20일 / 60일 고점 기준 눌림 매수
            high_20 = np.max(high[-20:-1]) if len(high) >= 20 else np.max(high[:-1])
            high_60 = np.max(high[-60:-1]) if len(high) >= 60 else np.max(high[:-1])
        
        # 분모 방어 (0 나눗셈 방지)
        if high_20 <= 0 or high_60 <= 0:
            return None

        dist_20    = abs(current - high_20) / high_20
        dist_60    = abs(current - high_60) / high_60
        avg_amount = np.mean(amount[-5:])
        avg_volume = np.mean(volume[-5:])   # 5일 평균 거래량 (우선순위 정렬용)

        if check_filters:
            # ── 조건 1: 20/60일 고점 근접 ──────────────
            if dist_20 > config.HIGH_PROXIMITY_RATE:
                return None

            # ── 조건 2: 5일 평균 거래대금 ≥ 100억 ──────
            if avg_amount < config.MIN_DAILY_VOLUME:
                return None

            # ── 조건 3: 20일 이동평균선 위에 위치 ────────
            ma20 = np.mean(close[-20:])
            if current < ma20:
                return None

            # ── 조건 4: 최근 5일 변동성 축소 ────────────
            recent_ranges = high[-5:]  - low[-5:]
            older_ranges  = high[-10:-5] - low[-10:-5]
            if np.mean(recent_ranges) >= np.mean(older_ranges):
                return None

            # ── 조건 5: 장기 하락 추세 제외 ──────────────
            ma60 = np.mean(close[-60:])
            if current < ma60 * 0.95:
                return None

            # ── 조건 6: 이미 30% 이상 급등 제외 ──────────
            low_20     = np.min(low[-20:])
            surge_rate = (current - low_20) / low_20
            if surge_rate > 0.30:
                return None

        return {
            "code":          code,
            "name":          name,
            "current_price": int(current),
            "market_cap":    0,       # ka10001 미호출 → 필요 시 개별 조회
            "avg_amount":    avg_amount,   # 5일 평균 거래대금
            "avg_volume":    avg_volume,   # 5일 평균 거래량
            "high_20":       int(high_20),
            "high_60":       int(high_60),
            "dist_20":       round(dist_20, 4),
            "dist_60":       round(dist_60, 4),
            "reason":        f"20일고점근접({dist_20:.1%}), 거래대금({avg_amount/1e8:.0f}억), 거래량({avg_volume:,.0f}주)",
        }

    # ─────────────────────────────────────────
    # 우선순위 정렬
    # ─────────────────────────────────────────
    def _sort_by_score(self, stocks: list) -> list:
        """
        거래대금(70%) + 거래량(30%) 가중평균 스코어로 내림차순 정렬.
        각 지표를 최대값으로 정규화(0~1)한 뒤 가중합산.
        avg_volume 필드가 없는 종목은 avg_amount만으로 정렬.
        """
        if not stocks:
            return stocks

        max_amount = max(s.get("avg_amount", 0) for s in stocks) or 1
        max_volume = max(s.get("avg_volume", 0) for s in stocks) or 1

        def score(s):
            s_amount = s.get("avg_amount", 0) / max_amount
            s_volume = s.get("avg_volume", 0) / max_volume
            return s_amount * 0.7 + s_volume * 0.3

        stocks.sort(key=score, reverse=True)

        # 상위 종목 스코어 로그
        top = stocks[:min(5, len(stocks))]
        score_log = " | ".join(
            f"{s['name']}({score(s):.3f})" for s in top
        )
        logger.info(f"[Scanner] 우선순위 상위: {score_log}")

        return stocks

    # ─────────────────────────────────────────
    # 조회용
    # ─────────────────────────────────────────
    def get_candidates(self) -> list:
        return self._candidates

    def get_candidate_codes(self) -> list:
        return [c["code"] for c in self._candidates]

    def print_summary(self):
        logger.info("=" * 60)
        logger.info(f"[Scanner] 후보 종목 {len(self._candidates)}개")
        for c in self._candidates:
            logger.info(
                f"  {c['name']:10s} ({c['code']}) | "
                f"현재가: {c['current_price']:,} | "
                f"거래대금: {c['avg_amount']/1e8:.0f}억 | "
                f"{c['reason']}"
            )
        logger.info("=" * 60)

        # 콘솔 출력 (사용자 확인용)
        if not self._candidates:
            print("\n  📋 스캔 결과: 후보 종목 없음\n")
            return

        import config as _cfg
        now_hm  = datetime.now().strftime("%H:%M")
        in_mkt  = config.TRADE_START_TIME <= now_hm <= config.TRADE_END_TIME
        timing  = "장 중" if in_mkt else "장 시작 전"
        print(f"\n  📋 후보 종목 스캔 결과 [{now_hm} / {timing}]: {len(self._candidates)}종목")
        print("  " + "─" * 58)
        for i, c in enumerate(self._candidates, start=1):
            cur   = c.get("current_price", 0)
            h20   = c.get("high_20", 0)
            h60   = c.get("high_60", 0)
            d20   = c.get("dist_20", 0)
            amt   = c.get("avg_amount", 0)
            est_keep = 1 - _cfg.STOP_LOSS_RATE
            print(f"  {i:>2}. [{c['name']}({c['code']})]")
            print(f"      현재가: {cur:,}원 | 20일고점: {h20:,}원 | 60일고점: {h60:,}원")
            print(f"      고점 근접도: {d20:.1%} | 거래대금: {amt/1e8:.0f}억원/일")
            threshold_pct = _cfg.NEAR_HIGH_BUY_THRESHOLD * 100
            print(f"      매수 조건: 20일/60일 신고가 ±{threshold_pct:.0f}% 이내 즉시 매수")
            print(f"      예상 손절가: 진입가 × {est_keep:.0%} (진입 후 -{_cfg.STOP_LOSS_RATE:.0%})")
            print(f"      대기 제한: {_cfg.WATCHING_TIMEOUT_CANDLES}봉 ({_cfg.WATCHING_TIMEOUT_CANDLES * _cfg.CANDLE_INTERVAL}분)")
        print("  " + "─" * 58 + "\n")


class MarketFilter:
    """
    코스닥 지수 필터 - 시장 전체 하락 시 매매 중단
    """

    def __init__(self, kiwoom):
        self.kiwoom = kiwoom
        self._kosdaq_candles = []

    def update(self, candles: list):
        """3분봉 업데이트"""
        self._kosdaq_candles = candles

    def is_bullish(self) -> bool:
        """
        코스닥 3분봉 5MA가 상승 중이면 True
        """
        if len(self._kosdaq_candles) < config.MARKET_FILTER_MA + 1:
            return True  # 데이터 부족 시 허용

        closes = [c["close"] for c in self._kosdaq_candles]
        ma_now  = sum(closes[-config.MARKET_FILTER_MA:]) / config.MARKET_FILTER_MA
        ma_prev = sum(closes[-(config.MARKET_FILTER_MA + 1):-1]) / config.MARKET_FILTER_MA

        return ma_now >= ma_prev
