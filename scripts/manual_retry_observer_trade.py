#!/usr/bin/env python3
import sqlite3
import sys
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

BASE_DIR = Path("/Users/sabrina0x/accumulation-radar")

env_file = Path(os.getenv("RADAR_ENV_FILE", ""))
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())

sys.path.insert(0, str(BASE_DIR))

import accumulation_radar as radar  # noqa: E402


def load_candidate(conn, symbol: str):
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        """
        SELECT symbol, candidate_time, source, source_reason, radar_status, radar_score,
               strength, confidence, reference_price, snapshot_price, snapshot_oi_d1h_pct,
               snapshot_oi_d6h_pct, snapshot_funding_rate, est_mcap, watch_status
        FROM entry_watchlist
        WHERE symbol = ?
        """,
        (symbol,),
    ).fetchone()
    return dict(row) if row else None


def main():
    if len(sys.argv) < 2:
        print("Usage: manual_retry_observer_trade.py <SYMBOL>")
        raise SystemExit(1)

    symbol = sys.argv[1].upper()
    db_path = Path(os.getenv("ACCUMULATION_DB_PATH", str(BASE_DIR / "accumulation.db")))
    conn = sqlite3.connect(str(db_path))
    candidate = load_candidate(conn, symbol)
    if not candidate:
        print(f"[RETRY] {symbol} not found in entry_watchlist")
        raise SystemExit(2)

    analysis = radar.analyze_observation_candidate(candidate)
    signal = analysis.get("trigger_signal")
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")

    conn.execute(
        """UPDATE entry_watchlist
           SET last_analysis_time = ?, watch_status = ?, trend = ?, setup_type = ?,
               support_price = ?, resistance_price = ?, suggested_entry = ?, suggested_stop = ?, notes = ?
           WHERE symbol = ?""",
        (
            now,
            analysis.get("watch_status"),
            analysis.get("trend"),
            analysis.get("setup_type"),
            analysis.get("support_price"),
            analysis.get("resistance_price"),
            analysis.get("suggested_entry"),
            analysis.get("suggested_stop"),
            analysis.get("notes"),
            symbol,
        ),
    )
    conn.commit()

    if not signal:
        print(f"[RETRY] {symbol} not triggerable now | {analysis.get('setup_type')} | {analysis.get('notes')}")
        raise SystemExit(3)

    accepted = radar.emit_trade_signals([signal])
    if not accepted:
        print(f"[RETRY] {symbol} retry sent but not executed/accepted")
        raise SystemExit(4)

    for item in accepted:
        conn.execute(
            "UPDATE entry_watchlist SET watch_status = 'TRIGGERED', trigger_time = ? WHERE symbol = ?",
            (now, item["symbol"]),
        )
        radar.track_executed_observer_position(conn, item["signal"], item.get("response", {}))
    conn.commit()
    print(f"[RETRY] {symbol} retry accepted")


if __name__ == "__main__":
    main()
