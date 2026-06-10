#!/usr/bin/env python3
"""
diagnostic_telegram.py — 텔레그램 봇 연결 진단 스크립트
Oracle VM에서 실행:  python3 diagnostic_telegram.py

무엇을 확인하는가:
  1. 봇 토큰 유효 여부 (getMe)
  2. 현재 대기 중인 업데이트 및 발신 chat_id 출력
  3. sendMessage 전송 테스트 (→ 어느 chat에 메시지가 가는지 확인)
  4. 30초간 라이브 폴링 (실제 명령이 수신되는지 확인)
"""

import os, sys, json, time, requests

# ─── 설정 로드 ───────────────────────────────────────────
# 1순위: 환경변수  2순위: .env 파일  3순위: 직접 입력
def _load_env(path=".env"):
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip("'\"")
    except FileNotFoundError:
        pass
    return env

_env = _load_env(os.path.join(os.path.dirname(__file__), ".env"))

TOKEN   = os.environ.get("TELEGRAM_BOT_TOKEN", _env.get("TELEGRAM_BOT_TOKEN", ""))
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID",   _env.get("TELEGRAM_CHAT_ID",   ""))

if not TOKEN:
    TOKEN = input("TELEGRAM_BOT_TOKEN을 입력하세요: ").strip()
if not CHAT_ID:
    CHAT_ID = input("TELEGRAM_CHAT_ID를 입력하세요: ").strip()

BASE = f"https://api.telegram.org/bot{TOKEN}"

print("\n" + "="*60)
print(f"  설정된 TOKEN  : ...{TOKEN[-10:]}")
print(f"  설정된 CHAT_ID: {CHAT_ID}")
print("="*60 + "\n")

# ─── 1. getMe ────────────────────────────────────────────
print("▶ [1/4] getMe — 봇 토큰 유효성 확인")
try:
    r = requests.get(f"{BASE}/getMe", timeout=10)
    d = r.json()
    if d.get("ok"):
        bot = d["result"]
        print(f"  ✅ 봇 이름: @{bot.get('username')} ({bot.get('first_name')})")
        print(f"     is_bot={bot.get('is_bot')}  can_read_all_group_messages={bot.get('can_read_all_group_messages')}")
    else:
        print(f"  ❌ 실패: {d.get('description')}")
        print("  → TOKEN이 잘못되었습니다. .env의 TELEGRAM_BOT_TOKEN을 확인하세요.")
        sys.exit(1)
except Exception as e:
    print(f"  ❌ 네트워크 오류: {e}")
    print("  → VM에서 api.telegram.org 접근이 차단됐을 수 있습니다 (방화벽/프록시).")
    sys.exit(1)

# ─── 2. deleteWebhook ────────────────────────────────────
print("\n▶ [2/4] deleteWebhook — 기존 웹훅 제거")
try:
    r = requests.post(f"{BASE}/deleteWebhook", timeout=10)
    d = r.json()
    if d.get("ok"):
        print(f"  ✅ 웹훅 제거 완료 (description: {d.get('description', 'none')})")
    else:
        print(f"  ⚠️  웹훅 제거 응답: {d}")
except Exception as e:
    print(f"  ⚠️  오류: {e}")

# ─── 3. 대기 중인 업데이트 ───────────────────────────────
print("\n▶ [3/4] getUpdates — 현재 대기 중인 메시지 확인")
try:
    r = requests.get(f"{BASE}/getUpdates", params={"timeout": 0, "limit": 20}, timeout=10)
    d = r.json()
    updates = d.get("result", [])
    if not updates:
        print("  ℹ️  대기 중인 업데이트 없음")
    else:
        print(f"  📬 대기 업데이트 {len(updates)}건:")
        for upd in updates:
            uid  = upd.get("update_id")
            msg  = upd.get("message", {})
            text = msg.get("text", "(no text)")
            date = msg.get("date", 0)
            from_id  = msg.get("from",  {}).get("id", "?")
            from_name= msg.get("from",  {}).get("username", "?")
            chat_id  = msg.get("chat",  {}).get("id", "?")
            chat_type= msg.get("chat",  {}).get("type", "?")
            ts = time.strftime("%H:%M:%S", time.localtime(date)) if date else "?"
            print(f"    update_id={uid}  text={text!r}  time={ts}")
            print(f"      발신자: @{from_name}(id={from_id})  채팅: id={chat_id} type={chat_type}")
            match = "✅ 일치" if str(chat_id) == str(CHAT_ID) else f"❌ 불일치! (설정={CHAT_ID})"
            print(f"      chat_id 비교: {match}")
