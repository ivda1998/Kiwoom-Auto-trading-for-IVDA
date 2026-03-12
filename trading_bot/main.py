# main.py - 트레이딩 봇 메인 진입점 (REST API 버전)
# OCX / PyQt5 의존성 완전 제거

import sys
import time
import threading
import logging
from datetime import datetime

import config
from logger import setup_logger, TradeLogger
from kiwoom_api import KiwoomAPI
from scanner import StockScanner, MarketFilter
from data_handler import DataManager
from strategy import BreakoutStrategy, SignalType
from execution import OrderExecutor
from risk_manager import RiskManager

# 로거 초기화
logger = setup_logger("main")


class TradingBot:
    """
    메인 트레이딩 봇 (REST API + WebSocket 기반)
    흐름: 로그인 → 종목 스캔 → 실시간 구독 → 봉 확정마다 전략 실행 → 청산
    """

    def __init__(self):
        self.kiwoom = KiwoomAPI()
        self.risk = RiskManager(self.kiwoom)
        self.scanner = StockScanner(self.kiwoom)
        self.market_filter = MarketFilter(self.kiwoom)
        self.data_mgr = DataManager(self.kiwoom)
        self.strategy = BreakoutStrategy()
        self.trade_logger = TradeLogger()
        self.executor = OrderExecutor(self.kiwoom, self.risk)

        self._candidates = []
        self._running = False
        self._stop_event = threading.Event()
        self._condition_seq: str = ""              # ka10171에서 확인한 seq
        self._known_condition_codes: set = set()   # 이미 전략에 등록된 조건식 종목
        self._last_prices: dict = {}               # 종목별 마지막 틱 가격 (상태 보드용)
        self._today_amount: dict = {}              # 종목별 오늘 누적 거래대금 (price×volume, 실시간)
        self._last_out_of_hours_log: dict = {}     # 시간 외 매수 차단 로그 중복 방지 (종목별 마지막 로그 시각)
        self._last_sim_poll: dict = {}             # 모의투자 틱 폴링용 타이머 (종목별)
        self._last_scan_time: float = 0.0          # 마지막 스캔 주기 체크용 타이머

    # ─────────────────────────────────────────
    # 시작
    # ─────────────────────────────────────────
    def start(self):
        logger.info("=" * 60)
        logger.info("[Bot] 트레이딩 봇 시작")
        logger.info(f"[Bot] 모드: {'모의투자' if config.IS_SIMULATION else '실계좌'}")

        # 키움 REST 토큰 발급
        if not self.kiwoom.login():
            logger.error("[Bot] 토큰 발급 실패 → 종료")
            sys.exit(1)

        logger.info("[Bot] 토큰 발급 성공")

        # 자본 초기화
        self.risk.initialize()

        # 기존 보유 종목 동기화
        self._sync_existing_positions()

        # 진입 전략(1/2) 대화형 선택
        self._choose_strategy_type()

        # 대화형 조건식 선택 (CONDITION_INTERACTIVE=True 일 때)
        self._choose_condition()

        # 후보 종목 초기 스캔 (일봉 기준)
        self._run_initial_scan()

        # 장 중 실시간 조건식 모니터링 시작 (ka10173)
        self._start_condition_polling()

        # 실시간 틱 콜백 등록
        self.kiwoom.add_tick_callback(self._on_tick)

        self._running = True
        logger.info("[Bot] 매매 대기 중...")

        # 메인 루프 (1분 주기 타이머 역할)
        self._main_loop()

    # ─────────────────────────────────────────
    # 대화형 전략 및 조건식 선택
    # ─────────────────────────────────────────
    def _choose_strategy_type(self):
        """
        진입 전략 (1번/2번) 사용자 선택
        """
        print("\n" + "=" * 55)
        print("  [진입 전략 선택]")
        print("  1) 20일/60일 신고가 돌파 및 ±1% 내 눌림 매수 (기존)")
        print("  2) 3분봉이 전일 종가를 상향 돌파 시, 다음 봉 시가 매수")
        print("=" * 55)
        
        while True:
            try:
                choice = input("  번호 입력 (1 또는 2, 기본=1): ").strip()
                if not choice:
                    config.ENTRY_STRATEGY_TYPE = 1
                    break
                elif choice in ("1", "2"):
                    config.ENTRY_STRATEGY_TYPE = int(choice)
                    break
                else:
                    print("  1 또는 2를 입력하세요.")
            except (EOFError, KeyboardInterrupt):
                config.ENTRY_STRATEGY_TYPE = 1
                print("\n  입력 취소 → 기본(1번) 전략 사용")
                break
        
        logger.info(f"[Bot] 선택된 진입 전략: {config.ENTRY_STRATEGY_TYPE}번")

    def _choose_condition(self):
        """
        CONDITION_INTERACTIVE=True 일 때 콘솔에서 조건식을 선택.
        선택한 조건식이 결과 없으면 재선택 메뉴 표시 (루프).
        선택 결과는 config.CONDITION_NAME / CONDITION_SEQ 를 런타임에 덮어씀.
        """
        if not getattr(config, "CONDITION_INTERACTIVE", False):
            config._interactive_selected = False
            return

        # WS 연결 후 조건식 목록 조회
        logger.info("[Bot] 조건식 목록 조회 중...")
        cond_list = self.kiwoom.get_condition_list()

        if not cond_list:
            logger.warning("[Bot] 조건식 목록 없음 → 자동 선택 사용")
            config._interactive_selected = False
            return

        while True:
            selected = self._show_condition_menu(cond_list)

            # 0(자동) 또는 Enter → config 기본값 사용, 폴백 허용
            if selected is None:
                logger.info(
                    f"[Bot] 조건식 자동 선택: "
                    f"'{getattr(config, 'CONDITION_NAME', '')}'"
                )
                config._interactive_selected = False
                return

            # config 런타임 덮어쓰기 (파일은 변경하지 않음)
            config.CONDITION_NAME = selected["name"]
            config.CONDITION_SEQ  = selected["seq"]
            config._interactive_selected = True
            logger.info(f"[Bot] 조건식 선택: '{selected['name']}' (seq={selected['seq']})")

            # 선택한 조건식 즉시 결과 확인 (ka10172)
            stocks = self.kiwoom.search_by_condition(selected["seq"])
            count  = len(stocks) if stocks else 0
            logger.info(f"[Bot] 조건식 '{selected['name']}' 결과: {count}개")

            if count > 0:
                print(f"  → '{selected['name']}' 결과: {count}개 종목\n")
                return

            # 결과 없음 → 재선택 메뉴
            print(f"\n  선택한 조건식 '{selected['name']}' 결과: 0개")
            print("  ─────────────────────────────────────────")
            print("  어떻게 하시겠습니까?")
            print("    1) 다른 조건식 선택")
            print("    2) 이 조건식으로 계속 (빈 후보, 실시간 편입 대기)")
            print("    3) 봇 종료")
            print("  ─────────────────────────────────────────")

            while True:
                try:
                    sub = input("  번호 입력 (1~3): ").strip()
                except (EOFError, KeyboardInterrupt):
                    sub = "2"

                if sub == "1":
                    break           # 바깥 while True 루프로 → 조건식 재선택
                elif sub == "2":
                    logger.info("[Bot] 빈 후보로 계속 진행 (실시간 편입 대기)")
                    return
                elif sub == "3":
                    print("  봇을 종료합니다.")
                    sys.exit(0)
                else:
                    print("  1, 2, 3 중 하나를 입력하세요.")

    def _show_condition_menu(self, cond_list: list):
        """
        조건식 목록을 콘솔에 표시하고 사용자 선택을 받아 반환.

        Returns:
            dict  - 선택한 조건식 {"name": ..., "seq": ...}
            None  - 0(자동) 선택
        """
        # 현재 설정된 이름의 기본 인덱스 계산
        default_idx = 0
        cur_name = getattr(config, "CONDITION_NAME", "").strip()
        for i, cond in enumerate(cond_list, start=1):
            if cond["name"] == cur_name:
                default_idx = i
                break

        print("\n" + "=" * 55)
        print("  [조건식 선택]")
        print("  0) 자동 선택 (config.py 설정 그대로 사용)")
        for i, cond in enumerate(cond_list, start=1):
            marker = "  ← 현재 설정" if i == default_idx else ""
            print(f"  {i:>2}) {cond['name']:<30s} (seq={cond['seq']}){marker}")
        print("=" * 55)

        while True:
            try:
                raw = input(f"  번호 입력 (0~{len(cond_list)}, 기본={default_idx}): ").strip()
                choice = int(raw) if raw else default_idx
                if 0 <= choice <= len(cond_list):
                    break
                print(f"  0~{len(cond_list)} 범위의 번호를 입력하세요.")
            except ValueError:
                print("  숫자를 입력하세요.")
            except (EOFError, KeyboardInterrupt):
                print("\n  입력 취소 → 자동 선택 사용")
                choice = 0
                break

        if choice == 0:
            return None

        print(f"  → '{cond_list[choice - 1]['name']}' (seq={cond_list[choice - 1]['seq']}) 선택됨")
        return cond_list[choice - 1]

    def _sync_existing_positions(self):
        """프로그램 재시작 시 기존에 보유 중인 종목을 불러와 상태 복구"""
        positions = self.kiwoom.get_positions()
        for p in positions:
            code = p["code"]
            name = p["name"]
            
            # 1) 포지션 복구
            self.executor.sync_position(
                code=code,
                name=name,
                qty=p["qty"],
                entry_price=p["entry_price"]
            )
            
            # 2) 전략 내에 상태 강제 진입(ENTERED) 처리 (기준고점 0으로)
            self.strategy.init_stock(code, name=name, high_20=0, high_60=0)
            self.strategy.notify_entered(code, entry_price=p["entry_price"])
            
            # 3) 감시 대상(candidates) 등록 (틱 조회를 위해 필요)
            if code not in [c["code"] for c in self._candidates]:
                self._candidates.append({
                    "code": code,
                    "name": name,
                    "high_20": 0.0,
                    "high_60": 0.0
                })
                
            # 4) 데이터 매니저 초기화 및 실시간 구독
            self.data_mgr.init_stock(
                code,
                on_candle_close=lambda candle, _code=code: self._on_candle_close(candle)
            )
            if not config.IS_SIMULATION:
                self.kiwoom.subscribe_realtime(code)
                
            logger.info(f"[Bot] 기존 보유 종목 복구: {name}({code}) {p['qty']}주, 진입가 {p['entry_price']:,.0f}")

    # ─────────────────────────────────────────
    # 후보 종목 초기 스캔 (봇 시작 시 1회, 일봉 기준)
    # ─────────────────────────────────────────
    def _run_initial_scan(self):
        now_hm   = datetime.now().strftime("%H:%M")
        in_market = config.TRADE_START_TIME <= now_hm <= config.TRADE_END_TIME
        if in_market:
            logger.info(
                f"[Bot] 후보 종목 스캔 (장 중 실행 {now_hm} | 일봉 기준 — "
                "오늘 장 중 데이터가 포함될 수 있음)"
            )
        else:
            logger.info(f"[Bot] 후보 종목 스캔 (장 시작 전 {now_hm} | 전일 종가 기준)")
        new_cands = self.scanner.run_scan()
        self.scanner.print_summary()

        existing_codes = {c["code"] for c in self._candidates}
        for c in new_cands:
            code = c["code"]
            if code not in existing_codes:
                self._candidates.append(c)
                # 전략에 종목 등록
                self.strategy.init_stock(code, name=c["name"], high_20=c.get("high_20", 0), high_60=c.get("high_60", 0))
                # 분봉 데이터 초기화
                self.data_mgr.init_stock(
                    code,
                    on_candle_close=lambda candle, _code=code: self._on_candle_close(candle)
                )
                if not config.IS_SIMULATION:
                    self.kiwoom.subscribe_realtime(code)
                logger.info(f"[Bot] 구독 시작: {c['name']}({code})")

        self._last_scan_time = time.time()
        self._print_status_board()

    def _update_candidates(self):
        """장중 주기적 조건식 재선별 스캔 (새로운 종목만 편입)"""
        logger.info("[Bot] 주기적 조건식 실시간 재스캔 중...")
        new_cands = self.scanner.run_scan()
        if not new_cands:
            return

        added_count = 0
        existing_codes = {c["code"] for c in self._candidates}
        
        for c in new_cands:
            code = c["code"]
            if code not in existing_codes:
                self._candidates.append(c)
                # 전략 등록
                self.strategy.init_stock(code, name=c["name"], high_20=c["high_20"], high_60=c["high_60"])
                self.data_mgr.init_stock(
                    code,
                    on_candle_close=lambda candle, _code=code: self._on_candle_close(candle)
                )
                self.kiwoom.subscribe_realtime(code)
                added_count += 1
                logger.info(f"[Bot] 주기적 스캔 신규 편입: {c['name']}({code})")
        
        if added_count > 0:
            print(f"\n  🔍 (주기적 수색) 신규 조건 만족 종목 {added_count}개 감시 추가 완료\n")
            self._print_status_board()

    # ─────────────────────────────────────────
    # 장 중 조건식 실시간 폴링 (ka10173)
    # ─────────────────────────────────────────
    def _start_condition_polling(self):
        """
        ka10173 실시간 등록 (WebSocket push 방식).
        폴링 루프 없이 WS 콜백으로 편입/이탈 이벤트를 수신.
        """
        use_condition = bool(
            getattr(config, "CONDITION_NAME", "").strip()
            or getattr(config, "CONDITION_SEQ",  "").strip()
        )
        if not use_condition:
            logger.info("[Bot] 조건식 미설정 → 실시간 등록 생략")
            return

        cond_list = self.kiwoom.get_condition_list()
        if not cond_list:
            logger.warning("[Bot] 조건식 목록 없음 → 실시간 등록 생략")
            return

        target_name = getattr(config, "CONDITION_NAME", "").strip()
        target_seq  = getattr(config, "CONDITION_SEQ",  "").strip()
        seq = ""
        if target_name:
            for c in cond_list:
                if c["name"] == target_name:
                    seq = c["seq"]
                    break
        if not seq and target_seq:
            seq = target_seq

        if not seq:
            logger.warning("[Bot] 조건식 seq를 찾을 수 없음 → 실시간 등록 생략")
            return

        self._condition_seq = seq
        self._known_condition_codes = {c["code"] for c in self._candidates}

        # 실시간 편입/이탈 콜백 등록 후 ka10173 WebSocket push 등록
        self.kiwoom.add_condition_callback(self._on_condition_event)
        self.kiwoom.register_condition_realtime(seq)
        logger.info(f"[Bot] 조건식 실시간 등록 완료 (seq={seq})")

    def _on_condition_event(self, code: str, name: str, action: str):
        """
        조건식 편입(action='IN') / 이탈(action='OUT') WebSocket 콜백.
        """
        if action == "IN":
            self._on_condition_in(code, name)
        else:
            logger.debug(f"[Bot] 조건식 이탈: {name}({code})")

    def _on_condition_in(self, code: str, name: str):
        """
        장 중 조건식에 신규 편입된 종목을 전략에 동적 등록.
        ka10081 일봉 세부 검증 후 통과 시 BreakoutStrategy에 추가.
        """
        logger.info(f"[Bot] 조건식 신규 편입: {name}({code})")

        # 이미 전략에 등록된 종목 스킵
        if self.strategy.get_state(code) is not None:
            logger.debug(f"[Bot] {code} 이미 등록됨, 스킵")
            return

        # 최대 후보 수 초과 시 스킵
        if len(self._candidates) >= config.MAX_CANDIDATES:
            logger.debug(f"[Bot] 최대 후보 수({config.MAX_CANDIDATES}) 초과, {code} 스킵")
            return

        # ka10081 일봉 기본 정보 수집 (조건식이 이미 선별 → 추가 필터 없음)
        result = self.scanner._evaluate_stock(code, name, check_filters=False)
        if not result:
            logger.debug(f"[Bot] {code} 일봉 데이터 수집 실패 → 등록 안함")
            return

        # 전략·데이터 등록
        self._candidates.append(result)
        self.strategy.init_stock(code, name=name, high_20=result["high_20"], high_60=result["high_60"])
        self.data_mgr.init_stock(
            code,
            on_candle_close=lambda candle, _code=code: self._on_candle_close(candle)
        )
        self.kiwoom.subscribe_realtime(code)
        logger.info(
            f"[Bot] 장 중 신규 등록 완료: {name}({code}) | "
            f"20일고점근접({result['dist_20']:.1%}), "
            f"거래대금({result['avg_amount']/1e8:.0f}억)"
        )

        # 콘솔에 상세 정보 출력
        est_keep_rate = 1 - config.STOP_LOSS_RATE
        threshold_pct = config.NEAR_HIGH_BUY_THRESHOLD * 100
        print(f"\n  ✅ 신규 후보 등록: [{name}({code})]")
        print(f"     현재가: {result['current_price']:,}원 | 20일고점: {result['high_20']:,}원 | 60일고점: {result.get('high_60', 0):,}원")
        print(f"     고점 근접도(20일): {result['dist_20']:.1%} | 거래대금: {result['avg_amount']/1e8:.0f}억원/일")
        print(f"     매수 조건: 20일/60일 신고가 ±{threshold_pct:.0f}% 이내 즉시 매수")
        print(f"     예상 손절가: 진입가 × {est_keep_rate:.0%} (진입 후 -{config.STOP_LOSS_RATE:.0%})")
        print(f"     대기 제한: {config.WATCHING_TIMEOUT_CANDLES}봉 ({config.WATCHING_TIMEOUT_CANDLES * config.CANDLE_INTERVAL}분)")
        self._print_status_board()

    # ─────────────────────────────────────────
    # 실시간 틱 처리
    # ─────────────────────────────────────────
    def _on_tick(self, tick: dict):
        code = tick["code"]
        price = tick["price"]

        # 마지막 가격 캐시 업데이트 (상태 보드용)
        self._last_prices[code] = price

        # 실시간 누적 거래대금 갱신 (price × volume)
        volume = tick.get("volume", 0)
        self._today_amount[code] = self._today_amount.get(code, 0) + price * volume

        # DataManager에 틱 전달 (봉 집계)
        self.data_mgr.on_tick(tick)

        # 포지션 보유 중인 종목: 실시간 청산 체크
        if self.executor.has_position(code):
            candles = self.data_mgr.get_candles(code)
            current = self.data_mgr.get_builder(code).get_current_candle()
            if current:
                signal = self.strategy.on_candle_update(
                    current, candles, price
                )
                if signal in (SignalType.SELL_PROFIT, SignalType.SELL_STOP, SignalType.SELL_TARGET, SignalType.SELL_TRAILING):
                    self._execute_sell(code, price, signal)
            return  # 포지션 보유 종목은 아래 진입 체크 생략

        # ── 후보 종목 실시간 진입 체크 (봉 마감 없이 즉시) ──
        if not self._in_trade_hours() or self.risk.is_halted:
            # 시간 외 차단 로그 (종목당 1분에 1번만 출력)
            if not self._in_trade_hours():
                last_t = self._last_out_of_hours_log.get(code, 0)
                now_ts = time.time()
                if now_ts - last_t >= 60:
                    state_chk = self.strategy.get_state(code)
                    if state_chk and state_chk.phase == "WATCHING":
                        logger.debug(
                            f"[Bot] {code} 시간 외 → 매수 차단 "
                            f"(거래 시작: {config.TRADE_START_TIME})"
                        )
                        self._last_out_of_hours_log[code] = now_ts
            return

        state = self.strategy.get_state(code)
        if not (state and state.phase == "WATCHING"):
            return

        # 1번 전략: 20/60일 신고가 ±NEAR_HIGH_BUY_THRESHOLD 이내 → 즉시 매수
        if getattr(config, "ENTRY_STRATEGY_TYPE", 1) == 1:
            for high in [state.high_20, state.high_60]:
                if high > 0 and abs(price - high) / high <= config.NEAR_HIGH_BUY_THRESHOLD:
                    if not self.market_filter.is_bullish():
                        logger.debug("[Bot] 시장 필터: 코스닥 하락 → 실시간 진입 보류")
                        break
                    state.phase = "ENTERING"  # 중복 진입 방지
                    logger.info(
                        f"[Bot] {code} 실시간 진입 시도: {price:,}원 / "
                        f"기준고점 {high:,}원 ({abs(price - high) / high:.2%})"
                    )
                    self._execute_buy_at(code, price)
                    break

    # ─────────────────────────────────────────
    # 봉 확정 처리
    # ─────────────────────────────────────────
    def _on_candle_close(self, candle):
        code = candle.code

        # 매매 시간 체크
        if not self._in_trade_hours():
            return

        # 시장 필터
        if not self.market_filter.is_bullish():
            logger.debug("[Bot] 시장 필터: 코스닥 하락 → 진입 보류")
            return

        # 리스크 체크
        if self.risk.is_halted:
            return

        # 이미 포지션 보유 중이면 청산 로직만
        if self.executor.has_position(code):
            return

        # 전략 신호 판단
        candles = self.data_mgr.get_candles(code)
        signal = self.strategy.on_candle_close(candle, candles)

        if signal == SignalType.BUY:
            self._execute_buy(code, candle)
        elif signal == SignalType.TIMEOUT:
            self._remove_candidate(code)

    # ─────────────────────────────────────────
    # 매수 실행
    # ─────────────────────────────────────────
    def _execute_buy(self, code: str, candle):
        """봉 확정 시 매수 (on_candle_close 콜백에서 호출)"""
        self._execute_buy_at(code, candle.close)

    def _execute_buy_at(self, code: str, price: int):
        """
        실제 매수 실행 (틱/봉 공통).
        - 봉 확정 진입: _execute_buy() → _execute_buy_at(candle.close)
        - 실시간 틱 진입: _on_tick() → _execute_buy_at(tick_price)
        매수 실패 시 state.phase를 WATCHING으로 복구해 다음 기회 유지.
        """
        state = self.strategy.get_state(code)
        if not state:
            return

        cand = next((c for c in self._candidates if c["code"] == code), None)
        name = cand["name"] if cand else code
        stoploss = round(price * (1 - config.STOP_LOSS_RATE))

        success = self.executor.buy(
            code=code,
            name=name,
            current_price=price,
            stoploss_price=stoploss
        )

        custom_targets = config.get_custom_targets()
        is_custom = bool(custom_targets.get(name) or custom_targets.get(code))
        
        if is_custom:
            buy_reason = "수동지정매수"
        else:
            if getattr(config, "ENTRY_STRATEGY_TYPE", 1) == 2:
                buy_reason = "전일종가돌파매수"
            else:
                buy_reason = "신고가근접진입"

        if success:
            self.strategy.notify_entered(code, price)
            pos = self.executor.get_position(code)
            self.trade_logger.log_trade(
                code=code, name=name, side="BUY",
                qty=pos.qty,
                price=price,
                reason=buy_reason
            )
            print(f"\n  🟢 매수 체결: [{name}({code})] "
                  f"{pos.qty}주 @ {price:,}원 | "
                  f"손절가: {pos.stoploss_price:,}원 | "
                  f"익절: 5MA 음전환\n")
            self._print_status_board()
        else:
            # 매수 실패 시 WATCHING으로 복구 (다음 틱/봉에서 재시도 가능)
            state.phase = "WATCHING"

    # ─────────────────────────────────────────
    # 매도 실행
    # ─────────────────────────────────────────
    def _execute_sell(self, code: str, current_price: float,
                      signal: SignalType):
        pos = self.executor.get_position(code)
        if not pos:
            return

        if signal == SignalType.SELL_PROFIT:
            reason = "5MA음전환"
        elif signal == SignalType.SELL_TARGET:
            reason = "목표가청산"
        elif signal == SignalType.SELL_TRAILING:
            reason = "트레일링스탑"
        else:
            reason = "손절"
            
        pnl = pos.pnl(current_price)
        pnl_rate = pos.pnl_rate(current_price)

        success = self.executor.sell(code, current_price, reason)
        if success:
            self.trade_logger.log_trade(
                code=code, name=pos.name, side="SELL",
                qty=pos.qty, price=current_price,
                pnl=pnl, pnl_rate=pnl_rate, reason=reason
            )
            self.strategy.reset_stock(code)
            
            if signal == SignalType.SELL_STOP:
                emoji = "🔴"
            elif signal in (SignalType.SELL_PROFIT, SignalType.SELL_TARGET):
                emoji = "🟢"
            else:
                emoji = "🟡"
                
            print(f"\n  {emoji} 매도 체결: [{pos.name}({code})] "
                  f"진입가: {pos.entry_price:,}원 → 매도가: {current_price:,}원 | "
                  f"손익: {pnl:+,.0f}원 ({pnl_rate:+.2%}) | 사유: {reason}\n")
            self._print_status_board()

    def _clear_all_positions(self, reason: str = "일괄청산"):
        logger.info(f"[Bot] 전량 청산 시작: {reason}")
        positions = self.executor.get_all_positions()
        for code, pos in list(positions.items()):
            cur_price = self._last_prices.get(code, pos.entry_price)
            success = self.executor.sell(code, cur_price, reason)
            if success:
                pnl = pos.pnl(cur_price)
                pnl_rate = pos.pnl_rate(cur_price)
                self.trade_logger.log_trade(
                    code=code, name=pos.name, side="SELL",
                    qty=pos.qty, price=cur_price,
                    pnl=pnl, pnl_rate=pnl_rate, reason=reason
                )
                self.strategy.reset_stock(code)
                print(f"\n  ⏰ 일괄 청산 체결: [{pos.name}({code})] 사유: {reason}\n")
        self._print_status_board()

    # ─────────────────────────────────────────
    # 후보 제거 (WATCHING 타임아웃)
    # ─────────────────────────────────────────
    def _remove_candidate(self, code: str):
        """WATCHING 타임아웃 → 후보 목록에서 제거 및 구독 해제"""
        cand = next((c for c in self._candidates if c["code"] == code), None)
        name = cand["name"] if cand else code
        logger.info(f"[Bot] 대기 타임아웃 → 후보 제거: {name}({code})")
        print(f"\n  ⏰ [{name}({code})] 대기 타임아웃 ({config.WATCHING_TIMEOUT_CANDLES}봉 경과) → 후보 목록에서 제거\n")
        self._candidates = [c for c in self._candidates if c["code"] != code]
        self.kiwoom.unsubscribe_realtime(code)
        self.strategy.remove_stock(code)
        self._print_status_board()

    # ─────────────────────────────────────────
    # 실시간 상태 대시보드
    # ─────────────────────────────────────────
    def _print_status_board(self):
        """현재 매매 현황을 콘솔에 출력 (대기/보유/손익 요약)"""
        now = datetime.now().strftime("%H:%M:%S")
        positions = self.executor.get_all_positions()

        # 대기 중 종목 (후보 중 보유 포지션 없는 종목)
        waiting = [c for c in self._candidates if c["code"] not in positions]

        in_hours = self._in_trade_hours()
        hours_str = (
            f"거래중 ({config.TRADE_START_TIME}~{config.TRADE_END_TIME})"
            if in_hours else
            f"⏸ 시간 외 (거래 시작: {config.TRADE_START_TIME})"
        )

        print("\n" + "═" * 62)
        print(f"  📊 매매 현황  [{now}]  {hours_str}")
        print("═" * 62)

        # ── 대기 중 ──
        print(f"  ⏳ 대기 중 ({len(waiting)}종목)")
        if waiting:
            for c in waiting:
                code  = c["code"]
                name  = c["name"]
                state = self.strategy.get_state(code)
                if state is None:
                    continue
                phase      = state.phase
                cnt        = state.total_candles
                timeout_in = config.WATCHING_TIMEOUT_CANDLES - cnt

                if phase == "WATCHING":
                    high_20   = state.high_20
                    high_60   = state.high_60
                    cur_p     = self._last_prices.get(code, c.get("current_price", 0))
                    # 각 기준 고점에 대한 현재 근접도 계산
                    dist_20 = abs(cur_p - high_20) / high_20 * 100 if high_20 > 0 else 0
                    dist_60 = abs(cur_p - high_60) / high_60 * 100 if high_60 > 0 else 0
                    phase_str = (
                        f"신고가 대기 ({cnt}봉 경과"
                        + (f", {timeout_in}봉 후 취소" if timeout_in > 0 else ", 취소 임박")
                        + f") | 20일고점 -{dist_20:.1f}% / 60일고점 -{dist_60:.1f}%"
                    )
                else:
                    phase_str = phase

                cur_price = self._last_prices.get(code, c.get("current_price", 0))
                # 실시간 누적 거래대금 우선 표시, 없으면 스캔 시점의 5일 평균
                today_amt = self._today_amount.get(code, 0)
                if today_amt > 0:
                    amt_str = f"오늘 거래대금: {today_amt/1e8:.1f}억원"
                else:
                    avg_amt = c.get("avg_amount", 0)
                    amt_str = f"5일평균 거래대금: {avg_amt/1e8:.0f}억원/일" if avg_amt > 0 else "거래대금: 집계 중..."
                print(f"    [{name}({code})] {phase_str}")
                print(f"      현재가: {cur_price:,}원 | 20일고점: {c.get('high_20', 0):,}원 | {amt_str}")
        else:
            print("    (없음)")

        # ── 보유 중 ──
        print(f"\n  💰 보유 중 ({len(positions)}종목)")
        if positions:
            for code, pos in positions.items():
                cur_price = self._last_prices.get(code, pos.entry_price)
                pnl      = (cur_price - pos.entry_price) * pos.qty
                pnl_rate = (cur_price - pos.entry_price) / pos.entry_price if pos.entry_price else 0
                pnl_emoji = "▲" if pnl >= 0 else "▼"
                print(f"    [{pos.name}({code})]")
                print(f"      진입가: {pos.entry_price:,}원 | 현재가: {cur_price:,}원 | "
                      f"손익: {pnl_emoji}{abs(pnl):,.0f}원 ({pnl_rate:+.2%})")
                print(f"      손절가: {pos.stoploss_price:,}원 | 익절 조건: 5MA 음전환")
        else:
            print("    (없음)")

        # ── 오늘 손익 요약 ──
        status = self.risk.get_status()
        pnl_emoji = "▲" if status['today_pnl'] >= 0 else "▼"
        print(f"\n  오늘 손익: {pnl_emoji}{abs(status['today_pnl']):,.0f}원 "
              f"({status['pnl_rate']:+.2%}) | 거래: {status['trade_count']}회")
              
        # ── 오늘 거래 상세 내역 ──
        today_trades = self.trade_logger.get_today_trades()
        if today_trades:
            print("\n  [오늘의 거래 상세 내역]")
            for t in today_trades:
                t_time, t_code, t_name, t_side, t_qty, t_price, t_pnl, t_reason = t
                if t_side == "BUY":
                    print(f"    {t_time} | 🔴 매수 | {t_name}({t_code}) | {t_qty}주 @ {t_price:,}원 | 사유: {t_reason}")
                else:
                    emoji = "🟢" if t_pnl > 0 else "🔵" if t_pnl == 0 else "🟡"
                    pnl_str = f"{t_pnl:+,.0f}원"
                    print(f"    {t_time} | {emoji} 매도 | {t_name}({t_code}) | {t_qty}주 @ {t_price:,}원 | 손익: {pnl_str} | 사유: {t_reason}")
        
        print("═" * 62 + "\n")

    # ─────────────────────────────────────────
    # 메인 루프 (1분 주기)
    # ─────────────────────────────────────────
    def _main_loop(self):
        """
        주기적 상태 체크 + 장 종료 감지
        모의투자의 경우 WebSocket 실시간 틱(REG)이 지원되지 않으므로 REST로 가짜 틱 폴링
        """
        last_print = 0.0
        try:
            while self._running and not self._stop_event.is_set():
                now_hm = datetime.now().strftime("%H:%M")
                now_ts = time.time()

                # 주기적 조건식 갱신 (장중에만)
                if self._in_trade_hours():
                    scan_interval = getattr(config, "SCAN_UPDATE_INTERVAL", 180)
                    if now_ts - self._last_scan_time >= scan_interval:
                        self._last_scan_time = now_ts
                        self._update_candidates()

                # 일괄 청산 (15:19)
                clear_time = getattr(config, "CLEAR_TIME", "15:19")
                if now_hm == clear_time and self._candidates:
                    if not getattr(self, "_cleared_today", False):
                        logger.info(f"[Bot] {clear_time} 일괄 청산 시간 도달!")
                        self._clear_all_positions(reason="시간외청산")
                        self._cleared_today = True

                # 장 종료 처리
                if now_hm >= config.TRADE_END_TIME:
                    self._on_market_close()
                    break

                # 모의투자 전용 REST 틱 폴링 (2분/3분 간격 동적)
                if config.IS_SIMULATION and self._in_trade_hours() and self._candidates:
                    target_codes = [
                        c["code"] for c in self._candidates
                        if (self.strategy.get_state(c["code"]) and 
                            self.strategy.get_state(c["code"]).phase == "WATCHING")
                        or self.executor.has_position(c["code"])
                    ]
                    for code in target_codes:
                        last_poll = self._last_sim_poll.get(code, 0.0)
                        
                        has_pos = self.executor.has_position(code)
                        is_near = False
                        
                        if not has_pos:
                            state = self.strategy.get_state(code)
                            last_price = self._last_prices.get(code, 0)
                            if state and last_price > 0:
                                for high in [state.high_20, state.high_60]:
                                    if high > 0 and abs(last_price - high) / high <= 0.02:
                                        is_near = True
                                        break
                            else:
                                is_near = True # 가격 정보가 없으면 빠른 갱신
                        
                        interval = 150 if (has_pos or is_near) else 240
                        
                        if now_ts - last_poll >= interval:
                            self._last_sim_poll[code] = now_ts
                            info = self.kiwoom.get_stock_info(code)
                            if info and info["current_price"] > 0:
                                fake_tick = {
                                    "code": code,
                                    "price": info["current_price"],
                                    "volume": 0,
                                    "time": datetime.now().strftime("%H%M%S")
                                }
                                self._on_tick(fake_tick)
                            # 모의투자 REST API 429(Too Many Requests) 방지를 위해 1.5초 간격 유지
                            time.sleep(1.5)

                # 주기적 상태 보드 출력 (60초 간격)
                if now_ts - last_print >= 60:
                    if self._candidates or self.executor.get_all_positions():
                        self._print_status_board()
                    last_print = now_ts

                wait_time = 5 if config.IS_SIMULATION else 60
                self._stop_event.wait(timeout=wait_time)

        except KeyboardInterrupt:
            logger.info("[Bot] 사용자 중단 (Ctrl+C)")
            self._on_market_close(force_sell=False)

    # ─────────────────────────────────────────
    # 장 종료
    # ─────────────────────────────────────────
    def _on_market_close(self, force_sell: bool = True):
        if not self._running:
            return

        logger.info("[Bot] 장 종료 처리 시작")

        if force_sell:
            # 미청산 포지션 강제 청산 (단, 수동 지정 종목 CUSTOM_TARGETS은 홀딩 정책)
            custom_targets = config.get_custom_targets()
            today_dt = datetime.now()

            for code, pos in list(self.executor.get_all_positions().items()):
                custom_conf = custom_targets.get(pos.name) or custom_targets.get(code)
                if custom_conf:
                    # 텔레그램 연동 등의 보존기한(holding_days) 처리 로직
                    holding_days = custom_conf.get("holding_days", 0)
                    created_at_str = custom_conf.get("created_at", "")
                    
                    if holding_days > 0 and created_at_str:
                        try:
                            created_dt = datetime.strptime(created_at_str, "%Y-%m-%d")
                            elapsed_days = (today_dt - created_dt).days
                            if elapsed_days > holding_days:
                                logger.warning(f"[Bot] 장마감 강제 청산 (수동종목 보존기한 {holding_days}일 만료): {pos.name}({code})")
                                self.executor.sell(code, pos.entry_price, "보존만료")
                                continue
                        except Exception as e:
                            logger.error(f"[Bot] 수동종목({pos.name}) 날짜 파싱 오류: {e}")

                    logger.info(f"[Bot] 장마감 청산 예외 (수동종목 홀딩): {pos.name}({code})")
                    continue
                
                logger.warning(f"[Bot] 장마감 강제 청산: {pos.name}({code})")
                self.executor.sell(code, pos.entry_price, "장마감")
        else:
            logger.info("[Bot] 포지션 유지 (사용자 중단으로 인한 강제청산 생략)")

        # 실시간 구독 해제
        self.kiwoom.unsubscribe_realtime()

        # 일별 요약
        summary = self.trade_logger.update_daily_summary()
        logger.info(
            f"[Bot] 오늘 요약 | "
            f"거래: {summary['total']}회 | "
            f"승률: {summary['win_rate']:.1%} | "
            f"손익: {summary['total_pnl']:+,.0f}원"
        )

        # 리셋
        self.executor.reset_daily()
        self.risk.reset_daily()
        self._running = False
        self._stop_event.set()

        logger.info("[Bot] 장 종료 처리 완료")

    # ─────────────────────────────────────────
    # 유틸
    # ─────────────────────────────────────────
    def _in_trade_hours(self) -> bool:
        now = datetime.now().strftime("%H:%M")
        s = config.TRADE_START_TIME
        e = config.TRADE_END_TIME
        ls = config.LUNCH_START
        le = config.LUNCH_END
        return (s <= now <= e) and not (ls <= now <= le)


# ─────────────────────────────────────────
# 진입점
# ─────────────────────────────────────────
if __name__ == "__main__":
    bot = TradingBot()
    bot.start()
