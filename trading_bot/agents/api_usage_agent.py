import logging
from datetime import datetime

from agents.base_agent import BaseAgent

logger = logging.getLogger(__name__)

_THRESHOLDS = (1500, 1600, 1650, 1680)  # kiwoom_api.py의 _WARN_THRESHOLDS와 동일한 기준
_CHECK_INTERVAL_SEC = 60  # TICK마다 매번 확인하지 않고 최소 이 간격으로만 점검


class ApiUsageAgent(BaseAgent):
    """키움 REST 일일 호출량(1,700회 한도)을 감시해 임계치 도달 시 텔레그램 알림.

    사용량 추적 자체는 kiwoom_api.py의 _post()가 이미 하고 있으므로, 이 에이전트는
    그 카운터를 주기적으로 읽어 새로 넘은 임계치가 있으면 중복 없이 1회만 알린다.
    실제 호출을 막거나 줄이는 로직은 없음 — 가시화·알림 전용.
    """

    def __init__(self, kiwoom, notifier):
        super().__init__()
        self.kiwoom = kiwoom
        self.notifier = notifier
        self._notified_today: set = set()
        self._notified_date = None
        self._last_check = 0.0

    def analyze_and_act(self, event: dict, context: dict):
        if event.get("type") != "TICK":
            return
        now_ts = datetime.now().timestamp()
        if now_ts - self._last_check < _CHECK_INTERVAL_SEC:
            return
        self._last_check = now_ts
        self._check_usage()

    def _check_usage(self):
        today = datetime.now().date()
        if self._notified_date != today:
            self._notified_date = today
            self._notified_today = set()

        usage = self.kiwoom.get_api_usage()
        count = usage["count"]

        for threshold in _THRESHOLDS:
            if count >= threshold and threshold not in self._notified_today:
                self._notified_today.add(threshold)
                logger.info(f"[ApiUsage] 임계치 {threshold} 도달 — 텔레그램 알림 전송")
                self.notifier.send(
                    f"⚠️ <b>API 호출량 경고</b>\n"
                    f"오늘 {count}/{usage['limit']}회 사용 ({usage['pct']}%)\n"
                    f"남은 호출: {usage['remaining']}회\n"
                    f"한도 초과 시 당일 모든 조회 API가 중단됩니다."
                )
