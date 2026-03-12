import sys, os, time, asyncio, json
sys.path.append('g:/autoTrade/trading_bot')
import config
config.IS_SIMULATION = True
from kiwoom_api import KiwoomAPI
import logging

logging.basicConfig(level=logging.INFO)

def ws_test():
    api = KiwoomAPI()
    api.login()
    
    old = api._dispatch_ws_message
    def new_dispatch(msg):
        print("RAW MSG:", msg)
        old(msg)
    api._dispatch_ws_message = new_dispatch

    tps = [
        {"item_cd": "005930", "item_tp": "J"},
    ]
    trns = ["H0STCNT0", "REAL", "STK_REAL", "stk_info", "S3_"]
    for trnm in trns:
        print(f"Testing trnm={trnm}")
        req = json.dumps({"trnm": trnm, "grp_no": "0001", "refresh": "1", "data": tps})
        try:
            api._ensure_ws()
            asyncio.run_coroutine_threadsafe(api._ws_conn.send(req), api._ws_loop).result(timeout=5)
            time.sleep(1)
        except Exception as e:
            print(e)
ws_test()
