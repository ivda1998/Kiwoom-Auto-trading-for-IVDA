import sys, os, time, asyncio
sys.path.append('g:/autoTrade/trading_bot')
import config
config.IS_SIMULATION = True
from kiwoom_api import KiwoomAPI
import logging

logging.basicConfig(level=logging.INFO)

def ws_test():
    api = KiwoomAPI()
    api.login()
    print("Logged in. Subscribing to 1 stock...")
    
    # Override dispatcher to print raw
    old = api._dispatch_ws_message
    def new_dispatch(msg):
        print("RAW MSG:", msg)
        old(msg)
    api._dispatch_ws_message = new_dispatch

    api.subscribe_realtime("005930")
    print("Subscribed 005930")
        
    for i in range(10):
        time.sleep(1)

ws_test()
