# skills/nh_status.py - 나무(NH) 봇 현황 조회 (텔레그램 /nh 명령용, 읽기 전용)
"""나무 봇은 키움 봇과 같은 텔레그램 토큰을 쓰고 명령 수신(폴링)을 끈 상태라,
키움 봇이 나무 인스턴스 폴더의 파일을 읽기만 해서 현황을 보여준다.

읽는 것 (전부 읽기 전용, 아무것도 쓰지 않음):
  data/heartbeat.txt, risk_state.json, leverage_samsung_pos.json, leverage_index_pos.json,
  leverage_index_halt.json, vm_positions.json, vm_pending_orders.json, vm_picks_insight.json,
  trade_history.db(mode=ro), logs/*.log
현재가는 호출하는 쪽이 price_fn(code) -> int 로 넘긴다(없으면 0 → 진입가로 대체).
"""
import html
import json
import os
import sqlite3
import subprocess
from datetime import datetime

MAX_LEN = 3900          # 텔레그램 4096자 제한 여유
MAX_PRICE_LOOKUPS = 8   # 현재가 조회 종목 수 상한(키움 API 예산 보호)


def _e(s) -> str:
    return html.escape(str(s if s is not None else ""))


def _read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _service_state(name: str = "trading_bot_nh") -> str:
    try:
        r = subprocess.run(["systemctl", "is-active", name], capture_output=True, text=True, timeout=3)
        return (r.stdout or r.stderr).strip() or "unknown"
    except Exception:
        return "확인 불가"


def _heartbeat(data_dir: str, now: datetime):
    """(마지막 하트비트 시각 문자열, 경과 초) — 없으면 (None, None)."""
    try:
        with open(os.path.join(data_dir, "heartbeat.txt"), "r", encoding="utf-8") as f:
            ts = datetime.fromisoformat(f.read().strip())
        return ts.strftime("%H:%M:%S"), max(0.0, (now - ts).total_seconds())
    except Exception:
        return None, None


def _trades(db_path: str, today: str, month_start: str):
    """(오늘 체결 목록, 오늘 실현손익, 이번 달 실현손익, 이번 달 매도 건수)."""
    if not os.path.exists(db_path):
        return [], 0.0, 0.0, 0
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2)
    try:
        rows = conn.execute(
            "SELECT trade_time, code, name, side, qty, price, pnl, pnl_rate, reason FROM trades "
            "WHERE trade_date=? ORDER BY id", (today,)).fetchall()
        m = conn.execute(
            "SELECT COALESCE(SUM(pnl),0), COUNT(*) FROM trades WHERE side='SELL' AND trade_date>=?",
            (month_start,)).fetchone()
    finally:
        conn.close()
    today_pnl = sum(r[6] or 0 for r in rows if r[3] == "SELL")
    return rows, today_pnl, float(m[0] or 0), int(m[1] or 0)


def _price(price_fn, code, fallback, budget):
    if price_fn is None or budget[0] <= 0:
        return fallback, False
    budget[0] -= 1
    try:
        p = int(price_fn(code) or 0)
    except Exception:
        p = 0
    return (p, True) if p > 0 else (fallback, False)


