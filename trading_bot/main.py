# main.py - 트레이딩 봇 메인 진입점 (하네스 기반 에이전트 아키텍처 버전)
# OCX / PyQt5 의존성 완전 제거

import sys
import time
import threading
import logging
from datetime import datetime

import config
from logger import setup_logger, TradeLogger
from kiwoom_api import KiwoomAPI
from risk_manager import RiskManager
from strategy import SignalType

# 하네스, 스킬, 훅, 에이전트 임포트
from skills.scan_skill import StockScanner, MarketFilter
from skills.market_data_skill import MarketDataSkill
from skills.execution_skill import ExecutionSkill
from hooks.risk_hook import RiskHook
from hooks.market_filter_hook import MarketFilterHook
from harness.kiwoom_harness import KiwoomHarness
from agents.breakout_agent import BreakoutTradingAgent

# 로거 초기화
logger = setup_logger("main")


class TradingBot:
    """
    메인 트레이딩 봇 (하네스 기반 에이전트 아키텍처)
    역할: 하네스, 에이전트, 스킬, 훅 조립 및 메인 런타임 제어
    """

    def __init__(self, auto_mode=False):
        self._auto_mode = auto_mode
        self._candidates = []
        self._running = False
        self._stop_event = threading.Event()
        self._cleared_today = False
        self._last_sim_poll = {}
        self._last_scan_time = 0.0

        # 1. 핵심 컴포넌트 초기화
        self.kiwoom = KiwoomAPI()
        self.risk = RiskManager(self.kiwoom)
        self.scanner = StockScanner(self.kiwoom)
        self.market_filter = MarketFilter(self.kiwoom)
        self.trade_logger = TradeLogger()

        # 2. 하네스 생성
        self.harness = KiwoomHarness(self.kiwoom)

        # 3. 스킬 등록
        self.execution_skill = ExecutionSkill(self.kiwoom, self.risk)
        self.market_data_skill = MarketDataSkill(self.kiwoom)
        self.harness.register_skill("execution", self.execution_skill)
        self.harness.register_skill("market_data", self.market_data_skill)

        # 4. 훅 등록
        self.risk_hook = RiskHook(self.risk)
        self.market_filter_hook = MarketFilterHook(self.market_filter)
        self.harness.register_hook(self.risk_hook)
        self.harness.register_hook(self.market_filter_hook)

        # 5. 에이전트 등록
        self.agent = BreakoutTradingAgent()
        self.harness.register_agent(self.agent)

        # 6. 하네스 런타임 공유 컨텍스트 설정
        self.harness.update_context("risk", self.risk)
        self.harness.update_context("market_filter", self.market_filter)
        self.harness.update_context("scanner", self.scanner)
        self.harness.update_context("candidates", self._candidates)
        self.harness.update_context("print_status_board", self._print_status_board)

    # ─────────────────────────────────────────
    # 시작
    # ─────────────────────────────────────────
    def start(self):
        logger.info("=" * 60)
        logger.info("[Bot] 하네스 기반 트레이딩 봇 시작")
        logger.info(f"[Bot] 모드: {'모의투자' if config.IS_SIMULATION else '실계좌'}")

        if self._auto_mode:
            logger.info("[Bot] 자동 시작 (09:00 대기 모드)")
            while True:
                now_hm = datetime.now().strftime("%H:%M")
                if config.TRADE_START_TIME <= now_hm < config.TRADE_END_TIME:
                    break
                elif now_hm >= config.TRADE_END_TIME:
                    logger.info("[Bot] 이미 장이 종료된 시간(15:30 이후)이므로, 내일 다시 시도합니다.")
                    return
                logger.info(f"[Bot] 09:00까지 대기 중... (현재: {now_hm})")
                time.sleep(60)

        # 키움 REST 토큰 발급
        if not self.kiwoom.login():
            logger.error("[Bot] 토큰 발급 실패 → 종료")
            sys.exit(1)

        logger.info("[Bot] 토큰 발급 성공")

        # 자본 초기화
        self.risk.initialize()

        # 기존 보유 종목 동기화
        self._sync_existing_positions()

        if self._auto_mode:
            logger.info(f"[Bot] 기본 진입 전략 사용: {getattr(config, 'ENTRY_STRATEGY_TYPE', 3)}번")
        else:
            # 진입 전략(1/2/3) 대화형 선택
            self._choose_strategy_type()

        if self._auto_mode:
            logger.info(f"[Bot] 기본 조건식 사용: {getattr(config, 'CONDITION_NAME', '')}")
            config._interactive_selected = False
        else:
            # 대화형 조건식 선택 (CONDITION_INTERACTIVE=True 일 때)
            self._choose_condition()

        # 후보 종목 초기 스캔 (일봉 기준)
        self._run_initial_scan()

        # 장 중 실시간 조건식 모니터링 시작 (ka10173)
        self._start_condition_polling()

        self._running = True
        logger.info("[Bot] 매매 대기 중...")

        # 메인 루프 (1분 주기 타이머 역할)
        self._main_loop()

    # ─────────────────────────────────────────
    # 대화형 전략 및 조건식 선택
    # ─────────────────────────────────────────
    def _choose_strategy_type(self):
        print("\n" + "=" * 55)
        print("  [진입 전략 선택]")
        print("  1) 20일/60일 신고가 돌파 및 ±1% 내 눌림 매수")
        print("  2) 3분봉이 전일 종가를 상향 돌파 시, 다음 봉 시가 매수")
        print("  3) 돌파 후 20MA 눌림 매수 (기본)")
        print("=" * 55)
        
        while True:
            try:
                choice = input("  번호 입력 (1, 2, 3 중 하나, 기본=3): ").strip()
                if not choice:
                    config.ENTRY_STRATEGY_TYPE = 3
                    break
                elif choice in ("1", "2", "3"):
                    config.ENTRY_STRATEGY_TYPE = int(choice)
                    break
                else:
                    print("  1, 2, 3 중 하나를 입력하세요.")
            except (EOFError, KeyboardInterrupt):
                config.ENTRY_STRATEGY_TYPE = 3
                print("\n  입력 취소 → 기본(3번) 전략 사용")
                break
        
        logger.info(f"[Bot] 선택된 진입 전략: {config.ENTRY_STRATEGY_TYPE}번")

    def _choose_condition(self):
        if not getattr(config, "CONDITION_INTERACTIVE", False):
            config._interactive_selected = False
            return

        logger.info("[Bot] 조건식 목록 조회 중...")
        cond_list = self.kiwoom.get_condition_list()

        if not cond_list:
            logger.warning("[Bot] 조건식 목록 없음 → 자동 선택 사용")
            config._interactive_selected = False
            return

        while True:
            selected = self._show_condition_menu(cond_list)

            if selected is None:
                logger.info(f"[Bot] 조건식 자동 선택: '{getattr(config, 'CONDITION_NAME', '')}'")
                config._interactive_selected = False
                return

            config.CONDITION_NAME = selected["name"]
            config.CONDITION_SEQ  = selected["seq"]
            config._interactive_selected = True
            logger.info(f"[Bot] 조건식 선택: '{selected['name']}' (seq={selected['seq']})")

            stocks = self.kiwoom.search_by_condition(selected["seq"])
            count  = len(stocks) if stocks else 0
            logger.info(f"[Bot] 조건식 '{selected['name']}' 결과: {count}개")

            if count > 0:
                print(f"  → '{selected['name']}' 결과: {count}개 종목\n")
                return

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
                    break
                elif sub == "2":
                    logger.info("[Bot] 빈 후보로 계속 진행 (실시간 편입 대기)")
                    return
                elif sub == "3":
                    print("  봇을 종료합니다.")
                    sys.exit(0)
                else:
                    print("  1, 2, 3 중 하나를 입력하세요.")

    def _show_condition_menu(self, cond_list: list):
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
        positions = self.kiwoom.get_positions()
        for p in positions:
            code = p["code"]
            name = p["name"]
            
            # 1) 포지션 복구
            self.execution_skill.sync_position(
                code=code,
                name=name,
                qty=p["qty"],
                entry_price=p["entry_price"]
            )
            
            # 2) 전략 내에 상태 강제 진입(ENTERED) 처리 (기준고점 0으로)
            self.agent.strategy.init_stock(code, name=name, high_20=0, high_60=0)
            self.agent.strategy.notify_entered(code, entry_price=p["entry_price"])
            
            # 3) 감시 대상(candidates) 등록 (틱 조회를 위해 필요)
            if code not in [c["code"] for c in self._candidates]:
                self._candidates.append({
                    "code": code,
                    "name": name,
                    "high_20": 0.0,
                    "high_60": 0.0
                })
                
            # 4) 데이터 매니저 초기화 및 실시간 구독
            self.market_data_skill.init_stock(
                code,
                on_candle_close=lambda candle, _code=code: self.harness.broadcast_event("CANDLE", candle)
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
            logger.info(f"[Bot] 후보 종목 스캔 (장 중 실행 {now_hm} | 일봉 기준 — 오늘 장 중 데이터 포함)")
        else:
            logger.info(f"[Bot] 후보 종목 스캔 (장 시작 전 {now_hm} | 전일 종가 기준)")
        
        new_cands = self.scanner.run_scan()
        self.scanner.print_summary()

        existing_codes = {c["code"] for c in self._candidates}
        for c in new_cands:
            code = c["code"]
            if code not in existing_codes:
                self._candidates.append(c)
                # 에이전트 내 전략 종목 등록
                self.agent.strategy.init_stock(code, name=c["name"], high_20=c.get("high_20", 0), high_60=c.get("high_60", 0))
                # 분봉 데이터 초기화
                self.market_data_skill.init_stock(
                    code,
                    on_candle_close=lambda candle, _code=code: self.harness.broadcast_event("CANDLE", candle)
                )
                if not config.IS_SIMULATION:
                    self.kiwoom.subscribe_realtime(code)
                logger.info(f"[Bot] 구독 시작: {c['name']}({code})")

        self._last_scan_time = time.time()
        self._print_status_board()

    # ─────────────────────────────────────────
    # 장 중 조건식 실시간 폴링 (ka10173)
    # ─────────────────────────────────────────
    def _start_condition_polling(self):
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

        # 하네스가 WebSocket callbacks을 통해 이벤트를 수신하도록 설정
        self.kiwoom.register_condition_realtime(seq)
        logger.info(f"[Bot] 조건식 실시간 등록 완료 (seq={seq})")

    # ─────────────────────────────────────────
    # 일괄 청산
    # ─────────────────────────────────────────
    def _clear_all_positions(self, reason: str = "일괄청산"):
        logger.info(f"[Bot] 전량 청산 시작: {reason}")
        positions = self.execution_skill.get_all_positions()
        
        last_prices = self.harness.get_context().get("last_prices", {})

        for code, pos in list(positions.items()):
            cur_price = last_prices.get(code, pos.entry_price)
            # 하네스를 거쳐 매도 진행
            success = self.harness.execute_action(self.agent, "execution", "SELL", code, cur_price, reason)
            if success:
                pnl = pos.pnl(cur_price)
                pnl_rate = pos.pnl_rate(cur_price)
                self.trade_logger.log_trade(
                    code=code, name=pos.name, side="SELL",
                    qty=pos.qty, price=cur_price,
                    pnl=pnl, pnl_rate=pnl_rate, reason=reason
                )
                self.agent.strategy.reset_stock(code)
                print(f"\n  ⏰ 일괄 청산 체결: [{pos.name}({code})] 사유: {reason}\n")
        self._print_status_board()

    # ─────────────────────────────────────────
    # 실시간 상태 대시보드
    # ─────────────────────────────────────────
    def _print_status_board(self):
        now = datetime.now().strftime("%H:%M:%S")
        positions = self.execution_skill.get_all_positions()

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
        
        market_msg = self.market_filter.status_msg
        market_pass = "✅ 허용 (매수 가능)" if self.market_filter.is_bullish() else "⏳ 대기 (마켓 역배열로 매수 보류)"
        print(f"  📈 시장 팩터: {market_msg}")
        print(f"  🚥 매수 상태: {market_pass}")
        print("═" * 62)

        # ── 대기 중 ──
        print(f"  ⏳ 대기 중 ({len(waiting)}종목)")
        
        last_prices = self.harness.get_context().setdefault("last_prices", {})
        today_amount = self.harness.get_context().setdefault("today_amount", {})

        if waiting:
            for c in waiting:
                code  = c["code"]
                name  = c["name"]
                state = self.agent.strategy.get_state(code)
                if state is None:
                    continue
                phase      = state.phase
                cnt        = state.total_candles
                timeout_in = config.WATCHING_TIMEOUT_CANDLES - cnt

                if phase == "WATCHING":
                    high_20   = state.high_20
                    high_60   = state.high_60
                    cur_p     = last_prices.get(code, c.get("current_price", 0))
                    dist_20 = abs(cur_p - high_20) / high_20 * 100 if high_20 > 0 else 0
                    dist_60 = abs(cur_p - high_60) / high_60 * 100 if high_60 > 0 else 0
                    strategy_type = getattr(config, "ENTRY_STRATEGY_TYPE", 1)
                    if strategy_type == 2:
                        phase_str = (
                            f"돌파 대기 ({cnt}봉 경과"
                            + (f", {timeout_in}봉 후 취소" if timeout_in > 0 else ", 취소 임박")
                            + f") | 전일종가 -{dist_20:.1f}%"
                        )
                    elif strategy_type == 3:
                        if state.is_breakout:
                            phase_str = f"20MA 눌림 대기 ({cnt}봉 경과" + (f", {timeout_in}봉 후 취소" if timeout_in > 0 else ", 취소 임박") + ")"
                        else:
                            phase_str = f"돌파 대기 ({cnt}봉 경과" + (f", {timeout_in}봉 후 취소" if timeout_in > 0 else ", 취소 임박") + f") | 일봉고점 -{min(dist_20, dist_60):.1f}%"
                    else:
                        phase_str = (
                            f"신고가 대기 ({cnt}봉 경과"
                            + (f", {timeout_in}봉 후 취소" if timeout_in > 0 else ", 취소 임박")
                            + f") | 20일고점 -{dist_20:.1f}% / 60일고점 -{dist_60:.1f}%"
                        )
                else:
                    phase_str = phase

                cur_price = last_prices.get(code, c.get("current_price", 0))
                today_amt = today_amount.get(code, 0)
                if today_amt > 0:
                    amt_str = f"오늘 거래대금: {today_amt/1e8:.1f}억원"
                else:
                    avg_amt = c.get("avg_amount", 0)
                    amt_str = f"5일평균 거래대금: {avg_amt/1e8:.0f}억원/일" if avg_amt > 0 else "거래대금: 집계 중..."
                print(f"    [{name}({code})] {phase_str}")
                strategy_type = getattr(config, "ENTRY_STRATEGY_TYPE", 1)
                if strategy_type == 2:
                    print(f"      현재가: {cur_price:,}원 | 전일종가: {c.get('high_20', 0):,}원 | {amt_str}")
                elif strategy_type == 3:
                    print(f"      현재가: {cur_price:,}원 | 일봉기준가: {state.daily_high:,}원 | {amt_str}")
                else:
                    print(f"      현재가: {cur_price:,}원 | 20일고점: {c.get('high_20', 0):,}원 | {amt_str}")
        else:
            print("    (없음)")

        # ── 보유 중 ──
        print(f"\n  💰 보유 중 ({len(positions)}종목)")
        if positions:
            for code, pos in positions.items():
                cur_price = last_prices.get(code, pos.entry_price)
                pnl      = (cur_price - pos.entry_price) * pos.qty
                pnl_rate = (cur_price - pos.entry_price) / pos.entry_price if pos.entry_price else 0
                pnl_emoji = "▲" if pnl >= 0 else "▼"
                print(f"    [{pos.name}({code})]")
                print(f"      진입가: {pos.entry_price:,}원 | 현재가: {cur_price:,}원 | "
                      f"손익: {pnl_emoji}{abs(pnl):,.0f}원 ({pnl_rate:+.2%})")
                
                custom_targets = config.get_custom_targets()
                custom_conf = custom_targets.get(pos.name) or custom_targets.get(code)
                if custom_conf:
                    sl = custom_conf.get("stop_loss", -1)
                    tp = custom_conf.get("take_profit", -1)
                    sl_str = f"{sl:,}원(수동)" if sl > 0 else "미지정"
                    tp_str = f"{tp:,}원(수동)" if tp > 0 else "미지정"
                    print(f"      손절가: {sl_str} | 익절 제한: {tp_str}")
                else:
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

    def _change_condition_runtime(self, new_cond_name: str):
        cond_list = self.kiwoom.get_condition_list()
        target_seq = None
        for c in cond_list:
            if c["name"] == new_cond_name:
                target_seq = c["seq"]
                break

        if not target_seq:
            logger.error(f"[Bot] '{new_cond_name}' 조건식을 찾을 수 없습니다.")
            return

        config.CONDITION_NAME = new_cond_name
        config.CONDITION_SEQ = target_seq
        logger.info(f"[Bot] 조건식을 '{new_cond_name}' (seq={target_seq}) 으로 변경합니다.")
        
        # 새 조건식 실시간 등록
        self._start_condition_polling()

    # ─────────────────────────────────────────
    # 메인 루프 (1분 주기)
    # ─────────────────────────────────────────
    def _main_loop(self):
        last_print = 0.0
        try:
            while self._running and not self._stop_event.is_set():
                now_hm = datetime.now().strftime("%H:%M")
                now_ts = time.time()

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

                # 모의투자 전용 REST 틱 폴링
                if config.IS_SIMULATION and self._in_trade_hours() and self._candidates:
                    target_codes = [
                        c["code"] for c in self._candidates
                        if (self.agent.strategy.get_state(c["code"]) and 
                            self.agent.strategy.get_state(c["code"]).phase == "WATCHING")
                        or self.execution_skill.has_position(c["code"])
                    ]
                    
                    last_prices = self.harness.get_context().get("last_prices", {})

                    for code in target_codes:
                        last_poll = self._last_sim_poll.get(code, 0.0)
                        has_pos = self.execution_skill.has_position(code)
                        is_near = False
                        
                        if not has_pos:
                            state = self.agent.strategy.get_state(code)
                            last_price = last_prices.get(code, 0)
                            if state and last_price > 0:
                                for high in [state.high_20, state.high_60]:
                                    if high > 0 and abs(last_price - high) / high <= 0.02:
                                        is_near = True
                                        break
                            else:
                                is_near = True
                        
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
                                # 하네스를 통해 틱 브로드캐스트
                                self.harness.broadcast_event("TICK", fake_tick)
                            time.sleep(1.5)

                # 수동 강제청산 확인
                import os
                if os.path.exists("force_sell.txt"):
                    logger.warning("[Bot] 수동 강제청산 명령어 감지 (force_sell.txt)")
                    self._clear_all_positions(reason="수동강제청산")
                    try:
                        os.remove("force_sell.txt")
                    except Exception as e:
                        logger.error(f"[Bot] force_sell.txt 삭제 실패: {e}")

                # 수동 검색식 변경 감지
                if os.path.exists("change_condition.txt"):
                    try:
                        with open("change_condition.txt", "r", encoding="utf-8") as f:
                            new_cond_name = f.read().strip()
                        os.remove("change_condition.txt")
                        if new_cond_name:
                            logger.info(f"[Bot] 수동 조건식 변경 감지: {new_cond_name}")
                            self._change_condition_runtime(new_cond_name)
                    except Exception as e:
                        logger.error(f"[Bot] 조건식 변경 실패: {e}")

                # 주기적 상태 보드 출력 (60초 간격)
                if now_ts - last_print >= 60:
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
            custom_targets = config.get_custom_targets()
            today_dt = datetime.now()

            for code, pos in list(self.execution_skill.get_all_positions().items()):
                custom_conf = custom_targets.get(pos.name) or custom_targets.get(code)
                if custom_conf:
                    holding_days = custom_conf.get("holding_days", 0)
                    created_at_str = custom_conf.get("created_at", "")
                    
                    if holding_days > 0 and created_at_str:
                        try:
                            created_dt = datetime.strptime(created_at_str, "%Y-%m-%d")
                            elapsed_days = (today_dt - created_dt).days
                            if elapsed_days > holding_days:
                                logger.warning(f"[Bot] 장마감 강제 청산 (수동종목 보존기한 {holding_days}일 만료): {pos.name}({code})")
                                self.execution_skill.sell(code, pos.entry_price, "보존만료")
                                continue
                        except Exception as e:
                            logger.error(f"[Bot] 수동종목({pos.name}) 날짜 파싱 오류: {e}")

                    logger.info(f"[Bot] 장마감 청산 예외 (수동종목 홀딩): {pos.name}({code})")
                    continue
                
                logger.warning(f"[Bot] 장마감 강제 청산: {pos.name}({code})")
                self.execution_skill.sell(code, pos.entry_price, "장마감")
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
        self.execution_skill.reset_daily()
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

    def _can_buy_time(self) -> bool:
        now = datetime.now().strftime("%H:%M")
        s = config.TRADE_START_TIME
        e = getattr(config, "BUY_END_TIME", "15:10")
        ls = config.LUNCH_START
        le = config.LUNCH_END
        return (s <= now <= e) and not (ls <= now <= le)


# ─────────────────────────────────────────
# 진입점
# ─────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    import time
    from datetime import datetime
    
    parser = argparse.ArgumentParser()
    parser.add_argument("--auto", action="store_true", help="Auto start at 09:00 with default strategy and condition")
    args = parser.parse_args()

    if args.auto:
        while True:
            bot = TradingBot(auto_mode=True)
            bot.start()
            
            print("\n[Bot] 오늘 장이 종료되었습니다. 다음 거래일을 위해 자정까지 대기합니다...")
            while True:
                now_hm = datetime.now().strftime("%H:%M")
                if "15:30" <= now_hm <= "23:59":
                    time.sleep(600)
                else: 
                    break
            
            print("[Bot] 새 날이 밝았습니다. 봇을 재가동합니다.\n")
            time.sleep(5)
    else:
        bot = TradingBot(auto_mode=False)
        bot.start()
