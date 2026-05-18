from .base_hook import BaseHook
import logging

logger = logging.getLogger(__name__)

class MarketFilterHook(BaseHook):
    """
    매수 실행 전 시장 추세(코스피/코스닥)가 우하향일 경우 매수 진입을 차단하는 필터 훅.
    """
    def __init__(self, market_filter):
        self.market_filter = market_filter

    def pre_action(self, context: dict, action: dict) -> bool:
        # 매수(BUY) 액션이 발생하기 직전에 시장 상황이 양호(Bullish)한지 검사
        if action["type"] == "BUY":
            if not self.market_filter.is_bullish():
                logger.warning(f"[MarketFilterHook] 시장 지수 우하향 감지 → 신규 매수 차단")
                return False
        return True

    def post_action(self, context: dict, action: dict, result) -> None:
        pass
