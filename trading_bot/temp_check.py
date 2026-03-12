import sys, os
sys.path.append('g:/autoTrade/trading_bot')
import config
config.IS_SIMULATION = True
from kiwoom_api import KiwoomAPI

api = KiwoomAPI()
api.login()
data = api._post('/api/dostk/chart', {'stk_cd': '005930', 'base_dt': '00000000', 'upd_stkpc_tp': '1'}, 'ka10081')
items = data.get('stk_dt_pole_chart_qry', [])
if items:
    print("Item keys:", items[0].keys())
    print("Item:", items[0])
else:
    print("No items", data)
