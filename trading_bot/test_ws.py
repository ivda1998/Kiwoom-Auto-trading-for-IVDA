import sys, os, time, asyncio
sys.path.append('g:/autoTrade/trading_bot')
import config
config.IS_SIMULATION = True
from kiwoom_api import KiwoomAPI
import logging

logging.basicConfig(level=logging.DEBUG)

def ws_test():
    api = KiwoomAPI()
    api.login()
    print("Testing get_condition_list")
    c = api.get_condition_list()
    if c:
        seq = c[0]['seq']
        print("Registering condition:", seq)
        api.register_condition_realtime(seq)
        
    for i in range(20):
        time.sleep(1)
        print("Waiting...", i)

ws_test()
