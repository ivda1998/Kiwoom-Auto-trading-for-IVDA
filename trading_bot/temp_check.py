import sys, os
sys.path.append('g:/autoTrade/trading_bot')
import config
config.IS_SIMULATION = True
from kiwoom_api import KiwoomAPI

api = KiwoomAPI()
api.login()

res_q = api.get_stock_info("Q610071")
print("Result for 'Q610071':", res_q)

res_num = api.get_stock_info("610071")
print("Result for '610071':", res_num)

import asyncio
# Close any lingering WS loop nicely
if api._ws_loop:
    api._ws_loop.call_soon_threadsafe(api._ws_loop.stop)

