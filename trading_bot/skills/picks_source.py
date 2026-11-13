# skills/picks_source.py - 추천 종목 출처 어댑터
"""Ddo invest의 서브 스크리닝(인사이트 스크리닝, messages.db의 insight_screening 표)을
vm_picks.json과 같은 형식의 파일로 옮긴다. config.VM_PICKS_SOURCE=insight 인 인스턴스 전용.

- invest DB는 읽기 전용으로만 연다. 쓰는 곳은 이 인스턴스의 DATA_DIR 안 파일 하나뿐.
- 매수 범위·손절가·1차 목표가가 없거나 순서가 맞지 않는 종목은 건너뛴다(손절 없는 매수 방지).
- 오늘 자 스크리닝이 없으면 파일 날짜가 어제로 남아 그날은 새 매수가 없다(스캔이 오늘 날짜만 채택).
- 실패는 전부 삼킨다 — 기존 파일이 그대로 남는다.
"""
import json
import logging
import os
import sqlite3

logger = logging.getLogger(__name__)
_last_key = {}   # out_path -> (date, created_at) 마지막으로 옮긴 스크리닝
_fail_n = [0]


def _num(v) -> int:
    try:
        return int(v) if v is not None and float(v) > 0 else 0
    except (TypeError, ValueError):
        return 0


def convert(payload: dict) -> dict:
    """스크리닝 payload → vm_picks 포맷 B. 가격 조건이 안 맞는 종목은 skipped로."""
    picks, skipped = [], []
    for k in payload.get("picks") or []:
        code = str(k.get("ticker") or "").strip()
        bmin, bmax = _num(k.get("buy_min")), _num(k.get("buy_max"))
        sl, tp1, tp2 = _num(k.get("stop_loss")), _num(k.get("take_profit_1")), _num(k.get("take_profit_2"))
        if not code or not (0 < sl < bmin <= bmax < tp1):
            skipped.append(str(k.get("name") or code))
            continue
        picks.append({
            "rank": k.get("rank") or len(picks) + 1,
            "name": k.get("name") or code,
            "code": code,
            "current_price": _num(k.get("price")),
            "buy_min": bmin, "buy_max": bmax,
            "take_profit_1": tp1,
            "take_profit_2": tp2 if tp2 > tp1 else 0,
            "stop_loss": sl,
            "holding_period": k.get("holding_period") or "",
            "holding_days": _num(k.get("holding_days")),
        })
    return {"date": payload.get("date"), "updated_at": payload.get("generated_at"),
            "source": "insight_screening", "picks": picks, "skipped": skipped}


def sync_insight_picks(db_path: str, out_path: str, data_dir: str) -> bool:
    """최신 스크리닝이 바뀌었으면 out_path에 다시 쓴다. 썼으면 True."""
    try:
        out_abs = os.path.abspath(out_path)
        if os.path.dirname(out_abs) != os.path.abspath(data_dir):
            # 다른 프로젝트의 vm_picks.json 등을 덮어쓰지 않도록 인스턴스 데이터 폴더 밖은 거부
            if _last_key.get(out_abs) != "refused":
                _last_key[out_abs] = "refused"
                logger.error(f"[PicksSource] 출력 경로가 데이터 폴더 밖이라 쓰지 않음: {out_abs}")
            return False
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
        try:
            row = conn.execute("SELECT date, created_at FROM insight_screening ORDER BY date DESC LIMIT 1").fetchone()
            if not row:
                return False
            key = (row[0], row[1])
            if _last_key.get(out_abs) == key and os.path.exists(out_abs):
                return False
            raw = conn.execute("SELECT payload FROM insight_screening WHERE date=?", (row[0],)).fetchone()
        finally:
            conn.close()
        data = convert(json.loads(raw[0]))
        if not data.get("date"):
            data["date"] = row[0]
        tmp = out_abs + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, out_abs)
        _last_key[out_abs] = key
        _fail_n[0] = 0
        logger.info(f"[PicksSource] 서브 스크리닝 {data['date']} → {os.path.basename(out_abs)}: "
                    f"{[p['name'] for p in data['picks']]}"
                    + (f" (가격 조건 미달로 제외: {data['skipped']})" if data["skipped"] else ""))
        return True
    except Exception as e:
        _fail_n[0] += 1
        if _fail_n[0] == 1 or _fail_n[0] % 20 == 0:      # 30초마다 불리므로 첫 번째와 20번째마다만 기록
            logger.warning(f"[PicksSource] 서브 스크리닝 동기화 실패 {_fail_n[0]}회(기존 파일 유지): {e}")
        return False
