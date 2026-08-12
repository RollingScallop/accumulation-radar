#!/usr/bin/env python3
import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.error import URLError, HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE_DIR = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.getenv("ACCUMULATION_DB_PATH", str(BASE_DIR / "accumulation.db")))
STATE_PATH = BASE_DIR / ".monitor_state.json"
STATUS_LOG = BASE_DIR / "monitor_status.log"
API_BASE = os.getenv("AI_TRADER_API_BASE", "http://127.0.0.1:3333/api").rstrip("/")
SYNC_INTERVAL_SEC = int(os.getenv("LOCAL_MONITOR_SYNC_INTERVAL_SEC", "180"))


def load_env_file(path):
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env_file(BASE_DIR / ".env.oi")


# --- 自愈：2026-07-27 远程写回文件时丢了执行权限，这里自动补回 ---
def _selfheal_exec_bits():
    import stat
    targets = [
        BASE_DIR / "scripts" / "run_radar_task.sh",
        Path("/Users/sabrina0x/radar-dashboard/scripts/sync-db-to-railway.sh"),
        Path("/Users/sabrina0x/ai project/ai-trader/backend/scripts/start-demo.sh"),
    ]
    for t in targets:
        try:
            if t.exists():
                mode = t.stat().st_mode
                if not (mode & stat.S_IXUSR):
                    t.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        except Exception:
            pass


_selfheal_exec_bits()


def cst_now():
    return datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")


