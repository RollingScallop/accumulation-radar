#!/usr/bin/env python3
"""
趋势双向阶梯策略批量回测。

第一版目标：
- 用历史 K 线按固定频率重放扫描。
- 入选后生成多空 10 阶挂单。
- 行情先触发哪边，就取消另一边。
- 模拟同方向阶梯成交、10% 硬止损和简化版动态止盈。
"""

import argparse
import csv
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from accumulation_radar import (  # noqa: E402
    FAPI,
    DB_PATH,
    TREND_MIN_24H_VOL_USD,
    TREND_MIN_SCORE,
    TREND_MIN_ADX,
    TREND_MIN_RANGE_PCT,
    TREND_MIN_VOLUME_RATIO,
    TREND_MIN_ATR_EXPANSION,
    TREND_RANGE_BARS,
    TREND_RISK_BUDGET_USD,
    TREND_STOP_LOSS_PCT,
    TREND_ORDER_STEPS,
    TREND_ORDER_WEIGHTS,
    api_get,
    atr_values,
    adx_value,
    ema,
    format_usd,
    get_all_perp_symbols,
)


INTERVAL_MS = {
    "5m": 5 * 60 * 1000,
    "15m": 15 * 60 * 1000,
    "1h": 60 * 60 * 1000,
    "4h": 4 * 60 * 60 * 1000,
    "1d": 24 * 60 * 60 * 1000,
}

PYRAMID_WEIGHTS = [0.22, 0.18, 0.15, 0.12, 0.10, 0.08, 0.06, 0.04, 0.03, 0.02]
CONFIRM_BUFFER_ATR = 0.20
CONFIRM_VOLUME_MULT = 1.20
HEDGE_STOP_PCT = 0.10


def parse_dt(value):
    text = value.strip()
    if len(text) == 10:
        text += "T00:00:00"
    dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def interval_hours(interval):
    if interval not in INTERVAL_MS:
        raise ValueError(f"unsupported interval: {interval}")
    return INTERVAL_MS[interval] / (60 * 60 * 1000)


def fetch_klines_range(symbol, interval, start_ms, end_ms, sleep_sec=0.08):
    rows = []
    cursor = start_ms
    while cursor < end_ms:
        batch = api_get(
            "/fapi/v1/klines",
            {
                "symbol": symbol,
                "interval": interval,
                "startTime": cursor,
                "endTime": end_ms,
                "limit": 1500,
            },
            base_url=FAPI,
        )
        if not batch or not isinstance(batch, list):
            break
        rows.extend(batch)
        last_open = int(batch[-1][0])
        next_cursor = last_open + INTERVAL_MS[interval]
        if next_cursor <= cursor:
            break
        cursor = next_cursor
        if len(batch) < 1500:
            break
        time.sleep(sleep_sec)

    parsed = []
    seen = set()
    for item in rows:
        open_time = int(item[0])
        if open_time in seen:
            continue
        seen.add(open_time)
        parsed.append({
            "open_time": open_time,
            "open": float(item[1]),
            "high": float(item[2]),
            "low": float(item[3]),
            "close": float(item[4]),
            "volume": float(item[7]),
        })
    parsed.sort(key=lambda row: row["open_time"])
    return parsed


def top_volume_symbols(limit):
    tickers = api_get("/fapi/v1/ticker/24hr")
    if not tickers:
        return []
    exclude = {"USDC", "USDP", "TUSD", "FDUSD", "BTCDOM", "DEFI", "USDM"}
    rows = []
    for item in tickers:
        symbol = item.get("symbol", "")
        coin = symbol.replace("USDT", "")
        if symbol.endswith("USDT") and coin not in exclude:
            rows.append((symbol, float(item.get("quoteVolume") or 0)))
    rows.sort(key=lambda item: item[1], reverse=True)
    return [symbol for symbol, _ in rows[:limit]]


