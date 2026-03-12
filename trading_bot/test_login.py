import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PyQt5.QtWidgets import QApplication
from PyQt5.QAxContainer import QAxWidget
from PyQt5.QtCore import QEventLoop, QTimer

app = QApplication(sys.argv)
ocx = QAxWidget("KHOPENAPI.KHOpenAPICtrl.1")
loop = QEventLoop()

def on_event_connect(err_code):
    print(f"[OnEventConnect] err_code = {err_code}")
    if err_code == 0:
        print("로그인 성공!")
        # 접속 서버 확인
        server = ocx.dynamicCall("KOA_Functions(QString, QString)", "GetServerGubun", "")
        print(f"서버 구분: {server} (0=실서버, 1=모의투자)")
        # 계좌 목록
        accounts = ocx.dynamicCall("GetLoginInfo(QString)", "ACCNO")
        print(f"계좌 목록: {accounts}")
        user_id = ocx.dynamicCall("GetLoginInfo(QString)", "USER_ID")
        print(f"사용자 ID: {user_id}")
    else:
        codes = {
            -100: "사용자 정보교환 실패",
            -101: "서버접속 실패",
            -102: "버전처리 실패",
        }
        print(f"로그인 실패: {codes.get(err_code, f'알 수 없는 오류({err_code})')}")
    loop.exit()

ocx.OnEventConnect.connect(on_event_connect)

# 모의투자 서버로 설정
print("모의투자 서버 설정...")
ocx.dynamicCall("KOA_Functions(QString, QString)", "SetInfoData", "1")

print("CommConnect() 호출 - 로그인 팝업이 뜹니다...")
ocx.dynamicCall("CommConnect()")

# 최대 60초 대기
QTimer.singleShot(60000, loop.exit)
loop.exec_()

print("완료")
app.quit()