except Exception as e:
    print(f"  ❌ 오류: {e}")

# ─── 4. sendMessage 테스트 ────────────────────────────────
print(f"\n▶ [4/5] sendMessage — 설정된 chat_id({CHAT_ID})로 메시지 전송")
try:
    r = requests.post(
        f"{BASE}/sendMessage",
        json={"chat_id": CHAT_ID, "text": "🔧 <b>진단 테스트</b>\n이 메시지가 보이면 send는 정상 작동 중입니다.", "parse_mode": "HTML"},
        timeout=10,
    )
    d = r.json()
    if d.get("ok"):
        print(f"  ✅ 전송 성공! 텔레그램에서 이 메시지를 확인하세요.")
        print(f"     message_id={d['result'].get('message_id')}")
    else:
        code = d.get("error_code")
        desc = d.get("description")
        print(f"  ❌ 전송 실패 (error_code={code}): {desc}")
        if code == 400 and "chat not found" in str(desc).lower():
            print("  → CHAT_ID가 잘못됐거나 봇이 해당 채팅에 참가하지 않았습니다.")
            print("  → 텔레그램에서 봇에게 먼저 /start 를 보내세요.")
        elif code == 403:
            print("  → 봇이 채팅에서 차단(block)됐습니다.")
except Exception as e:
    print(f"  ❌ 네트워크 오류: {e}")

# ─── 5. 라이브 폴링 30초 ────────────────────────────────
print("\n▶ [5/5] 라이브 폴링 — 30초간 메시지 수신 대기")
print("  ★ 지금 텔레그램에서 봇에게 /ping 을 보내세요! ★")
print()

offset = 0
# 먼저 현재 최대 update_id를 가져와서 이후 메시지만 수신
try:
    r = requests.get(f"{BASE}/getUpdates", params={"timeout": 0}, timeout=10)
    result = r.json().get("result", [])
    if result:
        offset = result[-1]["update_id"] + 1
        print(f"  (기존 {len(result)}건 건너뜀, offset={offset})")
except Exception:
    pass

start = time.time()
received = 0
try:
    while time.time() - start < 30:
        elapsed = int(time.time() - start)
        remaining = 30 - elapsed
        wait = min(10, remaining)
        if wait <= 0:
            break
        try:
            r = requests.get(
                f"{BASE}/getUpdates",
                params={"offset": offset, "timeout": wait, "allowed_updates": ["message"]},
                timeout=wait + 5,
            )
            d = r.json()
            if d.get("ok"):
                for upd in d.get("result", []):
                    offset = upd["update_id"] + 1
                    msg  = upd.get("message", {})
                    text = msg.get("text", "")
                    from_name = msg.get("from", {}).get("username", "?")
                    chat_id   = str(msg.get("chat", {}).get("id", "?"))
                    print(f"  📩 수신! text={text!r}  from=@{from_name}  chat_id={chat_id}")
                    match = "✅" if chat_id == str(CHAT_ID) else f"❌ 불일치(설정={CHAT_ID})"
                    print(f"     chat_id 일치여부: {match}")
                    received += 1
        except requests.exceptions.Timeout:
            pass
        except Exception as e:
            print(f"  ⚠️  폴링 오류: {e}")
            time.sleep(2)
except KeyboardInterrupt:
    print("\n  (Ctrl+C로 중단)")

if received == 0:
    print("  ⚠️  30초간 아무 메시지도 수신되지 않았습니다.")
    print()
    print("  가능한 원인:")
    print("  [A] 봇에게 메시지를 보내지 않았거나 잘못된 봇에게 보냄")
    print("  [B] Oracle VM에서 장기 폴링(HTTP timeout=10s) 연결이 방화벽에 의해 차단됨")
    print("       → 일반 HTTP POST는 되는데 장기 연결이 끊기는 경우")
    print("       → 해결: timeout=0 으로 폴링 (짧은 폴링 방식으로 변경)")
    print("  [C] 텔레그램 API 서버가 VM IP를 차단함 (드문 경우)")
else:
    print(f"\n  ✅ {received}건 수신 성공!")

print("\n" + "="*60)
print("  진단 완료")
print("="*60)