def radar_watchlist_symbols(limit=0, min_pool_score=0, include_sleeping=True):
    if not DB_PATH.exists():
        print(f"雷达数据库不存在: {DB_PATH}")
        return []
    conn = sqlite3.connect(str(DB_PATH))
    rows = conn.execute(
        """SELECT symbol, score, status
           FROM watchlist
           WHERE score >= ?
           ORDER BY score DESC""",
        (min_pool_score,),
    ).fetchall()
    conn.close()
    symbols = []
    for symbol, _score, status in rows:
        if not include_sleeping and status and "收筹中" in status:
            continue
        symbols.append(symbol)
        if limit and len(symbols) >= limit:
            break
    return symbols


def score_snapshot(klines_5m, klines_15m, idx_5m, idx_15m, min_score, enforce_filters=True):
    if idx_5m < TREND_RANGE_BARS + 60 or idx_15m < 70:
        return None

    window_5m = klines_5m[:idx_5m + 1]
    window_15m = klines_15m[:idx_15m + 1]
    closes_5m = [k["close"] for k in window_5m]
    highs_5m = [k["high"] for k in window_5m]
    lows_5m = [k["low"] for k in window_5m]
    vols_5m = [k["volume"] for k in window_5m]
    closes_15m = [k["close"] for k in window_15m]
    highs_15m = [k["high"] for k in window_15m]
    lows_15m = [k["low"] for k in window_15m]

    current_price = closes_5m[-1]
    recent_range = window_5m[-TREND_RANGE_BARS:]
    upper = max(k["high"] for k in recent_range)
    lower = min(k["low"] for k in recent_range)
    range_pct = (upper - lower) / current_price * 100 if current_price > 0 else 0

    atr_series = atr_values(highs_15m, lows_15m, closes_15m, 14)
    atr = atr_series[-1] if atr_series else 0
    prior_atr_window = atr_series[-40:-8] if len(atr_series) >= 40 else atr_series[:-8]
    recent_atr = sum(atr_series[-8:]) / min(len(atr_series), 8) if atr_series else 0
    prior_atr = sum(prior_atr_window) / len(prior_atr_window) if prior_atr_window else recent_atr
    atr_expansion = recent_atr / prior_atr if prior_atr > 0 else 0
    adx = adx_value(highs_15m, lows_15m, closes_15m, 14)

    recent_vol = sum(vols_5m[-12:]) / 12
    prior_vol_window = vols_5m[-60:-12]
    prior_vol = sum(prior_vol_window) / len(prior_vol_window) if prior_vol_window else recent_vol
    volume_ratio = recent_vol / prior_vol if prior_vol > 0 else 0

    ema9 = ema(closes_15m, 9)
    ema21 = ema(closes_15m, 21)
    ema55 = ema(closes_15m, 55)
    ema_spread_pct = (max(ema9[-1], ema21[-1], ema55[-1]) - min(ema9[-1], ema21[-1], ema55[-1])) / current_price * 100

    near_edge = min(abs(upper - current_price), abs(current_price - lower)) / current_price
    volatility_score = min(max((atr_expansion - 1.0) / 0.8, 0), 1) * 25
    volume_score = min(max((volume_ratio - 1.0) / 1.5, 0), 1) * 20
    adx_score = min(max((adx - 15) / 25, 0), 1) * 25
    ema_score = min(ema_spread_pct / 3.0, 1) * 15
    edge_score = min(max((0.08 - near_edge) / 0.08, 0), 1) * 15
    score = volatility_score + volume_score + adx_score + ema_score + edge_score

    if enforce_filters and (
        score < min_score
        or adx < TREND_MIN_ADX
        or range_pct < TREND_MIN_RANGE_PCT
        or volume_ratio < TREND_MIN_VOLUME_RATIO
        or atr_expansion < TREND_MIN_ATR_EXPANSION
        or atr <= 0
    ):
        return None

    return {
        "score": score,
        "price": current_price,
        "upper": upper,
        "lower": lower,
        "atr": atr,
        "adx": adx,
        "range_pct": range_pct,
        "volume_ratio": volume_ratio,
        "recent_volume": recent_vol,
        "atr_expansion": atr_expansion,
        "ema_spread_pct": ema_spread_pct,
    }


