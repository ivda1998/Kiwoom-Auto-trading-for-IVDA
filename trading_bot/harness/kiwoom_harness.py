from .base_harness import BaseHarness
import logging

logger = logging.getLogger(__name__)

class KiwoomHarness(BaseHarness):
    """
    키움 API 연동을 담당하는 구체 하네스 클래스.
    실시간 틱 및 조건식 이벤트를 감지하여 에이전트 그룹으로 중계합니다.
    """
    def __init__(self, kiwoom_api):
        super().__init__()
        self.kiwoom = kiwoom_api
        self.update_context("kiwoom", self.kiwoom)
        self.update_context("harness", self)

        # 키움 콜백 등록
        self.kiwoom.add_tick_callback(self._on_tick)
        self.kiwoom.add_condition_callback(self._on_condition)

    def _on_tick(self, tick: dict):
        """실시간 틱 이벤트를 수신하여 에이전트에 중계합니다."""
        self.broadcast_event("TICK", tick)

    def _on_condition(self, code: str, name: str, action: str):
        """실시간 조건식 편입/이탈 이벤트를 수신하여 에이전트에 중계합니다."""
        self.broadcast_event("CONDITION", {
            "code": code,
            "name": name,
            "action": action
        })