def log(message):
    line = f"[{cst_now()}] {message}"
    print(line)
    with STATUS_LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_state():
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_state(state):
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def api_get(path, timeout=15):
    url = f"{API_BASE}{path}"
    req = Request(url, headers={"Accept": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return True, json.loads(body)
    except HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        return False, {"status": exc.code, "body": body}
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        return False, {"error": str(exc)}


def telegram_notify(message):
    token = os.getenv("TG_BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TG_OBSERVER_CHAT_ID") or os.getenv("TG_CHAT_ID") or os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        return False

    data = urlencode({"chat_id": chat_id, "text": message}).encode("utf-8")
    req = Request(f"https://api.telegram.org/bot{token}/sendMessage", data=data, method="POST")
    try:
        with urlopen(req, timeout=10) as resp:
            resp.read()
        return True
    except Exception as exc:
        log(f"TG notify failed: {exc}")
        return False


def notify_once(key, message):
    state = load_state()
    notified = set(state.get("notified", []))
    if key in notified:
        return
    if telegram_notify(message):
        notified.add(key)
        state["notified"] = sorted(notified)
        save_state(state)


def ensure_position_table(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS position_watchlist (
            symbol TEXT PRIMARY KEY,
            source TEXT,
            side TEXT,
            entry_price REAL,
            stop_price REAL,
            reference_support REAL,
            quantity REAL,
            leverage REAL,
            status TEXT DEFAULT 'ACTIVE',
            opened_at TEXT,
            last_check_time TEXT,
            peak_price REAL,
            last_price REAL,
            guard_price REAL,
            last_oi_delta_pct REAL,
            last_funding_rate REAL,
            exit_signal TEXT,
            exit_reason TEXT,
            orders_canceled INTEGER DEFAULT 0,
            closed_at TEXT
        )"""
    )


def sync_positions():
    ok, payload = api_get("/account/positions")
    if not ok:
        log(f"DEMO positions sync failed: {payload}")
        return False

    positions = payload.get("data", []) if isinstance(payload, dict) else []
    actual = {
        p.get("symbol"): p
        for p in positions
        if p.get("symbol") and abs(float(p.get("amount") or 0)) > 0
    }

    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    ensure_position_table(conn)
    rows = conn.execute("SELECT * FROM position_watchlist WHERE status = 'ACTIVE'").fetchall()

    state = load_state()
    if rows and not actual:
        empty_count = int(state.get("empty_positions_sync_count", 0) or 0) + 1
        state["empty_positions_sync_count"] = empty_count
        save_state(state)
        if empty_count < 3:
            conn.close()
            log(f"LOCAL_SYNC positions empty from API, skip closing active rows (empty_count={empty_count})")
            return True
    elif actual:
        if state.get("empty_positions_sync_count"):
            state["empty_positions_sync_count"] = 0
            save_state(state)

    closed = 0
    updated = 0
    added = 0
    side_fixed = 0
    now = cst_now()

    for row in rows:
        row = dict(row)
        symbol = row["symbol"]
        pos = actual.get(symbol)
        if not pos:
            conn.execute(
                """UPDATE position_watchlist
                   SET status = 'CLOSED', closed_at = ?, exit_signal = 'SYNC_CLOSED',
                       exit_reason = 'sync_closed: position not found on exchange'
                   WHERE symbol = ?""",
                (now, symbol),
            )
            closed += 1
            continue

        side = pos.get("side") or row.get("side") or "LONG"
        entry = float(pos.get("entryPrice") or row.get("entry_price") or 0)
        mark = float(pos.get("markPrice") or row.get("last_price") or entry)
        qty = abs(float(pos.get("amount") or row.get("quantity") or 0))
        leverage = float(pos.get("leverage") or row.get("leverage") or 1)
        peak = max(float(row.get("peak_price") or 0), entry, mark)
        stop_price = float(row.get("stop_price") or 0)
        stop_hit = bool(stop_price) and (
            mark >= stop_price if side == "SHORT" else mark <= stop_price
        )
        exit_signal = "STOP_HIT_REVIEW" if stop_hit else (row.get("exit_signal") or "HOLD")
        exit_reason = (
            f"stop_hit_review: mark={mark} stop={stop_price}"
            if stop_hit
            else row.get("exit_reason")
        )
        if side != row.get("side"):
            side_fixed += 1
        conn.execute(
            """UPDATE position_watchlist
               SET side = ?, entry_price = ?, last_price = ?, peak_price = ?,
                   quantity = ?, leverage = ?, last_check_time = ?,
                   exit_signal = ?, exit_reason = ?
               WHERE symbol = ?""",
            (side, entry, mark, peak, qty, leverage, now, exit_signal, exit_reason, symbol),
        )
        if stop_hit:
            pnl = (entry - mark) * qty if side == "SHORT" else (mark - entry) * qty
            notify_once(
                f"stop-hit:{symbol}:{stop_price}",
                (
                    f"止损触发待处理 {symbol} {side}\n"
                    f"标记价: {mark}\n止损价: {stop_price}\n"
                    f"浮动盈亏: {pnl:.2f} USDT\n"
                    "当前仅告警和标记，未自动平仓。"
                ),
            )
        updated += 1

    watched_symbols = {dict(row)["symbol"] for row in rows}
    for symbol, pos in actual.items():
        if symbol in watched_symbols:
            continue
        side = pos.get("side") or "LONG"
        entry = float(pos.get("entryPrice") or 0)
        mark = float(pos.get("markPrice") or entry)
        qty = abs(float(pos.get("amount") or 0))
        leverage = float(pos.get("leverage") or 1)
        stop = entry * (0.97 if side == "LONG" else 1.03)
        conn.execute(
            """INSERT OR REPLACE INTO position_watchlist
               (symbol, source, side, entry_price, stop_price, reference_support,
                quantity, leverage, status, opened_at, last_check_time, peak_price,
                last_price, guard_price, exit_signal, exit_reason, orders_canceled)
               VALUES (?, 'binance_sync', ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?, ?, ?, 'HOLD',
                       'sync_added: position exists on exchange', 0)""",
            (symbol, side, entry, stop, stop, qty, leverage, now, now, max(entry, mark), mark, stop),
        )
        added += 1

    conn.commit()
    conn.close()
    log(f"LOCAL_SYNC positions ok: actual={len(actual)} updated={updated} added={added} closed={closed} side_fixed={side_fixed}")
    return True


def health_check():
    ok, payload = api_get("/health", timeout=10)
    if ok:
        status = payload.get("data", {}).get("status", "unknown")
        log(f"DEMO health={status}")
        return True
    log(f"DEMO health failed: {payload}")
    return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sync-once", action="store_true")
    parser.add_argument("--health-only", action="store_true")
    args = parser.parse_args()

    if args.health_only:
        sys.exit(0 if health_check() else 1)

    if args.sync_once:
        health_check()
        sys.exit(0 if sync_positions() else 1)

    state = load_state()
    now = int(time.time())
    last_sync = int(state.get("last_sync", 0) or 0)
    health_check()
    if now - last_sync >= SYNC_INTERVAL_SEC:
        if sync_positions():
            state["last_sync"] = now
            save_state(state)


if __name__ == "__main__":
    main()