def build_ladder(snapshot):
    side_notional = TREND_RISK_BUDGET_USD / max(TREND_STOP_LOSS_PCT, 1e-6)
    long_orders = []
    short_orders = []
    for idx, (step, weight) in enumerate(zip(TREND_ORDER_STEPS, PYRAMID_WEIGHTS), start=1):
        notional = side_notional * weight
        if idx == 1:
            long_price = snapshot["price"]
            short_price = snapshot["price"]
        else:
            long_price = snapshot["upper"] + snapshot["atr"] * step
            short_price = max(snapshot["lower"] - snapshot["atr"] * step, snapshot["lower"] * 0.5)
        long_orders.append({"index": idx, "price": long_price, "notional": notional, "qty": notional / long_price})
        short_orders.append({"index": idx, "price": short_price, "notional": notional, "qty": notional / short_price})
    return long_orders, short_orders


def avg_entry(fills):
    qty = sum(fill["qty"] for fill in fills)
    if qty <= 0:
        return 0
    return sum(fill["price"] * fill["qty"] for fill in fills) / qty


def leg_pnl(side, fills, price):
    if not fills:
        return 0
    qty = sum(fill["qty"] for fill in fills)
    entry = avg_entry(fills)
    return (price - entry) * qty if side == "LONG" else (entry - price) * qty


def leg_notional(fills):
    return sum(fill["notional"] for fill in fills)


def record_order(order_events, side, order, role, status, event_time=None, close_price=0, pnl=0):
    order_events.append({
        "side": side,
        "order_index": order["index"],
        "role": role,
        "status": status,
        "planned_price": order["price"],
        "planned_notional": order["notional"],
        "quantity": order["qty"],
        "event_time": event_time,
        "close_price": close_price,
        "pnl": pnl,
    })


def weakness_exit(side, bars, entry, current_index):
    if current_index < 3:
        return False, ""
    recent = bars[max(0, current_index - 2):current_index + 1]
    closes = [b["close"] for b in bars[:current_index + 1]]
    if len(closes) < 21:
        return False, ""
    ema9 = ema(closes, 9)[-1]
    ema21 = ema(closes, 21)[-1]
    if side == "LONG":
        no_new_high = recent[-1]["high"] <= max(b["high"] for b in bars[max(0, current_index - 6):current_index])
        long_upper_shadow = recent[-1]["high"] > 0 and (recent[-1]["high"] - max(recent[-1]["open"], recent[-1]["close"])) / recent[-1]["high"] >= 0.012
        ema_break = recent[-1]["close"] < ema9 or recent[-1]["close"] < ema21
        if no_new_high and (long_upper_shadow or ema_break) and recent[-1]["close"] > entry:
            return True, "kline weakness: no-new-high + shadow/ema-break"
    else:
        no_new_low = recent[-1]["low"] >= min(b["low"] for b in bars[max(0, current_index - 6):current_index])
        long_lower_shadow = recent[-1]["low"] > 0 and (min(recent[-1]["open"], recent[-1]["close"]) - recent[-1]["low"]) / recent[-1]["low"] >= 0.012
        ema_reclaim = recent[-1]["close"] > ema9 or recent[-1]["close"] > ema21
        if no_new_low and (long_lower_shadow or ema_reclaim) and recent[-1]["close"] < entry:
            return True, "kline weakness: no-new-low + shadow/ema-reclaim"
    return False, ""


