import sys, os, time, asyncio, json
sys.path.append('g:/autoTrade/trading_bot')
import config
from kiwoom_api import KiwoomAPI

config.IS_SIMULATION = True
api = KiwoomAPI()
api.login()

print("Fetching positions from kt00018 (Account Balance List)")
try:
    data = api._post(
        "/api/dostk/acnt",
        body={
            "qry_tp": "2", # 일반조회
            "dmst_stex_tp": "KRX",
        },
        api_id="kt00018",
    )
    print("Response keys:", list(data.keys()))
    if 'acnt_evlt_remn_indv_tot' in data:
        print("Positions:", data['acnt_evlt_remn_indv_tot'])
    else:
        print("Raw data:", data)
except Exception as e:
    print(f"Error test kt00018: {e}")
