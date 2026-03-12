import config

print("=== 현재 설정 ===")
print("CONDITION_NAME  :", repr(config.CONDITION_NAME))
print("CONDITION_SEQ   :", repr(config.CONDITION_SEQ))
print("CONDITION_FALLBACK:", config.CONDITION_FALLBACK)
print("CONDITION_STEX_TP:", config.CONDITION_STEX_TP)

use = bool(config.CONDITION_NAME.strip() or config.CONDITION_SEQ.strip())
print()
print("use_condition =", use)
if use:
    print("→ 조건식 경로 시도 (ka10171 → ka10172 → ka10081)")
else:
    print("→ 폴백 경로 사용 (ka10099 → ka10081)")

print()

# 조건식 이름 설정 시 동작 시뮬레이션
print("=== 시뮬레이션: CONDITION_NAME='고점근접전략' 설정 시 ===")
config.CONDITION_NAME = "고점근접전략"
use2 = bool(config.CONDITION_NAME.strip() or config.CONDITION_SEQ.strip())
print("use_condition =", use2)
if use2:
    print("→ 조건식 경로 시도 (ka10171 → ka10172 → ka10081)")
else:
    print("→ 폴백 경로 사용 (ka10099 → ka10081)")

print()
print("=== 시뮬레이션: CONDITION_SEQ='3' 설정 시 ===")
config.CONDITION_NAME = ""
config.CONDITION_SEQ  = "3"
use3 = bool(config.CONDITION_NAME.strip() or config.CONDITION_SEQ.strip())
print("use_condition =", use3)
if use3:
    print("→ 조건식 경로 시도 (seq=3 직접 지정)")
