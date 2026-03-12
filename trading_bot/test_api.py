# test_api.py - API 응답 확인용 (공식 샘플 방식으로 직접 테스트)
import os, json, requests, asyncio

APP_KEY    = os.environ.get("KIWOOM_APP_KEY", "")
APP_SECRET = os.environ.get("KIWOOM_APP_SECRET", "")
BASE_URL   = os.environ.get("KIWOOM_BASE_URL", "https://mockapi.kiwoom.com")
WS_URL     = os.environ.get("KIWOOM_WS_URL",
               "wss://mockapi.kiwoom.com:10000/api/dostk/websocket")

def post(endpoint, body, api_id, token=None, cont_yn="N", next_key=""):
    headers = {"Content-Type": "application/json;charset=UTF-8", "api-id": api_id,
               "cont-yn": cont_yn, "next-key": next_key}
    if token:
        headers["authorization"] = f"Bearer {token}"
    r = requests.post(BASE_URL + endpoint, headers=headers, json=body, timeout=15)
    print(f"\n[{api_id}] {r.status_code}")
    print(f"  resp-headers: cont-yn={r.headers.get('cont-yn')} next-key={r.headers.get('next-key','')}")
    try:
        d = r.json()
        print(f"  body: {json.dumps(d, ensure_ascii=False)[:500]}")
        return d
    except:
        print(f"  raw: {r.text[:300]}")
        return {}

# 1. 토큰 발급
print("=== 1. 토큰 발급 ===")
d = post("/oauth2/token", {"grant_type":"client_credentials","appkey":APP_KEY,"secretkey":APP_SECRET}, "au10001")
token = d.get("token","")
print(f"  token: {'OK' if token else 'FAIL'}")

if not token:
    print("토큰 발급 실패, 종료")
    exit(1)

# 2. 코스닥 전종목
print("\n=== 2. 코스닥 전종목 (ka10099) ===")
d = post("/api/dostk/stkinfo", {"mrkt_tp":"10"}, "ka10099", token)

# 3. 삼성전자 현재가
print("\n=== 3. 삼성전자 현재가 (ka10001) ===")
d = post("/api/dostk/stkinfo", {"stk_cd":"005930"}, "ka10001", token)

# 4. 삼성전자 일봉
print("\n=== 4. 삼성전자 일봉 (ka10081) ===")
d = post("/api/dostk/chart", {"stk_cd":"005930","base_dt":"00000000","upd_stkpc_tp":"1"}, "ka10081", token)

# 5. 잔고
print("\n=== 5. 계좌잔고 (kt00018) ===")
d = post("/api/dostk/acnt", {"qry_tp":"1","dmst_stex_tp":"KRX"}, "kt00018", token)

# ──────────────────────────────────────────────────────────────
# 6~8. WebSocket 테스트 (ka10171 / ka10172 / 실시간 구독)
# ──────────────────────────────────────────────────────────────

