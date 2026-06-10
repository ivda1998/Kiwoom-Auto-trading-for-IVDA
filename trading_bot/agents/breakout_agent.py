import logging
from datetime import datetime
import time
import sys

import config
from agents.base_agent import BaseAgent
from strategy import BreakoutStrategy, SignalType
from logger import TradeLogger

logger = logging.getLogger(__name__)

class BreakoutTradingAgent(BaseAgent):
    """
    돌파 매매 전략을 감지하고 판단하여 주문(Skill)을 실행하는 에이전트.
    """
    def __init__(self):
        super().__init__()
        self.strategy = BreakoutStrategy()
        self.trade_logger = TradeLogger()
        self._last_out_of_hours_log = {}

    def analyze_and_act(self, event: dict, context: dict):
        event_type = event["type"]
        event_data = event["data"]
        harness = context["harness"]
        
        # 스킬 취득
        execution = harness.skills.get("execution")
        market_data = harness.skills.get("market_data")
        risk = context.get("risk")
        
        if event_type == "TICK":
            self._handle_tick(event_data, harness, execution, market_data, risk)
        elif event_type == "CANDLE":
            self._handle_candle(event_data, harness, execution, market_data, risk)
        elif event_type == "CONDITION":
            self._handle_condition(event_data, harness, market_data)

    def _handle_tick(self, tick: dict, harness, execution, market_data, risk):
        code = tick["code"]
        price = tick["price"]

        # 틱 상태 업데이트 (컨텍스트는 TradingBot.__init__에서 미리 초기화됨)
        ctx = harness.get_context()
        last_prices = ctx.get("last_prices", {})
        last_prices[code] = price

        volume = tick.get("volume", 0)
        today_amount = ctx.get("today_amount", {})
        today_amount[code] = today_amount.get(code, 0) + price * volume

        today_volume = ctx.get("today_volume", {})
        today_volume[code] = today_volume.get(code, 0) + volume

        # ★ VM picks 조기 라우팅 (strategy/execution 완전 우회)
        # 조건 1: 오늘 vm_picks에 있는 종목 (매수 대기 + SL/TP)
        # 조건 2: 오늘 vm_picks에 없더라도 VM 포지션 보유 중인 종목 (오버나잇 SL/TP 감시)
        vm_manager  = harness.get_context().get("vm_manager")
        custom_conf = config.get_custom_targets().get(code)
        has_vm_pos  = bool(vm_manager and vm_manager.get_by_code(code))
        if vm_manager and (custom_conf or has_vm_pos):
            self._handle_vm_tick(code, price, harness, vm_manager, custom_conf or {})
            return  # 일반 전략 로직 건너뜀

        # 포지션 보유 중인 종목: 실시간 청산 체크
        if execution.has_position(code):
            candles = market_data.get_candles(code)
            builder = market_data.get_builder(code)
            if builder:
                current_candle = builder.get_current_candle()
                if current_candle:
                    signal = self.strategy.on_candle_update(current_candle, candles, price)
                    if signal in (SignalType.SELL_PROFIT, SignalType.SELL_STOP, SignalType.SELL_TARGET, SignalType.SELL_TRAILING):
                        self._execute_sell(code, price, signal, harness, execution)
            return

        # 매수 진입 체크 (대기 중 종목만)
        if not self._can_buy_time() or (risk and risk.is_halted):
            if not self._can_buy_time():
                last_t = self._last_out_of_hours_log.get(code, 0)
                now_ts = time.time()
                if now_ts - last_t >= 60:
                    state_chk = self.strategy.get_state(code)
                    if state_chk and state_chk.phase == "WATCHING":
                        logger.debug(f"[Agent] {code} 시간 외 → 매수 차단 (거래 시작: {config.TRADE_START_TIME})")
                        self._last_out_of_hours_log[code] = now_ts
            return

        state = self.strategy.get_state(code)
        if not (state and state.phase == "WATCHING"):
            return

        # 1번 전략: 20/60일 신고가 ±NEAR_HIGH_BUY_THRESHOLD 이내 즉시 매수
        if getattr(config, "ENTRY_STRATEGY_TYPE", 1) == 1:
            for high in [state.high_20, state.high_60]:
                if high > 0 and abs(price - high) / high <= config.NEAR_HIGH_BUY_THRESHOLD:
                    state.phase = "ENTERING"
                    logger.info(f"[Agent] {code} 실시간 진입 시도: {price:,}원 / 기준고점 {high:,}원 ({abs(price - high) / high:.2%})")
                    self._execute_buy_at(code, price, harness, execution)
                    break

    def _handle_candle(self, candle, harness, execution, market_data, risk):
        code = candle.code

        # 매매 시간 체크
        if not self._in_trade_hours():
            return

        # 리스크 체크
        if risk and risk.is_halted:
            return

        # 이미 포지션 보유 중이면 청산 로직만
        if execution.has_position(code):
            return

        # 전략 신호 판단
        candles = market_data.get_candles(code)
        signal = self.strategy.on_candle_close(candle, candles)

        if signal == SignalType.BUY:
            if self._can_buy_time():
                self._execute_buy_at(code, candle.close, harness, execution)
            else:
                logger.debug(f"[Agent] 매수 신호 발생했으나 매수 제한 시간 경과로 진입 보류")
        elif signal == SignalType.TIMEOUT:
            self._remove_candidate(code, harness, execution)

    def _handle_condition(self, cond_event: dict, harness, market_data):
        """실시간 조건검색 편입/이탈 감지"""
        code = cond_event["code"]
        name = cond_event["name"]
        action = cond_event["action"]

        if action == "IN":
            logger.info(f"[Agent] 조건식 신규 편입: {name}({code})")

            # 이미 전략에 등록된 종목 스킵
            if self.strategy.get_state(code) is not None:
                return

            candidates = harness.get_context().setdefault("candidates", [])
            if len(candidates) >= config.MAX_CANDIDATES:
                logger.debug(f"[Agent] 최대 후보 수 초과로 {code} 스킵")
                return

            # 일봉 세부 검증 (조건식이 이미 필터링했으므로 추가 로컬필터링 제외)
            scanner = harness.get_context().get("scanner")
            if not scanner:
                logger.warning("[Agent] Scanner가 컨텍스트에 없습니다.")
                return

            result = scanner._evaluate_stock(code, name, check_filters=False)
            if not result:
                logger.debug(f"[Agent] {code} 일봉 데이터 수집 실패 → 등록 취소")
                return

            # 전략 및 데이터 등록
            candidates.append(result)
            self.strategy.init_stock(code, name=name, high_20=result["high_20"], high_60=result["high_60"])
            
            # 분봉 데이터 초기화
            market_data.init_stock(
                code,
                on_candle_close=lambda c, _code=code: harness.broadcast_event("CANDLE", c)
            )
            
            # 실시간 틱 구독 (모의투자 제외)
            if not config.IS_SIMULATION:
                harness.kiwoom.subscribe_realtime(code)

            str_type = getattr(config, "ENTRY_STRATEGY_TYPE", 1)
            ref_label = "전일종가" if str_type == 2 else "20일고점"
            logger.info(f"[Agent] 장 중 신규 등록 완료: {name}({code}) | {ref_label}근접({result['dist_20']:.1%})")

            # 콘솔에 상세 정보 출력
            est_keep_rate = 1 - config.STOP_LOSS_RATE
            threshold_pct = config.NEAR_HIGH_BUY_THRESHOLD * 100
            print(f"\n  ✅ 신규 후보 등록: [{name}({code})]")
            if str_type == 2:
                print(f"     현재가: {result['current_price']:,}원 | 전일종가: {result['high_20']:,}원")
                print(f"     전일종가 근접도: {result['dist_20']:.1%} | 거래대금: {result['avg_amount']/1e8:.0f}억원/일")
            else:
                print(f"     현재가: {result['current_price']:,}원 | 20일고점: {result['high_20']:,}원 | 60일고점: {result.get('high_60', 0):,}원")
                print(f"     고점 근접도(20일): {result['dist_20']:.1%} | 거래대금: {result['avg_amount']/1e8:.0f}억원/일")
            
            custom_targets = config.get_custom_targets()
            custom_conf = custom_targets.get(name) or custom_targets.get(code)
            if custom_conf:
                buy_min = custom_conf.get("buy_min", 0)
                buy_max = custom_conf.get("buy_max", 0)
                sl = custom_conf.get("stop_loss", -1)
                tp = custom_conf.get("take_profit", -1)
                buy_str = f"{buy_min:,} ~ {buy_max:,}원 구간 매수" if buy_max > 0 else "지정 범위 없음"
                sl_str = f"{sl:,}원" if sl > 0 else "미지정"
                tp_str = f"{tp:,}원" if tp > 0 else "미지정"
                
                print(f"     [수동] 매수 조건: {buy_str}")
                print(f"     [수동] 예상 손절가: {sl_str} / 목표가: {tp_str}")
            else:
                if str_type == 2:
                    print(f"     매수 조건: 전일종가 돌파 시 다음 봉 시가 매수")
                else:
                    print(f"     매수 조건: 20일/60일 신고가 ±{threshold_pct:.0f}% 이내 즉시 매수")
                print(f"     예상 손절가: 진입가 × {est_keep_rate:.0%} (진입 후 -{config.STOP_LOSS_RATE:.0%})")
                print(f"     대기 제한: {config.WATCHING_TIMEOUT_CANDLES}봉 ({config.WATCHING_TIMEOUT_CANDLES * config.CANDLE_INTERVAL}분)")
            
            print_status_board = harness.get_context().get("print_status_board")
            if print_status_board:
                print_status_board()
        else:
            logger.debug(f"[Agent] 조건식 이탈: {name}({code})")

    def _execute_buy_at(self, code: str, price: int, harness, execution):
        state = self.strategy.get_state(code)
        if not state:
            return

        candidates = harness.get_context().get("candidates", [])
        cand = next((c for c in candidates if c["code"] == code), None)
        name = cand["name"] if cand else code
        stoploss = round(price * (1 - config.STOP_LOSS_RATE))

        # 하네스의 execute_action을 통해 매수 실행 (훅 검증을 거침)
        success = harness.execute_action(self, "execution", "BUY", code, name, price, stoploss)

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
            pos = execution.get_position(code)
            if pos:
                self.trade_logger.log_trade(
                    code=code, name=name, side="BUY",
                    qty=pos.qty,
                    price=price,
                    reason=buy_reason
                )
                notifier = harness.get_context().get("notifier")
                if notifier:
                    notifier.send(
                        f"🟢 <b>매수 체결</b> [{name}({code})]\n"
                        f"{pos.qty}주 @ {price:,}원\n"
                        f"손절가: {pos.stoploss_price:,}원 | 사유: {buy_reason}"
                    )
                if is_custom:
                    custom_conf = custom_targets.get(name) or custom_targets.get(code)
                    sl = custom_conf.get("stop_loss", -1)
                    tp = custom_conf.get("take_profit", -1)
                    sl_str = f"{sl:,}원" if sl > 0 else "미지정"
                    tp_str = f"{tp:,}원" if tp > 0 else "미지정"
                    
                    print(f"\n  🟢 수동 매수 체결: [{name}({code})] "
                          f"{pos.qty}주 @ {price:,}원 | "
                          f"손절가: {sl_str} | "
                          f"익절: {tp_str}\n")
                else:
                    print(f"\n  🟢 매수 체결: [{name}({code})] "
                          f"{pos.qty}주 @ {price:,}원 | "
                          f"손절가: {pos.stoploss_price:,}원 | "
                          f"익절: 5MA 음전환\n")
            print_status_board = harness.get_context().get("print_status_board")
            if print_status_board:
                print_status_board()
        else:
            # 매수 실패 시 WATCHING 상태로 롤백
            state.phase = "WATCHING"

    def _execute_sell(self, code: str, current_price: float, signal: SignalType, harness, execution):
        pos = execution.get_position(code)
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

        # 하네스의 execute_action을 통해 매도 실행
        success = harness.execute_action(self, "execution", "SELL", code, current_price, reason)
        if success:
            self.trade_logger.log_trade(
                code=code, name=pos.name, side="SELL",
                qty=pos.qty, price=current_price,
                pnl=pnl, pnl_rate=pnl_rate, reason=reason
            )
            self.strategy.reset_stock(code)
            notifier = harness.get_context().get("notifier")
            if notifier:
                sell_emoji = "🔴" if signal == SignalType.SELL_STOP else "🟢"
                notifier.send(
                    f"{sell_emoji} <b>매도 체결</b> [{pos.name}({code})]\n"
                    f"{pos.qty}주 @ {current_price:,}원\n"
                    f"손익: {pnl:+,.0f}원 ({pnl_rate:+.2%}) | 사유: {reason}"
                )

            if signal == SignalType.SELL_STOP:
                emoji = "🔴"
            elif signal in (SignalType.SELL_PROFIT, SignalType.SELL_TARGET):
                emoji = "🟢"
            else:
                emoji = "🟡"
                
            print(f"\n  {emoji} 매도 체결: [{pos.name}({code})] "
                  f"진입가: {pos.entry_price:,}원 → 매도가: {current_price:,}원 | "
                  f"손익: {pnl:+,.0f}원 ({pnl_rate:+.2%}) | 사유: {reason}\n")
            print_status_board = harness.get_context().get("print_status_board")
            if print_status_board:
                print_status_board()

    def _remove_candidate(self, code: str, harness, execution):
        """대기 타임아웃 발생 시 후보 목록에서 종목 삭제 및 구독 해제"""
        candidates = harness.get_context().get("candidates", [])
        cand = next((c for c in candidates if c["code"] == code), None)
        name = cand["name"] if cand else code
        logger.info(f"[Agent] 대기 타임아웃 → 후보 제거: {name}({code})")
        print(f"\n  ⏰ [{name}({code})] 대기 타임아웃 ({config.WATCHING_TIMEOUT_CANDLES}봉 경과) → 후보 목록에서 제거\n")
        
        # 목록 필터링 후 덮어쓰기
        harness.get_context()["candidates"] = [c for c in candidates if c["code"] != code]
        
        harness.kiwoom.unsubscribe_realtime(code)
        self.strategy.remove_stock(code)
        
        print_status_board = harness.get_context().get("print_status_board")
        if print_status_board:
            print_status_board()

    def _handle_vm_tick(self, code: str, price: float, harness, vm_manager, custom_conf: dict):
        """VM picks 전용 틱 처리: 실시간 SL/TP 청산 + 범위 진입."""
        notifier = harness.get_context().get("notifier")

        # 1. 열린 포지션 SL/TP 체크
        for pos in list(vm_manager.get_by_code(code)):
            sl_price = pos.stop_loss  or 0
            tp_price = pos.take_profit or 0
            if sl_price > 0 and price <= sl_price:
                ok = vm_manager.sell_vm(pos.position_id, price, "손절")
                if ok:
                    pnl = pos.pnl(price)
                    if notifier:
                        notifier.send(
                            f"🔴 <b>VM 손절</b> [{pos.name}({code})]\n"
                            f"{pos.qty}주 @ {price:,}원 | 손익: {pnl:+,.0f}원"
                        )
                    self.trade_logger.log_trade(
                        code=code, name=pos.name, side="SELL",
                        qty=pos.qty, price=price,
                        pnl=pnl, pnl_rate=pos.pnl_rate(price), reason="손절"
                    )
                continue

            if tp_price > 0 and price >= tp_price:
                ok = vm_manager.sell_vm(pos.position_id, price, "목표가청산")
                if ok:
                    pnl = pos.pnl(price)
                    if notifier:
                        notifier.send(
                            f"🟢 <b>VM 목표가 청산</b> [{pos.name}({code})]\n"
                            f"{pos.qty}주 @ {price:,}원 | 손익: {pnl:+,.0f}원"
                        )
                    self.trade_logger.log_trade(
                        code=code, name=pos.name, side="SELL",
                        qty=pos.qty, price=price,
                        pnl=pnl, pnl_rate=pos.pnl_rate(price), reason="목표가청산"
                    )

        # 2. 신규 매수 체크 (분할 매수: 1/3 지점 50% → 2/3 지점 50%)
        if not self._can_buy_time():
            return

        buy_min    = custom_conf.get("buy_min") or 0
        buy_max    = custom_conf.get("buy_max") or 0
        created_at = custom_conf.get("created_at", "")

        # buy_min/max 둘 다 0이면 진입 불가 (config에서 미지정 시 1/9999999로 설정됨)
        if buy_min == 0 and buy_max == 0:
            return
        # 매수 범위 밖이면 리턴
        if price < buy_min or price > buy_max:
            return

        sl   = custom_conf.get("stop_loss") or 0
        tp   = custom_conf.get("take_profit") or 0
        name = custom_conf.get("name", code)

        # 매수 범위를 3등분한 임계값
        buy_range  = buy_max - buy_min
        threshold1 = buy_min + buy_range / 3        # 1/3 지점 → 1차(50%) 매수
        threshold2 = buy_min + buy_range * 2 / 3    # 2/3 지점 → 2차(50%) 매수
        half_amount = max(1, getattr(config, "VM_TRADE_AMOUNT", 1_000_000) // 2)

        vm_buy_queue = harness.get_context().get("vm_buy_queue", [])

        def _queued_tranches():
            return sum(
                1 for q in harness.get_context().get("vm_buy_queue", [])
                if q["code"] == code and q["created_at"] == created_at
            )

        def _do_buy(tranche: int):
            """tranche 번째 매수 시도 — 실패 시 대기열에 추가."""
            ok = vm_manager.buy_vm(code, name, price, sl, tp, created_at,
                                   amount=half_amount)
            if ok:
                pos_list = vm_manager.get_by_code(code)
                new_pos  = pos_list[-1] if pos_list else None
                qty      = new_pos.qty if new_pos else max(1, int(half_amount / price))
                self.trade_logger.log_trade(
                    code=code, name=name, side="BUY",
                    qty=qty, price=price, reason=f"VM분할매수{tranche}차"
                )
                if notifier:
                    notifier.send(
                        f"🟢 <b>VM 분할매수 {tranche}차</b> [{name}({code})]\n"
                        f"{qty}주 @ {price:,}원 "
                        f"({'1/3' if tranche == 1 else '2/3'} 지점)\n"
                        f"손절가: {sl:,}원 | 목표가: {tp:,}원"
                    )
                logger.info(
                    f"[Agent] VM {tranche}차 매수 완료: {name}({code}) "
                    f"{qty}주 @ {price:,}원"
                )
                return True
            else:
                queue = harness.get_context().setdefault("vm_buy_queue", [])
                queue.append({
                    "code": code, "name": name,
                    "stop_loss": sl, "take_profit": tp,
                    "created_at": created_at,
                    "amount": half_amount,
                    "tranche": tranche,
                })
                logger.info(
                    f"[Agent] VM {tranche}차 대기열 추가: {name}({code}) — 예수금 부족"
                )
                return False

        # ── 1차 매수: price >= threshold1, 보유 트랜치 0개 ──────────────────
        tranche_count = vm_manager.count_positions_for_date(code, created_at)
        total = tranche_count + _queued_tranches()

        if total == 0 and price >= threshold1:
            bought = _do_buy(1)
            if not bought:
                return  # 예수금 부족 → 2차도 불가
            return  # 1차 성공 후 즉시 리턴 — 2차는 다음 틱에서 평가

        # ── 2차 매수: price >= threshold2, 보유 트랜치 1개 ──────────────────
        tranche_count = vm_manager.count_positions_for_date(code, created_at)
        total = tranche_count + _queued_tranches()

        if total == 1 and price >= threshold2:
            _do_buy(2)

    def _in_trade_hours(self) -> bool:
        """config.is_trade_hours() 래퍼 — 단일 진실 공급원 유지."""
        return config.is_trade_hours()

    def _can_buy_time(self) -> bool:
        """config.is_buy_time() 래퍼 — 단일 진실 공급원 유지."""
        return config.is_buy_time()
