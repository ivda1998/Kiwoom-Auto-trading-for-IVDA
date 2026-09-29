# -*- coding: utf-8 -*-
"""VM 일일 헬스체크 — 봇 생존/매매/오류 요약을 텔레그램으로 보고.

무인 운영 중(연휴 등)에 이상을 알아챌 수 있는 유일한 경로라, 문제가 없어도 매일 보낸다.
메시지가 오지 않는 것 자체가 이상 신호가 되도록 하는 것이 목적이다.

VM cron 전용: python3 /home/ubuntu/autoTrade/trading_bot/tools/vm_health_check.py
"""
import glob
import html
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

BASE_DIR = "/home/ubuntu/autoTrade/trading_bot"
sys.path.insert(0, BASE_DIR)
os.chdir(BASE_DIR)

import config  # noqa: E402  (.env 로드를 위해 chdir 이후에 import)

SERVICE = "trading_bot"
STALE_REFRESH_MIN = 15      # 장중 이 시간 넘게 분봉 갱신이 없으면 이상


def sh(cmd: str) -> str:
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True,
                              timeout=20).stdout.strip()
    except Exception as e:
        return f"(조회실패: {e})"


def is_market_holiday(d) -> tuple:
    if d.weekday() >= 5:
        return True, "주말"
    try:
        import holidays
        name = holidays.KR(years=d.year).get(d)
        if name:
            return True, name
    except ImportError:
        pass
    return False, ""


def today_log_lines(today: str) -> list:
    """오늘 날짜로 시작하는 로그 라인 수집.

    봇이 시작 시점 날짜로 파일명을 고정해서 쓰기 때문에, 오늘 로그가 어제 이름의
    파일에 들어있을 수 있다 — 그래서 파일명이 아니라 라인의 날짜로 고른다.
    """
    lines = []
    for path in glob.glob(os.path.join(config.LOG_DIR, "trading_*.log")):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines += [ln.rstrip("\n") for ln in f if ln.startswith(today)]
        except OSError:
            continue
    return sorted(lines)


def send_telegram(text: str):
    token, chat = config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_CHAT_ID
    if not token or not chat:
        print("[health] 텔레그램 설정 없음 — 전송 생략")
        return
    data = urllib.parse.urlencode({
        "chat_id": chat, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=data)
    with urllib.request.urlopen(req, timeout=20) as r:
        json.load(r)


def main():
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    holiday, holiday_name = is_market_holiday(now.date())

    active = sh(f"systemctl is-active {SERVICE}")
    since = sh(f"systemctl show {SERVICE} -p ActiveEnterTimestamp --value")
    lines = today_log_lines(today)

    lev_in = [l for l in lines if "LEVERAGE 진입" in l or "INVERSE 진입" in l]
    lev_out = [l for l in lines if "[LeverageAgent] 청산" in l]
    buys = [l for l in lines if "매수 완료" in l]
    sells = [l for l in lines if "매도 완료" in l or "[Execution] 청산" in l]
    errors = [l for l in lines if "[ERROR]" in l or "[CRITICAL]" in l]

    refreshes = [l for l in lines if "005930 refresh" in l]
    last_refresh = refreshes[-1][11:19] if refreshes else None

    alerts = []
    if active != "active":
        alerts.append(f"봇 서비스가 {active} 상태")
    if errors:
        alerts.append(f"ERROR 로그 {len(errors)}건")
    if not holiday and "0900" <= now.strftime("%H%M") <= "1530":
        if not last_refresh:
            alerts.append("장중인데 삼성전자 분봉 갱신 기록 없음")
        else:
            gap = now - datetime.strptime(f"{today} {last_refresh}", "%Y-%m-%d %H:%M:%S")
            if gap > timedelta(minutes=STALE_REFRESH_MIN):
                alerts.append(f"분봉 갱신이 {int(gap.total_seconds()//60)}분째 멈춤")

    head = "🚨 <b>VM 헬스체크 — 확인 필요</b>" if alerts else "✅ <b>VM 헬스체크 — 정상</b>"
    msg = [head, f"{today} {now.strftime('%H:%M')} | 서비스: {active} (기동 {since})"]

    if alerts:
        msg.append("")
        msg += [f"⚠️ {a}" for a in alerts]

    msg.append("")
    if holiday:
        msg.append(f"📅 휴장({holiday_name}) — 매매 없음이 정상")
    else:
        msg.append(f"📈 레버리지: 진입 {len(lev_in)}건 / 청산 {len(lev_out)}건")
        msg.append(f"📊 돌파전략: 매수 {len(buys)}건 / 매도 {len(sells)}건")
        msg.append(f"🕒 마지막 분봉 갱신: {last_refresh or '없음'}")

    # 로그 원문에는 '<Handle ...>' 같은 텍스트가 섞일 수 있어 반드시 이스케이프한다
    # (안 하면 텔레그램 HTML 파싱 실패로 이 보고 자체가 전송되지 않는다)
    for l in lev_out[-3:]:
        msg.append("  └ " + html.escape(l.split(" - ")[-1][:120]))
    for l in errors[-3:]:
        msg.append("  ❗ " + html.escape(l.split(" - ")[-1][:120]))

    text = "\n".join(msg)
    print(text)
    try:
        send_telegram(text)
    except Exception as e:
        print(f"[health] 텔레그램 전송 실패: {e}", file=sys.stderr)


if __name__ == "__main__":
    main()