async def ws_test():
    import websockets as ws_lib

    print(f"\n=== 6. WebSocket 연결 테스트 ===")
    print(f"  URI: {WS_URL}")
    print(f"  token: {token[:20]}...")

    try:
        async with ws_lib.connect(
            WS_URL,
            additional_headers={"authorization": f"Bearer {token}"},
            open_timeout=10,
        ) as ws:
            print("  [OK] WebSocket 연결 성공!")

            # ── WS 로그인 인증 (필수: 다른 TR 전에 먼저 전송) ──
            print("\n  WS 로그인 인증 중...")
            await ws.send(json.dumps({"trnm": "LOGIN", "token": token}))
            try:
                login_raw  = await asyncio.wait_for(ws.recv(), timeout=10)
                login_resp = json.loads(login_raw)
                rc = login_resp.get("return_code", -1)
                if rc == 0:
                    print(f"  [OK] WS 로그인 성공 (return_code={rc})")
                else:
                    print(f"  [FAIL] WS 로그인 실패: rc={rc}  msg={login_resp.get('return_msg','')}")
                    return
            except asyncio.TimeoutError:
                print("  [FAIL] WS 로그인 응답 타임아웃 (10초)")
                return

            # ── 6-1. ka10171: 조건식 목록 ──
            print("\n=== 7. 조건식 목록 (ka10171 / CNSRLST) ===")
            await ws.send(json.dumps({"trnm": "CNSRLST"}))
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
                resp = json.loads(raw)
                print(f"  trnm: {resp.get('trnm')}  return_code: {resp.get('return_code')}")
                data = resp.get("data", [])
                print(f"  data 타입: {type(data).__name__}  길이: {len(data) if isinstance(data, list) else 'N/A'}")
                if data:
                    print(f"  첫 번째 항목: {data[0]}")
                    print(f"  전체: {json.dumps(data, ensure_ascii=False)[:300]}")

                # data 구조 분석
                if isinstance(data, list) and data:
                    first = data[0]
                    if isinstance(first, list):
                        print(f"  ▶ 응답 형식: 리스트 of 리스트  (예: [seq, name])")
                    elif isinstance(first, dict):
                        print(f"  ▶ 응답 형식: 리스트 of dict  (키: {list(first.keys())})")

                cond_seq = None
                for item in data:
                    if isinstance(item, list) and len(item) >= 2:
                        print(f"    seq={item[0]}  name={item[1]}")
                        if cond_seq is None:
                            cond_seq = str(item[0])
                    elif isinstance(item, dict):
                        seq  = item.get("seq", item.get("cond_no",""))
                        name = item.get("cond_nm", item.get("cond_name",""))
                        print(f"    seq={seq}  name={name}")
                        if cond_seq is None:
                            cond_seq = str(seq)

            except asyncio.TimeoutError:
                print("  ka10171 응답 없음 (10초 타임아웃)")
                cond_seq = None

            # ── 6-2. ka10172: 조건검색 ──
            if cond_seq:
                print(f"\n=== 8. 조건검색 (ka10172 / CNSRREQ seq={cond_seq}) ===")
                await ws.send(json.dumps({
                    "trnm":        "CNSRREQ",
                    "seq":         cond_seq,
                    "search_type": "0",
                    "stex_tp":     "K",
                }))
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=15)
                    resp = json.loads(raw)
                    print(f"  trnm: {resp.get('trnm')}  return_code: {resp.get('return_code')}")
                    data2 = resp.get("data", [])
                    print(f"  data 타입: {type(data2).__name__}  길이: {len(data2) if isinstance(data2, list) else 'N/A'}")
                    if data2:
                        print(f"  첫 번째 항목: {data2[0]}")
                        if isinstance(data2, list) and data2:
                            first2 = data2[0]
                            if isinstance(first2, list):
                                print(f"  ▶ 응답 형식: 리스트 of 리스트  (예: [code, name])")
                            elif isinstance(first2, dict):
                                print(f"  ▶ 응답 형식: 리스트 of dict  (키: {list(first2.keys())})")
                        print(f"  종목 목록(최대 5개):")
                        for item in data2[:5]:
                            if isinstance(item, list):
                                print(f"    code={item[0]}  name={item[1] if len(item)>1 else ''}")
                            elif isinstance(item, dict):
                                print(f"    {item}")
                except asyncio.TimeoutError:
                    print("  ka10172 응답 없음 (15초 타임아웃)")

            # ── 6-3. 실시간 체결 구독 테스트 (삼성전자) ──
            print(f"\n=== 9. 실시간 체결 구독 테스트 (REG, 삼성전자 005930) ===")
            # item_tp: "J"=주식체결, "0B"=주식체결(일부문서), "S3_"=체결
            # 장 외 시간에는 return_code=105111 이 정상 (실시간 미제공)
            await ws.send(json.dumps({
                "trnm":    "REG",
                "grp_no":  "0001",
                "refresh": "1",
                "data":    [{"item_cd": "005930", "item_tp": "J"}],
            }))
            print("  구독 요청 전송 완료. 5초간 실시간 메시지 대기 (장 외 시간에는 응답 없을 수 있음)...")
            try:
                for _ in range(5):
                    raw = await asyncio.wait_for(ws.recv(), timeout=3)
                    resp = json.loads(raw)
                    rc_r = resp.get("return_code", "")
                    trnm_r = resp.get("trnm", "")
                    print(f"  수신: trnm={trnm_r} rc={rc_r} | {json.dumps(resp, ensure_ascii=False)[:200]}")
                    if rc_r != 0 and rc_r != "":
                        print(f"  [WARN] 오류 응답: {resp.get('return_msg','')}")
                        print(f"  (장 외 시간에는 return_code=105111 이 정상)")
                        break
            except asyncio.TimeoutError:
                print("  (3초 내 메시지 없음 - 장 외 시간이거나 실시간 TR 코드가 다를 수 있음)")

    except Exception as e:
        print(f"  [FAIL] WebSocket 연결/통신 실패: {type(e).__name__}: {e}")

asyncio.run(ws_test())