def simulate_trade(symbol, snapshot, future_bars, scan_time, max_bars, confirm_buffer_atr=CONFIRM_BUFFER_ATR):
    long_orders, short_orders = build_ladder(snapshot)
    side = "HEDGED"
    long_fills = [long_orders[0]]
    short_fills = [short_orders[0]]
    order_events = []
    trend_side = None
    trend_fills = []
    hedge_fills = []
    hedge_closed = False
    hedge_close_price = 0
    realized_hedge_pnl = 0
    realized_hedge_notional = 0
    peak_group_pnl = 0
    peak_favorable = 0
    pending_side = None
    pending_since = 0
    exit_price = 0
    exit_reason = "timeout"
    entry_time = future_bars[0]["open_time"] if future_bars else scan_time
    exit_time = future_bars[min(len(future_bars), max_bars) - 1]["open_time"] if future_bars else scan_time
    record_order(order_events, "LONG", long_orders[0], "PROBE", "FILLED", entry_time)
    record_order(order_events, "SHORT", short_orders[0], "PROBE", "FILLED", entry_time)

    bars = future_bars[:max_bars]
    for i, bar in enumerate(bars):
        bars_since_entry = i

        if trend_side is None:
            long_level = snapshot["upper"] + snapshot["atr"] * confirm_buffer_atr
            short_level = snapshot["lower"] - snapshot["atr"] * confirm_buffer_atr
            volume_ok = bar["volume"] >= max(snapshot.get("recent_volume", 0), 1) * CONFIRM_VOLUME_MULT
            long_breakout_close = bar["close"] >= long_level and volume_ok
            short_breakout_close = bar["close"] <= short_level and volume_ok

            if pending_side == "LONG":
                if bar["close"] >= snapshot["upper"]:
                    trend_side = "LONG"
                    trend_fills = long_fills
                    hedge_fills = short_fills
                else:
                    pending_side = None
            elif pending_side == "SHORT":
                if bar["close"] <= snapshot["lower"]:
                    trend_side = "SHORT"
                    trend_fills = short_fills
                    hedge_fills = long_fills
                else:
                    pending_side = None
            elif long_breakout_close and not short_breakout_close:
                pending_side = "LONG"
                pending_since = bar["open_time"]
                continue
            elif short_breakout_close and not long_breakout_close:
                pending_side = "SHORT"
                pending_since = bar["open_time"]
                continue
            elif bars_since_entry >= 24:
                exit_price = bar["close"]
                exit_reason = f"hedged-probe-timeout pending={pending_side or '-'}"
                exit_time = bar["open_time"]
                break
            else:
                continue

        active_orders = long_orders if trend_side == "LONG" else short_orders
        trend_fills = long_fills if trend_side == "LONG" else short_fills
        hedge_fills = short_fills if trend_side == "LONG" else long_fills

        if not hedge_closed and hedge_fills:
            hedge_entry = avg_entry(hedge_fills)
            if trend_side == "LONG" and bar["high"] >= hedge_entry * (1 + HEDGE_STOP_PCT):
                hedge_close_price = hedge_entry * (1 + HEDGE_STOP_PCT)
                hedge_pnl = leg_pnl("SHORT", hedge_fills, hedge_close_price)
                realized_hedge_pnl += hedge_pnl
                realized_hedge_notional += leg_notional(hedge_fills)
                for fill in hedge_fills:
                    record_order(order_events, "SHORT", fill, "HEDGE", "CLOSED_HEDGE_STOP", bar["open_time"], hedge_close_price, hedge_pnl)
                short_fills = []
                hedge_fills = []
                hedge_closed = True
            elif trend_side == "SHORT" and bar["low"] <= hedge_entry * (1 - HEDGE_STOP_PCT):
                hedge_close_price = hedge_entry * (1 - HEDGE_STOP_PCT)
                hedge_pnl = leg_pnl("LONG", hedge_fills, hedge_close_price)
                realized_hedge_pnl += hedge_pnl
                realized_hedge_notional += leg_notional(hedge_fills)
                for fill in hedge_fills:
                    record_order(order_events, "LONG", fill, "HEDGE", "CLOSED_HEDGE_STOP", bar["open_time"], hedge_close_price, hedge_pnl)
                long_fills = []
                hedge_fills = []
                hedge_closed = True

        for order in active_orders[1:]:
            if any(fill["index"] == order["index"] for fill in trend_fills):
                continue
            touched = bar["high"] >= order["price"] if trend_side == "LONG" else bar["low"] <= order["price"]
            if touched:
                trend_fills.append(order)
                record_order(order_events, trend_side, order, "TREND", "FILLED", bar["open_time"])

        if trend_side == "LONG":
            long_fills = trend_fills
            short_fills = hedge_fills
        else:
            short_fills = trend_fills
            long_fills = hedge_fills

        trend_entry = avg_entry(trend_fills)
        filled_count = len(trend_fills)
        full_position = filled_count >= len(active_orders)
        mark_price = bar["close"]
        group_pnl = realized_hedge_pnl + leg_pnl("LONG", long_fills, mark_price) + leg_pnl("SHORT", short_fills, mark_price)
        peak_group_pnl = max(peak_group_pnl, group_pnl)
        total_notional = leg_notional(long_fills) + leg_notional(short_fills) + realized_hedge_notional
        group_pnl_pct = group_pnl / total_notional if total_notional > 0 else 0

        if trend_side == "LONG":
            current_favorable = (bar["high"] - trend_entry) / trend_entry if trend_entry > 0 else 0
        else:
            current_favorable = (trend_entry - bar["low"]) / trend_entry if trend_entry > 0 else 0
        peak_favorable = max(peak_favorable, current_favorable)

        if group_pnl <= -TREND_RISK_BUDGET_USD:
            exit_price = mark_price
            exit_reason = "group-hard-stop"
            exit_time = bar["open_time"]
            break

        back_inside_range = snapshot["lower"] < bar["close"] < snapshot["upper"]
        if filled_count >= 2 and bars_since_entry >= 6 and back_inside_range and group_pnl < 0:
            exit_price = mark_price
            exit_reason = "failed-breakout-back-inside-range"
            exit_time = bar["open_time"]
            break

        lock_ratio = profit_lock_ratio(filled_count, peak_group_pnl, total_notional)
        if hedge_closed and lock_ratio > 0:
            group_guard = peak_group_pnl * lock_ratio
            if group_pnl <= group_guard:
                exit_price = mark_price
                exit_reason = f"group-profit-lock filled={filled_count} peak={peak_group_pnl:.2f}u lock={lock_ratio:.2f}"
                exit_time = bar["open_time"]
                break

        giveback = (peak_group_pnl - group_pnl) / peak_group_pnl if peak_group_pnl > 0 else 0

        if bars_since_entry >= 24 and filled_count <= 3 and peak_favorable < 0.015:
            exit_price = bar["close"]
            exit_reason = f"stagnation-exit filled={filled_count} peak={peak_favorable*100:.1f}%"
            exit_time = bar["open_time"]
            break
        if bars_since_entry >= 72 and peak_favorable < 0.03:
            exit_price = bar["close"]
            exit_reason = f"weak-trend-timeout filled={filled_count} peak={peak_favorable*100:.1f}%"
            exit_time = bar["open_time"]
            break
        if hedge_closed and filled_count >= 4 and peak_group_pnl > 0 and giveback >= 0.60:
            exit_price = bar["close"]
            exit_reason = "group-profit-giveback"
            exit_time = bar["open_time"]
            break
        weak, weak_reason = weakness_exit(trend_side, bars, trend_entry, i)
        if hedge_closed and full_position and peak_group_pnl > 0 and weak:
            exit_price = bar["close"]
            exit_reason = f"group-full-position-dynamic-tp: {weak_reason}"
            exit_time = bar["open_time"]
            break

    if trend_side is None and exit_reason != "hedged-probe-timeout":
        return None

    if exit_price <= 0:
        exit_price = bars[-1]["close"] if bars else snapshot["price"]

    trend_fills = long_fills if trend_side == "LONG" else short_fills
    entry = avg_entry(trend_fills)
    notional = leg_notional(long_fills) + leg_notional(short_fills) + realized_hedge_notional
    pnl = realized_hedge_pnl + leg_pnl("LONG", long_fills, exit_price) + leg_pnl("SHORT", short_fills, exit_price)
    filled_keys = {(event["side"], event["order_index"]) for event in order_events if event["status"].startswith("FILLED") or event["status"].startswith("CLOSED")}
    for order in long_orders:
        if ("LONG", order["index"]) not in filled_keys:
            role = "TREND" if trend_side == "LONG" else "PLANNED_HEDGE_SIDE"
            record_order(order_events, "LONG", order, role, "NOT_FILLED")
    for order in short_orders:
        if ("SHORT", order["index"]) not in filled_keys:
            role = "TREND" if trend_side == "SHORT" else "PLANNED_HEDGE_SIDE"
            record_order(order_events, "SHORT", order, role, "NOT_FILLED")
    return {
        "symbol": symbol,
        "scan_time": scan_time,
        "entry_time": entry_time or scan_time,
        "exit_time": exit_time,
        "side": trend_side or "HEDGED",
        "score": snapshot["score"],
        "entry": entry,
        "exit": exit_price,
        "filled_orders": len(trend_fills),
        "full_position": len(trend_fills) >= 10,
        "hedge_notional": leg_notional(short_fills if trend_side == "LONG" else long_fills) + realized_hedge_notional,
        "trend_notional": leg_notional(trend_fills),
        "hedge_closed": hedge_closed,
        "hedge_close_price": hedge_close_price,
        "realized_hedge_pnl": realized_hedge_pnl,
        "notional": notional,
        "pnl": pnl,
        "pnl_pct_on_notional": pnl / notional if notional > 0 else 0,
        "peak_favorable_pct": peak_group_pnl / notional if notional > 0 else 0,
        "exit_reason": exit_reason,
        "strategy_group_exit": True,
        "adx": snapshot["adx"],
        "range_pct": snapshot["range_pct"],
        "volume_ratio": snapshot["volume_ratio"],
        "atr_expansion": snapshot["atr_expansion"],
        "orders": sorted(order_events, key=lambda item: (item["side"], item["order_index"], item["status"])),
    }


