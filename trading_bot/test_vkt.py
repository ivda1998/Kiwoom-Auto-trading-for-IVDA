import sys, os, time, asyncio, json
sys.path.append('g:/autoTrade/trading_bot')
import config
from kiwoom_api import KiwoomAPI

config.IS_SIMULATION = True
api = KiwoomAPI()
api.login()

print("Testing kt00001 (Balance)")
try:
    data = api._post(
        "/api/dostk/acnt",
        body={
            "acnt_no": config.ACCOUNT_NUMBER,
            "qry_tp": "3",
            "dmst_stex_tp": "KRX",
        },
        api_id="kt00001",
    )
    print(f"ID: kt00001 => {data}")
except Exception as e:
    print(f"Error test 1: {e}")

print("Testing kt10000/kt10001 (Order)")
try:
    # 매수 테스트 (kt10000)
    data = api._post(
        "/api/dostk/ordr",
        body={
            "acnt_no":  config.ACCOUNT_NUMBER,
            "stk_cd":   "005930",
            "ord_qty":  "1",
            "ord_prc":  "0",
            "trde_tp":  "03", 
            "dmst_stex_tp": "KRX",
        },
        api_id="kt10000",
    )
    print(f"ID: kt10000 (buy) => {data}")

    # 매도 테스트 (kt10001)
    data = api._post(
        "/api/dostk/ordr",
        body={
            "acnt_no":  config.ACCOUNT_NUMBER,
            "stk_cd":   "005930",
            "ord_qty":  "1",
            "ord_prc":  "0",
            "trde_tp":  "03", 
            "dmst_stex_tp": "KRX",
        },
        api_id="kt10001",
    )
    print(f"ID: kt10001 (sell) => {data}")

except Exception as e:
    print(f"Error test 2: {e}")
