#!/usr/bin/env python3
"""Export the local radar watchlist for the strategy configurator page."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB_PATH = ROOT / "accumulation.db"
OUT_PATH = ROOT / "web" / "radar_watchlist.json"
EMBED_PATH = ROOT / "web" / "radar_watchlist_embed.js"


def main():
    if not DB_PATH.exists():
        raise SystemExit(f"database not found: {DB_PATH}")

    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute(
        """SELECT symbol, score, status
           FROM watchlist
           ORDER BY score DESC"""
    ).fetchall()
    conn.close()

    payload = {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "source": str(DB_PATH.name),
        "symbols": [
            {
                "symbol": symbol,
                "score": round(float(score or 0), 4),
                "status": status or "",
            }
            for symbol, score, status in rows
        ],
    }
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    EMBED_PATH.write_text(
        "window.RADAR_WATCHLIST = "
        + json.dumps(payload["symbols"], ensure_ascii=False, separators=(",", ":"))
        + ";\n",
        encoding="utf-8",
    )
    print(f"exported {len(payload['symbols'])} symbols -> {OUT_PATH}")
    print(f"exported browser embed -> {EMBED_PATH}")


if __name__ == "__main__":
    main()
