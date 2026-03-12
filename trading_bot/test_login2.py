# test_login2.py - 모의투자 서버 로그인 테스트 (에러코드 상세 출력)
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
    err_map = {
        0:    "로그인 성공",
        -100: "사용자 정보교환 실패",
        -101: "서버접속 실패 (네트워크 또는 서버 문제)",
        -102: "버전처리 실패",
        -103: "개인방화벽 실패",
        -104: "메모리 보호 실패",
        -105: "함수입력값 오류",
        -106: "통신연결 종료",
    }
    msg = err_map.get(err_code, f"알 수 없는 오류 코드: {err_code}")
    print(f"\n[OnEventConnect] 에러코드={err_code} → {msg}")

    if err_code == 0:
        server = ocx.dynamicCall("KOA_Functions(QString, QString)", "GetServerGubun", "")
        print(f"접속 서버: {'모의투자' if server == '1' else '실서버'} (raw={server})")
        accounts = ocx.dynamicCall("GetLoginInfo(QString)", "ACCNO")
        print(f"계좌목록: {accounts}")
        user_id = ocx.dynamicCall("GetLoginInfo(QString)", "USER_ID")
        print(f"사용자ID: {user_id}")
    loop.exit()

def on_timeout():
    print("\n[타임아웃] 60초 동안 응답 없음 - 팝업이 뜨지 않거나 응답을 기다리는 중")
    loop.exit()

ocx.OnEventConnect.connect(on_event_connect)

# 모의투자 서버 설정
ret = ocx.dynamicCall("KOA_Functions(QString, QString)", "SetInfoData", "1")
print(f"SetInfoData('1') 반환값: {ret}")

print("CommConnect() 호출 중...")
print("→ 로그인 팝업창이 떠야 합니다. 팝업이 보이면 모의투자 계정으로 로그인하세요.")
print("→ 팝업이 안 보이면 작업표시줄을 확인하세요.\n")
ocx.dynamicCall("CommConnect()")

# 최대 120초 대기
QTimer.singleShot(120000, on_timeout)
loop.exec_()
print("\n스크립트 종료")
