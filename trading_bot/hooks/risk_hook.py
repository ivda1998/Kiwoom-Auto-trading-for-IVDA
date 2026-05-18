from .base_hook import BaseHook
import logging

logger = logging.getLogger(__name__)

class RiskHook(BaseHook):
    """
    매수 실행 전 일일 손실 한도 등을 체크하여 위험 거래를 선제적으로 차단하는 훅.
    """
    def __init__(self, risk_manager):
        self.risk = risk_manager

    def pre_action(self, context: dict, action: dict) -> bool:
        # 매수(BUY) 액션이 발생하기 직전에 RiskManager를 통해 위험 수준 판단
        if action["type"] == "BUY":
            if not self.risk.can_buy():
                logger.warning(f"[RiskHook] 리스크 체크 실패: {action['type']} 행동 차단")
                return False
        return True

    def post_action(self, context: dict, action: dict, result) -> None:
        # 상태 반영 및 로깅 등 후처리 (현재는 OrderExecutor/ExecutionSkill 내부에서 처리하므로 패스)
        pass
