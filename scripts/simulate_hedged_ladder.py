#!/usr/bin/env python3
"""
单币对冲趋势加仓纸上模拟。

用法：
  python3 scripts/simulate_hedged_ladder.py --symbol IOUSDT --reset
  python3 scripts/simulate_hedged_ladder.py --symbol IOUSDT

脚本不下真实订单，只把模拟状态保存在 JSON 文件里。
"""

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from accumulation_radar import (  # noqa: E402
    DB_PATH,
    TREND_RISK_BUDGET_USD,
    TREND_STOP_LOSS_PCT,
    api_get,
    fetch_intraday_klines,
    format_price,
)
from scripts.backtest_trend_ladder import (  # noqa: E402
    CONFIRM_BUFFER_ATR,
    HEDGE_STOP_PCT,
    PYRAMID_WEIGHTS,
    avg_entry,
    build_ladder,
    leg_notional,
    leg_pnl,
    profit_lock_ratio,
    score_snapshot,
)


DEFAULT_STATE = ROOT / ".hedged_ladder_sim.json"


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def load_state(path):
    if not path.exists():
        return None
    return json.loads(path.read_text())


def save_state(path, state):
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def radar_candidates(limit=30, active_only=True):
    if not DB_PATH.exists():
        return []
    conn = sqlite3.connect(str(DB_PATH))
    query = "SELECT symbol, score, status FROM watchlist"
    params = []
    if active_only:
        query += " WHERE status NOT LIKE ?"
        params.append("%收筹中%")
    query += " ORDER BY score DESC LIMIT ?"
    params.append(limit)
    rows = conn.execute(query, params).fetchall()
    if not rows and active_only:
        rows = conn.execute(
            "SELECT symbol, score, status FROM watchlist ORDER BY score DESC LIMIT ?",
            (limit,),
        ).fetchall()
    conn.close()
    return rows


def choose_radar_symbol():
    rows = radar_candidates(limit=1, active_only=True)
    return rows[0][0] if rows else ""


def current_snapshot(symbol, interval="1h"):
    klines = fetch_intraday_klines(symbol, interval, 220)
    if len(klines) < 120:
        raise RuntimeError(f"{symbol} K线不足，无法模拟")
    snapshot = score_snapshot(klines, klines, len(klines) - 1, len(klines) - 1, 0, enforce_filters=False)
    if not snapshot:
        raise RuntimeError(f"{symbol} 当前不满足基础趋势数据要求")
    snapshot["interval"] = interval
    return snapshot, klines[-1]


def choose_best_radar_symbol(interval="1h", limit=30, active_only=True):
    best = None
    errors = []
    for symbol, pool_score, status in radar_candidates(limit=limit, active_only=active_only):
        try:
            snapshot, bar = current_snapshot(symbol, interval)
        except Exception as exc:
            errors.append(f"{symbol}:{exc}")
            continue
        row = {
            "symbol": symbol,
            "pool_score": float(pool_score or 0),
            "status": status,
            "snapshot": snapshot,
            "bar": bar,
            "rank_score": snapshot["score"] + min(float(pool_score or 0), 100) * 0.25,
        }
        if best is None or row["rank_score"] > best["rank_score"]:
            best = row
    if not best:
        raise RuntimeError(f"雷达池前 {limit} 个币在 {interval} 周期都无法生成模拟快照；样例错误: {errors[:3]}")
    return best


def make_initial_state(symbol, snapshot):
    long_orders, short_orders = build_ladder(snapshot)
    long_fills = [long_orders[0]]
    short_fills = [short_orders[0]]
    return {
        "symbol": symbol,
        "created_at": utc_now(),
        "updated_at": utc_now(),
        "status": "HEDGED_PROBE",
        "trend_side": None,
        "snapshot": snapshot,
        "long_orders": long_orders,
        "short_orders": short_orders,
        "long_fills": long_fills,
        "short_fills": short_fills,
        "realized_hedge_pnl": 0.0,
        "realized_hedge_notional": 0.0,
        "hedge_closed": False,
        "hedge_close_price": 0.0,
        "peak_group_pnl": 0.0,
        "last_bar_time": 0,
        "events": [
            {
                "time": utc_now(),
                "type": "OPEN_HEDGE_PROBE",
                "message": f"LONG_1 + SHORT_1 opened at {snapshot['price']}",
            }
        ],
    }


