#!/usr/bin/env python3
"""Local backend for the hedged trend ladder configurator.

Phase 1 is intentionally dry-run first:
- Accept strategy plans from the HTML configurator.
- Persist groups/orders/events into the radar SQLite database.
- Simulate L0 LONG/SHORT market probe acceptance.
- Let the operator confirm LONG or SHORT and activate only that side.

Live Binance execution is blocked unless HEDGED_LADDER_ALLOW_LIVE=true.
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from accumulation_radar import get_symbol_rules, quantize_down  # noqa: E402


DEFAULT_HOST = os.getenv("HEDGED_LADDER_HOST", "127.0.0.1")
DEFAULT_PORT = int(os.getenv("HEDGED_LADDER_PORT", "8787"))
DB_PATH = Path(os.getenv("HEDGED_LADDER_DB_PATH", str(ROOT / "hedged-ladder.db")))
ALLOW_LIVE = os.getenv("HEDGED_LADDER_ALLOW_LIVE", "false").lower() == "true"
SERVER_VERSION = "hedged-ladder-backend/0.1"


def utc_now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def json_dumps(payload):
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def normalize_symbol(value):
    return "".join(ch for ch in str(value or "").upper().strip() if ch.isalnum())


def group_id_for(symbol):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    suffix = int(time.time() * 1000) % 1000
    return f"HTL_{symbol}_{stamp}_{suffix:03d}"


def connect_db():
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(conn):
    conn.execute(
        """CREATE TABLE IF NOT EXISTS hedged_ladder_groups (
            id TEXT PRIMARY KEY,
            symbol TEXT NOT NULL,
            status TEXT NOT NULL,
            dry_run INTEGER NOT NULL,
            timeframe TEXT,
            leverage REAL,
            margin_mode TEXT,
            reference_price REAL,
            hedge_stop_pct REAL,
            backend_version TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            raw_plan_json TEXT NOT NULL
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS hedged_ladder_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_group_id TEXT NOT NULL,
            layer_index INTEGER NOT NULL,
            side TEXT NOT NULL,
            role TEXT NOT NULL,
            order_type TEXT NOT NULL,
            planned_price REAL,
            notional REAL,
            margin_used REAL,
            quantity REAL,
            binance_order_id TEXT,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            raw_order_json TEXT,
            raw_response_json TEXT,
            FOREIGN KEY(strategy_group_id) REFERENCES hedged_ladder_groups(id)
        )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS hedged_ladder_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_group_id TEXT,
            event_type TEXT NOT NULL,
            message TEXT,
            payload_json TEXT,
            created_at TEXT NOT NULL
        )"""
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_hedged_ladder_orders_group ON hedged_ladder_orders(strategy_group_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_hedged_ladder_events_group ON hedged_ladder_events(strategy_group_id)"
    )
    conn.commit()


def add_event(conn, group_id, event_type, message="", payload=None):
    conn.execute(
        """INSERT INTO hedged_ladder_events
           (strategy_group_id, event_type, message, payload_json, created_at)
           VALUES (?, ?, ?, ?, ?)""",
        (group_id, event_type, message, json_dumps(payload or {}), utc_now()),
    )


def row_to_dict(row):
    return dict(row) if row else None


def validate_plan(plan, dry_run):
    errors = []
    warnings = []
    config = plan.get("config") or {}
    symbol = normalize_symbol(config.get("symbol") or plan.get("market", {}).get("symbol"))
    if not symbol:
        errors.append("missing symbol")

    if config.get("positionMode") != "hedge":
        errors.append("positionMode must be hedge")
    if not dry_run and not ALLOW_LIVE:
        errors.append("live trading is blocked; set HEDGED_LADDER_ALLOW_LIVE=true to enable")

    leverage = float(config.get("leverage") or 0)
    if leverage <= 0:
        errors.append("leverage must be positive")

    reference_price = float(config.get("referencePrice") or 0)
    if reference_price <= 0:
        errors.append("referencePrice must be positive")

    layers = plan.get("layers") or []
    probe = next((item for item in layers if int(item.get("index", -1)) == 0), None)
    trend_layers = [item for item in layers if int(item.get("index", -1)) > 0 and item.get("enabled")]
    if config.get("openProbeHedge", True) and not probe:
        errors.append("missing L0 probe layer")
    if config.get("openProbeHedge", True) and probe and float(probe.get("notional") or 0) <= 0:
        errors.append("L0 notional must be positive")
    if not trend_layers:
        errors.append("at least one trend layer must be enabled")

    rules = get_symbol_rules(symbol) if symbol else None
    if rules and not rules.get("tradable", True):
        errors.append(rules.get("reason", f"{symbol} not tradable"))
    if not rules:
        warnings.append("symbol rules unavailable; using submitted price/quantity precision")

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "symbol": symbol,
        "rules": rules,
    }


def normalize_layer_order(symbol, layer, side, role, reference_price, leverage, rules):
    layer_index = int(layer.get("index", 0))
    notional = float(layer.get("notional") or 0)
    submitted_price = float(layer.get("longPrice" if side == "LONG" else "shortPrice") or 0)
    planned_price = reference_price if layer_index == 0 or submitted_price <= 0 else submitted_price
    quantity = float(layer.get("longQty" if side == "LONG" else "shortQty") or 0)
    if planned_price > 0 and quantity <= 0:
        quantity = notional / planned_price

    if rules and rules.get("tradable", True):
        planned_price = quantize_down(planned_price, rules["tick_size"], rules.get("price_precision", 8))
        quantity = quantize_down(quantity, rules["step_size"], rules.get("quantity_precision", 8))
        min_qty = float(rules.get("min_qty") or 0)
        min_notional = float(rules.get("min_notional") or 0)
        if min_qty > 0 and quantity < min_qty:
            quantity = quantize_down(min_qty, rules["step_size"], rules.get("quantity_precision", 8))
        if min_notional > 0 and planned_price > 0 and quantity * planned_price < min_notional:
            quantity = quantize_down(min_notional / planned_price, rules["step_size"], rules.get("quantity_precision", 8))
    else:
        planned_price = round(planned_price, 10)
        quantity = round(quantity, 8)

    return {
        "symbol": symbol,
        "layerIndex": layer_index,
        "side": side,
        "positionSide": side,
        "role": role,
        "orderType": "MARKET" if layer_index == 0 else str(layer.get("orderType") or "limit").upper(),
        "plannedPrice": planned_price,
        "notional": notional,
        "marginUsed": notional / max(float(leverage or 1), 1),
        "quantity": quantity,
    }


def insert_order(conn, group_id, order, status):
    now = utc_now()
    conn.execute(
        """INSERT INTO hedged_ladder_orders
           (strategy_group_id, layer_index, side, role, order_type, planned_price,
            notional, margin_used, quantity, binance_order_id, status, created_at,
            updated_at, raw_order_json, raw_response_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            group_id,
            order["layerIndex"],
            order["side"],
            order["role"],
            order["orderType"],
            order["plannedPrice"],
            order["notional"],
            order["marginUsed"],
            order["quantity"],
            None,
            status,
            now,
            now,
            json_dumps(order),
            None,
        ),
    )