def profit_lock_ratio(filled_count, peak_group_pnl, total_notional):
    """浮盈加仓后的利润保护线。

    阶梯单越往后成交，说明趋势越延伸，也越需要把已出现的浮盈锁住。
    返回值代表锁住峰值浮盈的比例。
    """
    if total_notional <= 0 or peak_group_pnl <= 0:
        return 0
    peak_pct = peak_group_pnl / total_notional
    if filled_count >= 10 and peak_pct >= 0.015:
        return 0.60
    if filled_count >= 8 and peak_pct >= 0.02:
        return 0.45
    if filled_count >= 5 and peak_pct >= 0.03:
        return 0.35
    if peak_pct >= 0.06:
        return 0.50
    return 0


def backtest_symbol(symbol, start_ms, end_ms, args, remaining_trades=None):
    interval = args.interval
    warmup_bars = 180
    warmup_ms = int(warmup_bars * INTERVAL_MS[interval])
    klines = fetch_klines_range(symbol, interval, start_ms - warmup_ms, end_ms)
    if len(klines) < 140:
        return []

    last_trade_scan = 0
    trades = []
    bars_per_hour = 1 / interval_hours(interval)
    horizon_bars = max(1, int(args.horizon_hours * bars_per_hour))
    cooldown_bars = max(1, int(args.cooldown_hours * bars_per_hour))

    for idx, bar in enumerate(klines):
        if bar["open_time"] < start_ms or bar["open_time"] > end_ms:
            continue
        if idx - last_trade_scan < cooldown_bars:
            continue
        if idx % args.scan_interval_bars != 0:
            continue

        snapshot = score_snapshot(klines, klines, idx, idx, args.min_score)
        if not snapshot:
            continue
        snapshot["interval"] = interval
        future = klines[idx + 1:idx + 1 + horizon_bars]
        result = simulate_trade(symbol, snapshot, future, bar["open_time"], horizon_bars, args.confirm_buffer_atr)
        if result:
            trades.append(result)
            last_trade_scan = idx
            if remaining_trades is not None and len(trades) >= remaining_trades:
                break
    return trades