def fill_trend_orders(state, bar):
    trend_side = state["trend_side"]
    orders = state["long_orders"] if trend_side == "LONG" else state["short_orders"]
    fills = state["long_fills"] if trend_side == "LONG" else state["short_fills"]
    for order in orders[1:]:
        if any(fill["index"] == order["index"] for fill in fills):
            continue
        touched = bar["high"] >= order["price"] if trend_side == "LONG" else bar["low"] <= order["price"]
        if touched:
            fills.append(order)
            state["events"].append({
                "time": utc_now(),
                "type": "FILL_TREND_ORDER",
                "message": f"{trend_side}_{order['index']} filled price={order['price']:.10f} notional={order['notional']:.2f}",
            })
    if trend_side == "LONG":
        state["long_fills"] = fills
    else:
        state["short_fills"] = fills


def maybe_confirm_trend(state, bar, confirm_buffer_atr):
    if state["trend_side"]:
        return
    snapshot = state["snapshot"]
    long_level = snapshot["upper"] + snapshot["atr"] * confirm_buffer_atr
    short_level = snapshot["lower"] - snapshot["atr"] * confirm_buffer_atr
    long_confirm = bar["high"] >= long_level or bar["close"] >= snapshot["upper"]
    short_confirm = bar["low"] <= short_level or bar["close"] <= snapshot["lower"]
    if long_confirm and short_confirm:
        state["trend_side"] = "LONG" if bar["close"] >= bar["open"] else "SHORT"
    elif long_confirm:
        state["trend_side"] = "LONG"
    elif short_confirm:
        state["trend_side"] = "SHORT"
    if state["trend_side"]:
        state["status"] = f"{state['trend_side']}_ACTIVE"
        state["events"].append({
            "time": utc_now(),
            "type": "CONFIRM_TREND",
            "message": f"trend confirmed: {state['trend_side']}",
        })


def maybe_close_hedge(state, bar):
    if state["hedge_closed"] or not state["trend_side"]:
        return
    if state["trend_side"] == "LONG":
        hedge_fills = state["short_fills"]
        hedge_entry = avg_entry(hedge_fills)
        if hedge_fills and bar["high"] >= hedge_entry * (1 + HEDGE_STOP_PCT):
            close_price = hedge_entry * (1 + HEDGE_STOP_PCT)
            state["realized_hedge_pnl"] += leg_pnl("SHORT", hedge_fills, close_price)
            state["realized_hedge_notional"] += leg_notional(hedge_fills)
            state["short_fills"] = []
            state["hedge_closed"] = True
            state["hedge_close_price"] = close_price
            state["events"].append({
                "time": utc_now(),
                "type": "CLOSE_HEDGE_STOP",
                "message": f"SHORT hedge stopped at {close_price:.10f}",
            })
    else:
        hedge_fills = state["long_fills"]
        hedge_entry = avg_entry(hedge_fills)
        if hedge_fills and bar["low"] <= hedge_entry * (1 - HEDGE_STOP_PCT):
            close_price = hedge_entry * (1 - HEDGE_STOP_PCT)
            state["realized_hedge_pnl"] += leg_pnl("LONG", hedge_fills, close_price)
            state["realized_hedge_notional"] += leg_notional(hedge_fills)
            state["long_fills"] = []
            state["hedge_closed"] = True
            state["hedge_close_price"] = close_price
            state["events"].append({
                "time": utc_now(),
                "type": "CLOSE_HEDGE_STOP",
                "message": f"LONG hedge stopped at {close_price:.10f}",
            })


def current_group_pnl(state, price):
    return (
        state["realized_hedge_pnl"]
        + leg_pnl("LONG", state["long_fills"], price)
        + leg_pnl("SHORT", state["short_fills"], price)
    )


def total_notional(state):
    return leg_notional(state["long_fills"]) + leg_notional(state["short_fills"]) + state["realized_hedge_notional"]


def maybe_exit_profit_lock(state, bar):
    if not state["trend_side"] or not state["hedge_closed"]:
        return
    mark_price = bar["close"]
    pnl = current_group_pnl(state, mark_price)
    notional = total_notional(state)
    state["peak_group_pnl"] = max(state["peak_group_pnl"], pnl)
    trend_fills = state["long_fills"] if state["trend_side"] == "LONG" else state["short_fills"]
    lock_ratio = profit_lock_ratio(len(trend_fills), state["peak_group_pnl"], notional)
    if lock_ratio <= 0:
        return
    guard = state["peak_group_pnl"] * lock_ratio
    if pnl <= guard:
        state["status"] = "COMPLETED"
        state["events"].append({
            "time": utc_now(),
            "type": "EXIT_GROUP_PROFIT_LOCK",
            "message": f"group exit pnl={pnl:.2f} guard={guard:.2f}",
        })


