# logger.py - 로그 설정 + 거래 내역 DB 저장

import logging
import logging.handlers
import sqlite3
import os
from datetime import datetime

import config


# ─────────────────────────────────────────
# 로그 설정
# ─────────────────────────────────────────
def setup_logger(name: str = "trading_bot") -> logging.Logger:
    """
    파일 + 콘솔 동시 출력 로거 설정.
    루트 로거에 핸들러를 추가하여 kiwoom_api 등 모든 모듈 로그가
    같은 파일에 기록되도록 한다.
    """
    os.makedirs(config.LOG_DIR, exist_ok=True)

    level = getattr(logging, config.LOG_LEVEL, logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # ── 루트 로거에 핸들러 등록 (모든 모듈 로그 캡처) ──
    root = logging.getLogger()
    root.setLevel(level)

    if not root.handlers:
        # 콘솔 핸들러
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        root.addHandler(ch)

        # 날짜별 파일 핸들러 (자정에 롤오버)
        log_file = os.path.join(
            config.LOG_DIR,
            f"trading_{datetime.now().strftime('%Y%m%d')}.log"
        )
        fh = logging.handlers.TimedRotatingFileHandler(
            log_file, when="midnight", backupCount=30, encoding="utf-8"
        )
        fh.setFormatter(fmt)
        root.addHandler(fh)

    # ── named logger 반환 (핸들러 없이 루트로 propagate) ──
    logger = logging.getLogger(name)
    logger.setLevel(level)
    # 이전에 직접 핸들러가 붙어 있으면 제거 (중복 출력 방지)
    for h in list(logger.handlers):
        logger.removeHandler(h)

    return logger


# ─────────────────────────────────────────
# 거래 내역 DB
# ─────────────────────────────────────────
class TradeLogger:
    """
    SQLite 기반 거래 내역 저장
    """

    def __init__(self, db_path: str = None):
        self.db_path = db_path or config.DB_PATH
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS trades (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_date  TEXT NOT NULL,
                    trade_time  TEXT NOT NULL,
                    code        TEXT NOT NULL,
                    name        TEXT,
                    side        TEXT NOT NULL,   -- BUY / SELL
                    qty         INTEGER NOT NULL,
                    price       REAL NOT NULL,
                    amount      REAL NOT NULL,
                    pnl         REAL DEFAULT 0,
                    pnl_rate    REAL DEFAULT 0,
                    reason      TEXT,
                    created_at  TEXT DEFAULT (datetime('now','localtime'))
                )
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS daily_summary (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    trade_date      TEXT UNIQUE,
                    total_trades    INTEGER DEFAULT 0,
                    win_count       INTEGER DEFAULT 0,
                    lose_count      INTEGER DEFAULT 0,
                    total_pnl       REAL DEFAULT 0,
                    win_rate        REAL DEFAULT 0,
                    created_at      TEXT DEFAULT (datetime('now','localtime'))
                )
            """)
            conn.commit()

    def log_trade(self, code: str, name: str, side: str,
                  qty: int, price: float, pnl: float = 0.0,
                  pnl_rate: float = 0.0, reason: str = ""):
        now = datetime.now()
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                INSERT INTO trades
                (trade_date, trade_time, code, name, side,
                 qty, price, amount, pnl, pnl_rate, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                now.strftime("%Y-%m-%d"),
                now.strftime("%H:%M:%S"),
                code, name, side,
                qty, price, qty * price,
                pnl, pnl_rate, reason
            ))
            conn.commit()

    def update_daily_summary(self, trade_date: str = None):
        """일별 요약 집계"""
        if trade_date is None:
            trade_date = datetime.now().strftime("%Y-%m-%d")

        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("""
                SELECT pnl FROM trades
                WHERE trade_date = ? AND side = 'SELL'
            """, (trade_date,)).fetchall()

            total = len(rows)
            wins = sum(1 for r in rows if r[0] > 0)
            loses = sum(1 for r in rows if r[0] <= 0)
            total_pnl = sum(r[0] for r in rows)
            win_rate = wins / total if total > 0 else 0.0

            conn.execute("""
                INSERT INTO daily_summary
                (trade_date, total_trades, win_count, lose_count,
                 total_pnl, win_rate)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(trade_date) DO UPDATE SET
                    total_trades = excluded.total_trades,
                    win_count    = excluded.win_count,
                    lose_count   = excluded.lose_count,
                    total_pnl    = excluded.total_pnl,
                    win_rate     = excluded.win_rate
            """, (trade_date, total, wins, loses, total_pnl, win_rate))
            conn.commit()

        return {
            "date": trade_date,
            "total": total,
            "wins": wins,
            "loses": loses,
            "total_pnl": total_pnl,
            "win_rate": win_rate
        }

    def get_today_trades(self) -> list:
        trade_date = datetime.now().strftime("%Y-%m-%d")
        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("""
                SELECT trade_time, code, name, side, qty, price, pnl, reason
                FROM trades WHERE trade_date = ?
                ORDER BY trade_time
            """, (trade_date,)).fetchall()
        return rows
