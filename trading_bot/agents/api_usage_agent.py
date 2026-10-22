import logging
from datetime import datetime

from agents.base_agent import BaseAgent

logger = logging.getLogger(__name__)

_THRESHOLDS = (1500, 1600, 1650, 1680)  # kiwoom_api.py의 _WARN_THRESHOLDS와 동일한 기준
_CHECK_INTERVAL_SEC = 60  # TICK마다 매번 확인하지 않고 최소 이 간격으로만 점검


def _fmt(counts: dict) -> str:
    items = sorted(((k, v) for k, v in counts.items() if v), key=lambda x: -x[1])
    return ", ".join(f"{k}={v}" for k, v in items) or "-"


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
        self._last_hour_logged = None
        self._prev_by_api: dict = {}

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
        by_api = usage.get("by_api") or {}

        # 시간대별 API 내역 로그(원인 추적용) — 정시가 바뀔 때마다 직전 1시간 증분 + 누계
        hour = datetime.now().strftime("%Y-%m-%d %H")
        if self._last_hour_logged != hour:
            if self._last_hour_logged and self._last_hour_logged[:10] == hour[:10]:
                delta = {k: v - self._prev_by_api.get(k, 0) for k, v in by_api.items()}
                logger.info(f"[ApiUsage] {self._last_hour_logged}시 구간 +{sum(delta.values())}회 "
                            f"({_fmt(delta)}) | 누계 {count}회 ({_fmt(by_api)})")
            self._last_hour_logged = hour
            self._prev_by_api = dict(by_api)

        if not usage.get("limit"):
            return   # 일일 한도가 없는 증권사(나무) — 시간대별 로그만 남김
        for threshold in _THRESHOLDS:
            if count >= threshold and threshold not in self._notified_today:
                self._notified_today.add(threshold)
                logger.info(f"[ApiUsage] 임계치 {threshold} 도달 — 텔레그램 알림 전송")
                self.notifier.send(
                    f"⚠️ <b>API 호출량 경고</b>\n"
                    f"오늘 {count}/{usage['limit']}회 사용 ({usage['pct']}%)\n"
                    f"남은 호출: {usage['remaining']}회\n"
                    f"API별: {_fmt(by_api)}\n"
                    f"1500회부터 미보유 종목 조회 중단, 1650회부터 분봉 갱신 중단(보유 감시만 유지)."
                )
