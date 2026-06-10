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
from notifier import TelegramNotifier, TelegramLogHandler

# 하네스, 스킬, 훅, 에이전트 임포트
from skills.scan_skill import StockScanner, MarketFilter
from skills.market_data_skill import MarketDataSkill
from skills.execution_skill import ExecutionSkill
from skills.vm_position_manager import VMPositionManager
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
        self._vm_picks_mtime = 0.0
        self._last_vm_picks_check = 0.0
        self._last_heartbeat = 0.0
        self._last_queue_check = 0.0  # VM 매수 대기열 처리 타이머
        self._kiwoom_ready = True      # 키움 로그인 성공 여부 (False → Telegram 감시 전용 모드)

        # 텔레그램 알림
        self.notifier = TelegramNotifier(
            token=config.TELEGRAM_BOT_TOKEN,
            chat_id=config.TELEGRAM_CHAT_ID,
        )

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
        # ── 오브젝트 참조 ──────────────────────────────────────────────────
        self.harness.update_context("risk",              self.risk)
        self.harness.update_context("market_filter",     self.market_filter)
        self.harness.update_context("scanner",           self.scanner)
        self.harness.update_context("candidates",        self._candidates)
        self.harness.update_context("print_status_board", self._print_status_board)
        self.harness.update_context("notifier",          self.notifier)
        # ── 실시간 집계 딕셔너리 (에이전트가 setdefault 없이 get() 가능) ──
        self.harness.update_context("last_prices",  {})   # code → 최근 체결가
        self.harness.update_context("today_amount", {})   # code → 당일 거래대금(누적)
        self.harness.update_context("today_volume", {})   # code → 당일 거래량(누적)

        # 7. VM picks 전용 포지션 매니저 (다중 포지션 + JSON 영속 + 고정금액)
        import os as _os_main
        vm_persist_path = _os_main.path.join(_os_main.path.dirname(config.DB_PATH), "vm_positions.json")
        self.vm_manager = VMPositionManager(self.kiwoom, self.risk, vm_persist_path)
        self.harness.register_skill("vm", self.vm_manager)   # 하네스 스킬로 등록
        self.harness.update_context("vm_manager",  self.vm_manager)
        self.harness.update_context("vm_buy_queue", [])

        # 8. 텔레그램 원격 명령 등록
        self._register_telegram_commands()

    # ─────────────────────────────────────────
    # 시작
    # ─────────────────────────────────────────
    def start(self):
        logger.info("=" * 60)
        logger.info("[Bot] 하네스 기반 트레이딩 봇 시작")
        logger.info(f"[Bot] 모드: {'모의투자' if config.IS_SIMULATION else '실계좌'}")

        # 텔레그램 알림 핸들러 + 폴링 시작
        logging.getLogger().addHandler(TelegramLogHandler(self.notifier))
        self.notifier.start_polling()
        mode_str = "모의투자" if config.IS_SIMULATION else "실계좌"
        self.notifier.send(
            f"🚀 <b>트레이딩 봇 시작</b>\n"
            f"모드: {mode_str} | VM: {'ON' if config.VM_MODE else 'OFF'}\n"
            f"전략: {getattr(config, 'ENTRY_STRATEGY_TYPE', 3)}번 | "
            f"조건식: {getattr(config, 'CONDITION_NAME', '(미설정)')}"
        )

        if self._auto_mode:
            logger.info("[Bot] 자동 시작 (09:00 대기 모드)")
            while True:
                now_hm = datetime.now().strftime("%H:%M")
                if config.TRADE_START_TIME <= now_hm < config.TRADE_END_TIME:
                    break
                elif now_hm >= config.TRADE_END_TIME:
                    logger.info("[Bot] 이미 장이 종료된 시간(15:30 이후)이므로, 내일 다시 시도합니다.")
                    # 오버나잇 포지션 복구 (Telegram /pos 명령 응답 가능하도록)
                    self.vm_manager.load()
                    logger.info(f"[Bot] 오버나잇 포지션 {len(self.vm_manager.get_all())}개 로드 완료")
                    return
                logger.info(f"[Bot] 09:00까지 대기 중... (현재: {now_hm})")
                time.sleep(60)

        # 키움 REST 토큰 발급
        self._kiwoom_ready = self.kiwoom.login()
        if not self._kiwoom_ready:
            if config.VM_MODE:
                logger.warning("[Bot] ⚠️ 키움 로그인 실패 — Telegram 감시 전용 모드로 전환")
                self.notifier.send(
                    "⚠️ <b>키움 로그인 실패</b>\n"
                    "Telegram 감시 전용 모드로 동작합니다.\n"
                    "5분마다 재연결 시도 | /ping /status /log /reload 명령 가능"
                )
                self.vm_manager.load()
                self._running = True
                self._vm_telegram_loop()
                return
            else:
                logger.error("[Bot] 토큰 발급 실패 → 종료")
                sys.exit(1)

        logger.info("[Bot] 토큰 발급 성공")

        # 자본 초기화
        self.risk.initialize()

        # VM 포지션 영속 복구 (재시작 후 오버나잇 포지션 복원)
        if config.VM_MODE:
            self.vm_manager.load()
            for vm_pos in list(self.vm_manager.get_all().values()):
                _code, _name = vm_pos.code, vm_pos.name
                if _code not in [c["code"] for c in self._candidates]:
                    self._candidates.append({"code": _code, "name": _name,
                                              "high_20": 0.0, "high_60": 0.0})
                self.market_data_skill.init_stock(
                    _code,
                    on_candle_close=lambda _c, __code=_code: self.harness.broadcast_event("CANDLE", _c)
                )
                if not config.IS_SIMULATION:
                    self.kiwoom.subscribe_realtime(_code)
                logger.info(f"[Bot] VM 포지션 복구: {_name}({_code}) {vm_pos.qty}주")

        # 기존 보유 종목 동기화 (execution_skill 전용 — vm_manager 종목 제외)
        self._sync_existing_positions()

        if self._auto_mode or config.VM_MODE:
            logger.info(f"[Bot] 기본 진입 전략 사용: {getattr(config, 'ENTRY_STRATEGY_TYPE', 3)}번")
        else:
            # 진입 전략(1/2/3) 대화형 선택
            self._choose_strategy_type()

        if self._auto_mode or config.VM_MODE:
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
        # vm_manager가 이미 관리하는 종목은 execution_skill 복구에서 제외
        vm_codes = {pos.code for pos in self.vm_manager.get_all().values()}
        positions = self.kiwoom.get_positions()
        for p in positions:
            code = p["code"]
            if code in vm_codes:
                continue  # vm_manager가 이미 관리 중
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

        # VM 포지션 청산
        # "시간외청산"(자동 일괄)은 holding_days 남은 포지션 오버나잇 유지
        # "/sell", "force" 등 수동 명령은 holding_days 무관하게 전부 청산
        is_auto_close = (reason == "시간외청산")
        for pid, vm_pos in list(self.vm_manager.get_all().items()):
            if is_auto_close:
                vm_conf     = config.get_custom_targets().get(vm_pos.code, {})
                holding_days = vm_conf.get("holding_days", 0)
                if holding_days > 0 and vm_pos.created_at:
                    try:
                        created_dt = datetime.strptime(vm_pos.created_at, "%Y-%m-%d")
                        elapsed    = (datetime.now() - created_dt).days
                        if elapsed < holding_days:
                            logger.info(
                                f"[Bot] VM 오버나잇 유지 ({elapsed}/{holding_days}일): "
                                f"{vm_pos.name}({vm_pos.code})"
                            )
                            continue
                    except Exception:
                        pass
            cur_price = last_prices.get(vm_pos.code, vm_pos.entry_price)
            ok = self.vm_manager.sell_vm(pid, cur_price, reason)
            if ok:
                self.trade_logger.log_trade(
                    code=vm_pos.code, name=vm_pos.name, side="SELL",
                    qty=vm_pos.qty, price=cur_price,
                    pnl=vm_pos.pnl(cur_price), pnl_rate=vm_pos.pnl_rate(cur_price),
                    reason=reason
                )
                print(f"\n  ⏰ VM 청산: [{vm_pos.name}({vm_pos.code})] 사유: {reason}\n")

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

    def _reload_vm_picks_if_changed(self):
        import os
        import json as _json
        path = config.VM_PICKS_PATH
        if not os.path.exists(path):
            return
        mtime = os.path.getmtime(path)
        if mtime <= self._vm_picks_mtime:
            return
        self._vm_picks_mtime = mtime
        logger.info("[Bot] vm_picks.json 변경 감지 → 신규 후보 재스캔")

        # ── 자동 검증: 파일 갱신 시마다 이슈 체크 후 텔레그램 알림 ─────────
        self._auto_validate_vm_picks(path)

        # 기존 VM 포지션의 SL/TP 업데이트 (동일 종목 재추천 대응)
        try:
            with open(path, "r", encoding="utf-8") as _f:
                new_picks = _json.load(_f)
            # Format B: {"date": "...", "picks": [...]}
            if isinstance(new_picks, dict) and "picks" in new_picks and isinstance(new_picks["picks"], list):
                _pick_items = {
                    p["code"]: {
                        "stop_loss":   p.get("stop_loss", 0),
                        "take_profit": p.get("take_profit_1", 0),
                        "name":        p.get("name", p.get("code", "")),
                    }
                    for p in new_picks["picks"] if p.get("code")
                }
            else:
                _pick_items = new_picks  # Format A
            for _code, _conf in _pick_items.items():
                if not isinstance(_conf, dict):
                    continue
                _sl = _conf.get("stop_loss", 0)
                _tp = _conf.get("take_profit", 0)
                if _sl and _tp and self.vm_manager.has_any(_code):
                    self.vm_manager.update_sltp(_code, _sl, _tp)
                    logger.info(f"[Bot] VM 포지션 SL/TP 업데이트: {_code} SL={_sl:,} TP={_tp:,}")
                    self.notifier.send(
                        f"🔄 <b>SL/TP 업데이트</b> [{_conf.get('name', _code)}({_code})]\n"
                        f"손절가: {_sl:,}원 | 목표가: {_tp:,}원"
                    )
        except Exception as _e:
            logger.debug(f"[Bot] vm_picks SL/TP 업데이트 파싱 오류: {_e}")

        # 재스캔을 백그라운드 스레드로 실행 — 메인 루프(폴링) 블로킹 방지
        if getattr(self, "_rescan_in_progress", False):
            logger.info("[Bot] 재스캔 이미 진행 중 — 스킵")
            return

        def _do_rescan():
            self._rescan_in_progress = True
            try:
                new_cands = self.scanner.run_scan()
                existing_codes = {c["code"] for c in self._candidates}
                for c in new_cands:
                    code = c["code"]
                    if code not in existing_codes:
                        self._candidates.append(c)
                        self.agent.strategy.init_stock(
                            code, name=c["name"],
                            high_20=c.get("high_20", 0), high_60=c.get("high_60", 0)
                        )
                        self.market_data_skill.init_stock(
                            code,
                            on_candle_close=lambda candle, _c=code: self.harness.broadcast_event("CANDLE", candle)
                        )
                        if not config.IS_SIMULATION:
                            self.kiwoom.subscribe_realtime(code)
                        logger.info(f"[Bot] vm_picks 신규 후보 등록: {c['name']}({code})")
            finally:
                self._rescan_in_progress = False

        import threading as _threading
        _threading.Thread(target=_do_rescan, name="vm-rescan", daemon=True).start()

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

                # VM 매수 대기열 처리 (60초 간격)
                if config.VM_MODE and now_ts - self._last_queue_check >= 60:
                    self._last_queue_check = now_ts
                    self._process_vm_buy_queue()

                # 모의투자 전용 REST 틱 폴링
                if config.IS_SIMULATION and self._in_trade_hours() and self._candidates:
                    custom_targets_map = config.get_custom_targets()
                    # VM 포지션 보유 종목 집합 (어제 산 종목 포함)
                    vm_pos_codes = {p.code for p in self.vm_manager.get_all().values()}

                    target_codes = [
                        c["code"] for c in self._candidates
                        if (self.agent.strategy.get_state(c["code"]) and
                            self.agent.strategy.get_state(c["code"]).phase == "WATCHING")
                        or self.execution_skill.has_position(c["code"])
                        or custom_targets_map.get(c["code"])   # vm picks 대기·매수 범위
                        or c["code"] in vm_pos_codes           # vm 포지션 보유 (SL/TP 감시)
                    ]

                    last_prices = self.harness.get_context().get("last_prices", {})

                    for code in target_codes:
                        last_poll   = self._last_sim_poll.get(code, 0.0)
                        # 3분 단일 간격 (API 할당량 절약)
                        interval = 180

                        if now_ts - last_poll >= interval:
                            self._last_sim_poll[code] = now_ts
                            try:
                                info = self.kiwoom.get_stock_info(code, fast=True)
                            except Exception:
                                continue   # 429 즉시 스킵 → 다음 3분에 재시도 (블로킹 방지)
                            if info and info["current_price"] > 0:
                                fake_tick = {
                                    "code": code,
                                    "price": info["current_price"],
                                    "volume": info.get("acc_trd_vol", 0),
                                    "time": datetime.now().strftime("%H%M%S"),
                                }
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

                # vm_picks.json 변경 감지 및 신규 후보 등록 (30초 간격)
                if config.VM_MODE and now_ts - self._last_vm_picks_check >= 30:
                    self._last_vm_picks_check = now_ts
                    self._reload_vm_picks_if_changed()

                # 헬스체크 파일 갱신 + 텔레그램 폴링 쓰레드 감시 (60초 간격)
                if now_ts - self._last_heartbeat >= 60:
                    self._last_heartbeat = now_ts
                    self._write_heartbeat()
                    self._print_status_board()
                    last_print = now_ts
                    # 폴링 쓰레드가 죽었으면 자동 재시작
                    self.notifier.ensure_polling()

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

            # VM 포지션 장마감 처리 (holding_days 미만은 오버나잇 유지)
            last_prices = self.harness.get_context().get("last_prices", {})
            for pid, vm_pos in list(self.vm_manager.get_all().items()):
                vm_conf = config.get_custom_targets().get(vm_pos.code, {})
                holding_days = vm_conf.get("holding_days", 0)
                if holding_days > 0 and vm_pos.created_at:
                    try:
                        created_dt = datetime.strptime(vm_pos.created_at, "%Y-%m-%d")
                        elapsed = (today_dt - created_dt).days
                        if elapsed < holding_days:
                            logger.info(
                                f"[Bot] VM 포지션 홀딩 유지 "
                                f"({elapsed}/{holding_days}일): {vm_pos.name}({vm_pos.code})"
                            )
                            continue  # 오버나잇 유지
                    except Exception as _e:
                        logger.debug(f"[Bot] VM 포지션 날짜 파싱 오류: {_e}")
                cur_price = last_prices.get(vm_pos.code, vm_pos.entry_price)
                ok = self.vm_manager.sell_vm(pid, cur_price, "장마감")
                if ok:
                    self.trade_logger.log_trade(
                        code=vm_pos.code, name=vm_pos.name, side="SELL",
                        qty=vm_pos.qty, price=cur_price,
                        pnl=vm_pos.pnl(cur_price), pnl_rate=vm_pos.pnl_rate(cur_price),
                        reason="장마감"
                    )
                    logger.warning(f"[Bot] VM 포지션 장마감 청산: {vm_pos.name}({vm_pos.code})")

            # VM 매수 대기열 초기화
            self.harness.get_context()["vm_buy_queue"] = []
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

        # 텔레그램 일일 요약 전송
        pnl = summary.get("total_pnl", 0)
        pnl_emoji = "📈" if pnl >= 0 else "📉"
        today_str = datetime.now().strftime("%Y-%m-%d")

        msg_lines = [
            f"{pnl_emoji} <b>오늘 장 마감 요약 [{today_str}]</b>",
            f"거래 횟수: {summary.get('total', 0)}회",
            f"승: {summary.get('wins', 0)}회 / 패: {summary.get('loses', 0)}회 "
            f"(승률: {summary.get('win_rate', 0):.1%})",
            f"실현 손익: {pnl:+,.0f}원",
        ]

        # 오버나잇 유지 VM 포지션
        last_prices  = self.harness.get_context().get("last_prices", {})
        vm_positions = self.vm_manager.get_all()
        if vm_positions:
            msg_lines.append(f"\n🌙 <b>오버나잇 보유 포지션 ({len(vm_positions)}개)</b>")
            total_unreal = 0
            for pid, pos in vm_positions.items():
                cur = last_prices.get(pos.code, pos.entry_price)
                pnl_pos  = pos.pnl(cur)
                rate_pos = pos.pnl_rate(cur)
                total_unreal += pnl_pos
                emoji = "▲" if pnl_pos >= 0 else "▼"
                # holding_days 진행도
                vm_conf      = config.get_custom_targets().get(pos.code, {})
                holding_days = vm_conf.get("holding_days", 0)
                try:
                    created_dt = datetime.strptime(pos.created_at, "%Y-%m-%d")
                    elapsed    = (datetime.now() - created_dt).days
                    hold_str   = f" ({elapsed}/{holding_days}일)" if holding_days else ""
                except Exception:
                    hold_str = ""
                msg_lines.append(
                    f"  {emoji} <b>{pos.name}({pos.code})</b>{hold_str}\n"
                    f"    {pos.qty}주 @ {pos.entry_price:,}원 진입 → 현재 {cur:,}원\n"
                    f"    평가손익: {pnl_pos:+,.0f}원 ({rate_pos:+.2%})\n"
                    f"    SL: {pos.stop_loss:,}원 | TP: {pos.take_profit:,}원"
                )
            msg_lines.append(f"  └ 미실현 합계: {total_unreal:+,.0f}원")
        else:
            msg_lines.append("\n✅ 오버나잇 포지션 없음 (전량 청산)")

        self.notifier.send("\n".join(msg_lines))
        self.notifier.stop()

        # 일별 DB 백업
        self._backup_db()

        # 리셋
        self.execution_skill.reset_daily()
        self.risk.reset_daily()
        self._running = False
        self._stop_event.set()

        logger.info("[Bot] 장 종료 처리 완료")

    # ─────────────────────────────────────────
    # 텔레그램 원격 명령
    # ─────────────────────────────────────────
    def _register_telegram_commands(self):
        n = self.notifier

        def cmd_status(_args):
            return self._get_status_message()

        def cmd_sell(_args):
            self._clear_all_positions(reason="원격명령강제청산")
            return "✅ 전량 청산 명령 실행 완료"

        def cmd_stop(_args):
            self._stop_event.set()
            self._running = False
            return "🛑 봇 종료 명령 실행. systemd Restart=on-failure 로 자동 재시작됩니다."

        def cmd_reload(_args):
            self._vm_picks_mtime = 0.0  # 강제 재로드 트리거
            self._reload_vm_picks_if_changed()
            return "🔄 vm_picks.json 강제 재로드 완료"

        def cmd_log(_args):
            import os
            log_dir = config.LOG_DIR
            today = datetime.now().strftime("%Y%m%d")
            candidates = [f for f in os.listdir(log_dir) if today in f] if os.path.isdir(log_dir) else []
            if not candidates:
                return "📋 오늘 로그 파일 없음"
            log_path = os.path.join(log_dir, sorted(candidates)[-1])
            try:
                with open(log_path, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                tail = "".join(lines[-15:])
                return f"📋 <b>최근 로그 (15줄)</b>\n<code>{tail[:3000]}</code>"
            except Exception as e:
                return f"❌ 로그 읽기 실패: {e}"

        def cmd_ping(_args):
            return f"🏓 pong! [{datetime.now().strftime('%H:%M:%S')}] 봇 정상 실행 중"

        def cmd_pos(_args):
            """현재 보유 포지션 상세 조회 (장 외 시간 REST 현재가 사용)"""
            vm_positions = self.vm_manager.get_all()
            gen_positions = self.execution_skill.get_all_positions()
            last_prices  = self.harness.get_context().get("last_prices", {})
            now_str = datetime.now().strftime("%H:%M:%S")

            if not vm_positions and not gen_positions:
                return f"📂 보유 포지션 없음 [{now_str}]"

            def _get_price(code, fallback):
                """실시간 틱 없으면 REST로 현재가 조회"""
                price = last_prices.get(code, 0)
                if not price and self._kiwoom_ready:
                    try:
                        info  = self.kiwoom.get_stock_info(code)
                        price = info.get("current_price", 0)
                        flu   = info.get("flu_rt")
                    except Exception:
                        flu = None
                    return price, flu
                return price, None

            lines = [f"📂 <b>보유 포지션 [{now_str}]</b>"]
            total_unreal = 0

            # ── VM 포지션 ──────────────────────────────────────────────
            if vm_positions:
                lines.append(f"\n🤖 <b>VM 포지션 ({len(vm_positions)}개)</b>")
                for pid, pos in vm_positions.items():
                    cur, flu = _get_price(pos.code, pos.entry_price)
                    if not cur:
                        cur = pos.entry_price
                    pnl_pos  = pos.pnl(cur)
                    rate_pos = pos.pnl_rate(cur)
                    total_unreal += pnl_pos
                    emoji = "▲" if pnl_pos >= 0 else "▼"

                    # 홀딩 진행도
                    vm_conf      = config.get_custom_targets().get(pos.code, {})
                    holding_days = vm_conf.get("holding_days", 0)
                    try:
                        elapsed   = (datetime.now() - datetime.strptime(pos.created_at, "%Y-%m-%d")).days
                        hold_str  = f" ({elapsed}/{holding_days}일)" if holding_days else ""
                    except Exception:
                        hold_str = ""

                    flu_str = f" {flu:+.2f}%" if flu is not None else ""
                    lines.append(
                        f"\n  {emoji} <b>{pos.name} ({pos.code})</b>{hold_str}\n"
                        f"    진입: {pos.entry_price:,}원 | 현재: {cur:,}원{flu_str}\n"
                        f"    평가손익: {pnl_pos:+,.0f}원 ({rate_pos:+.2%})\n"
                        f"    수량: {pos.qty}주 | 평가금액: {int(cur * pos.qty):,}원\n"
                        f"    SL: {pos.stop_loss:,}원 | TP: {pos.take_profit:,}원"
                    )
                lines.append(f"\n  └ VM 미실현 합계: {total_unreal:+,.0f}원")

            # ── 일반 포지션 ────────────────────────────────────────────
            if gen_positions:
                lines.append(f"\n📊 <b>일반 포지션 ({len(gen_positions)}개)</b>")
                gen_unreal = 0
                for code, pos in gen_positions.items():
                    cur, flu = _get_price(code, pos.entry_price)
                    if not cur:
                        cur = pos.entry_price
                    pnl_pos  = pos.pnl(cur)
                    rate_pos = pos.pnl_rate(cur)
                    gen_unreal += pnl_pos
                    emoji = "▲" if pnl_pos >= 0 else "▼"
                    flu_str = f" {flu:+.2f}%" if flu is not None else ""
                    lines.append(
                        f"\n  {emoji} <b>{pos.name} ({code})</b>\n"
                        f"    진입: {pos.entry_price:,}원 | 현재: {cur:,}원{flu_str}\n"
                        f"    평가손익: {pnl_pos:+,.0f}원 ({rate_pos:+.2%})\n"
                        f"    수량: {pos.qty}주 | 평가금액: {int(cur * pos.qty):,}원"
                    )
                lines.append(f"\n  └ 일반 미실현 합계: {gen_unreal:+,.0f}원")
                total_unreal += gen_unreal

            lines.append(f"\n💰 <b>미실현 손익 총합: {total_unreal:+,.0f}원</b>")
            return "\n".join(lines)

        def cmd_picks(_args):
            """vm_picks.json 종목 현황 + 포지션 상태 조회"""
            import os as _os
            import json as _json
            path = config.VM_PICKS_PATH
            if not _os.path.exists(path):
                return f"⚠️ vm_picks.json 없음\n경로: {path}"
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = _json.load(f)
            except Exception as e:
                return f"❌ vm_picks.json 읽기 실패: {e}"

            # 포맷 B: {"date":..., "picks":[...]} — market_analyzer 출력
            if isinstance(raw, dict) and "picks" in raw and isinstance(raw["picks"], list):
                date_str = raw.get("date", "?")
                picks = []
                for p in raw["picks"]:
                    picks.append({
                        "code":           p.get("code", ""),
                        "name":           p.get("name", ""),
                        "buy_min":        p.get("buy_min"),
                        "buy_max":        p.get("buy_max"),
                        "stop_loss":      p.get("stop_loss"),
                        "take_profit":    p.get("take_profit_1"),
                        "take_profit_2":  p.get("take_profit_2"),
                        "holding_period": p.get("holding_period", ""),
                        "current_price":  p.get("current_price") or 0,  # 기준가 (±1% 계산용)
                        "rank":           p.get("rank", 0),
                    })
                picks.sort(key=lambda x: x["rank"])
            # 포맷 A: {"000660": {...}, ...} — 기존 플랫 딕셔너리
            elif isinstance(raw, dict):
                date_str = datetime.now().strftime("%Y-%m-%d")
                picks = []
                for code, v in raw.items():
                    if isinstance(v, dict):
                        picks.append({"code": code, "rank": 0, **v})
            else:
                return "⚠️ vm_picks.json 형식 인식 불가"

            if not picks:
                return "⚠️ vm_picks.json에 종목 없음"

            vm_positions   = self.vm_manager.get_all()
            vm_queue_codes = {q["code"] for q in self.harness.get_context().get("vm_buy_queue", [])}
            last_prices    = self.harness.get_context().get("last_prices", {})
            today_volume   = self.harness.get_context().get("today_volume", {})
            rank_emoji = {1: "1️⃣", 2: "2️⃣", 3: "3️⃣"}

            lines = [f"📋 <b>VM Picks [{date_str} 기준]</b>\n"]
            for i, p in enumerate(picks, 1):
                code    = p["code"]
                name    = p.get("name", code)
                buy_min   = p.get("buy_min")   or 0
                buy_max   = p.get("buy_max")   or 0
                sl        = p.get("stop_loss") or 0
                tp1       = p.get("take_profit")   or 0
                tp2       = p.get("take_profit_2") or 0
                period    = p.get("holding_period", "")
                no_range  = not (buy_min > 0 and buy_max > 0)  # 매수 범위 미지정 여부

                cur_price = last_prices.get(code, 0)
                flu_rt    = None   # 등락률
                inv_data  = None   # 수급

                # 실시간 틱 없으면 REST ka10001로 폴백 (현재가 + 등락률 + 거래량)
                if not cur_price and self._kiwoom_ready:
                    try:
                        info      = self.kiwoom.get_stock_info(code)
                        cur_price = info.get("current_price", 0)
                        flu_rt    = info.get("flu_rt")
                        vol_rest  = info.get("acc_trd_vol", 0)
                        if vol_rest:
                            today_volume[code] = vol_rest
                    except Exception:
                        pass

                # REST로도 등락률 없으면 별도 조회
                if cur_price and flu_rt is None and self._kiwoom_ready:
                    try:
                        info   = self.kiwoom.get_stock_info(code)
                        flu_rt = info.get("flu_rt")
                    except Exception:
                        pass

                # 수급 (개인/기관/외국인) — ka10060
                if self._kiwoom_ready:
                    try:
                        inv_data = self.kiwoom.get_investor_data(code)
                    except Exception:
                        pass

                vol = today_volume.get(code, 0)

                em = rank_emoji.get(i, f"{i}.")
                lines.append(f"{em} <b>{name} ({code})</b>")

                # 현재가 · 등락률 · 거래량
                if cur_price:
                    chg_str  = (f" {flu_rt:+.2f}%" if flu_rt is not None else "")
                    price_str = f"{cur_price:,}원{chg_str}"
                else:
                    price_str = "—"
                vol_str = f"{vol:,}주" if vol else "(장중 집계)"

                # 매수 범위 대비 위치 표시
                if cur_price and buy_min and buy_max:
                    if cur_price < buy_min:
                        range_str = f" ↓{buy_min - cur_price:,}원"
                    elif cur_price > buy_max:
                        range_str = f" ↑{cur_price - buy_max:,}원 초과"
                    else:
                        range_str = " ✅범위내"
                else:
                    range_str = ""
                lines.append(f"   현재가: {price_str}{range_str} | 거래량: {vol_str}")

                # 수급 (개인/기관/외국인)
                if inv_data and any(v is not None for v in inv_data.values()):
                    def _fmt(v):
                        if v is None:
                            return "—"
                        sign = "+" if v >= 0 else ""
                        return f"{sign}{v:,}주"
                    lines.append(
                        f"   개인: {_fmt(inv_data['individual'])} | "
                        f"기관: {_fmt(inv_data['institution'])} | "
                        f"외국인: {_fmt(inv_data['foreign'])}"
                    )

                if no_range:
                    lines.append(f"   매수 범위: 미지정 → SL 초과 시 즉시 진입")
                elif buy_min and buy_max:
                    lines.append(f"   매수 범위: {buy_min:,} ~ {buy_max:,}원")
                if sl:
                    lines.append(f"   손절가: {sl:,}원")
                tp_str = f"{tp1:,}원" if tp1 else "—"
                if tp2:
                    tp_str += f" (2차: {tp2:,}원)"
                lines.append(f"   목표가: {tp_str}")
                if period:
                    lines.append(f"   보유 기간: {period}")

                pos_list = [v for v in vm_positions.values() if v.code == code]
                if pos_list:
                    for pos in pos_list:
                        lines.append(
                            f"   ✅ 포지션 보유: {pos.qty}주 @ {pos.entry_price:,}원 진입"
                        )
                elif code in vm_queue_codes:
                    lines.append("   💰 예수금 부족 대기열")
                else:
                    lines.append("   ⏳ 진입 대기 중")
                lines.append("")

            return "\n".join(lines)

        _STRATEGY_NAMES = {1: "신고가 돌파", 2: "전일 종가 돌파", 3: "20MA 눌림 매수"}

        def cmd_strategy(args):
            """전략 설정 조회(/strategy) 또는 변경(/strategy 1~3 / vm on|off / amount N / condition 이름)"""
            parts = args.strip().split()

            if not parts:
                # 현재 설정 조회
                vm_flag = "✅ ON" if config.VM_MODE else "❌ OFF"
                stype   = getattr(config, "ENTRY_STRATEGY_TYPE", 3)
                amt     = getattr(config, "VM_TRADE_AMOUNT", 1_000_000)
                sl_rate = getattr(config, "STOP_LOSS_RATE", 0.03)
                tp_rate = getattr(config, "TARGET_PROFIT_RATE", 0.07)
                cond    = config.CONDITION_NAME or config.CONDITION_SEQ or "없음"
                clear_t = getattr(config, "CLEAR_TIME", "15:20")
                lines = ["⚙️ <b>전략 설정 현황</b>\n"]

                if config.VM_MODE:
                    # VM 모드 활성: picks 전용 설정만 표시
                    lines += [
                        f"모드: 🤖 VM Picks 자동매매 ✅",
                        f"VM 매수금액: {amt:,}원 (종목당 고정)",
                        f"손절가·목표가: vm_picks.json 절대값 사용",
                        f"  → 손절율({sl_rate:.1%})·익절율({tp_rate:.1%})은 VM picks에 미적용",
                        "",
                        f"일괄청산: {clear_t}",
                        f"  → holding_days 미달 포지션은 오버나잇 유지",
                        "",
                        "일반 전략 (VM picks 외 조건식 종목에만 적용):",
                        f"  진입 전략: {stype}번 ({_STRATEGY_NAMES.get(stype, '?')})",
                        f"  조건식: {cond}",
                        f"  손절율: {sl_rate:.1%} | 익절율: {tp_rate:.1%}",
                    ]
                else:
                    lines += [
                        f"모드: 일반 전략 매매 (VM Picks OFF)",
                        f"진입 전략: {stype}번 ({_STRATEGY_NAMES.get(stype, '?')})",
                        f"조건식: {cond}",
                        f"손절율: {sl_rate:.1%} | 익절율: {tp_rate:.1%}",
                        f"일괄청산: {clear_t}",
                    ]

                lines += [
                    "\n📌 <b>변경 명령</b>",
                    "/strategy 1~3 — 진입 전략 변경",
                    "/strategy vm on|off — VM picks 모드 토글",
                    "/strategy amount 2000000 — VM 매수금액 변경",
                    "/strategy condition #이름 — 조건식 변경 (런타임)",
                ]
                return "\n".join(lines)

            sub = parts[0].lower()

            # 전략 번호 변경
            if sub in ("1", "2", "3"):
                n_val = int(sub)
                config.ENTRY_STRATEGY_TYPE = n_val
                return f"✅ 진입 전략 → {n_val}번 ({_STRATEGY_NAMES[n_val]})"

            # VM 모드 토글
            if sub == "vm" and len(parts) >= 2:
                onoff = parts[1].lower()
                if onoff == "on":
                    config.VM_MODE = True
                    return "✅ VM Picks 모드 ON — vm_picks.json 기반 매매 활성화"
                if onoff == "off":
                    config.VM_MODE = False
                    return "✅ VM Picks 모드 OFF — 일반 전략 매매로 전환"
                return "❓ 사용법: /strategy vm on|off"

            # VM 매수금액 변경
            if sub == "amount" and len(parts) >= 2:
                try:
                    amt = int(parts[1].replace(",", ""))
                    config.VM_TRADE_AMOUNT = amt
                    return f"✅ VM 매수금액 → {amt:,}원"
                except ValueError:
                    return "❓ 사용법: /strategy amount 1000000"

            # 조건식 변경 (런타임)
            if sub == "condition" and len(parts) >= 2:
                cond_name = " ".join(parts[1:])
                if not self._kiwoom_ready:
                    return "❌ Kiwoom 비활성 상태에서는 조건식 변경 불가"
                self._change_condition_runtime(cond_name)
                return f"🔄 조건식 변경 요청: {cond_name}"

            return "❓ 사용법: /strategy [1~3 | vm on/off | amount <금액> | condition <이름>]"

        def cmd_validate(_args):
            """vm_picks.json 품질 검증 — 필드 누락·가격 논리·날짜 오류 체크"""
            import os as _os
            import json as _json
            path = config.VM_PICKS_PATH
            today_str = datetime.now().strftime("%Y-%m-%d")

            if not _os.path.exists(path):
                return f"❌ 파일 없음: {path}"
            try:
                with open(path, "r", encoding="utf-8") as f:
                    raw = _json.load(f)
            except Exception as e:
                return f"❌ 파일 읽기 실패: {e}"

            # 헤더 정보
            file_date   = raw.get("date", "?")
            updated_at  = raw.get("updated_at", "?")
            picks_list  = raw.get("picks", []) if isinstance(raw, dict) else []
            date_ok     = file_date == today_str
            date_emoji  = "✅" if date_ok else "⚠️"

            lines = [
                f"🔍 <b>vm_picks.json 검증</b>",
                f"파일 날짜: {date_emoji} {file_date} (오늘: {today_str})",
                f"업데이트: {updated_at}",
                f"종목 수: {len(picks_list)}개",
                "",
            ]

            if not date_ok:
                lines.append("⚠️ <b>경고: 파일이 오늘 날짜가 아닙니다!</b>")

            # 각 종목 검증
            issues_total = 0
            for p in picks_list:
                rank        = p.get("rank", "?")
                name        = p.get("name", "?")
                code        = p.get("code", "?")
                cur_price   = p.get("current_price") or 0
                buy_min_raw = p.get("buy_min")
                buy_max_raw = p.get("buy_max")
                sl          = p.get("stop_loss") or 0
                tp1         = p.get("take_profit_1") or 0
                tp2         = p.get("take_profit_2") or 0
                period      = p.get("holding_period", "?")

                # 자동 계산된 buy_min/max
                no_range = buy_min_raw is None or buy_max_raw is None
                if no_range and cur_price > 0:
                    buy_min_eff = int(cur_price * 0.985)
                    buy_max_eff = int(cur_price * 1.000)
                    range_note  = f"{buy_min_eff:,}~{buy_max_eff:,}원 (자동: 기준가×-1.5%)"
                elif not no_range:
                    buy_min_eff = buy_min_raw or 0
                    buy_max_eff = buy_max_raw or 0
                    range_note  = f"{buy_min_eff:,}~{buy_max_eff:,}원"
                else:
                    buy_min_eff = buy_max_eff = 0
                    range_note  = "⚠️ 미지정 + 기준가 없음"

                # 검증 항목
                issues = []
                if not sl:
                    issues.append("손절가 없음")
                if not tp1:
                    issues.append("목표가 없음")
                if sl > 0 and tp1 > 0:
                    rr = (tp1 - buy_max_eff) / (buy_max_eff - sl) if buy_max_eff > sl > 0 else 0
                    if rr < 1.0 and rr > 0:
                        issues.append(f"리스크/리워드 낮음 ({rr:.1f})")
                if buy_min_eff > 0 and sl > 0 and buy_min_eff < sl:
                    issues.append("매수 하한가 < 손절가!")
                if buy_max_eff > 0 and tp1 > 0 and buy_max_eff > tp1:
                    issues.append("매수 상한가 > 목표가!")

                issues_total += len(issues)
                status = "✅" if not issues else ("⚠️" if len(issues) <= 1 else "❌")

                lines.append(f"{status} <b>{rank}위 {name} ({code})</b>")
                lines.append(f"   기준가: {cur_price:,}원 | 매수 범위: {range_note}")
                lines.append(f"   손절: {sl:,}원 | 목표1: {tp1:,}원" + (f" | 목표2: {tp2:,}원" if tp2 else ""))
                lines.append(f"   보유: {period}")
                if no_range:
                    lines.append(f"   ℹ️ 매수 범위 미지정 → 자동 계산 적용")
                for iss in issues:
                    lines.append(f"   ⚠️ {iss}")
                lines.append("")

            verdict = "✅ 검증 통과" if (date_ok and issues_total == 0) else f"⚠️ 이슈 {issues_total}건"
            lines.append(f"<b>결론: {verdict}</b>")
            return "\n".join(lines)

        def cmd_help(_args):
            return (
                "📖 <b>사용 가능한 명령</b>\n"
                "/ping — 봇 응답 확인\n"
                "/status — 오늘 손익·대기 현황\n"
                "/pos — 보유 포지션 상세 (장 외 시간 가능)\n"
                "/validate — vm_picks.json 품질 검증\n"
                "/picks — VM picks 종목·가격 현황\n"
                "/strategy — 전략 설정 조회/변경\n"
                "/sell — 전량 강제 청산\n"
                "/reload — vm_picks.json 강제 재로드\n"
                "/log — 최근 로그 15줄\n"
                "/stop — 봇 종료\n"
                "/help — 이 도움말"
            )

        n.register_command("ping",     cmd_ping)
        n.register_command("status",   cmd_status)
        n.register_command("pos",      cmd_pos)
        n.register_command("picks",    cmd_picks)
        n.register_command("validate", cmd_validate)
        n.register_command("strategy", cmd_strategy)
        n.register_command("sell",     cmd_sell)
        n.register_command("stop",     cmd_stop)
        n.register_command("reload",   cmd_reload)
        n.register_command("log",      cmd_log)
        n.register_command("help",     cmd_help)

    def _get_status_message(self) -> str:
        kiwoom_str = "✅ 연결됨" if self._kiwoom_ready else "❌ 비활성 (Telegram 감시 모드)"
        positions = self.execution_skill.get_all_positions()
        last_prices = self.harness.get_context().get("last_prices", {})
        vm_positions = self.vm_manager.get_all()
        # 대기 종목: 일반 후보 중 포지션 없는 것 + vm picks 대기열
        waiting = [c for c in self._candidates if c["code"] not in positions and c["code"] not in {p.code for p in vm_positions.values()}]
        vm_queue = self.harness.get_context().get("vm_buy_queue", [])
        status = self.risk.get_status()
        pnl_emoji = "📈" if status["today_pnl"] >= 0 else "📉"
        total_pos_count = len(positions) + len(vm_positions)
        lines = [
            f"📊 <b>현황 [{datetime.now().strftime('%H:%M:%S')}]</b>",
            f"Kiwoom: {kiwoom_str}",
            f"{pnl_emoji} 오늘 손익: {status['today_pnl']:+,.0f}원 ({status['pnl_rate']:+.2%}) | 거래: {status['trade_count']}회",
            f"대기: {len(waiting)}종목 | 보유: {total_pos_count}종목",
        ]
        # 일반 포지션
        for code, pos in positions.items():
            cur = last_prices.get(code, pos.entry_price)
            pnl = pos.pnl(cur)
            rate = pos.pnl_rate(cur)
            emoji = "▲" if pnl >= 0 else "▼"
            lines.append(f"  {emoji} [{pos.name}] {pos.qty}주 {rate:+.2%} ({pnl:+,.0f}원)")
        # VM 포지션
        if vm_positions:
            lines.append(f"\n🤖 <b>VM 포지션 ({len(vm_positions)}개)</b>")
            for pid, vm_pos in vm_positions.items():
                cur = last_prices.get(vm_pos.code, vm_pos.entry_price)
                pnl = vm_pos.pnl(cur)
                rate = vm_pos.pnl_rate(cur)
                emoji = "▲" if pnl >= 0 else "▼"
                lines.append(
                    f"  {emoji} [{vm_pos.name}({vm_pos.code})] {vm_pos.qty}주 "
                    f"{rate:+.2%} ({pnl:+,.0f}원)\n"
                    f"    SL: {vm_pos.stop_loss:,}원 | TP: {vm_pos.take_profit:,}원"
                )
        # VM 대기열
        if vm_queue:
            lines.append(f"\n⏳ <b>VM 매수 대기열 ({len(vm_queue)}개, 예수금 부족)</b>")
            for item in vm_queue:
                lines.append(f"  - [{item['name']}({item['code']})]")
        return "\n".join(lines)

    # ─────────────────────────────────────────
    # VM 매수 대기열 처리 (예수금 부족 대기 종목 재시도)
    # ─────────────────────────────────────────
    def _process_vm_buy_queue(self):
        queue = self.harness.get_context().get("vm_buy_queue", [])
        if not queue:
            return
        last_prices = self.harness.get_context().get("last_prices", {})
        remaining = []
        for item in list(queue):
            code = item["code"]
            current_price = last_prices.get(code, 0)
            if current_price <= 0:
                remaining.append(item)
                continue
            # 해당 트랜치가 이미 완료됐으면 제거
            tranche       = item.get("tranche", 1)
            cur_count     = self.vm_manager.count_positions_for_date(code, item["created_at"])
            if cur_count >= tranche:
                logger.info(
                    f"[Bot] VM 대기열 제거 ({tranche}차 이미 완료): {item['name']}({code})"
                )
                continue
            ok = self.vm_manager.buy_vm(
                code=code,
                name=item["name"],
                price=current_price,
                stop_loss=item["stop_loss"],
                take_profit=item["take_profit"],
                created_at=item["created_at"],
                amount=item.get("amount"),
            )
            if ok:
                vm_pos_list = self.vm_manager.get_by_code(code)
                vm_pos = vm_pos_list[-1] if vm_pos_list else None
                qty = vm_pos.qty if vm_pos else 0
                logger.info(
                    f"[Bot] VM 대기 매수 체결: {item['name']}({code}) "
                    f"{qty}주 @ {current_price:,}원"
                )
                self.trade_logger.log_trade(
                    code=code, name=item["name"], side="BUY",
                    qty=qty, price=current_price, reason="VM대기매수"
                )
                self.notifier.send(
                    f"🟢 <b>VM 대기 매수 체결 ({tranche}차)</b> [{item['name']}({code})]\n"
                    f"{qty}주 @ {current_price:,}원\n"
                    f"손절가: {item['stop_loss']:,}원 | 목표가: {item['take_profit']:,}원"
                )
            else:
                remaining.append(item)  # 여전히 예수금 부족 → 계속 대기
        self.harness.get_context()["vm_buy_queue"] = remaining

    # ─────────────────────────────────────────
    # 헬스체크 + DB 백업
    # ─────────────────────────────────────────
    def _write_heartbeat(self):
        import os
        path = os.path.join(os.path.dirname(config.DB_PATH), "heartbeat.txt")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(datetime.now().isoformat())
        except Exception as e:
            logger.debug(f"[Bot] heartbeat 파일 쓰기 실패: {e}")

    def _vm_telegram_loop(self):
        """
        Kiwoom 없이 동작하는 VM Telegram 감시 루프.
        Telegram 폴링 + vm_picks.json 모니터링 + 5분마다 Kiwoom 재연결 시도.
        재연결 성공 시 os.execv()로 프로세스를 새로 시작해 전체 봇 모드로 전환.
        """
        logger.info("[Bot] VM Telegram 감시 루프 시작 (Kiwoom 비활성)")
        _last_kiwoom_retry = time.time()  # 진입 직후 즉시 재시도 방지 (5분 뒤 첫 시도)
        _RETRY_INTERVAL = 300  # 5분

        while self._running and not self._stop_event.is_set():
            now_ts = time.time()

            # vm_picks.json 변경 감지 (30초 간격) — scanner 없이 파일만 읽음
            if config.VM_MODE and now_ts - self._last_vm_picks_check >= 30:
                self._last_vm_picks_check = now_ts
                self._reload_vm_picks_lightweight()

            # heartbeat + Telegram 폴링 쓰레드 감시 (60초 간격)
            if now_ts - self._last_heartbeat >= 60:
                self._last_heartbeat = now_ts
                self._write_heartbeat()
                self.notifier.ensure_polling()

            # Kiwoom 재연결 시도 (5분 간격)
            if now_ts - _last_kiwoom_retry >= _RETRY_INTERVAL:
                _last_kiwoom_retry = now_ts
                logger.info("[Bot] Kiwoom 재연결 시도 중...")
                if self.kiwoom.login():
                    logger.info("[Bot] ✅ Kiwoom 재연결 성공 → 프로세스 재시작")
                    self.notifier.send("✅ <b>키움 재연결 성공!</b>\n봇을 재시작합니다...")
                    time.sleep(2)
                    import os as _os_exec
                    _os_exec.execv(
                        sys.executable,
                        [sys.executable, _os_exec.path.abspath(__file__)] + sys.argv[1:]
                    )

            self._stop_event.wait(timeout=15)

    def _reload_vm_picks_lightweight(self):
        """
        Kiwoom 없이 vm_picks.json 파일만 파싱하여 vm_manager SL/TP 업데이트.
        scanner.run_scan() 을 호출하지 않으므로 Kiwoom 불필요.
        """
        import os as _os_lw
        import json as _json_lw
        path = config.VM_PICKS_PATH
        if not _os_lw.path.exists(path):
            return
        mtime = _os_lw.path.getmtime(path)
        if mtime <= self._vm_picks_mtime:
            return
        self._vm_picks_mtime = mtime
        logger.info("[Bot] vm_picks.json 변경 감지 (Telegram 감시 모드)")
        try:
            with open(path, "r", encoding="utf-8") as f:
                picks = _json_lw.load(f)
            # Format B: {"date": "...", "picks": [...]}
            if isinstance(picks, dict) and "picks" in picks and isinstance(picks["picks"], list):
                pick_items = {
                    p["code"]: {
                        "stop_loss":   p.get("stop_loss", 0),
                        "take_profit": p.get("take_profit_1", 0),
                        "buy_min":     p.get("buy_min"),
                        "buy_max":     p.get("buy_max"),
                        "name":        p.get("name", p.get("code", "")),
                    }
                    for p in picks["picks"] if p.get("code")
                }
            else:
                pick_items = picks  # Format A
            for code, conf in pick_items.items():
                if not isinstance(conf, dict):
                    continue
                sl   = conf.get("stop_loss", 0)
                tp   = conf.get("take_profit", 0)
                name = conf.get("name", code)
                if sl and tp and self.vm_manager.has_any(code):
                    self.vm_manager.update_sltp(code, sl, tp)
                    self.notifier.send(
                        f"🔄 <b>SL/TP 업데이트</b> [{name}({code})]\n"
                        f"손절가: {sl:,}원 | 목표가: {tp:,}원"
                    )
                logger.info(
                    f"[Bot] vm_picks 감지: {name}({code}) "
                    f"buy={conf.get('buy_min')}-{conf.get('buy_max')} "
                    f"SL={sl} TP={tp}"
                )
        except Exception as e:
            logger.debug(f"[Bot] vm_picks lightweight 파싱 오류: {e}")

    def _auto_validate_vm_picks(self, path: str):
        """vm_picks.json 자동 품질 검증 — 이슈 있을 때만 텔레그램 알림."""
        import json as _j
        today_str = datetime.now().strftime("%Y-%m-%d")
        try:
            with open(path, "r", encoding="utf-8") as f:
                raw = _j.load(f)
        except Exception:
            return

        file_date  = raw.get("date", "") if isinstance(raw, dict) else ""
        picks_list = raw.get("picks", []) if isinstance(raw, dict) else []

        issues = []
        if file_date and file_date != today_str:
            issues.append(f"⚠️ 파일 날짜 불일치: {file_date} (오늘: {today_str})")

        null_range_names = []
        for p in picks_list:
            name = p.get("name", p.get("code", "?"))
            if p.get("buy_min") is None or p.get("buy_max") is None:
                null_range_names.append(name)
            if not p.get("stop_loss"):
                issues.append(f"⚠️ {name}: 손절가 없음")
            if not p.get("take_profit_1"):
                issues.append(f"⚠️ {name}: 목표가 없음")

        if null_range_names:
            issues.append(f"ℹ️ 매수 범위 미지정 → 자동 계산: {', '.join(null_range_names)}")

        if issues:
            msg = (
                f"🔍 <b>vm_picks.json 검증 [{file_date}]</b>\n"
                + "\n".join(issues)
                + f"\n\n종목: {', '.join(p.get('name','?') for p in picks_list)}"
            )
            self.notifier.send(msg)
            logger.info(f"[Bot] vm_picks 검증 알림 전송: {len(issues)}건")
        else:
            logger.info(f"[Bot] vm_picks 검증 통과: {len(picks_list)}종목 정상")

    def _backup_db(self):
        import os
        import shutil
        src = config.DB_PATH
        if not os.path.exists(src):
            return
        backup_dir = os.path.join(os.path.dirname(src), "backups")
        os.makedirs(backup_dir, exist_ok=True)
        dst = os.path.join(backup_dir, f"trade_history_{datetime.now().strftime('%Y%m%d')}.db")
        try:
            shutil.copy2(src, dst)
            logger.info(f"[Bot] DB 백업 완료: {dst}")
        except Exception as e:
            logger.error(f"[Bot] DB 백업 실패: {e}")

    # ─────────────────────────────────────────
    # 유틸
    # ─────────────────────────────────────────
    def _in_trade_hours(self) -> bool:
        """config.is_trade_hours() 래퍼 — 단일 진실 공급원 유지."""
        return config.is_trade_hours()

    def _can_buy_time(self) -> bool:
        """config.is_buy_time() 래퍼 — 단일 진실 공급원 유지."""
        return config.is_buy_time()


# ─────────────────────────────────────────
# 진입점
# ─────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    import time
    from datetime import datetime, timedelta

    def _is_trading_day(date) -> bool:
        try:
            import holidays
            kr = holidays.KR(years=date.year)
            return date.weekday() < 5 and date not in kr
        except ImportError:
            # holidays 패키지 미설치 시 주말만 제외
            return date.weekday() < 5

    def _sleep_until(target: datetime):
        delta = (target - datetime.now()).total_seconds()
        if delta > 0:
            time.sleep(delta)

    parser = argparse.ArgumentParser()
    parser.add_argument("--auto", action="store_true", help="Auto start at 09:00 with default strategy and condition")
    args = parser.parse_args()

    if args.auto:
        while True:
            today = datetime.now().date()
            if not _is_trading_day(today):
                next_check = datetime.combine(today + timedelta(days=1), datetime.min.time()).replace(hour=8, minute=50)
                print(f"\n[Bot] 비거래일 ({today}): 주말 또는 공휴일입니다. {next_check.strftime('%m/%d %H:%M')}에 재확인합니다.")
                logger.info(f"[Bot] 비거래일 ({today}) — {next_check.strftime('%Y-%m-%d %H:%M')}에 재확인")
                _sleep_until(next_check)
                continue

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
