# run_scan.py - 종목 스캔 단독 실행 (REST API 버전)
# 실행: python run_scan.py
# Docker: docker-compose run --rm trading_bot python run_scan.py

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config
from logger import setup_logger
from kiwoom_api import KiwoomAPI
from skills.scan_skill import StockScanner

logger = setup_logger("run_scan")


def main():
    print("=" * 65)
    print("  키움 REST API 종목 스캔")
    print(f"  모드: {'모의투자' if config.IS_SIMULATION else '실계좌'}")
    print(f"  계좌: {config.ACCOUNT_NUMBER}")
    print("=" * 65)

    # REST API 로그인 (토큰 발급)
    kiwoom = KiwoomAPI()
    if not kiwoom.login():
        print("[오류] 토큰 발급 실패 - APP_KEY/APP_SECRET 확인 필요")
        print("  환경변수: KIWOOM_APP_KEY, KIWOOM_APP_SECRET")
        sys.exit(1)

    print("[로그인] 토큰 발급 성공")

    # 스캔 실행
    scanner = StockScanner(kiwoom)

    print("\n종목 스캔 시작 (코스닥 전 종목)...")
    print("-" * 65)

    try:
        candidates = scanner.run_scan(market="10")
    except Exception as e:
        import traceback
        print(f"\n[오류] 스캔 실패: {e}")
        traceback.print_exc()
        sys.exit(1)

    # 결과 출력
    print("\n" + "=" * 65)
    print(f"  ★ 최종 후보 종목: {len(candidates)}개")
    print("=" * 65)

    if candidates:
        print(f"{'순위':>3}  {'종목명':10s}  {'코드':6s}  {'현재가':>8s}  "
              f"{'거래대금(억)':>10s}  {'20일고점대비':>10s}")
        print("-" * 65)
        for i, c in enumerate(candidates, 1):
            print(
                f"{i:>3}  {c['name']:10s}  {c['code']:6s}  "
                f"{c['current_price']:>8,}  "
                f"{c['avg_amount']/1e8:>10.1f}  "
                f"{c['dist_20']:>9.2%}"
            )
    else:
        print("  → 오늘은 조건에 맞는 종목이 없습니다.")

    print("=" * 65)


if __name__ == "__main__":
    main()
