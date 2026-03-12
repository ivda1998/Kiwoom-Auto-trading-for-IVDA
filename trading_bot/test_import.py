import sys
print(f"Python: {sys.version}")

tests = [
    ("PyQt5.QtWidgets", "QApplication"),
    ("PyQt5.QtCore", "QTimer, QEventLoop"),
    ("PyQt5.QAxContainer", "QAxWidget"),
    ("pandas", "DataFrame"),
    ("numpy", "array"),
]

all_ok = True
for module, items in tests:
    try:
        m = __import__(module, fromlist=[items])
        print(f"  OK  {module}")
    except ImportError as e:
        print(f"  NG  {module}: {e}")
        all_ok = False

print()
if all_ok:
    print("모든 import 성공 - main.py 실행 준비 완료!")
else:
    print("일부 모듈 실패 - 위 내용 확인 필요")
