# main.py - 트레이딩 봇 메인 진입점 (하네스 기반 에이전트 아키텍처 버전)
# OCX / PyQt5 의존성 완전 제거

import sys
import time
import threading
import logging
from datetime import datetime

import config
from logger import setup_logger, TradeLogger
from kiwoom_api import KiwoomAPI, classify_movers_overlap
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
from agents.leverage_agent import LeverageInverseAgent
from agents.personal_invest_agent import PersonalInvestAgent
from agents.api_usage_agent import ApiUsageAgent

# 로거 초기화
logger = setup_logger("main")


def _update_env_value(key: str, value: str) -> None:
    """trading_bot/.env의 키 값을 업데이트하거나 없으면 줄 끝에 추가."""
    import os as _os_env
    env_path = _os_env.path.join(_os_env.path.dirname(__file__), ".env")
    try:
        lines = open(env_path, "r", encoding="utf-8").readlines() if _os_env.path.exists(env_path) else []
        found = False
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.startswith(f"{key}=") or stripped.startswith(f"{key} ="):
                lines[i] = f"{key}={value}\n"
                found = True
                break
        if not found:
            lines.append(f"{key}={value}\n")
        with open(env_path, "w", encoding="utf-8") as f:
            f.writelines(lines)
    except Exception as e:
        logger.warning(f"[Env] .env 저장 실패 ({key}): {e}")