def fmt_time(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def write_outputs(trades, output_path):
    output_path.parent.mkdir(parents=True, exist_ok=True)
    json_path = output_path.with_suffix(".json")
    csv_path = output_path.with_suffix(".csv")
    orders_json_path = output_path.with_name(output_path.name + "_orders").with_suffix(".json")
    orders_csv_path = output_path.with_name(output_path.name + "_orders").with_suffix(".csv")
    json_path.write_text(json.dumps(trades, ensure_ascii=False, indent=2))
    fields = [
        "trade_id", "symbol", "scan_time", "entry_time", "exit_time", "side", "score",
        "entry", "exit", "filled_orders", "full_position", "notional", "pnl",
        "pnl_pct_on_notional", "peak_favorable_pct", "exit_reason", "strategy_group_exit",
        "hedge_notional", "trend_notional", "hedge_closed", "hedge_close_price", "realized_hedge_pnl",
        "adx", "range_pct", "volume_ratio", "atr_expansion",
    ]
    with csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for trade_id, trade in enumerate(trades, start=1):
            row = dict(trade)
            row.pop("orders", None)
            row["trade_id"] = trade_id
            for key in ("scan_time", "entry_time", "exit_time"):
                row[key] = fmt_time(row[key])
            writer.writerow(row)

    order_rows = []
    for trade_id, trade in enumerate(trades, start=1):
        for order in trade.get("orders", []):
            row = {
                "trade_id": trade_id,
                "symbol": trade["symbol"],
                "trade_side": trade["side"],
                "scan_time": fmt_time(trade["scan_time"]),
                "exit_time": fmt_time(trade["exit_time"]),
                **order,
            }
            if row.get("event_time"):
                row["event_time"] = fmt_time(row["event_time"])
            else:
                row["event_time"] = ""
            order_rows.append(row)
    orders_json_path.write_text(json.dumps(order_rows, ensure_ascii=False, indent=2))
    order_fields = [
        "trade_id", "symbol", "trade_side", "scan_time", "exit_time",
        "side", "order_index", "role", "status", "planned_price",
        "planned_notional", "quantity", "event_time", "close_price", "pnl",
    ]
    with orders_csv_path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=order_fields)
        writer.writeheader()
        writer.writerows(order_rows)
    return json_path, csv_path, orders_json_path, orders_csv_path