def advance_state(state, bar, confirm_buffer_atr):
    if state["status"] == "COMPLETED":
        return state
    if state.get("last_bar_time") == bar["open_time"]:
        return state
    state["last_bar_time"] = bar["open_time"]
    maybe_confirm_trend(state, bar, confirm_buffer_atr)
    if state["trend_side"]:
        maybe_close_hedge(state, bar)
        fill_trend_orders(state, bar)
        maybe_exit_profit_lock(state, bar)
    state["updated_at"] = utc_now()
    return state


def print_state(state, bar):
    symbol = state["symbol"]
    price = bar["close"]
    pnl = current_group_pnl(state, price)
    notional = total_notional(state)
    long_notional = leg_notional(state["long_fills"])
    short_notional = leg_notional(state["short_fills"])
    trend_fills = state["long_fills"] if state.get("trend_side") == "LONG" else state["short_fills"]
    print(f"\n{symbol} 单币纸上模拟")
    print(f"周期: {state.get('snapshot', {}).get('interval', '1h')} | 状态: {state['status']} | 趋势方向: {state.get('trend_side') or '-'} | 当前价: {format_price(price)}")
    print(f"策略组净盈亏: {pnl:.2f}U | 名义仓位: {notional:.2f}U | 峰值净盈亏: {state.get('peak_group_pnl', 0):.2f}U")
    print(f"LONG仓位: {long_notional:.2f}U / {len(state['long_fills'])}单")
    print(f"SHORT仓位: {short_notional:.2f}U / {len(state['short_fills'])}单")
    print(f"对冲腿已平: {state['hedge_closed']} | 对冲已实现: {state['realized_hedge_pnl']:.2f}U")
    print("最近事件:")
    for event in state["events"][-8:]:
        print(f"  {event['type']}: {event['message']}")


def main():
    parser = argparse.ArgumentParser(description="单币对冲趋势加仓纸上模拟")
    parser.add_argument("--symbol", help="指定一个交易对，例如 IOUSDT；不传则从雷达池取最高分启动币")
    parser.add_argument("--select-radar", action="store_true", help="按当前周期从雷达池重新评分，选择最强1个币")
    parser.add_argument("--radar-limit", type=int, default=30, help="从雷达池读取前N个候选重新评分")
    parser.add_argument("--include-sleeping", action="store_true", help="选币时包含仍处于收筹中的币")
    parser.add_argument("--state-file", default=str(DEFAULT_STATE), help="模拟状态文件")
    parser.add_argument("--reset", action="store_true", help="重置并重新开首组对冲试探")
    parser.add_argument("--interval", default="1h", help="趋势判断周期，默认1h；建议用1h/2h/4h")
    parser.add_argument("--confirm-buffer-atr", type=float, default=CONFIRM_BUFFER_ATR, help="方向确认缓冲，单位ATR")
    args = parser.parse_args()

    state_path = Path(args.state_file)
    selected = None
    if args.select_radar:
        selected = choose_best_radar_symbol(args.interval, args.radar_limit, active_only=not args.include_sleeping)
        symbol = selected["symbol"]
    else:
        symbol = (args.symbol or "").upper() or choose_radar_symbol()
    if not symbol:
        raise SystemExit("没有指定 symbol，雷达池也没有可用代币")

    if selected:
        snapshot, bar = selected["snapshot"], selected["bar"]
        print(
            f"雷达周期选币: {symbol} | 雷达分={selected['pool_score']:.1f} | "
            f"周期分={snapshot['score']:.1f} | 状态={selected['status']}"
        )
    else:
        snapshot, bar = current_snapshot(symbol, args.interval)
    state = None if args.reset else load_state(state_path)
    if not state or state.get("symbol") != symbol:
        state = make_initial_state(symbol, snapshot)
    else:
        state["snapshot"] = snapshot
    state = advance_state(state, bar, args.confirm_buffer_atr)
    save_state(state_path, state)
    print_state(state, bar)
    print(f"\n状态文件: {state_path}")


if __name__ == "__main__":
    main()