def create_strategy(payload):
    plan = payload.get("plan") or {}
    dry_run = bool(payload.get("dryRun", True))
    validation = validate_plan(plan, dry_run)
    if not validation["ok"]:
        return 400, {
            "ok": False,
            "errors": validation["errors"],
            "warnings": validation["warnings"],
        }

    config = plan.get("config") or {}
    layers = plan.get("layers") or []
    symbol = validation["symbol"]
    rules = validation["rules"]
    reference_price = float(config.get("referencePrice") or 0)
    leverage = float(config.get("leverage") or 1)
    group_id = group_id_for(symbol)
    status = "HEDGED_PROBE_OPENED" if dry_run else "HEDGED_PROBE_OPENING"
    now = utc_now()

    conn = connect_db()
    init_db(conn)
    try:
        conn.execute("BEGIN")
        conn.execute(
            """INSERT INTO hedged_ladder_groups
               (id, symbol, status, dry_run, timeframe, leverage, margin_mode,
                reference_price, hedge_stop_pct, backend_version, created_at,
                updated_at, raw_plan_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                group_id,
                symbol,
                status,
                1 if dry_run else 0,
                config.get("timeframe"),
                leverage,
                config.get("marginMode"),
                reference_price,
                float(config.get("hedgeStopPct") or 0),
                SERVER_VERSION,
                now,
                now,
                json_dumps(plan),
            ),
        )

        accepted_orders = []
        for layer in layers:
            if not layer.get("enabled"):
                continue
            layer_index = int(layer.get("index", -1))
            if layer_index == 0:
                for side in ("LONG", "SHORT"):
                    order = normalize_layer_order(symbol, layer, side, "PROBE", reference_price, leverage, rules)
                    order_status = "DRY_RUN_FILLED" if dry_run else "PENDING_LIVE_EXECUTION"
                    insert_order(conn, group_id, order, order_status)
                    accepted_orders.append(order)
            elif layer_index > 0:
                for side in ("LONG", "SHORT"):
                    order = normalize_layer_order(symbol, layer, side, "TREND", reference_price, leverage, rules)
                    insert_order(conn, group_id, order, "WAITING_TREND_CONFIRM")

        add_event(
            conn,
            group_id,
            "STRATEGY_STARTED",
            "L0 hedge probe accepted in dry-run" if dry_run else "L0 hedge probe queued for live execution",
            {"dryRun": dry_run, "acceptedOrders": accepted_orders, "warnings": validation["warnings"]},
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return 200, {
        "ok": True,
        "strategyGroupId": group_id,
        "status": status,
        "dryRun": dry_run,
        "acceptedOrders": accepted_orders,
        "warnings": validation["warnings"],
    }


def confirm_direction(payload):
    group_id = str(payload.get("strategyGroupId") or payload.get("groupId") or "").strip()
    direction = str(payload.get("direction") or "").upper().strip()
    if direction not in {"LONG", "SHORT"}:
        return 400, {"ok": False, "errors": ["direction must be LONG or SHORT"]}
    if not group_id:
        return 400, {"ok": False, "errors": ["missing strategyGroupId"]}

    conn = connect_db()
    init_db(conn)
    group = conn.execute("SELECT * FROM hedged_ladder_groups WHERE id = ?", (group_id,)).fetchone()
    if not group:
        conn.close()
        return 404, {"ok": False, "errors": ["strategy group not found"]}

    now = utc_now()
    trend_status = "DRY_RUN_PLACED" if group["dry_run"] else "PENDING_LIVE_EXECUTION"
    opposite = "SHORT" if direction == "LONG" else "LONG"
    conn.execute("BEGIN")
    conn.execute(
        """UPDATE hedged_ladder_orders
           SET status = ?, updated_at = ?
           WHERE strategy_group_id = ? AND role = 'TREND' AND side = ?""",
        (trend_status, now, group_id, direction),
    )
    conn.execute(
        """UPDATE hedged_ladder_orders
           SET status = 'CANCELLED_OPPOSITE_SIDE', updated_at = ?
           WHERE strategy_group_id = ? AND role = 'TREND' AND side = ?""",
        (now, group_id, opposite),
    )
    conn.execute(
        "UPDATE hedged_ladder_groups SET status = ?, updated_at = ? WHERE id = ?",
        (f"{direction}_ACTIVE", now, group_id),
    )
    add_event(conn, group_id, "TREND_CONFIRMED", f"{direction} trend confirmed", {"direction": direction})
    conn.commit()
    orders = fetch_orders(conn, group_id)
    conn.close()
    return 200, {"ok": True, "strategyGroupId": group_id, "status": f"{direction}_ACTIVE", "orders": orders}


def close_group(payload):
    group_id = str(payload.get("strategyGroupId") or payload.get("groupId") or "").strip()
    reason = str(payload.get("reason") or "manual_close")
    if not group_id:
        return 400, {"ok": False, "errors": ["missing strategyGroupId"]}
    conn = connect_db()
    init_db(conn)
    group = conn.execute("SELECT * FROM hedged_ladder_groups WHERE id = ?", (group_id,)).fetchone()
    if not group:
        conn.close()
        return 404, {"ok": False, "errors": ["strategy group not found"]}
    now = utc_now()
    conn.execute("BEGIN")
    conn.execute(
        """UPDATE hedged_ladder_orders
           SET status = CASE
               WHEN status LIKE 'DRY_RUN%' OR status = 'PENDING_LIVE_EXECUTION' THEN 'DRY_RUN_CLOSED'
               WHEN status = 'WAITING_TREND_CONFIRM' THEN 'CANCELLED_GROUP_CLOSED'
               ELSE status
           END,
           updated_at = ?
           WHERE strategy_group_id = ?""",
        (now, group_id),
    )
    conn.execute(
        "UPDATE hedged_ladder_groups SET status = 'COMPLETED', updated_at = ? WHERE id = ?",
        (now, group_id),
    )
    add_event(conn, group_id, "GROUP_CLOSED", "strategy group closed", {"reason": reason})
    conn.commit()
    orders = fetch_orders(conn, group_id)
    conn.close()
    return 200, {"ok": True, "strategyGroupId": group_id, "status": "COMPLETED", "orders": orders}


def fetch_orders(conn, group_id):
    return [
        row_to_dict(row)
        for row in conn.execute(
            "SELECT * FROM hedged_ladder_orders WHERE strategy_group_id = ? ORDER BY layer_index, side",
            (group_id,),
        ).fetchall()
    ]


def fetch_events(conn, group_id):
    return [
        row_to_dict(row)
        for row in conn.execute(
            "SELECT * FROM hedged_ladder_events WHERE strategy_group_id = ? ORDER BY id",
            (group_id,),
        ).fetchall()
    ]


def list_groups(limit=50):
    conn = connect_db()
    init_db(conn)
    rows = conn.execute(
        """SELECT id, symbol, status, dry_run, timeframe, leverage, margin_mode,
                  reference_price, hedge_stop_pct, created_at, updated_at
           FROM hedged_ladder_groups
           ORDER BY created_at DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    conn.close()
    return 200, {"ok": True, "groups": [row_to_dict(row) for row in rows]}


def get_group(group_id):
    conn = connect_db()
    init_db(conn)
    group = conn.execute("SELECT * FROM hedged_ladder_groups WHERE id = ?", (group_id,)).fetchone()
    if not group:
        conn.close()
        return 404, {"ok": False, "errors": ["strategy group not found"]}
    result = {
        "ok": True,
        "group": row_to_dict(group),
        "orders": fetch_orders(conn, group_id),
        "events": fetch_events(conn, group_id),
    }
    conn.close()
    return 200, result


class Handler(BaseHTTPRequestHandler):
    server_version = SERVER_VERSION

    def log_message(self, fmt, *args):
        print(f"[{utc_now()}] {self.address_string()} {fmt % args}")

    def _send(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self._send(200, {"ok": True})

    def read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode("utf-8") if length else "{}"
        return json.loads(raw or "{}")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        try:
            if path in {"", "/api/strategy/health"}:
                self._send(
                    200,
                    {
                        "ok": True,
                        "version": SERVER_VERSION,
                        "dbPath": str(DB_PATH),
                        "allowLive": ALLOW_LIVE,
                        "time": utc_now(),
                    },
                )
                return
            if path == "/api/strategy/groups":
                limit = int((parse_qs(parsed.query).get("limit") or ["50"])[0])
                status, payload = list_groups(max(1, min(limit, 200)))
                self._send(status, payload)
                return
            if path.startswith("/api/strategy/groups/"):
                group_id = path.split("/")[-1]
                status, payload = get_group(group_id)
                self._send(status, payload)
                return
            self._send(404, {"ok": False, "errors": ["not found"]})
        except Exception as exc:
            self._send(500, {"ok": False, "errors": [str(exc)]})

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        try:
            payload = self.read_json()
            if path == "/api/strategy/start":
                status, result = create_strategy(payload)
                self._send(status, result)
                return
            if path == "/api/strategy/confirm":
                status, result = confirm_direction(payload)
                self._send(status, result)
                return
            if path == "/api/strategy/close":
                status, result = close_group(payload)
                self._send(status, result)
                return
            self._send(404, {"ok": False, "errors": ["not found"]})
        except json.JSONDecodeError:
            self._send(400, {"ok": False, "errors": ["invalid json"]})
        except Exception as exc:
            self._send(500, {"ok": False, "errors": [str(exc)]})


def main():
    parser = argparse.ArgumentParser(description="Run hedged trend ladder local backend")
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--init-db", action="store_true", help="initialize DB tables and exit")
    args = parser.parse_args()

    conn = connect_db()
    init_db(conn)
    conn.close()
    if args.init_db:
        print(f"initialized hedged ladder tables in {DB_PATH}")
        return

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"{SERVER_VERSION} listening on http://{args.host}:{args.port}")
    print(f"database: {DB_PATH}")
    print(f"live trading allowed: {ALLOW_LIVE}")
    server.serve_forever()


if __name__ == "__main__":
    main()