def build_nh_status(inst_dir: str, price_fn=None, now: datetime = None, service_fn=None) -> str:
    now = now or datetime.now()
    data_dir = os.path.join(inst_dir, "data")
    if not os.path.isdir(data_dir):
        return f"🌳 나무 봇 폴더를 찾을 수 없음: {_e(data_dir)}"
    today_iso = now.strftime("%Y-%m-%d")
    lines = [f"🌳 <b>나무 봇 현황 [{now.strftime('%H:%M:%S')}]</b>"]
    budget = [MAX_PRICE_LOOKUPS]

    # ── 가동 상태 ──────────────────────────────────────────────
    svc = (service_fn or _service_state)()
    hb, age = _heartbeat(data_dir, now)
    if hb is None:
        hb_str = "하트비트 없음"
    else:
        stale = age > 180
        hb_str = f"하트비트 {hb} ({int(age // 60)}분 {int(age % 60)}초 전){' ⚠️' if stale else ''}"
    svc_icon = "🟢" if svc == "active" else "🔴"
    lines.append(f"{svc_icon} 서비스: {_e(svc)} | {hb_str}")

    # ── 손익 ─────────────────────────────────────────────────
    month_start = now.strftime("%Y-%m-01")
    try:
        rows, today_pnl, month_pnl, month_n = _trades(os.path.join(data_dir, "trade_history.db"),
                                                      today_iso, month_start)
        n_buy = sum(1 for r in rows if r[3] == "BUY")
        n_sell = len(rows) - n_buy
        lines.append(f"💰 오늘 실현손익: <b>{today_pnl:+,.0f}원</b> (매수 {n_buy}·매도 {n_sell}건) | "
                     f"이번 달 {month_pnl:+,.0f}원 ({month_n}건)")
    except Exception as ex:
        rows = []
        lines.append(f"💰 손익 조회 실패: {_e(ex)}")
    halt = _read_json(os.path.join(data_dir, "leverage_index_halt.json")) or {}
    if halt.get("date") == today_iso and halt.get("reason"):
        lines.append(f"⛔ 지수 전략 오늘 정지: {_e(halt['reason'])}")
    risk = _read_json(os.path.join(data_dir, "risk_state.json")) or {}
    if risk.get("date") == today_iso and risk.get("is_halted"):
        lines.append("⛔ 리스크 한도로 오늘 신규 매수 정지")

    # ── 레버리지 포지션 ──────────────────────────────────────
    lines.append("\n⚡ <b>레버리지</b>")
    any_lev = False
    for label, fname in (("삼성", "leverage_samsung_pos.json"), ("지수", "leverage_index_pos.json")):
        pos = _read_json(os.path.join(data_dir, fname))
        if not pos:
            continue
        any_lev = True
        entry, qty = int(pos.get("entry_price") or 0), int(pos.get("qty") or 0)
        cur, live = _price(price_fn, pos.get("code"), entry, budget)
        pnl = (cur - entry) * qty
        rate = (cur / entry - 1) if entry else 0.0
        side = "롱" if pos.get("direction") in ("LEVERAGE", "LONG") else "숏"
        carry = f" 🌙 이월({_e(pos['carry_date'])})" if pos.get("carry_date") else ""
        cur_str = f"{cur:,}원" if live else "현재가 없음"
        lines.append(
            f"  {'▲' if pnl >= 0 else '▼'} [{label}·{side}] {_e(pos.get('name'))} {qty}주{carry}\n"
            f"    진입 {entry:,} → {cur_str} | {pnl:+,.0f}원 ({rate:+.2%})\n"
            f"    진입시각 {_e(str(pos.get('entered_at', ''))[5:16].replace('T', ' '))}"
            f" | SL {int(pos.get('stop_loss') or 0):,}")
    if not any_lev:
        lines.append("  보유 없음")

    # ── VM 지정종목 포지션 ───────────────────────────────────
    vm = _read_json(os.path.join(data_dir, "vm_positions.json")) or []
    lines.append(f"\n🤖 <b>지정종목 보유</b>")
    if not vm:
        lines.append("  보유 없음")
    else:
        by_code = {}
        for p in vm:
            g = by_code.setdefault(p.get("code"), {"name": p.get("name"), "qty": 0, "cost": 0,
                                                   "sl": p.get("stop_loss"), "tp": p.get("take_profit"),
                                                   "since": p.get("created_at")})
            q = int(p.get("qty") or 0)
            g["qty"] += q
            g["cost"] += q * int(p.get("entry_price") or 0)
            g["since"] = min(g["since"] or "", p.get("created_at") or "") or g["since"]
        total = 0
        for code, g in by_code.items():
            avg = g["cost"] / g["qty"] if g["qty"] else 0
            cur, live = _price(price_fn, code, int(avg), budget)
            pnl = (cur - avg) * g["qty"]
            total += pnl
            cur_str = f"{cur:,}원" if live else "현재가 없음"
            lines.append(
                f"  {'▲' if pnl >= 0 else '▼'} {_e(g['name'])}({code}) {g['qty']}주 · {_e(g['since'])}~\n"
                f"    평단 {avg:,.0f} → {cur_str} | {pnl:+,.0f}원 ({(cur / avg - 1) if avg else 0:+.2%})\n"
                f"    손절 {int(g['sl'] or 0):,} | 목표 {int(g['tp'] or 0):,}")
        lines.append(f"  평가손익 합계(세전): {total:+,.0f}원")

    pend = [o for o in (_read_json(os.path.join(data_dir, "vm_pending_orders.json")) or [])
            if not o.get("filled")]
    if pend:
        lines.append(f"\n⏳ <b>미체결 매수 {len(pend)}건</b>")
        for o in pend[:8]:
            lines.append(f"  {_e(o.get('name'))} {o.get('tranche', '')}차 {int(o.get('price') or 0):,}원 × "
                         f"{o.get('qty')}주 ({_e(o.get('created_at'))})")

    picks = _read_json(os.path.join(data_dir, "vm_picks_insight.json"))
    if picks:
        names = ", ".join(_e(p.get("name")) for p in picks.get("picks", [])) or "없음"
        skipped = f" | 제외: {', '.join(map(_e, picks['skipped']))}" if picks.get("skipped") else ""
        lines.append(f"\n📋 추천 출처(서브 스크리닝) {_e(picks.get('date'))}: {names}{skipped}")

    # ── 오늘 체결 ────────────────────────────────────────────
    if rows:
        lines.append(f"\n🧾 <b>오늘 체결 (최근 {min(len(rows), 8)}건)</b>")
        for t, code, name, side, qty, price, pnl, rate, reason in rows[-8:]:
            tail = f" {pnl:+,.0f}원({(rate or 0):+.2%})" if side == "SELL" else ""
            lines.append(f"  {t[:5]} {'매수' if side == 'BUY' else '매도'} {_e(name)} {qty}주 "
                         f"@{price:,.0f}{tail} · {_e(reason)}")

    msg = "\n".join(lines)
    return msg if len(msg) <= MAX_LEN else msg[:MAX_LEN] + "\n…(생략)"


def build_nh_log(inst_dir: str, n: int = 15) -> str:
    log_dir = os.path.join(inst_dir, "logs")
    try:
        files = [os.path.join(log_dir, f) for f in os.listdir(log_dir) if f.endswith(".log")]
    except Exception:
        files = []
    if not files:
        return "🌳 나무 봇 로그 파일 없음"
    path = max(files, key=os.path.getmtime)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            tail = f.readlines()[-n:]
    except Exception as ex:
        return f"🌳 나무 로그 읽기 실패: {_e(ex)}"
    body = _e("".join(line[:200] for line in tail))[:3500]
    return f"🌳 <b>나무 봇 로그 {_e(os.path.basename(path))} (최근 {len(tail)}줄)</b>\n<code>{body}</code>"