LEVERAGE_STRATEGY_NAMES = {
    "BOLLINGER": "일봉추세+볼린저밴드돌파",
    "TREND_MACD": "일봉추세+MACD크로스",
    "MACD_HA": "MACD+하이킨아시",
    "BASIC": "연속봉+5MA+거래량",
}


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
        self._last_acc_vol  = {}   # 삼성전자 시뮬 폴링: 누적거래량 → 증분 변환용
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

        self.leverage_agent = LeverageInverseAgent()
        self.harness.register_agent(self.leverage_agent)

        self.personal_invest_agent = PersonalInvestAgent()
        self.harness.register_agent(self.personal_invest_agent)

        self.api_usage_agent = ApiUsageAgent(self.kiwoom, self.notifier)
        self.harness.register_agent(self.api_usage_agent)

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

        # 텔레그램 알림 핸들러 + 폴링 시작 (start() 재호출 시 중복 등록 방지)
        _root = logging.getLogger()
        if not any(isinstance(h, TelegramLogHandler) for h in _root.handlers):
            _root.addHandler(TelegramLogHandler(self.notifier))
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

        # 레버리지-인버스 전략: 삼성전자 + ETF 구독
        self._init_leverage_strategy()

        # 후보 종목 초기 스캔 (일봉 기준)
        self._run_initial_scan()

        # 장 중 실시간 조건식 모니터링 시작 (ka10173)
        self._start_condition_polling()

        self._running = True
        logger.info("[Bot] 매매 대기 중...")

        # 메인 루프 (1분 주기 타이머 역할)
        self._main_loop()

    # ─────────────────────────────────────────
    # 레버리지-인버스 전략 초기화
    # ─────────────────────────────────────────
    def _init_leverage_strategy(self):
        samsung = config.LEVERAGE_SAMSUNG_CODE
        lev     = config.LEVERAGE_ETF_CODE
        inv     = config.INVERSE_ETF_CODE

        # 삼성전자: 신호용 분봉 캔들 빌더 + 실시간 구독 (주기: LEVERAGE_CANDLE_INTERVAL)
        # api_feed=True: 봉은 ka10080 공식 분봉만 사용 (틱 집계 안 함)
        lev_interval = getattr(config, "LEVERAGE_CANDLE_INTERVAL", 10)
        self.market_data_skill.init_stock(
            samsung,
            on_candle_close=lambda c, _s=samsung: self.harness.broadcast_event("CANDLE", c),
            interval_min=lev_interval,
            api_feed=True,
        )
        self.kiwoom.subscribe_realtime(samsung)

        # ETF 2종목: 틱 구독만 (SL/TP 감시용)
        for code in (lev, inv):
            self.market_data_skill.init_stock(code)
            self.kiwoom.subscribe_realtime(code)

        self.leverage_agent.reset_daily()
        logger.info(
            f"[Bot] 레버리지 전략 초기화: 삼성({samsung}, {lev_interval}분봉) "
            f"레버리지({lev}) 인버스({inv})"
        )

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
        # 다른 에이전트가 이미 관리하는 종목은 execution_skill(일반 돌파) 복구에서 제외.
        # vm_manager뿐 아니라 PersonalInvestAgent 종목도 반드시 제외해야 한다 —
        # 제외하지 않으면 개별주 계획 포지션이 일반 돌파 전략에 흡수돼 계획 손절가(예:
        # 실리콘투 33,000원) 대신 일반 전략의 기본 -3% 손절이 붙어 엉뚱하게 청산된다
        # (실측 2026-10-06: 재시작 후 흡수 → -3% 손절 36,115원 발동 → -94,560원 손실).
        vm_codes = {pos.code for pos in self.vm_manager.get_all().values()}
        invest_codes = set(self.personal_invest_agent.watched_codes())
        skip_codes = vm_codes | invest_codes
        positions = self.kiwoom.get_positions()

        # 개별주 유령 포지션 정리 — 외부(다른 에이전트/수동)에서 청산된 종목이
        # PersonalInvestAgent 상태에 보유로 남아있으면 실계좌 기준으로 제거한다.
        real_qty = {p["code"]: p["qty"] for p in positions}
        self.personal_invest_agent.reconcile_with_account(real_qty)

        for p in positions:
            code = p["code"]
            if code in skip_codes:
                continue  # vm_manager 또는 PersonalInvestAgent가 이미 관리 중
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
                # 지정가 분할매수 발주 (VM picks 전용)
                if config.VM_MODE and getattr(config, "VM_PICKS_ENABLED", True):
                    _vm_conf = config.get_custom_targets().get(code)
                    if _vm_conf:
                        self.agent._place_vm_limit_orders(code, _vm_conf, self.vm_manager, self.kiwoom, self.notifier)
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

        # 미체결 지정가 매수 주문 전량 취소
        self.vm_manager.cancel_pending_buys()

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
                        # 지정가 분할매수 발주 (VM picks 전용)
                        _vm_conf = config.get_custom_targets().get(code)
                        if _vm_conf and config.VM_MODE and getattr(config, "VM_PICKS_ENABLED", True):
                            self.agent._place_vm_limit_orders(code, _vm_conf, self.vm_manager, self.kiwoom, self.notifier)
                        logger.info(f"[Bot] vm_picks 신규 후보 등록: {c['name']}({code})")
                # 기존 후보 중 VM picks 종목에도 지정가 발주 시도
                # (초기 스캔 후 vm_picks.json이 갱신되어 재스캔될 때 누락 방지)
                if config.VM_MODE and getattr(config, "VM_PICKS_ENABLED", True):
                    for _code, _vm_conf in config.get_custom_targets().items():
                        self.agent._place_vm_limit_orders(_code, _vm_conf, self.vm_manager, self.kiwoom, self.notifier)
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

                # 레버리지 전략 EOD 강제청산 — 삼성전자 틱/캔들 이벤트에 의존하지 않는
                # 독립 타이머 체크 (API 일일 한도 초과 등으로 신호분봉 갱신이 끊겨도
                # 당일 청산이 누락되지 않도록 매 루프마다 시각만으로 트리거)
                if hasattr(self, "leverage_agent") and getattr(self.leverage_agent, "_position", None):
                    self.leverage_agent._check_force_exit(self.harness.get_context())

                # 장 종료 처리
                if now_hm >= config.TRADE_END_TIME:
                    self._on_market_close()
                    break

                # VM 매수 대기열 + 미체결 fill 폴백 처리 (60초 간격)
                if config.VM_MODE and now_ts - self._last_queue_check >= 60:
                    self._last_queue_check = now_ts
                    self._process_vm_buy_queue()
                    # 모의투자: 시뮬 폴링 TICK → _handle_vm_tick이 pending fill 처리하므로 중복 API 호출 생략
                    if not config.IS_SIMULATION:
                        self._check_pending_fills()

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

                    # 레버리지 전략 종목 추가 (삼성전자 + ETF — 모의투자 무구독 대응)
                    if hasattr(self, "leverage_agent"):
                        ag = self.leverage_agent
                        target_codes = list(dict.fromkeys(
                            target_codes + [ag.SAMSUNG_CODE, ag.LEVERAGE_CODE, ag.INVERSE_CODE]
                        ))

                    # 개별주 계획매매 종목 추가 (대기 계획 + 보유 포지션)
                    if hasattr(self, "personal_invest_agent"):
                        target_codes = list(dict.fromkeys(
                            target_codes + self.personal_invest_agent.watched_codes()
                        ))

                    # ── 폴링 주기 차등화 (API 일일한도 1700 보호, 2026-10-06) ──
                    # 포지션 보유 종목은 SL/TP 실시간 감시가 필요하므로 짧게(180초),
                    # 단순 진입 대기 후보는 길게(기본 420초) 폴링해 총 호출수를 줄인다.
                    # 실측: 감시종목 ~10개를 전부 180초로 돌리면 풀세션(6.5h)에 시세폴링만
                    # ~1,300콜이라 캔들·잔고와 합쳐 1700 한도를 장중 초과했음.
                    held_codes = {c for c in target_codes if self.execution_skill.has_position(c)}
                    held_codes |= {p.code for p in self.vm_manager.get_all().values()}
                    held_codes |= set(self.personal_invest_agent.get_all_positions().keys())
                    if hasattr(self, "leverage_agent"):
                        held_codes |= {self.leverage_agent.LEVERAGE_CODE,
                                       self.leverage_agent.INVERSE_CODE}
                    poll_active = getattr(config, "REST_POLL_INTERVAL", 180)
                    poll_idle   = getattr(config, "REST_POLL_INTERVAL_IDLE", 420)

                    for code in target_codes:
                        if code == "005930":
                            continue  # 삼성전자는 하단 ka10080 갱신 블록에서 처리

                        last_poll = self._last_sim_poll.get(code, 0.0)
                        interval  = poll_active if code in held_codes else poll_idle

                        if now_ts - last_poll >= interval:
                            try:
                                info = self.kiwoom.get_stock_info(code, fast=True)
                            except Exception:
                                self._last_sim_poll[code] = now_ts - interval + 30
                                continue
                            if info and info["current_price"] > 0:
                                self._last_sim_poll[code] = now_ts

                                # 거래량: acc_trd_vol(일 누계) → 증분 변환
                                new_acc_vol  = info.get("acc_trd_vol", 0)
                                prev_acc_vol = self._last_acc_vol.get(code, new_acc_vol)
                                inc_vol      = max(0, new_acc_vol - prev_acc_vol)
                                self._last_acc_vol[code] = new_acc_vol

                                fake_tick = {
                                    "code":   code,
                                    "price":  info["current_price"],
                                    "volume": inc_vol,
                                    "time":   datetime.now().strftime("%H%M%S"),
                                }
                                self.harness._on_tick(fake_tick)
                            else:
                                self._last_sim_poll[code] = now_ts - interval + 30
                            time.sleep(1.5)

                    # 삼성전자 갱신은 아래 독립 블록에서 처리

                # ── 삼성전자 신호 분봉: ka10080 갱신 (실전/시뮬 공용, 레버리지 전략 독립 블록) ──
                # 봉은 API 공식 분봉만 사용 (api_feed=True 빌더). 새 봉이 기대되는 시점에만 폴링.
                if self._in_trade_hours() and hasattr(self, "leverage_agent"):
                    last_refresh = self._last_sim_poll.get("005930", 0.0)
                    if now_ts - last_refresh >= 30:  # 최소 30초 간격
                        market_data = self.harness.skills.get("market_data")
                        if market_data:
                            lev_min = getattr(config, "LEVERAGE_CANDLE_INTERVAL", 10)
                            candles = market_data.get_candles("005930")
                            # 봉 datetime은 시작 시각 → 다음 봉 완성 = 시작 + 2×주기 (+10초 여유)
                            due = (
                                not candles or
                                (datetime.now() - candles[-1].datetime).total_seconds()
                                >= lev_min * 120 + 10
                            )
                            if due:
                                n = market_data.refresh_candles("005930", count=5)
                                self._last_sim_poll["005930"] = now_ts
                                if n > 0:
                                    logger.info(
                                        f"[MarketData] 삼성전자 {lev_min}분봉 갱신: {n}개 주입 (ka10080)"
                                    )
                                elif n < 0:
                                    logger.warning(
                                        "[MarketData] 삼성전자 분봉 갱신 실패 → 30초 후 재시도"
                                    )

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
        pnl          = summary.get("total_pnl", 0)
        pnl_rate     = summary.get("pnl_rate", 0)
        total_amount = summary.get("total_amount", 0)
        pnl_emoji    = "📈" if pnl >= 0 else "📉"
        today_str    = datetime.now().strftime("%Y-%m-%d")

        # 거래금액 대비 수익률 표기 (거래 없으면 생략)
        pnl_rate_str = f" ({pnl_rate:+.2%})" if total_amount > 0 else ""

        msg_lines = [
            f"{pnl_emoji} <b>오늘 장 마감 요약 [{today_str}]</b>",
            f"거래 횟수: {summary.get('total', 0)}회",
            f"승: {summary.get('wins', 0)}회 / 패: {summary.get('loses', 0)}회 "
            f"(승률: {summary.get('win_rate', 0):.1%})",
            f"실현 손익: {pnl:+,.0f}원{pnl_rate_str}  <i>(거래세 차감 후)</i>",
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
            invest_positions = self.personal_invest_agent.get_all_positions()
            last_prices  = self.harness.get_context().get("last_prices", {})
            now_str = datetime.now().strftime("%H:%M:%S")

            if not vm_positions and not gen_positions and not invest_positions:
                lev_pos_chk = getattr(self.leverage_agent, "_position", None)
                if not lev_pos_chk:
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

            FEE_RATE = 0.00015  # 매수/매도 각각 0.015%

            def _net_pnl(entry_price, cur_price, qty, is_etf=False):
                """수수료·세금 반영 순손익 (매도 기준 추정)"""
                tax_rate = 0.0 if is_etf else 0.002  # ETF 세금없음, 주식 0.2%
                buy_fee  = entry_price * qty * FEE_RATE
                sell_fee = cur_price   * qty * FEE_RATE
                tax      = cur_price   * qty * tax_rate
                gross    = (cur_price - entry_price) * qty
                net      = gross - buy_fee - sell_fee - tax
                invest   = entry_price * qty + buy_fee
                rate     = net / invest if invest else 0.0
                return net, rate

            lines = [f"📂 <b>보유 포지션 [{now_str}]</b>"]
            total_unreal = 0

            # ── VM 포지션 ──────────────────────────────────────────────
            if vm_positions:
                lines.append(f"\n🤖 <b>VM 포지션 ({len(vm_positions)}개)</b>")
                vm_unreal = 0
                for pid, pos in vm_positions.items():
                    cur, flu = _get_price(pos.code, pos.entry_price)
                    if not cur:
                        cur = pos.entry_price
                    pnl_pos, rate_pos = _net_pnl(pos.entry_price, cur, pos.qty, is_etf=False)
                    vm_unreal += pnl_pos
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
                        f"    순손익(수수료+세금): {pnl_pos:+,.0f}원 ({rate_pos:+.2%})\n"
                        f"    수량: {pos.qty}주 | 평가금액: {int(cur * pos.qty):,}원\n"
                        f"    SL: {pos.stop_loss:,}원 | TP: {pos.take_profit:,}원"
                    )
                lines.append(f"\n  └ VM 순손익 합계: {vm_unreal:+,.0f}원")
                total_unreal += vm_unreal

            # ── 일반 포지션 ────────────────────────────────────────────
            if gen_positions:
                lines.append(f"\n📊 <b>일반 포지션 ({len(gen_positions)}개)</b>")
                gen_unreal = 0
                for code, pos in gen_positions.items():
                    cur, flu = _get_price(code, pos.entry_price)
                    if not cur:
                        cur = pos.entry_price
                    pnl_pos, rate_pos = _net_pnl(pos.entry_price, cur, pos.qty, is_etf=False)
                    gen_unreal += pnl_pos
                    emoji = "▲" if pnl_pos >= 0 else "▼"
                    flu_str = f" {flu:+.2f}%" if flu is not None else ""
                    lines.append(
                        f"\n  {emoji} <b>{pos.name} ({code})</b>\n"
                        f"    진입: {pos.entry_price:,}원 | 현재: {cur:,}원{flu_str}\n"
                        f"    순손익(수수료+세금): {pnl_pos:+,.0f}원 ({rate_pos:+.2%})\n"
                        f"    수량: {pos.qty}주 | 평가금액: {int(cur * pos.qty):,}원"
                    )
                lines.append(f"\n  └ 일반 순손익 합계: {gen_unreal:+,.0f}원")
                total_unreal += gen_unreal

            # ── 개별주 계획매매 포지션 ──────────────────────────────────
            if invest_positions:
                lines.append(f"\n📋 <b>개별주 계획매매 ({len(invest_positions)}개)</b>")
                invest_unreal = 0
                for code, pos in invest_positions.items():
                    cur, flu = _get_price(code, pos["entry_price"])
                    if not cur:
                        cur = pos["entry_price"]
                    pnl_pos, rate_pos = _net_pnl(pos["entry_price"], cur, pos["qty"], is_etf=False)
                    invest_unreal += pnl_pos
                    emoji = "▲" if pnl_pos >= 0 else "▼"
                    flu_str = f" {flu:+.2f}%" if flu is not None else ""
                    lines.append(
                        f"\n  {emoji} <b>{pos['name']} ({code})</b>\n"
                        f"    진입: {pos['entry_price']:,}원 | 현재: {cur:,}원{flu_str}\n"
                        f"    순손익(수수료+세금): {pnl_pos:+,.0f}원 ({rate_pos:+.2%})\n"
                        f"    수량: {pos['qty']}주 | 목표: {pos['target']:,}원 | 손절: {pos['stop']:,}원"
                    )
                lines.append(f"\n  └ 개별주 순손익 합계: {invest_unreal:+,.0f}원")
                total_unreal += invest_unreal

            invest_plans = self.personal_invest_agent.get_all_plans()
            if invest_plans:
                lines.append(f"\n⏳ <b>개별주 매수 대기 중 ({len(invest_plans)}개)</b>")
                for code, plan in invest_plans.items():
                    lines.append(
                        f"  - {plan['name']}({code}) 진입 {plan['entry_min']:,}~{plan['entry_max']:,}원"
                    )

            # ── 삼성전자 롱숏 전략 포지션 ─────────────────────────────
            lev_pos = getattr(self.leverage_agent, "_position", None)
            if lev_pos:
                etf_code  = lev_pos["code"]
                cur_lev, flu_lev = _get_price(etf_code, lev_pos["entry_price"])
                if not cur_lev:
                    cur_lev = lev_pos["entry_price"]
                pnl_lev, rate_lev = _net_pnl(
                    lev_pos["entry_price"], cur_lev, lev_pos["qty"], is_etf=True
                )
                total_unreal += pnl_lev
                emoji = "▲" if pnl_lev >= 0 else "▼"
                dir_label = "레버리지(롱)" if lev_pos["direction"] == "LEVERAGE" else "인버스(숏)"
                flu_str = f" {flu_lev:+.2f}%" if flu_lev is not None else ""
                lines.append(
                    f"\n⚡ <b>삼성전자 롱숏 전략</b>\n"
                    f"  {emoji} <b>{lev_pos['name']} ({etf_code})</b> [{dir_label}]\n"
                    f"    진입: {lev_pos['entry_price']:,}원 | 현재: {cur_lev:,}원{flu_str}\n"
                    f"    순손익(수수료): {pnl_lev:+,.0f}원 ({rate_lev:+.2%})\n"
                    f"    수량: {lev_pos['qty']}주 | SL: {lev_pos['stop_loss']:,}원 | TP: {lev_pos['take_profit']:,}원"
                )

            lines.append(f"\n💰 <b>순손익 총합(수수료·세금 반영): {total_unreal:+,.0f}원</b>")
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
                    _pend = self.vm_manager.get_pending_for_code(code)
                    if _pend:
                        _ps = sorted(_pend, key=lambda p: p.tranche)
                        _pp = [f"{p.tranche}차={p.price:,}" for p in _ps]
                        lines.append(f"   매수 범위: 실시간 재계산 ({' / '.join(_pp)}원)")
                    else:
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
            """3개 전략 토글/금액 조회·변경 (/strategy vm|breakout|lev on/off/amount N)"""
            parts = args.strip().split()

            def _flag(b): return "✅ ON" if b else "❌ OFF"

            if not parts:
                # ── 현재 설정 조회 ─────────────────────────────────────────
                vm_on   = config.VM_MODE and getattr(config, "VM_PICKS_ENABLED", True)
                bo_on   = getattr(config, "BREAKOUT_ENABLED", True)
                lev_on  = getattr(config, "LEVERAGE_ENABLED", True)

                vm_amt  = getattr(config, "VM_TRADE_AMOUNT", 1_000_000)
                bo_amt  = getattr(config, "BREAKOUT_TRADE_AMOUNT", 0)
                lev_amt = getattr(config, "LEVERAGE_AMOUNT", 500_000)

                stype   = getattr(config, "ENTRY_STRATEGY_TYPE", 3)
                sl_rate = getattr(config, "STOP_LOSS_RATE", 0.03)
                tp_rate = getattr(config, "TARGET_PROFIT_RATE", 0.07)
                cond    = config.CONDITION_NAME or config.CONDITION_SEQ or "없음"
                clear_t = getattr(config, "CLEAR_TIME", "15:20")

                bo_amt_str = (
                    f"{bo_amt:,}원 (고정)" if bo_amt > 0
                    else f"자본×{config.POSITION_RATIO:.0%} (비율)"
                )

                lines = [
                    "⚙️ <b>전략 설정 현황</b>\n",
                    "━━━━━━━━━━━━━━━━━━━━━━",
                    f"[1] 🤖 VM Picks 자동매매   {_flag(vm_on)}",
                    f"    매수금액: {vm_amt:,}원/종목",
                    f"    SL/TP: vm_picks.json 절대가 사용",
                    "",
                    f"[2] 📈 일반전략 (3분봉 돌파) {_flag(bo_on)}",
                    f"    매수금액: {bo_amt_str}",
                    f"    전략: {stype}번 ({_STRATEGY_NAMES.get(stype,'?')}) | 조건식: {cond}",
                    f"    손절: {sl_rate:.1%} | 익절: {tp_rate:.1%} | 청산: {clear_t}",
                    "",
                    f"[3] ⚡ 삼성전자 롱숏전략     {_flag(lev_on)}",
                    f"    매수금액: {lev_amt:,}원/회",
                    f"    전략: {getattr(config,'LEVERAGE_STRATEGY','BASIC')} "
                    f"({LEVERAGE_STRATEGY_NAMES.get(getattr(config,'LEVERAGE_STRATEGY','BASIC'), '연속봉+5MA+거래량')})",
                    f"    SL: {config.LEVERAGE_SL_RATE:.1%} | TP: {config.LEVERAGE_TP_RATE:.1%} "
                    f"| 강제청산: {config.LEVERAGE_FORCE_EXIT}",
                    "━━━━━━━━━━━━━━━━━━━━━━",
                    "\n📌 <b>변경 명령</b>",
                    "/strategy vm on|off — VM Picks 토글",
                    "/strategy breakout on|off — 돌파 전략 토글",
                    "/strategy lev on|off — 레버리지 전략 토글",
                    "/strategy vm amount 1000000 — VM 매수금액 변경",
                    "/strategy breakout amount 500000 — 돌파 매수금액 (0=비율)",
                    "/strategy lev amount 500000 — 레버리지 매수금액 변경",
                    "/strategy lev strategy basic|macd_ha — 레버리지 전략 변경",
                    "/strategy 1~3 — 돌파 진입 전략 번호 변경",
                    "/strategy condition #이름 — 조건식 변경",
                ]
                return "\n".join(lines)

            sub = parts[0].lower()

            # ── 전략 번호 변경 ─────────────────────────────────────────────
            if sub in ("1", "2", "3"):
                n_val = int(sub)
                config.ENTRY_STRATEGY_TYPE = n_val
                return f"✅ 진입 전략 → {n_val}번 ({_STRATEGY_NAMES[n_val]})"

            # ── vm 서브커맨드 ──────────────────────────────────────────────
            if sub == "vm" and len(parts) >= 2:
                cmd2 = parts[1].lower()
                if cmd2 == "on":
                    config.VM_PICKS_ENABLED = True
                    config.VM_MODE = True
                    return "✅ VM Picks 자동매매 ON"
                if cmd2 == "off":
                    config.VM_PICKS_ENABLED = False
                    return "✅ VM Picks 자동매매 OFF (기존 포지션 SL/TP는 계속 감시)"
                if cmd2 == "amount" and len(parts) >= 3:
                    try:
                        amt = int(parts[2].replace(",", ""))
                        config.VM_TRADE_AMOUNT = amt
                        _update_env_value("VM_TRADE_AMOUNT", str(amt))
                        return f"✅ VM 매수금액 → {amt:,}원 (.env 저장)"
                    except ValueError:
                        return "❓ 사용법: /strategy vm amount 1000000"
                return "❓ 사용법: /strategy vm on|off|amount <금액>"

            # ── breakout 서브커맨드 ────────────────────────────────────────
            if sub == "breakout" and len(parts) >= 2:
                cmd2 = parts[1].lower()
                if cmd2 == "on":
                    config.BREAKOUT_ENABLED = True
                    return "✅ 일반전략(돌파) ON"
                if cmd2 == "off":
                    config.BREAKOUT_ENABLED = False
                    return "✅ 일반전략(돌파) OFF (기존 포지션 청산 로직은 유지)"
                if cmd2 == "amount" and len(parts) >= 3:
                    try:
                        amt = int(parts[2].replace(",", ""))
                        config.BREAKOUT_TRADE_AMOUNT = amt
                        _update_env_value("BREAKOUT_TRADE_AMOUNT", str(amt))
                        amt_str = f"{amt:,}원 (고정)" if amt > 0 else f"자본×{config.POSITION_RATIO:.0%} (비율 복원)"
                        return f"✅ 돌파 매수금액 → {amt_str} (.env 저장)"
                    except ValueError:
                        return "❓ 사용법: /strategy breakout amount 500000 (0=비율)"
                return "❓ 사용법: /strategy breakout on|off|amount <금액>"

            # ── lev 서브커맨드 ─────────────────────────────────────────────
            if sub == "lev" and len(parts) >= 2:
                cmd2 = parts[1].lower()
                if cmd2 == "on":
                    config.LEVERAGE_ENABLED = True
                    return "✅ 삼성전자 롱숏전략 ON"
                if cmd2 == "off":
                    config.LEVERAGE_ENABLED = False
                    return "✅ 삼성전자 롱숏전략 OFF (보유 포지션은 SL/TP 계속 감시)"
                if cmd2 == "amount" and len(parts) >= 3:
                    try:
                        amt = int(parts[2].replace(",", ""))
                        config.LEVERAGE_AMOUNT = amt
                        _update_env_value("LEVERAGE_AMOUNT", str(amt))
                        return f"✅ 레버리지 매수금액 → {amt:,}원 (.env 저장)"
                    except ValueError:
                        return "❓ 사용법: /strategy lev amount 500000"
                if cmd2 == "strategy" and len(parts) >= 3:
                    chosen = parts[2].upper()
                    if chosen in ("BASIC", "MACD_HA", "TREND_MACD", "BOLLINGER"):
                        config.LEVERAGE_STRATEGY = chosen
                        _update_env_value("LEVERAGE_STRATEGY", chosen)
                        label = LEVERAGE_STRATEGY_NAMES.get(chosen, chosen)
                        return f"✅ 레버리지 전략 → {chosen} ({label}) (.env 저장)"
                    return "❓ 사용법: /strategy lev strategy basic|macd_ha|trend_macd|bollinger"
                return "❓ 사용법: /strategy lev on|off|amount <금액>|strategy basic|macd_ha|trend_macd|bollinger"

            # ── 조건식 변경 ────────────────────────────────────────────────
            if sub == "condition" and len(parts) >= 2:
                cond_name = " ".join(parts[1:])
                if not self._kiwoom_ready:
                    return "❌ Kiwoom 비활성 상태에서는 조건식 변경 불가"
                self._change_condition_runtime(cond_name)
                return f"🔄 조건식 변경 요청: {cond_name}"

            return (
                "❓ 사용법:\n"
                "/strategy — 현황 조회\n"
                "/strategy vm|breakout|lev on|off — 토글\n"
                "/strategy vm|breakout|lev amount <금액> — 금액 변경\n"
                "/strategy 1~3 — 진입 전략 번호\n"
                "/strategy condition #이름 — 조건식"
            )

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

                # 자동 계산된 buy_min/max (pending 있으면 실제 threshold 우선 사용)
                no_range = buy_min_raw is None or buy_max_raw is None
                if not no_range:
                    buy_min_eff = buy_min_raw or 0
                    buy_max_eff = buy_max_raw or 0
                    range_note  = f"{buy_min_eff:,}~{buy_max_eff:,}원"
                    _no_range_note = None
                else:
                    _pend_v = self.vm_manager.get_pending_for_code(code)
                    if _pend_v:
                        _ps_v = sorted(_pend_v, key=lambda p: p.tranche)
                        buy_min_eff = min(p.price for p in _ps_v)
                        buy_max_eff = max(p.price for p in _ps_v)
                        _pp_v = [f"{p.tranche}차={p.price:,}" for p in _ps_v]
                        range_note  = f"{buy_min_eff:,}~{buy_max_eff:,}원 (실시간 pending)"
                        _no_range_note = f"   ℹ️ 실시간 재계산: {' / '.join(_pp_v)}원"
                    elif cur_price > 0:
                        buy_min_eff = int(cur_price * 0.985)
                        buy_max_eff = int(cur_price * 1.000)
                        range_note  = f"{buy_min_eff:,}~{buy_max_eff:,}원 (자동: 기준가×-1.5%)"
                        _no_range_note = f"   ℹ️ 매수 범위 미지정 → 자동 계산 적용"
                    else:
                        buy_min_eff = buy_max_eff = 0
                        range_note  = "⚠️ 미지정 + 기준가 없음"
                        _no_range_note = f"   ⚠️ 매수 범위 미지정 + 기준가 없음"

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
                if no_range and _no_range_note:
                    lines.append(_no_range_note)
                for iss in issues:
                    lines.append(f"   ⚠️ {iss}")
                lines.append("")

            verdict = "✅ 검증 통과" if (date_ok and issues_total == 0) else f"⚠️ 이슈 {issues_total}건"
            lines.append(f"<b>결론: {verdict}</b>")
            return "\n".join(lines)

        def cmd_lev(args):
            """레버리지 전략 현황 + 삼성전자 신호 분석 / 강제청산"""
            parts = args.strip().split()
            lev_pos  = getattr(self.leverage_agent, "_position", None)
            exited   = getattr(self.leverage_agent, "_force_exited_today", False)
            now_str  = datetime.now().strftime("%H:%M:%S")
            ag       = self.leverage_agent

            # /lev exit — 강제 청산
            if parts and parts[0].lower() == "exit":
                if not lev_pos:
                    return "⚡ 레버리지 전략: 보유 포지션 없음"
                ctx = self.harness.get_context()
                ctx["harness"] = self.harness
                ag._exit("텔레그램강제청산", 0, ctx)
                return "✅ 레버리지 포지션 강제 청산 요청 완료"

            lines = [f"⚡ <b>삼성전자 롱숏 전략 [{now_str}]</b>"]
            lines.append(
                f"진입: {config.LEVERAGE_ENTRY_START}~{config.LEVERAGE_ENTRY_END} | "
                f"강제청산: {config.LEVERAGE_FORCE_EXIT}"
            )

            # ── 포지션 현황 ────────────────────────────────────────────
            if exited:
                lines.append("\n🔴 오늘 강제청산 완료 (재진입 없음)")
            elif not lev_pos:
                lines.append("\n🟡 포지션 없음 — 신호 대기 중")
            else:
                last_prices = self.harness.get_context().get("last_prices", {})
                cur = last_prices.get(lev_pos["code"], lev_pos["entry_price"])
                pnl_rate = (cur - lev_pos["entry_price"]) / lev_pos["entry_price"] if lev_pos["entry_price"] else 0
                pnl_amt  = (cur - lev_pos["entry_price"]) * lev_pos["qty"]
                emoji    = "▲" if pnl_amt >= 0 else "▼"
                dir_lbl  = "레버리지(롱)" if lev_pos["direction"] == "LEVERAGE" else "인버스(숏)"
                lines += [
                    f"\n{emoji} <b>{lev_pos['name']} ({lev_pos['code']})</b> [{dir_lbl}]",
                    f"수량: {lev_pos['qty']}주 | 진입: {lev_pos['entry_price']:,}원 | 현재: {cur:,}원",
                    f"평가손익: {pnl_amt:+,.0f}원 ({pnl_rate:+.2%})",
                    f"SL: {lev_pos['stop_loss']:,}원 | TP: {lev_pos['take_profit']:,}원",
                    f"진입시각: {lev_pos.get('entered_at', '?')[:19]}",
                ]

            # ── 삼성전자 신호 분석 ─────────────────────────────────────
            lev_strategy = getattr(config, "LEVERAGE_STRATEGY", "BASIC")
            strat_label  = LEVERAGE_STRATEGY_NAMES.get(lev_strategy, "연속봉+5MA+거래량")
            lines.append(f"\n📊 <b>삼성전자 신호 분석</b> [전략: {strat_label}]")
            try:
                market_data = self.harness.skills.get("market_data")
                # 장전 등 전략 초기화 전이면 온디맨드로 빌더 생성 (ka10080 전일봉 포함 150개)
                if market_data and not market_data.get_builder(ag.SAMSUNG_CODE) and self._kiwoom_ready:
                    lev_interval = getattr(config, "LEVERAGE_CANDLE_INTERVAL", 10)
                    market_data.init_stock(
                        ag.SAMSUNG_CODE,
                        on_candle_close=lambda c: self.harness.broadcast_event("CANDLE", c),
                        interval_min=lev_interval,
                        api_feed=True,
                    )
                candles = market_data.get_candles(ag.SAMSUNG_CODE) if market_data else []
                closed  = [c for c in candles if c.is_closed]

                sam_price = self.harness.get_context().get("last_prices", {}).get(ag.SAMSUNG_CODE, 0)
                price_str = f"{sam_price:,}원" if sam_price else "틱 없음"
                lines.append(f"삼성전자 현재가: {price_str} | 확정봉 수: {len(closed)}개")

                if lev_strategy == "MACD_HA":
                    # ── MACD + 하이킨아시 디스플레이 ─────────────────────
                    fast = getattr(config, "LEVERAGE_MACD_FAST",   12)
                    slow = getattr(config, "LEVERAGE_MACD_SLOW",   26)
                    sig  = getattr(config, "LEVERAGE_MACD_SIGNAL",  9)
                    min_c = slow + sig - 1
                    if len(closed) < min_c:
                        lines.append(f"캔들 부족 ({len(closed)}/{min_c}봉) — MACD 분석 불가")
                    else:
                        last = closed[-1]
                        lines.append(
                            f"최신봉[{last.datetime.strftime('%H:%M')}] "
                            f"O={last.open:,} H={last.high:,} L={last.low:,} C={last.close:,}"
                        )
                        # HA 계산
                        ha      = ag._calc_heikin_ashi(closed)
                        last_ha = ha[-1]
                        ha_bull = last_ha["close"] > last_ha["open"]
                        ha_bear = last_ha["close"] < last_ha["open"]
                        ha_dir  = "▲HA양봉" if ha_bull else ("▼HA음봉" if ha_bear else "━HA도지")
                        lines.append(
                            f"HA캔들: {ha_dir} "
                            f"(HA_O={last_ha['open']:,.0f} HA_C={last_ha['close']:,.0f})"
                        )
                        # MACD 계산
                        macd_val, signal_val = ag._calc_macd_values(
                            [c.close for c in closed], fast, slow, sig
                        )
                        if macd_val is not None:
                            macd_sym = "▲ MACD&gt;Signal ✅" if macd_val > signal_val else "▼ MACD&lt;Signal ❌"
                            lines.append(
                                f"MACD({fast}/{slow}/{sig}): {macd_val:+.2f} | "
                                f"Signal: {signal_val:+.2f} → {macd_sym}"
                            )
                            # 롱/숏 조건
                            long_conds  = [("MACD&gt;Signal", macd_val > signal_val), ("HA양봉", ha_bull)]
                            short_conds = [("MACD&lt;Signal", macd_val < signal_val), ("HA음봉", ha_bear)]
                            long_ok  = all(v for _, v in long_conds)
                            short_ok = all(v for _, v in short_conds)
                            lines.append(
                                "🟢 롱조건: " +
                                " | ".join(f"{'✅' if v else '❌'}{n}" for n, v in long_conds) +
                                f" → {'✅ 충족' if long_ok else '❌ 미충족'}"
                            )
                            lines.append(
                                "🔴 숏조건: " +
                                " | ".join(f"{'✅' if v else '❌'}{n}" for n, v in short_conds) +
                                f" → {'✅ 충족' if short_ok else '❌ 미충족'}"
                            )

                elif lev_strategy in ("BOLLINGER", "TREND_MACD"):
                    # ── 일봉추세 + 볼린저밴드돌파/MACD크로스 디스플레이 ──
                    fast_ma = getattr(config, "LEVERAGE_TREND_FAST_MA", 10)
                    slow_ma = getattr(config, "LEVERAGE_TREND_SLOW_MA", 20)
                    trend = ag._get_daily_trend(self.harness) if self._kiwoom_ready else None
                    trend_sym = "▲상승(롱만)" if trend == "UP" else ("▼하락(숏만)" if trend == "DOWN" else "❓판단불가")
                    lines.append(f"일봉추세({fast_ma}일/{slow_ma}일선): {trend_sym}")

                    if lev_strategy == "BOLLINGER":
                        period = getattr(config, "LEVERAGE_BB_PERIOD", 27)
                        mult   = getattr(config, "LEVERAGE_BB_MULT", 1.1)
                        min_c  = period + 1
                        if len(closed) < min_c:
                            lines.append(f"캔들 부족 ({len(closed)}/{min_c}봉) — 볼린저밴드 분석 불가")
                        else:
                            last = closed[-1]
                            closes = [c.close for c in closed]
                            mid, upper, lower = ag._calc_bollinger_values(closes, period, mult)
                            lines.append(
                                f"최신봉[{last.datetime.strftime('%H:%M')}] 종가: {last.close:,}원"
                            )
                            lines.append(
                                f"볼린저({period}/{mult}): 중심{mid:,.0f} 상단{upper:,.0f} 하단{lower:,.0f}"
                            )
                            long_ok  = trend == "UP" and last.close > upper
                            short_ok = trend == "DOWN" and last.close < lower
                            lines.append(f"🟢 롱조건(상승추세+상단돌파): {'✅ 충족' if long_ok else '❌ 미충족'}")
                            lines.append(f"🔴 숏조건(하락추세+하단돌파): {'✅ 충족' if short_ok else '❌ 미충족'}")
                    else:
                        fast = getattr(config, "LEVERAGE_MACD_FAST",   5)
                        slow = getattr(config, "LEVERAGE_MACD_SLOW",   13)
                        sig  = getattr(config, "LEVERAGE_MACD_SIGNAL", 6)
                        min_c = slow + sig
                        if len(closed) < min_c:
                            lines.append(f"캔들 부족 ({len(closed)}/{min_c}봉) — MACD 분석 불가")
                        else:
                            last = closed[-1]
                            macd_val, signal_val = ag._calc_macd_values(
                                [c.close for c in closed], fast, slow, sig
                            )
                            lines.append(f"최신봉[{last.datetime.strftime('%H:%M')}] 종가: {last.close:,}원")
                            if macd_val is not None:
                                macd_sym = "▲ MACD&gt;Signal" if macd_val > signal_val else "▼ MACD&lt;Signal"
                                lines.append(
                                    f"MACD({fast}/{slow}/{sig}): {macd_val:+.2f} | "
                                    f"Signal: {signal_val:+.2f} → {macd_sym}"
                                )

                else:
                    # ── 기본전략 디스플레이 (연속봉 + 5MA + 거래량) ──────
                    if len(closed) >= ag.SIGNAL_MA_PERIOD:
                        ma   = sum(c.close for c in closed[-ag.SIGNAL_MA_PERIOD:]) / ag.SIGNAL_MA_PERIOD
                        last = closed[-1]

                        ma_ok_long  = last.close > ma
                        ma_ok_short = last.close < ma
                        ma_sym = "▲" if ma_ok_long else ("▼" if ma_ok_short else "=")
                        lines.append(
                            f"5MA: {ma:,.0f}원 | 최신봉[{last.datetime.strftime('%H:%M')}] "
                            f"종가: {last.close:,}원 → {ma_sym} "
                            f"{'MA 위' if ma_ok_long else 'MA 아래' if ma_ok_short else 'MA 동일'}"
                        )

                        sample    = closed[-11:-1] if len(closed) >= 12 else closed[:-1]
                        avg_vol   = sum(c.volume for c in sample) / len(sample) if sample else 0
                        threshold = avg_vol * ag.SIGNAL_VOL_RATIO
                        vol_ok    = last.volume >= threshold
                        vol_sym   = "✅" if vol_ok else "❌"
                        lines.append(
                            f"거래량: {last.volume:,} vs 평균{avg_vol:,.0f}×{ag.SIGNAL_VOL_RATIO} "
                            f"= {threshold:,.0f} {vol_sym}"
                        )

                        if len(closed) >= ag.SIGNAL_CONSEC:
                            recent = closed[-ag.SIGNAL_CONSEC:]
                            consec_strs = [
                                f"{'▲양봉' if c.is_bullish else '▼음봉'}[{c.datetime.strftime('%H:%M')}] "
                                f"O={c.open:,} C={c.close:,} V={c.volume:,}"
                                for c in recent
                            ]
                            all_bull = all(c.is_bullish for c in recent)
                            all_bear = all(c.close < c.open for c in recent)
                            lines.append("최근 2봉:")
                            for s in consec_strs:
                                lines.append(f"  {s}")

                            long_conds = [
                                ("MA 위(롱)", ma_ok_long),
                                ("연속양봉",  all_bull),
                                ("거래량",    vol_ok),
                            ]
                            long_ok  = all(v for _, v in long_conds)
                            lines.append(
                                "🟢 롱조건: " +
                                " | ".join(f"{'✅' if v else '❌'}{n}" for n, v in long_conds) +
                                f" → {'✅ 충족' if long_ok else '❌ 미충족'}"
                            )
                            short_conds = [
                                ("MA 아래(숏)", ma_ok_short),
                                ("연속음봉",    all_bear),
                                ("거래량",      vol_ok),
                            ]
                            short_ok  = all(v for _, v in short_conds)
                            lines.append(
                                "🔴 숏조건: " +
                                " | ".join(f"{'✅' if v else '❌'}{n}" for n, v in short_conds) +
                                f" → {'✅ 충족' if short_ok else '❌ 미충족'}"
                            )
                        else:
                            lines.append("최근 2봉: 데이터 부족")
                    else:
                        lines.append(f"캔들 부족 ({len(closed)}/{ag.SIGNAL_MA_PERIOD}봉) — 분석 불가")

                # ── 종합 신호 (전략 공통) ─────────────────────────────
                signal  = ag._calc_signal(candles)
                now_hm  = datetime.now().strftime("%H:%M")
                in_time = config.LEVERAGE_ENTRY_START <= now_hm <= config.LEVERAGE_ENTRY_END
                if signal == "LEVERAGE":
                    sig_str = "🟢 레버리지(롱) 진입 가능"
                elif signal == "INVERSE":
                    sig_str = "🔴 인버스(숏) 진입 가능"
                else:
                    sig_str = "⚪ 신호 없음"
                time_str = "" if in_time else f" (진입시간 외: {now_hm})"
                lines.append(f"→ <b>신호: {sig_str}{time_str}</b>")
            except Exception as e:
                lines.append(f"⚠️ 신호 분석 실패: {e}")

            if not exited:
                lines.append("\n<i>/lev exit — 강제 청산</i>")
            return "\n".join(lines)

        def cmd_invest(_args):
            """개별주 계획매매 등록/조회/취소
            등록: /invest 종목코드 entry=최소-최대 target=목표가 stop=손절가 [amount=금액]
                  entry에 단일값만 주면 ±0.3% 범위로 자동 설정
            조회: /invest list
            취소: /invest cancel 종목코드
            """
            pia = self.personal_invest_agent
            usage = (
                "❓ 사용법:\n"
                "/invest 종목코드 entry=최소-최대 target=목표가 stop=손절가 [amount=금액]\n"
                "예: /invest 005930 entry=80000-82000 target=90000 stop=76000\n"
                "/invest list — 현황 조회\n"
                "/invest cancel 종목코드 — 대기 계획 취소"
            )
            args = (_args or "").strip()
            if not args:
                return usage

            parts = args.split()
            sub = parts[0].lower()

            if sub == "list":
                if not pia._plans and not pia._positions:
                    return "📋 등록된 개별주 계획/포지션 없음"
                lines = ["📋 <b>개별주 계획매매 현황</b>"]
                if pia._plans:
                    lines.append(f"\n⏳ <b>대기 중 계획 ({len(pia._plans)})</b>")
                    for code, p in pia._plans.items():
                        lines.append(
                            f"{p['name']}({code}) 진입 {p['entry_min']:,.0f}~{p['entry_max']:,.0f} | "
                            f"목표 {p['target']:,.0f} | 손절 {p['stop']:,.0f} | R:R {p['risk_reward']}"
                        )
                if pia._positions:
                    lines.append(f"\n📂 <b>보유 중 ({len(pia._positions)})</b>")
                    for code, p in pia._positions.items():
                        lines.append(
                            f"{p['name']}({code}) {p['qty']}주 @ {p['entry_price']:,.0f} | "
                            f"목표 {p['target']:,.0f} | 손절 {p['stop']:,.0f}"
                        )
                return "\n".join(lines)

            if sub == "cancel":
                if len(parts) < 2:
                    return "❓ 사용법: /invest cancel 종목코드"
                code = parts[1]
                ok = pia.cancel_plan(code)
                return f"✅ {code} 계획 취소 완료" if ok else f"❌ {code} 대기 중인 계획 없음"

            # 신규 등록
            code = parts[0]
            if not code.isdigit() or len(code) != 6:
                return usage

            kv = {}
            for token in parts[1:]:
                if "=" not in token:
                    continue
                k, v = token.split("=", 1)
                kv[k.strip().lower()] = v.strip().replace(",", "")

            if "entry" not in kv or "target" not in kv or "stop" not in kv:
                return usage

            try:
                entry_raw = kv["entry"]
                if "-" in entry_raw:
                    e_min_s, e_max_s = entry_raw.split("-", 1)
                    entry_min, entry_max = float(e_min_s), float(e_max_s)
                else:
                    base = float(entry_raw)
                    entry_min, entry_max = base * 0.997, base * 1.003
                target = float(kv["target"])
                stop   = float(kv["stop"])
                amount = int(float(kv.get("amount", config.PERSONAL_INVEST_AMOUNT)))
            except ValueError:
                return "❓ 숫자 형식이 올바르지 않습니다.\n" + usage

            try:
                info = self.kiwoom.get_stock_info(code)
                name = info.get("name") or code
            except Exception:
                name = code

            result = pia.register_plan(code, name, entry_min, entry_max, target, stop, amount)
            if not result["ok"]:
                return f"❌ {result['msg']}"

            p = result["plan"]
            return (
                f"✅ <b>개별주 계획 등록</b> [{name}({code})]\n"
                f"진입: {entry_min:,.0f}~{entry_max:,.0f}원 | 목표: {target:,.0f}원 | 손절: {stop:,.0f}원\n"
                f"예정수량: {p['qty_planned']}주 (투자금 {amount:,}원) | 손익비(R:R) {p['risk_reward']}\n"
                f"진입가 범위에 들어오면 자동 매수, 목표가/손절가 도달 시 자동 청산됩니다."
            )

        def cmd_movers(_args):
            """등락률 상위 N + 거래량급증(절대수량) 상위 N 조회, 겹치는 종목 구분
            사용법: /movers [N] (기본 N=50)
            """
            try:
                limit = int(_args.strip()) if _args and _args.strip() else 50
            except ValueError:
                return "❓ 사용법: /movers [조회개수] (기본 50)"

            movers = self.kiwoom.get_top_change_rate(limit=limit)
            surges = self.kiwoom.get_volume_surge(limit=limit)  # 기본 qty(급증수량) 기준
            if not movers and not surges:
                return "❌ 조회 실패 (API 응답 없음)"

            result = classify_movers_overlap(movers, surges)
            overlap, movers_only, surge_only = (
                result["overlap"], result["movers_only"], result["surge_only"]
            )

            # 텔레그램 메시지 길이(4096자) 보호용 섹션별 표시 상한 — N이 비정상적으로
            # 커도(/movers 500 등) 메시지가 깨지지 않도록 별도 상한을 둠(조회 자체는 N 그대로)
            DISPLAY_CAP = 60

            def _fmt_section(items, with_surge_qty):
                shown = items[:DISPLAY_CAP]
                out = []
                for r in shown:
                    if with_surge_qty:
                        out.append(f"{r['name']} {r['flu_rt']:+.1f}% | 급증수량 {r.get('surge_qty', 0):+,}주")
                    else:
                        out.append(f"{r['name']} {r['flu_rt']:+.1f}%")
                if len(items) > DISPLAY_CAP:
                    out.append(f"...외 {len(items) - DISPLAY_CAP}건 생략")
                return out

            now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
            lines = [
                f"📊 <b>등락률·거래량급증 상위 {limit}개 비교</b>",
                f"조회: {now_str}",
                f"겹침 {len(overlap)} / 등락률만 {len(movers_only)} / 거래량만 {len(surge_only)}",
            ]

            if overlap:
                lines.append(f"\n🔥 <b>겹치는 종목 ({len(overlap)})</b>")
                lines += _fmt_section(
                    sorted(overlap, key=lambda x: x["flu_rt"], reverse=True), with_surge_qty=True
                )

            if movers_only:
                lines.append(f"\n📈 <b>등락률만 상위 ({len(movers_only)})</b>")
                lines += _fmt_section(movers_only, with_surge_qty=False)

            if surge_only:
                lines.append(f"\n💹 <b>거래량만 급증 ({len(surge_only)})</b>")
                lines += _fmt_section(surge_only, with_surge_qty=True)

            return "\n".join(lines)

        def cmd_apiusage(_args):
            """키움 REST 일일 호출량(1,700회 한도) 조회"""
            usage = self.kiwoom.get_api_usage()
            return (
                f"📡 <b>API 호출량 ({usage['date']})</b>\n"
                f"{usage['count']} / {usage['limit']}회 사용 ({usage['pct']}%)\n"
                f"남은 호출: {usage['remaining']}회"
            )

        def cmd_help(_args):
            return (
                "📖 <b>사용 가능한 명령</b>\n"
                "/ping — 봇 응답 확인\n"
                "/status — 오늘 손익·대기 현황\n"
                "/pos — 보유 포지션 상세 (장 외 시간 가능)\n"
                "/lev — 레버리지 전략 현황 (/lev exit 강제청산)\n"
                "/validate — vm_picks.json 품질 검증\n"
                "/picks — VM picks 종목·가격 현황\n"
                "/strategy — 3전략 토글·금액 조회/변경\n"
                "/movers [N] — 등락률·거래량급증 상위 N개 비교 (기본 50)\n"
                "/invest 코드 entry=.. target=.. stop=.. — 개별주 계획매매 등록\n"
                "/invest list|cancel 코드 — 계획 조회/취소\n"
                "/apilimit — 오늘 API 호출량 조회 (1,700회 한도 중 사용량)\n"
                "/sell — 전량 강제 청산\n"
                "/reload — vm_picks.json 강제 재로드\n"
                "/log — 최근 로그 15줄\n"
                "/stop — 봇 종료\n"
                "/help — 이 도움말"
            )

        n.register_command("ping",     cmd_ping)
        n.register_command("status",   cmd_status)
        n.register_command("pos",      cmd_pos)
        n.register_command("lev",      cmd_lev)
        n.register_command("picks",    cmd_picks)
        n.register_command("validate", cmd_validate)
        n.register_command("strategy", cmd_strategy)
        n.register_command("movers",   cmd_movers)
        n.register_command("invest",   cmd_invest)
        n.register_command("apilimit", cmd_apiusage)
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

        # 계좌 잔고 조회
        acnt_line = None
        try:
            if self._kiwoom_ready:
                balance = self.kiwoom.get_balance()
                total = balance.get("total", 0)
                available = balance.get("available", 0)
                initial = self.risk._initial_capital
                if config.IS_SIMULATION:
                    acnt_line = f"💰 예수금: {total:,}원 | 주문가능: {available:,}원"
                else:
                    acnt_rate = (total - initial) / initial if initial > 0 and total > 0 else 0.0
                    acnt_line = f"💰 총평가: {total:,}원 ({acnt_rate:+.2%}) | 주문가능: {available:,}원"
        except Exception:
            pass

        lines = [
            f"📊 <b>현황 [{datetime.now().strftime('%H:%M:%S')}]</b>",
            f"Kiwoom: {kiwoom_str}",
        ]
        if acnt_line:
            lines.append(acnt_line)
        lines += [
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
        # 레버리지 전략 요약
        lev_pos = getattr(self.leverage_agent, "_position", None)
        if lev_pos:
            cur_lev = last_prices.get(lev_pos["code"], lev_pos["entry_price"])
            rate_lev = (cur_lev - lev_pos["entry_price"]) / lev_pos["entry_price"] if lev_pos["entry_price"] else 0
            dir_lbl  = "롱" if lev_pos["direction"] == "LEVERAGE" else "숏"
            lines.append(
                f"\n⚡ <b>레버리지</b> {lev_pos['name']}({dir_lbl}) "
                f"{lev_pos['qty']}주 {rate_lev:+.2%} | /lev 상세"
            )
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

    def _check_pending_fills(self):
        """틱 누락 대비 60s 폴백 — 미체결 pending 주문에 대해 ka10001 현재가 조회 후 fill 추론."""
        pending_codes = {
            p.code for p in self.vm_manager._pending.values() if not p.filled
        }
        for code in pending_codes:
            try:
                info = self.kiwoom.get_stock_info(code, fast=True)
            except Exception:
                continue
            if not info or info.get("current_price", 0) <= 0:
                continue
            current_price = int(info["current_price"])
            for pending in self.vm_manager.get_pending_for_code(code):
                if current_price <= pending.price:
                    filled = self.vm_manager.on_fill_detected(pending.ord_no, current_price)
                    if filled:
                        logger.info(
                            f"[Bot] 폴링 fill 추론: {pending.name}({code}) "
                            f"{pending.tranche}차 @ {current_price:,}원"
                        )
                        self.trade_logger.log_trade(
                            code=code, name=pending.name, side="BUY",
                            qty=pending.qty, price=current_price,
                            reason=f"VM지정분할매수{pending.tranche}차(폴백)"
                        )
                        if self.notifier:
                            self.notifier.send(
                                f"🟢 <b>VM 분할매수 {pending.tranche}차 체결</b> [{pending.name}({code})]\n"
                                f"{pending.qty}주 @ {current_price:,}원 "
                                f"({'2/3 지점' if pending.tranche == 1 else '1/3 지점'})\n"
                                f"손절가: {pending.stop_loss:,}원 | 목표가: {pending.take_profit:,}원"
                            )

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

    def _telegram_idle_wait(self, until: datetime):
        """비거래일(주말/공휴일) 대기 — 텔레그램 명령 응답만 유지하고 매매 로직은 돌리지 않음.

        기존에는 비거래일 전체를 순수 time.sleep()으로 보내 Notifier를 아예 생성하지
        않았고, 그 결과 주말에는 /help·/pos 등 모든 명령이 응답하지 않았다
        (실측 2026-10-04 00:11 — Oct3 토요일 진입 직후부터 Oct4 08:50 재확인까지
        완전 무응답 상태였음).
        """
        self.notifier.start_polling()
        logger.info(f"[Bot] 비거래일 텔레그램 대기 모드 (다음 거래일 확인: {until.strftime('%Y-%m-%d %H:%M')})")
        while datetime.now() < until:
            self._stop_event.wait(timeout=60)
            self.notifier.ensure_polling()
        self.notifier.stop()

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

        for p in picks_list:
            name = p.get("name", p.get("code", "?"))
            if not p.get("stop_loss"):
                issues.append(f"⚠️ {name}: 손절가 없음")
            if not p.get("take_profit_1"):
                issues.append(f"⚠️ {name}: 목표가 없음")

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
                # 비거래일에도 텔레그램 명령(/help, /pos 등)은 응답 가능하도록 유지
                idle_bot = TradingBot(auto_mode=True)
                idle_bot._telegram_idle_wait(next_check)
                continue

            bot = TradingBot(auto_mode=True)
            bot.start()

            # 텔레그램 폴링 스레드 명시적 종료 — 다음 날 TradingBot()이 새 폴링을
            # 시작하기 전에 멈추지 않으면 두 스레드가 같은 봇 토큰으로 getUpdates를
            # 호출해 "Conflict: terminated by other getUpdates request"가 발생하고
            # 텔레그램 명령 수신이 전부 불안정해진다.
            bot.notifier.stop()

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
