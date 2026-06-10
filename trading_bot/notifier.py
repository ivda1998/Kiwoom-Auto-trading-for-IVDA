# notifier.py — 텔레그램 알림 + 원격 명령 수신

import logging
import threading
import time
import requests

logger = logging.getLogger(__name__)

_SEND_URL         = "https://api.telegram.org/bot{token}/sendMessage"
_UPDATES_URL      = "https://api.telegram.org/bot{token}/getUpdates"
_DEL_WEBHOOK_URL  = "https://api.telegram.org/bot{token}/deleteWebhook"


class TelegramNotifier:
    """
    텔레그램 봇 알림 전송 + 장기 폴링(Long Polling) 기반 명령 수신.
    token/chat_id 가 빈 문자열이면 모든 기능이 무음 처리됨.
    """

    def __init__(self, token: str, chat_id: str):
        self.token   = token.strip()
        self.chat_id = chat_id.strip()
        self._enabled     = bool(self.token and self.chat_id)
        self._offset      = 0
        self._commands    = {}       # name(str) -> callable(args: str) -> str|None
        self._stop_event  = threading.Event()
        self._poll_thread = None

    # ─────────────────────────────────────
    # 명령 등록
    # ─────────────────────────────────────
    def register_command(self, name: str, handler):
        """handler(args: str) -> str  (반환값이 응답 메시지)"""
        self._commands[name.lstrip("/").lower()] = handler

    # ─────────────────────────────────────
    # 알림 전송
    # ─────────────────────────────────────
    def send(self, text: str, parse_mode: str = "HTML"):
        if not self._enabled:
            return
        try:
            url = _SEND_URL.format(token=self.token)
            resp = requests.post(
                url,
                json={"chat_id": self.chat_id, "text": text, "parse_mode": parse_mode},
                timeout=10,
            )
            if not resp.json().get("ok"):
                logger.warning(f"[Telegram] 전송 실패: {resp.json().get('description')}")
        except Exception as e:
            logger.warning(f"[Telegram] 메시지 전송 오류: {e}")

    # ─────────────────────────────────────
    # 폴링 시작 / 중지
    # ─────────────────────────────────────
    def start_polling(self):
        if not self._enabled:
            logger.info("[Telegram] 토큰/채팅ID 미설정 — 폴링 생략")
            return

        # 웹훅이 걸려있으면 getUpdates가 동작하지 않으므로 먼저 제거
        self._delete_webhook()

        # 시작 시 대기 중인 기존 메시지를 모두 건너뜀 (봇 재시작 전 쌓인 명령 무시)
        self._skip_pending_updates()

        self._stop_event.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_loop, name="telegram-poll", daemon=True
        )
        self._poll_thread.start()
        logger.info("[Telegram] 명령 수신 폴링 시작 (offset=%d)", self._offset)

    def stop(self):
        self._stop_event.set()

    def ensure_polling(self):
        """
        폴링 쓰레드가 죽었으면 자동 재시작.
        _main_loop()에서 60초마다 호출.
        """
        if not self._enabled:
            return
        if self._poll_thread is None:
            return
        if self._stop_event.is_set():
            return  # 의도적으로 중지된 경우
        if not self._poll_thread.is_alive():
            logger.warning("[Telegram] ⚠️ 폴링 쓰레드 종료 감지 → 자동 재시작")
            self._stop_event.clear()
            self._poll_thread = threading.Thread(
                target=self._poll_loop, name="telegram-poll", daemon=True
            )
            self._poll_thread.start()
            logger.info("[Telegram] 폴링 쓰레드 재시작 완료 (offset=%d)", self._offset)

    # ─────────────────────────────────────
    # 내부
    # ─────────────────────────────────────
    def _delete_webhook(self):
        """웹훅 제거 (Long Polling과 공존 불가)"""
        try:
            url = _DEL_WEBHOOK_URL.format(token=self.token)
            r = requests.post(url, timeout=10)
            if r.json().get("ok"):
                logger.info("[Telegram] 웹훅 제거 완료 (Long Polling 전환)")
        except Exception as e:
            logger.warning(f"[Telegram] 웹훅 제거 실패: {e}")

    def _skip_pending_updates(self, max_age_secs: int = 120):
        """
        봇 시작 전 쌓인 오래된 업데이트만 건너뜀.
        max_age_secs(기본 120초) 이내에 도착한 메시지는 보존 → 재시작 직전 명령도 처리됨.
        """
        import time as _time
        try:
            url = _UPDATES_URL.format(token=self.token)
            r = requests.get(url, params={"timeout": 0}, timeout=10)
            data = r.json()
            if not data.get("ok"):
                return
            updates = data.get("result", [])
            now_ts   = _time.time()
            skipped  = 0
            for update in updates:
                msg_date = update.get("message", {}).get("date", 0)  # Unix timestamp
                if msg_date > 0 and (now_ts - msg_date) > max_age_secs:
                    # 오래된 업데이트 → offset 전진(건너뜀)
                    self._offset = update["update_id"] + 1
                    skipped += 1
                # 최근 메시지는 offset 전진 안 함 → poll_loop 에서 그대로 처리
            kept = len(updates) - skipped
            if skipped:
                logger.info(
                    f"[Telegram] 오래된 업데이트 {skipped}건 건너뜀 (offset→{self._offset})"
                )
            if kept:
                logger.info(
                    f"[Telegram] 최근 업데이트 {kept}건 보존 → 폴링 루프에서 처리"
                )
        except Exception as e:
            logger.warning(f"[Telegram] 초기 offset 설정 실패: {e}")

    def _poll_loop(self):
        url = _UPDATES_URL.format(token=self.token)
        consecutive_errors = 0
        while not self._stop_event.is_set():
            try:
                resp = requests.get(
                    url,
                    params={"offset": self._offset, "timeout": 8, "allowed_updates": ["message"]},
                    timeout=15,
                )
                data = resp.json()
                if data.get("ok"):
                    consecutive_errors = 0
                    for update in data.get("result", []):
                        self._offset = update["update_id"] + 1
                        self._dispatch(update)
                else:
                    logger.warning(f"[Telegram] getUpdates 오류: {data.get('description')}")
                    self._stop_event.wait(timeout=5)
            except requests.exceptions.Timeout:
                # Long polling 정상 타임아웃 — 즉시 재시도
                consecutive_errors = 0  # 타임아웃은 정상; 에러 카운터 리셋
            except Exception as e:
                consecutive_errors += 1
                wait = min(60, 5 * consecutive_errors)
                logger.warning(
                    f"[Telegram] 폴링 오류 ({consecutive_errors}회): "
                    f"{type(e).__name__}: {e} → {wait}초 후 재시도"
                )
                self._stop_event.wait(timeout=wait)

    def _dispatch(self, update: dict):
        msg  = update.get("message", {})
        text = msg.get("text", "").strip()

        # 텍스트 없는 업데이트(사진·스티커 등) 무시
        if not text:
            return

        # 커맨드가 아닌 일반 메시지 무시
        if not text.startswith("/"):
            return

        # 발신 chat_id 기록 (진단용)
        from_chat_id = str(msg.get("chat", {}).get("id", ""))

        parts = text.split(None, 1)
        cmd   = parts[0].lstrip("/").lower().split("@")[0]  # /sell@botname → sell
        args  = parts[1] if len(parts) > 1 else ""

        logger.info(f"[Telegram] 명령 수신: /{cmd} {args!r} (발신chat={from_chat_id}, 설정chat={self.chat_id})")

        handler = self._commands.get(cmd)
        if handler:
            try:
                reply = handler(args)
                if reply:
                    self.send(reply)
            except Exception as e:
                logger.error(f"[Telegram] 명령 처리 오류 (/{cmd}): {e}", exc_info=True)
                self.send(f"❌ 명령 오류: {e}")
        else:
            cmds = ", ".join(f"/{k}" for k in sorted(self._commands))
            self.send(f"❓ 알 수 없는 명령: <code>/{cmd}</code>\n사용 가능: {cmds}")


class TelegramLogHandler(logging.Handler):
    """ERROR 이상 로그를 텔레그램으로 실시간 전송하는 핸들러."""

    def __init__(self, notifier: TelegramNotifier):
        super().__init__(level=logging.ERROR)
        self.notifier = notifier
        self.setFormatter(logging.Formatter("%(asctime)s %(name)s\n%(message)s"))

    def emit(self, record: logging.LogRecord):
        try:
            msg = self.format(record)
            self.notifier.send(f"⚠️ <b>에러 발생</b>\n<code>{msg[:1000]}</code>")
        except Exception:
            pass
