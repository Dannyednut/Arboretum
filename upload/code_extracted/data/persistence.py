import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from core.legacy_models import BasisSignal
from core.models import ExecutionReport

logger = logging.getLogger("Persistence")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class PersistenceManager:
    def __init__(self, db_path: str = "bot_data.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS signals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    pair TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    basis_pct REAL NOT NULL,
                    funding_rate_pct REAL NOT NULL,
                    net_yield_pct REAL NOT NULL,
                    notional_usd REAL NOT NULL,
                    reason TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS executions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    pair TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    accepted INTEGER NOT NULL,
                    dry_run INTEGER NOT NULL,
                    fully_filled INTEGER NOT NULL,
                    error TEXT,
                    raw_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS risk_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    pair TEXT,
                    allowed INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    raw_json TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS funding_accruals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    pair TEXT NOT NULL,
                    amount REAL NOT NULL
                )
                """
            )
            conn.commit()

    def log_signal(self, signal: BasisSignal) -> int:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO signals
                (timestamp, pair, direction, basis_pct, funding_rate_pct, net_yield_pct, notional_usd, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    signal.pair.key,
                    signal.direction.value,
                    signal.basis_pct,
                    signal.funding_rate_pct,
                    signal.net_yield_pct,
                    signal.notional_usd,
                    signal.reason,
                ),
            )
            return int(cursor.lastrowid)

    def log_execution(self, report: ExecutionReport) -> int:
        payload = {
            "legs": [leg.__dict__ for leg in report.legs],
            "raw_response": report.raw_response,
        }
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO executions
                (timestamp, pair, direction, accepted, dry_run, fully_filled, error, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now(),
                    report.signal.pair.key,
                    report.signal.direction.value,
                    int(report.accepted),
                    int(report.dry_run),
                    int(report.fully_filled),
                    report.error,
                    json.dumps(payload, sort_keys=True),
                ),
            )
            return int(cursor.lastrowid)

    def log_risk_event(
        self, pair: Optional[str], allowed: bool, reason: str, raw: Optional[Dict[str, Any]] = None
    ) -> int:
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.execute(
                """
                INSERT INTO risk_events (timestamp, pair, allowed, reason, raw_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                (utc_now(), pair, int(allowed), reason, json.dumps(raw or {}, sort_keys=True)),
            )
            return int(cursor.lastrowid)

    def log_trade(self, pair: str, side: str, spot_px: float, perp_px: float, size_usd: float, net_yield: float):
        logger.warning("log_trade is deprecated; use log_signal and log_execution")

    def log_funding(self, pair: str, amount: float) -> None:
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT INTO funding_accruals (timestamp, pair, amount) VALUES (?, ?, ?)",
                (utc_now(), pair, amount),
            )
