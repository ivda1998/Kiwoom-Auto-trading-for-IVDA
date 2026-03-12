@echo off
set KIWOOM_APP_KEY=JtXjudRggDydX1uL_5dAGVTCOIDL1FPo2GCXutrKedQ
set KIWOOM_APP_SECRET=tMe4rEj5ADZzvNe7HQKytnk20h1k9jXG0tMnQL2Wvag
set KIWOOM_ACCOUNT=81202949
set IS_SIMULATION=true
set KIWOOM_BASE_URL=https://mockapi.kiwoom.com
set KIWOOM_WS_URL=wss://mockapi.kiwoom.com:10000/api/dostk/websocket
python G:\autoTrade\trading_bot\main.py
pause
