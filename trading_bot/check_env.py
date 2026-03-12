import sys, ast, os

print("=" * 50)
print(f"Python: {sys.version}")
print(f"비트: {'64bit' if sys.maxsize > 2**32 else '32bit'}")
print(f"경로: {sys.executable}")
print("=" * 50)

# 필수 패키지 확인
packages = ["PyQt5", "pandas", "numpy"]
print("\n[패키지 확인]")
for pkg in packages:
    try:
        m = __import__(pkg)
        ver = getattr(m, "__version__", "버전미상")
        print(f"  OK  {pkg} {ver}")
    except ImportError:
        print(f"  NG  {pkg} -- 미설치")

# 키움 OCX 등록 여부
print("\n[키움 OpenAPI OCX 확인]")
try:
    import winreg
    key = winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "KHOPENAPI.KHOpenAPICtrl.1")
    winreg.CloseKey(key)
    print("  OK  KHOPENAPI.KHOpenAPICtrl.1 등록됨")
except Exception as e:
    print(f"  NG  키움 OCX 미등록: {e}")

# 소스 파일 문법 검사
print("\n[소스 문법 검사]")
base = os.path.dirname(os.path.abspath(__file__))
files = [
    "config.py","logger.py","kiwoom_api.py","scanner.py",
    "data_handler.py","strategy.py","execution.py",
    "risk_manager.py","backtest_engine.py","main.py"
]
all_ok = True
for f in files:
    path = os.path.join(base, f)
    try:
        with open(path, encoding="utf-8") as fh:
            ast.parse(fh.read())
        print(f"  OK  {f}")
    except SyntaxError as e:
        print(f"  ERR {f}: line {e.lineno} - {e.msg}")
        all_ok = False
    except FileNotFoundError:
        print(f"  --  {f} (파일없음)")

print("\n" + "=" * 50)
print("결과:", "전체 정상" if all_ok else "오류 있음 -- 위 내용 확인")
print("=" * 50)