def summarize(trades):
    if not trades:
        return "没有产生交易。"
    total = len(trades)
    wins = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    pnl = sum(t["pnl"] for t in trades)
    avg = pnl / total
    win_rate = len(wins) / total * 100
    full = sum(1 for t in trades if t["full_position"])
    long_count = sum(1 for t in trades if t["side"] == "LONG")
    short_count = total - long_count
    best = max(trades, key=lambda t: t["pnl"])
    worst = min(trades, key=lambda t: t["pnl"])
    return "\n".join([
        f"交易数: {total}",
        f"胜率: {win_rate:.1f}% ({len(wins)}/{total})",
        f"总盈亏: {pnl:.2f} USDT | 平均每笔: {avg:.2f} USDT",
        f"方向: LONG {long_count} / SHORT {short_count}",
        f"10单全满: {full}",
        f"最好: {best['symbol']} {best['side']} {best['pnl']:.2f} USDT",
        f"最差: {worst['symbol']} {worst['side']} {worst['pnl']:.2f} USDT",
        f"止损次数: {sum(1 for t in trades if t['exit_reason'] == 'hard-stop')}",
        f"锁利/动态止盈/回撤退出: {sum(1 for t in trades if 'profit' in t['exit_reason'] or 'dynamic' in t['exit_reason'])}",
        f"策略组联动退出: {sum(1 for t in trades if t.get('strategy_group_exit'))}",
    ])


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)

    parser = argparse.ArgumentParser(description="批量回测趋势双向阶梯策略")
    parser.add_argument("--symbols", help="逗号分隔交易对，例如 BTCUSDT,ETHUSDT")
    parser.add_argument("--top-volume", type=int, default=0, help="按当前24h成交额选前N个USDT永续")
    parser.add_argument("--radar-watchlist", action="store_true", help="使用庄家雷达 watchlist 表里的收筹池代币")
    parser.add_argument("--watchlist-limit", type=int, default=0, help="限制雷达收筹池读取数量，0表示不限制")
    parser.add_argument("--min-pool-score", type=float, default=0, help="雷达收筹池最低score过滤")
    parser.add_argument("--active-radar-only", action="store_true", help="排除状态仍为收筹中的币，只回测开始放量/放量启动标的")
    parser.add_argument("--all-perps", action="store_true", help="扫描全部USDT永续，时间会比较久")
    parser.add_argument("--start", required=True, help="UTC开始时间，例如 2026-01-01")
    parser.add_argument("--end", required=True, help="UTC结束时间，例如 2026-05-01")
    parser.add_argument("--interval", default="1h", choices=sorted(INTERVAL_MS.keys()), help="回测推进周期，建议1h/4h/1d")
    parser.add_argument("--scan-interval-bars", type=int, default=1, help="每多少根周期K线扫描一次，默认每根都扫")
    parser.add_argument("--horizon-hours", type=float, default=48, help="每个计划最多向后模拟多少小时")
    parser.add_argument("--cooldown-hours", type=float, default=12, help="同一币两次交易之间冷却小时数")
    parser.add_argument("--min-score", type=float, default=TREND_MIN_SCORE, help="覆盖默认trend_score阈值")
    parser.add_argument("--confirm-buffer-atr", type=float, default=CONFIRM_BUFFER_ATR, help="方向确认缓冲，单位ATR，默认0.05")
    parser.add_argument("--max-trades", type=int, default=0, help="最多回测多少笔交易，0表示不限制；只做一个突破可设为1")
    parser.add_argument("--output", default="backtests/trend_ladder_backtest", help="输出路径前缀，不含扩展名")
    args = parser.parse_args()

    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    elif args.radar_watchlist:
        symbols = radar_watchlist_symbols(
            limit=args.watchlist_limit,
            min_pool_score=args.min_pool_score,
            include_sleeping=not args.active_radar_only,
        )
    elif args.top_volume:
        symbols = top_volume_symbols(args.top_volume)
    elif args.all_perps:
        symbols = get_all_perp_symbols()
    else:
        symbols = top_volume_symbols(20)

    start_ms = int(parse_dt(args.start).timestamp() * 1000)
    end_ms = int(parse_dt(args.end).timestamp() * 1000)
    print(f"回测范围: {fmt_time(start_ms)} -> {fmt_time(end_ms)} UTC")
    print(f"代币数量: {len(symbols)} | {', '.join(symbols[:12])}{' ...' if len(symbols) > 12 else ''}")
    print(f"单方向风险预算: {TREND_RISK_BUDGET_USD:.2f} USDT | 10%止损反推名义: {format_usd(TREND_RISK_BUDGET_USD / TREND_STOP_LOSS_PCT)}")

    all_trades = []
    for i, symbol in enumerate(symbols, start=1):
        remaining = args.max_trades - len(all_trades) if args.max_trades else None
        if remaining is not None and remaining <= 0:
            break
        print(f"[{i}/{len(symbols)}] {symbol} ...")
        trades = backtest_symbol(symbol, start_ms, end_ms, args, remaining_trades=remaining)
        print(f"  trades={len(trades)}")
        all_trades.extend(trades)
        time.sleep(0.2)

    all_trades.sort(key=lambda t: (t["scan_time"], t["symbol"]))
    json_path, csv_path, orders_json_path, orders_csv_path = write_outputs(all_trades, ROOT / args.output)
    print("\n" + summarize(all_trades))
    print(f"\n已输出: {json_path}")
    print(f"已输出: {csv_path}")
    print(f"已输出: {orders_json_path}")
    print(f"已输出: {orders_csv_path}")


if __name__ == "__main__":
    main()
