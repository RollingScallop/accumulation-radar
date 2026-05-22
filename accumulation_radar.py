#!/usr/bin/env python3
"""
庄家收筹雷达 v1 — 发现庄家横盘吸筹 + OI异动

核心逻辑：
1. 庄家拉盘前必须先收筹 → 长期横盘+低量 = 收筹中
2. OI暴涨 = 大资金进场建仓 = 即将拉盘
3. 两个信号叠加 = 最强信号

两个模块：
A. 横盘收筹标的池（每天扫一次）→ 找正在被庄家收筹的币
B. OI异动监控（每小时扫）→ 标的池内的币有OI异动立即报警

数据源：币安合约API（免费公开，零成本）
"""

import json
import os
import sys
import time
import requests
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

# === 加载 .env ===
env_file = Path(__file__).parent / ".env.oi"
if env_file.exists():
    with open(env_file) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

# === 配置 ===
TG_BOT_TOKEN = os.getenv("TG_BOT_TOKEN", "")
TG_CHAT_ID = os.getenv("TG_CHAT_ID", "")
TG_OBSERVER_CHAT_ID = os.getenv("TG_OBSERVER_CHAT_ID", "")
ENABLE_TG_PUSH = os.getenv("ENABLE_TG_PUSH", "false").lower() == "true"
AI_TRADER_WEBHOOK_URL = os.getenv("AI_TRADER_WEBHOOK_URL", "http://127.0.0.1:3000/api/webhooks/radar")
AI_TRADER_WEBHOOK_SECRET = os.getenv("AI_TRADER_WEBHOOK_SECRET", "")
AI_TRADER_AUTO_EXECUTE = os.getenv("AI_TRADER_AUTO_EXECUTE", "false").lower() == "true"
AI_TRADER_SIGNAL_LIMIT = int(os.getenv("AI_TRADER_SIGNAL_LIMIT", "3"))
RADAR_OBSERVE_BEFORE_ENTRY = os.getenv("RADAR_OBSERVE_BEFORE_ENTRY", "true").lower() == "true"
OBSERVE_MAX_CANDIDATES = int(os.getenv("OBSERVE_MAX_CANDIDATES", "8"))
OBSERVE_LOOKBACK_HOURS = int(os.getenv("OBSERVE_LOOKBACK_HOURS", "48"))
OBSERVER_RISK_BUDGET_USD = float(os.getenv("OBSERVER_RISK_BUDGET_USD", "100"))
OBSERVER_LIQUIDATION_BUFFER_PCT = float(os.getenv("OBSERVER_LIQUIDATION_BUFFER_PCT", "0.015"))
FAPI = "https://fapi.binance.com"
DB_PATH = Path(__file__).parent / "accumulation.db"

# 收筹标的池参数
MIN_SIDEWAYS_DAYS = 45        # 至少横盘45天
MAX_RANGE_PCT = 80            # 横盘期价格波动<80%（宽松点，庄家盘波动可以大）
MAX_AVG_VOL_USD = 20_000_000  # 日均成交<$20M（低量才是收筹）
MIN_DATA_DAYS = 50            # 至少50天数据

# OI异动参数
MIN_OI_DELTA_PCT = 3.0        # OI变化至少3%
MIN_OI_USD = 2_000_000        # 最低OI门槛 $2M

# 放量突破参数
VOL_BREAKOUT_MULT = 3.0       # 当日Vol > 3x均值 = 放量


def api_get(endpoint, params=None):
    """币安API请求"""
    url = f"{FAPI}{endpoint}"
    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 429:
                time.sleep(2)
            else:
                return None
        except:
            time.sleep(1)
    return None


def init_db():
    """初始化数据库"""
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS watchlist (
        symbol TEXT PRIMARY KEY,
        coin TEXT,
        added_date TEXT,
        sideways_days INT,
        range_pct REAL,
        avg_vol REAL,
        low_price REAL,
        high_price REAL,
        current_price REAL,
        score REAL,
        status TEXT DEFAULT 'watching',
        last_oi_alert TEXT,
        notes TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS alerts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT,
        alert_type TEXT,
        alert_time TEXT,
        price REAL,
        oi_delta_pct REAL,
        vol_ratio REAL,
        details TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS market_snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        snapshot_time TEXT,
        mode TEXT,
        symbol TEXT,
        coin TEXT,
        price REAL,
        px_chg_pct REAL,
        vol_24h REAL,
        funding_rate REAL,
        oi_usd REAL,
        oi_d1h_pct REAL,
        oi_d6h_pct REAL,
        est_mcap REAL,
        circ_supply REAL,
        in_watchlist INTEGER,
        watchlist_status TEXT,
        radar_score REAL,
        sideways_days INT,
        UNIQUE(snapshot_time, symbol)
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS entry_watchlist (
        symbol TEXT PRIMARY KEY,
        candidate_time TEXT,
        source TEXT,
        source_reason TEXT,
        radar_status TEXT,
        radar_score REAL,
        strength REAL,
        confidence REAL,
        reference_price REAL,
        snapshot_price REAL,
        snapshot_oi_d1h_pct REAL,
        snapshot_oi_d6h_pct REAL,
        snapshot_funding_rate REAL,
        est_mcap REAL,
        watch_status TEXT DEFAULT 'WATCHING',
        last_analysis_time TEXT,
        trend TEXT,
        setup_type TEXT,
        support_price REAL,
        resistance_price REAL,
        suggested_entry REAL,
        suggested_stop REAL,
        trigger_time TEXT,
        notes TEXT
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS report_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        report_key TEXT,
        sent_at TEXT,
        symbols_json TEXT
    )""")
    conn.commit()
    return conn


def get_all_perp_symbols():
    """获取所有USDT永续合约"""
    info = api_get("/fapi/v1/exchangeInfo")
    if not info:
        return []
    return [s["symbol"] for s in info["symbols"]
            if s["quoteAsset"] == "USDT" 
            and s["contractType"] == "PERPETUAL"
            and s["status"] == "TRADING"]


def analyze_accumulation(symbol, klines):
    """分析单个币的收筹特征"""
    if len(klines) < MIN_DATA_DAYS:
        return None
    
    data = []
    for k in klines:
        data.append({
            "ts": k[0],
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "vol": float(k[7]),  # quote volume (USDT)
        })
    
    coin = symbol.replace("USDT", "")
    
    # === 排除稳定币和指数 ===
    EXCLUDE = {"USDC", "USDP", "TUSD", "FDUSD", "BTCDOM", "DEFI", "USDM"}
    if coin in EXCLUDE:
        return None
    
    # === 排除已经暴涨过+崩盘的币 ===
    # 最近7天vs之前的均价，如果已经涨>300%就跳过（来不及了）
    recent_7d = data[-7:]
    prior = data[:-7]
    if not prior:
        return None
    
    recent_avg_px = sum(d["close"] for d in recent_7d) / len(recent_7d)
    prior_avg_px = sum(d["close"] for d in prior) / len(prior)
    
    if prior_avg_px > 0 and ((recent_avg_px - prior_avg_px) / prior_avg_px) > 3.0:
        return None  # 已经涨了300%+，来不及了
    
    # === 寻找横盘区间 ===
    # 从最近往回找，找最长的横盘期（价格波动<MAX_RANGE_PCT%）
    best_sideways = 0
    best_range = 0
    best_low = 0
    best_high = 0
    best_avg_vol = 0
    
    # 用滑动窗口从60天到全部
    for window in range(MIN_SIDEWAYS_DAYS, len(prior) + 1):
        window_data = prior[-window:]
        lows = [d["low"] for d in window_data]
        highs = [d["high"] for d in window_data]
        
        w_low = min(lows)
        w_high = max(highs)
        
        if w_low <= 0:
            continue
        
        range_pct = ((w_high - w_low) / w_low) * 100
        
        if range_pct <= MAX_RANGE_PCT:
            avg_vol = sum(d["vol"] for d in window_data) / len(window_data)
            if avg_vol <= MAX_AVG_VOL_USD:
                if window > best_sideways:
                    best_sideways = window
                    best_range = range_pct
                    best_low = w_low
                    best_high = w_high
                    best_avg_vol = avg_vol
    
    if best_sideways < MIN_SIDEWAYS_DAYS:
        return None
    
    # === 计算收筹评分 ===
    # 横盘越久越好（庄家需要时间吸筹）
    days_score = min(best_sideways / 90, 1.0) * 25  # 90天满分25
    
    # 区间越窄越好（控盘紧）
    range_score = max(0, (1 - best_range / MAX_RANGE_PCT)) * 20  # 越窄越高，满分20
    
    # 成交量越低越好（死水一潭 = 筹码集中）
    vol_score = max(0, (1 - best_avg_vol / MAX_AVG_VOL_USD)) * 20  # 越低越高，满分20
    
    # 最近是否开始放量？（放量是启动信号）
    recent_vol = sum(d["vol"] for d in recent_7d) / len(recent_7d)
    vol_breakout = recent_vol / best_avg_vol if best_avg_vol > 0 else 0
    breakout_score = min(vol_breakout / VOL_BREAKOUT_MULT, 1.0) * 15  # 放量加分，满分15
    
    # 市值越低空间越大（核心！低市值=大空间）
    # 用当前价格*日均成交量/换手率来粗估市值排名
    # 实际市值在推送时用CoinGecko补充
    est_mcap = data[-1]["close"] * best_avg_vol * 30  # 粗略估算
    if est_mcap > 0 and est_mcap < 50_000_000:
        mcap_score = 20  # <$50M 满分
    elif est_mcap < 100_000_000:
        mcap_score = 15
    elif est_mcap < 200_000_000:
        mcap_score = 10
    elif est_mcap < 500_000_000:
        mcap_score = 5
    else:
        mcap_score = 0
    
    total_score = days_score + range_score + vol_score + breakout_score + mcap_score
    
    # 状态判断
    if vol_breakout >= VOL_BREAKOUT_MULT:
        status = "🔥放量启动"
    elif vol_breakout >= 1.5:
        status = "⚡开始放量"
    else:
        status = "💤收筹中"
    
    return {
        "symbol": symbol,
        "coin": coin,
        "sideways_days": best_sideways,
        "range_pct": best_range,
        "low_price": best_low,
        "high_price": best_high,
        "avg_vol": best_avg_vol,
        "current_price": data[-1]["close"],
        "recent_vol": recent_vol,
        "vol_breakout": vol_breakout,
        "score": total_score,
        "status": status,
        "data_days": len(data),
    }


def scan_accumulation_pool():
    """扫描全市场，找正在被收筹的币"""
    print("📊 扫描全市场收筹标的...")
    
    symbols = get_all_perp_symbols()
    print(f"  共 {len(symbols)} 个合约")
    
    results = []
    
    for i, sym in enumerate(symbols):
        klines = api_get("/fapi/v1/klines", {
            "symbol": sym, "interval": "1d", "limit": 180
        })
        
        if klines and isinstance(klines, list):
            r = analyze_accumulation(sym, klines)
            if r:
                results.append(r)
        
        if (i + 1) % 10 == 0:
            time.sleep(0.5)
        if (i + 1) % 100 == 0:
            print(f"  进度: {i+1}/{len(symbols)}... 已发现{len(results)}个")
    
    results.sort(key=lambda x: x["score"], reverse=True)
    print(f"  ✅ 发现 {len(results)} 个收筹标的")
    return results


def scan_oi_changes(watchlist_symbols):
    """对标的池内的币扫描OI异动"""
    print(f"📊 扫描OI异动（{len(watchlist_symbols)}个标的）...")
    
    alerts = []
    
    for sym in watchlist_symbols:
        # OI历史
        oi_hist = api_get("/futures/data/openInterestHist", {
            "symbol": sym, "period": "1h", "limit": 3
        })
        
        if not oi_hist or len(oi_hist) < 2:
            continue
        
        prev_oi = float(oi_hist[-2]["sumOpenInterestValue"])
        curr_oi = float(oi_hist[-1]["sumOpenInterestValue"])
        
        if prev_oi <= 0 or curr_oi < MIN_OI_USD:
            continue
        
        delta_pct = ((curr_oi - prev_oi) / prev_oi) * 100
        
        if abs(delta_pct) >= MIN_OI_DELTA_PCT:
            # 拿当前价格
            ticker = api_get("/fapi/v1/ticker/24hr", {"symbol": sym})
            if not ticker:
                continue
            
            price = float(ticker["lastPrice"])
            vol_24h = float(ticker["quoteVolume"])
            px_chg = float(ticker["priceChangePercent"])
            
            # 拿费率
            funding = api_get("/fapi/v1/fundingRate", {"symbol": sym, "limit": 1})
            fr = float(funding[0]["fundingRate"]) if funding else 0
            
            coin = sym.replace("USDT", "")
            
            alerts.append({
                "symbol": sym,
                "coin": coin,
                "price": price,
                "oi_usd": curr_oi,
                "oi_delta_pct": delta_pct,
                "oi_delta_usd": curr_oi - prev_oi,
                "vol_24h": vol_24h,
                "px_chg_pct": px_chg,
                "funding_rate": fr,
            })
        
        time.sleep(0.3)
    
    alerts.sort(key=lambda x: abs(x["oi_delta_pct"]), reverse=True)
    print(f"  ✅ 发现 {len(alerts)} 个OI异动")
    return alerts


def format_usd(v):
    if v >= 1e9: return f"${v/1e9:.1f}B"
    if v >= 1e6: return f"${v/1e6:.1f}M"
    if v >= 1e3: return f"${v/1e3:.0f}K"
    return f"${v:.0f}"


def format_price(v):
    """统一显示三位有效数字（0除外）"""
    if v == 0:
        return "$0.00"
    return f"${v:.3g}"


def coin_label(coin, is_new=False):
    return f"{coin}(新)" if is_new else coin


def load_recent_report_symbols(conn, report_key, limit=3):
    """读取最近几条同类报告里出现过的代币。"""
    c = conn.cursor()
    c.execute(
        """SELECT symbols_json
           FROM report_history
           WHERE report_key = ?
           ORDER BY id DESC
           LIMIT ?""",
        (report_key, limit),
    )
    seen = set()
    for (symbols_json,) in c.fetchall():
        try:
            symbols = json.loads(symbols_json or "[]")
        except json.JSONDecodeError:
            symbols = []
        for symbol in symbols:
            if symbol:
                seen.add(symbol)
    return seen


def save_report_symbols(conn, report_key, symbols):
    """记录本次报告出现过的代币，并只保留最近三条。"""
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    payload = json.dumps(sorted(set(symbols)), ensure_ascii=False)
    c = conn.cursor()
    c.execute(
        "INSERT INTO report_history (report_key, sent_at, symbols_json) VALUES (?, ?, ?)",
        (report_key, now, payload),
    )
    c.execute(
        """DELETE FROM report_history
           WHERE report_key = ?
             AND id NOT IN (
                 SELECT id FROM report_history
                 WHERE report_key = ?
                 ORDER BY id DESC
                 LIMIT 3
             )""",
        (report_key, report_key),
    )
    conn.commit()


def ema(values, period):
    """简单EMA，返回与输入等长的序列。"""
    if not values:
        return []
    alpha = 2 / (period + 1)
    result = [values[0]]
    for value in values[1:]:
        result.append((value * alpha) + (result[-1] * (1 - alpha)))
    return result


def sma(values, period):
    """简单移动均值。"""
    if len(values) < period:
        return sum(values) / len(values) if values else 0
    window = values[-period:]
    return sum(window) / len(window)


def fetch_intraday_klines(symbol, interval="15m", limit=120):
    rows = api_get("/fapi/v1/klines", {"symbol": symbol, "interval": interval, "limit": limit})
    if not rows or not isinstance(rows, list):
        return []
    data = []
    for item in rows:
        data.append({
            "open_time": item[0],
            "open": float(item[1]),
            "high": float(item[2]),
            "low": float(item[3]),
            "close": float(item[4]),
            "volume": float(item[7]),
        })
    return data


def analyze_observation_candidate(candidate):
    """基于 15m 趋势 + 5m 触发，判断是否适合回调/突破入场。"""
    klines_15m = fetch_intraday_klines(candidate["symbol"], "15m", 120)
    klines_5m = fetch_intraday_klines(candidate["symbol"], "5m", 120)
    if len(klines_15m) < 30 or len(klines_5m) < 30:
        return {
            "watch_status": "WATCHING",
            "trend": "UNKNOWN",
            "setup_type": "insufficient-data",
            "support_price": 0,
            "resistance_price": 0,
            "suggested_entry": 0,
            "suggested_stop": 0,
            "notes": "not-enough-intraday-klines",
            "trigger_signal": None,
        }

    closes_15m = [k["close"] for k in klines_15m]
    lows_15m = [k["low"] for k in klines_15m]
    highs_15m = [k["high"] for k in klines_15m]
    vols_15m = [k["volume"] for k in klines_15m]
    closes_5m = [k["close"] for k in klines_5m]
    lows_5m = [k["low"] for k in klines_5m]
    highs_5m = [k["high"] for k in klines_5m]
    vols_5m = [k["volume"] for k in klines_5m]

    ema9_15m = ema(closes_15m, 9)
    ema21_15m = ema(closes_15m, 21)
    ema8_5m = ema(closes_5m, 8)
    ema21_5m = ema(closes_5m, 21)

    current_price = closes_5m[-1]
    support_15m = min(lows_15m[-8:])
    support_5m = min(lows_5m[-12:])
    support_price = max(support_15m, support_5m)
    resistance_price = max(highs_15m[-8:-1])

    if current_price <= 0 or support_price <= 0 or current_price <= support_price:
        return {
            "watch_status": "INVALIDATED",
            "trend": "BROKEN",
            "setup_type": "support-broken",
            "support_price": support_price,
            "resistance_price": resistance_price,
            "suggested_entry": 0,
            "suggested_stop": 0,
            "notes": "current price under support",
            "trigger_signal": None,
        }

    support_distance_pct = (current_price - support_price) / current_price
    recent_low_15m = min(lows_15m[-24:])
    recent_run_pct = (current_price - recent_low_15m) / recent_low_15m if recent_low_15m > 0 else 0
    volume_ratio_5m = vols_5m[-1] / max(sma(vols_5m[:-1], 20), 1)
    higher_lows = min(lows_15m[-4:]) >= min(lows_15m[-8:-4]) * 0.995
    bullish_trend = (
        current_price > ema21_15m[-1]
        and ema9_15m[-1] > ema21_15m[-1]
        and ema9_15m[-1] >= ema9_15m[-4]
        and higher_lows
    )

    oi_ok = candidate["snapshot_oi_d6h_pct"] > 0 or candidate["snapshot_oi_d1h_pct"] > 0
    funding_ok = candidate["snapshot_funding_rate"] <= 0.00015
    snapshot_gate = oi_ok and funding_ok

    pullback_ready = (
        bullish_trend
        and 0.008 <= support_distance_pct <= 0.035
        and closes_5m[-1] > ema8_5m[-1] > ema21_5m[-1]
        and min(lows_5m[-6:]) >= support_price * 0.998
        and recent_run_pct <= 0.18
    )
    breakout_ready = (
        bullish_trend
        and support_distance_pct <= 0.055
        and current_price >= resistance_price * 0.998
        and closes_5m[-1] > ema8_5m[-1]
        and volume_ratio_5m >= 1.35
        and recent_run_pct <= 0.22
    )

    if recent_run_pct >= 0.25 and support_distance_pct < 0.012:
        return {
            "watch_status": "WATCHING",
            "trend": "EXTENDED",
            "setup_type": "wait-reset",
            "support_price": support_price,
            "resistance_price": resistance_price,
            "suggested_entry": 0,
            "suggested_stop": 0,
            "notes": f"too extended recent_run={recent_run_pct*100:.1f}% support_gap={support_distance_pct*100:.1f}%",
            "trigger_signal": None,
        }

    if not snapshot_gate:
        return {
            "watch_status": "WATCHING",
            "trend": "BULLISH" if bullish_trend else "SIDEWAYS",
            "setup_type": "wait-oi-funding",
            "support_price": support_price,
            "resistance_price": resistance_price,
            "suggested_entry": 0,
            "suggested_stop": 0,
            "notes": f"snapshot gate unmet oi1h={candidate['snapshot_oi_d1h_pct']:+.1f}% oi6h={candidate['snapshot_oi_d6h_pct']:+.1f}% funding={candidate['snapshot_funding_rate']*100:+.4f}%",
            "trigger_signal": None,
        }

    def build_position_fields(entry_price, stop_price):
        stop_distance = max(entry_price - stop_price, 0)
        if stop_distance <= 0 or entry_price <= 0:
            return 0, 1
        quantity = round(OBSERVER_RISK_BUDGET_USD / stop_distance, 3)
        stop_distance_pct = stop_distance / entry_price
        max_safe_leverage = max(1, int(1 / (stop_distance_pct + OBSERVER_LIQUIDATION_BUFFER_PCT)))
        return quantity, max_safe_leverage

    if pullback_ready:
        entry_price = current_price
        stop_price = round(support_price * 0.996, 10)
        quantity, leverage = build_position_fields(entry_price, stop_price)
        reason = (
            f"observer pullback: trend bullish, support={support_price:.6f}, "
            f"oi1h={candidate['snapshot_oi_d1h_pct']:+.1f}% oi6h={candidate['snapshot_oi_d6h_pct']:+.1f}% "
            f"funding={candidate['snapshot_funding_rate']*100:+.4f}%"
        )
        return {
            "watch_status": "READY",
            "trend": "BULLISH",
            "setup_type": "pullback-entry",
            "support_price": support_price,
            "resistance_price": resistance_price,
            "suggested_entry": entry_price,
            "suggested_stop": stop_price,
            "notes": reason,
            "trigger_signal": {
                "symbol": candidate["symbol"],
                "signalType": "BUY",
                "strength": min(92, max(74, int(candidate["strength"] + 8))),
                "confidence": round(min(0.9, max(0.72, candidate["confidence"] + 0.08)), 2),
                "entryPrice": entry_price,
                "stopLoss": stop_price,
                "takeProfit": round(entry_price * 1.25, 10),
                "leverage": leverage,
                "quantity": quantity,
                "interval": "5m",
                "confirmInterval": "15m",
                "autoExecute": AI_TRADER_AUTO_EXECUTE,
                "source": "accumulation-radar-observer",
                "reason": reason,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        }

    if breakout_ready:
        entry_price = round(max(current_price, resistance_price * 1.001), 10)
        stop_price = round(support_price * 0.996, 10)
        quantity, leverage = build_position_fields(entry_price, stop_price)
        reason = (
            f"observer breakout: resistance={resistance_price:.6f} broken with vol x{volume_ratio_5m:.2f}, "
            f"oi1h={candidate['snapshot_oi_d1h_pct']:+.1f}% oi6h={candidate['snapshot_oi_d6h_pct']:+.1f}% "
            f"funding={candidate['snapshot_funding_rate']*100:+.4f}%"
        )
        return {
            "watch_status": "READY",
            "trend": "BULLISH",
            "setup_type": "breakout-entry",
            "support_price": support_price,
            "resistance_price": resistance_price,
            "suggested_entry": entry_price,
            "suggested_stop": stop_price,
            "notes": reason,
            "trigger_signal": {
                "symbol": candidate["symbol"],
                "signalType": "BUY",
                "strength": min(95, max(76, int(candidate["strength"] + 10))),
                "confidence": round(min(0.92, max(0.74, candidate["confidence"] + 0.1)), 2),
                "entryPrice": entry_price,
                "stopLoss": stop_price,
                "takeProfit": round(entry_price * 1.28, 10),
                "leverage": leverage,
                "quantity": quantity,
                "interval": "5m",
                "confirmInterval": "15m",
                "autoExecute": AI_TRADER_AUTO_EXECUTE,
                "source": "accumulation-radar-observer",
                "reason": reason,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        }

    setup_type = "wait-pullback" if bullish_trend and support_distance_pct > 0.035 else "wait-breakout"
    return {
        "watch_status": "WATCHING",
        "trend": "BULLISH" if bullish_trend else "SIDEWAYS",
        "setup_type": setup_type,
        "support_price": support_price,
        "resistance_price": resistance_price,
        "suggested_entry": 0,
        "suggested_stop": 0,
        "notes": f"trend={'bullish' if bullish_trend else 'sideways'} support_gap={support_distance_pct*100:.1f}% run={recent_run_pct*100:.1f}% volx={volume_ratio_5m:.2f}",
        "trigger_signal": None,
    }


def _post_telegram_message(url, payload, label, attempts=3):
    """发送单条TG消息，带重试"""
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            resp = requests.post(url, json=payload, timeout=15)
            if resp.status_code == 200:
                return True, f"[TG] {label} ✓"
            last_error = f"status={resp.status_code} body={resp.text[:200]}"
        except Exception as e:
            last_error = str(e)

        if attempt < attempts:
            print(f"[TG] {label} retry {attempt}/{attempts - 1}: {last_error}")
            time.sleep(min(2 * attempt, 5))

    return False, f"[TG] {label} ✗ {last_error}"


def build_pool_report(results, top_n=25):
    """生成收筹标的池报告"""
    if not results:
        return ""
    
    now = datetime.now(timezone(timedelta(hours=8)))
    
    lines = [
        f"🏦 **庄家收筹雷达** — 标的池更新",
        f"⏰ {now.strftime('%Y-%m-%d %H:%M')} CST",
        f"━━━━━━━━━━━━━━━━━━",
        f"扫描 {len(results)} 个合约，发现标的：",
        "",
    ]
    
    # 分组：放量启动 > 开始放量 > 收筹中
    firing = [r for r in results if "放量启动" in r["status"]]
    warming = [r for r in results if "开始放量" in r["status"]]
    sleeping = [r for r in results if "收筹中" in r["status"]]
    
    if firing:
        lines.append(f"🔥 **放量启动** ({len(firing)}个) — 最高优先级！")
        for r in firing[:10]:
            lines.append(
                f"  🔥 **{r['coin']}** | 分:{r['score']:.0f} | "
                f"横盘{r['sideways_days']}天 | 波动{r['range_pct']:.0f}% | "
                f"Vol放大{r['vol_breakout']:.1f}x"
            )
            lines.append(
                f"     ${r['current_price']:.6f} | "
                f"区间: ${r['low_price']:.6f}~${r['high_price']:.6f} | "
                f"日均Vol: {format_usd(r['avg_vol'])}"
            )
        lines.append("")
    
    if warming:
        lines.append(f"⚡ **开始放量** ({len(warming)}个) — 关注中")
        for r in warming[:10]:
            lines.append(
                f"  ⚡ {r['coin']} | 分:{r['score']:.0f} | "
                f"横盘{r['sideways_days']}天 | 波动{r['range_pct']:.0f}% | "
                f"Vol{r['vol_breakout']:.1f}x | "
                f"现价：{format_price(r['current_price'])}"
            )
        lines.append("")
    
    if sleeping:
        lines.append(f"💤 **收筹中** ({len(sleeping)}个) — 持续监控")
        for r in sleeping[:15]:
            lines.append(
                f"  💤 {r['coin']} | 分:{r['score']:.0f} | "
                f"横盘{r['sideways_days']}天 | 波动{r['range_pct']:.0f}% | "
                f"日均Vol {format_usd(r['avg_vol'])} | "
                f"现价：{format_price(r['current_price'])}"
            )
    
    return "\n".join(lines)


def build_oi_alert_report(alerts, watchlist_coins):
    """生成OI异动报告（只报标的池内的）"""
    if not alerts:
        return ""
    
    now = datetime.now(timezone(timedelta(hours=8)))
    
    # 区分：池内 vs 池外
    in_pool = [a for a in alerts if a["symbol"] in watchlist_coins]
    out_pool = [a for a in alerts if a["symbol"] not in watchlist_coins]
    
    lines = [
        f"📊 **OI异动扫描** [收筹池]",
        f"⏰ {now.strftime('%Y-%m-%d %H:%M')} CST",
        f"━━━━━━━━━━━━━━━━━━",
        "",
    ]
    
    if in_pool:
        lines.append(f"🎯 **收筹池内异动** ({len(in_pool)}个) ⚠️ 重点关注!")
        for a in in_pool[:10]:
            emoji = "🟢" if a["oi_delta_pct"] > 0 else "🔴"
            lines.append(
                f"  {emoji} **{a['coin']}** | OI: {a['oi_delta_pct']:+.1f}% "
                f"({format_usd(a['oi_usd'])}) | "
                f"24h: {a['px_chg_pct']:+.1f}% | 现价：{format_price(a['price'])}"
            )
            # 信号解读
            if a["oi_delta_pct"] > 0 and abs(a["px_chg_pct"]) < 3:
                lines.append(f"     ⚡ 暗流涌动！OI涨但价格平 = 庄家建仓中")
            elif a["oi_delta_pct"] > 0 and a["px_chg_pct"] > 3:
                lines.append(f"     🚀 放量拉升！OI+价格同涨 = 启动中")
        lines.append("")
    
    if out_pool:
        lines.append(f"📋 池外异动 ({len(out_pool)}个)")
        for a in out_pool[:8]:
            emoji = "🟢" if a["oi_delta_pct"] > 0 else "🔴"
            lines.append(
                f"  {emoji} {a['coin']} | OI: {a['oi_delta_pct']:+.1f}% | "
                f"24h: {a['px_chg_pct']:+.1f}% | 现价：{format_price(a['price'])}"
            )
    
    return "\n".join(lines)


def send_telegram(text, chat_id=None):
    """发送TG消息"""
    if not ENABLE_TG_PUSH:
        print("[TG] Disabled by ENABLE_TG_PUSH=false")
        return False
    if not TG_BOT_TOKEN:
        print("[TG] No token configured")
        return False
    target_chat_id = chat_id or TG_OBSERVER_CHAT_ID or TG_CHAT_ID
    if not target_chat_id:
        print("[TG] No chat id configured")
        return False
    
    url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
    
    # 分段发送（TG限制4096字）
    chunks = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > 3800:
            chunks.append(current)
            current = line
        else:
            current += "\n" + line if current else line
    if current:
        chunks.append(current)
    
    all_ok = True
    for chunk in chunks:
        ok, msg = _post_telegram_message(url, {
            "chat_id": target_chat_id,
            "text": chunk,
            "parse_mode": "Markdown"
        }, f"Sent ({len(chunk)} chars)")
        print(msg)

        if not ok:
            all_ok = False
            ok_plain, msg_plain = _post_telegram_message(url, {
                "chat_id": target_chat_id,
                "text": chunk.replace("*", "").replace("_", ""),
            }, f"Sent plain ({len(chunk)} chars)")
            print(msg_plain)
            all_ok = all_ok and ok_plain
        time.sleep(0.5)
    return all_ok


def emit_trade_signals(signals):
    """把雷达候选信号推送给 ai-trader 执行层。"""
    if not signals:
        print("[WEBHOOK] No trade signals to emit")
        return []

    if not AI_TRADER_WEBHOOK_URL:
        print("[WEBHOOK] AI_TRADER_WEBHOOK_URL not configured")
        return []

    headers = {"Content-Type": "application/json"}
    if AI_TRADER_WEBHOOK_SECRET:
        headers["x-webhook-secret"] = AI_TRADER_WEBHOOK_SECRET

    accepted = []
    for signal in signals[:AI_TRADER_SIGNAL_LIMIT]:
        payload = {"signal": signal}
        try:
            resp = requests.post(AI_TRADER_WEBHOOK_URL, json=payload, headers=headers, timeout=15)
            if resp.status_code == 200:
                print(f"[WEBHOOK] {signal['symbol']} {signal['signalType']} accepted")
                accepted.append(signal["symbol"])
            else:
                print(f"[WEBHOOK] {signal['symbol']} failed: {resp.status_code} {resp.text[:200]}")
        except Exception as e:
            print(f"[WEBHOOK] {signal['symbol']} error: {e}")
    return accepted


def build_trade_signals(chase, combined, ambush):
    """从雷达结果里提炼保守的自动交易信号。默认只发 BUY。"""
    candidates = []
    seen = set()
    now = datetime.now(timezone.utc).isoformat()

    for item in ambush:
        if item["sym"] in seen:
            continue
        if item["d6h"] < 3 or abs(item["px_chg"]) > 6:
            continue
        if item["sw_days"] < 45 or item["est_mcap"] > 300e6:
            continue

        price = item["price"]
        strength = min(95, max(72, int(item["total"])))
        confidence = min(0.9, max(0.68, 0.55 + item["total"] / 200))

        candidates.append({
            "symbol": item["sym"],
            "signalType": "BUY",
            "strength": strength,
            "confidence": round(confidence, 2),
            "entryPrice": price,
            "stopLoss": round(price * 0.93, 10),
            "takeProfit": round(price * 1.18, 10),
            "leverage": 3,
            "interval": "15m",
            "confirmInterval": "1h",
            "autoExecute": AI_TRADER_AUTO_EXECUTE,
            "source": "accumulation-radar",
            "reason": f"ambush score={item['total']} oi6h={item['d6h']:+.1f}% sideways={item['sw_days']}d mcap≈{item['est_mcap']:.0f}",
            "timestamp": now,
        })
        seen.add(item["sym"])

    for item in chase:
        chase_symbol = item.get("symbol") or item.get("sym")
        if not chase_symbol:
            continue
        if chase_symbol in seen:
            continue
        if item["px_chg"] < 4 or item["px_chg"] > 18:
            continue
        if item["fr_pct"] > -0.02 or item["vol"] < 5_000_000:
            continue

        price = item["price"]
        strength = 76 if "加速" in item.get("trend", "") else 71
        confidence = 0.69 if "加速" in item.get("trend", "") else 0.64

        candidates.append({
            "symbol": chase_symbol,
            "signalType": "BUY",
            "strength": strength,
            "confidence": confidence,
            "entryPrice": price,
            "stopLoss": round(price * 0.955, 10),
            "takeProfit": round(price * 1.12, 10),
            "leverage": 2,
            "interval": "5m",
            "confirmInterval": "15m",
            "autoExecute": AI_TRADER_AUTO_EXECUTE,
            "source": "accumulation-radar",
            "reason": f"chase funding={item['fr_pct']:.3f}% trend={item['trend']} px24h={item['px_chg']:+.1f}%",
            "timestamp": now,
        })
        seen.add(chase_symbol)

    for item in combined:
        if item["sym"] in seen:
            continue
        if item["total"] < 70 or item["d6h"] < 2:
            continue

        price = item["price"]
        confidence = min(0.85, max(0.66, 0.5 + item["total"] / 250))

        candidates.append({
            "symbol": item["sym"],
            "signalType": "BUY",
            "strength": min(88, item["total"]),
            "confidence": round(confidence, 2),
            "entryPrice": price,
            "stopLoss": round(price * 0.94, 10),
            "takeProfit": round(price * 1.15, 10),
            "leverage": 3,
            "interval": "15m",
            "confirmInterval": "1h",
            "autoExecute": AI_TRADER_AUTO_EXECUTE,
            "source": "accumulation-radar",
            "reason": f"combined score={item['total']} funding={item['fr_pct']:.3f}% oi6h={item['d6h']:+.1f}%",
            "timestamp": now,
        })
        seen.add(item["sym"])

    candidates.sort(key=lambda item: (item["confidence"], item["strength"]), reverse=True)
    return candidates[:AI_TRADER_SIGNAL_LIMIT]


def run_entry_observer(conn, limit=OBSERVE_MAX_CANDIDATES):
    """对观察池做 5m/15m 实时跟踪，只在更好的点位触发最终 BUY。"""
    candidates = load_active_watch_candidates(conn, limit=limit)
    if not candidates:
        print("  👀 观察池为空")
        return

    c = conn.cursor()
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    ready_signals = []
    report_lines = [f"👀 **观察池** {now} CST"]

    for candidate in candidates:
        analysis = analyze_observation_candidate(candidate)
        c.execute(
            """UPDATE entry_watchlist
               SET last_analysis_time = ?, watch_status = ?, trend = ?, setup_type = ?,
                   support_price = ?, resistance_price = ?, suggested_entry = ?, suggested_stop = ?,
                   notes = ?, trigger_time = CASE WHEN ? IS NOT NULL THEN ? ELSE trigger_time END
               WHERE symbol = ?""",
            (
                now,
                analysis["watch_status"],
                analysis["trend"],
                analysis["setup_type"],
                analysis["support_price"],
                analysis["resistance_price"],
                analysis["suggested_entry"],
                analysis["suggested_stop"],
                analysis["notes"],
                analysis["trigger_signal"]["timestamp"] if analysis["trigger_signal"] else None,
                now,
                candidate["symbol"],
            ),
        )

        setup_desc = analysis["setup_type"]
        report_lines.append(
            f"  {candidate['symbol']:<12} {analysis['watch_status']:<9} {setup_desc:<16} "
            f"支撑{format_price(analysis['support_price'])} 阻力{format_price(analysis['resistance_price'])}"
        )

        if analysis["trigger_signal"] is not None:
            ready_signals.append(analysis["trigger_signal"])

    conn.commit()

    if len(report_lines) > 1:
        send_telegram("\n".join(report_lines[:15]))

    if ready_signals:
        print(f"  ✅ 观察器触发 {len(ready_signals)} 个最终入场信号")
        accepted_symbols = emit_trade_signals(ready_signals[:AI_TRADER_SIGNAL_LIMIT])
        for sym in accepted_symbols:
            c.execute(
                "UPDATE entry_watchlist SET watch_status = 'TRIGGERED', trigger_time = ? WHERE symbol = ?",
                (now, sym),
            )
        conn.commit()
    else:
        print("  ⏳ 观察器本轮未触发最终入场")


def save_watchlist(conn, results):
    """保存标的池到数据库"""
    c = conn.cursor()
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    
    for r in results:
        c.execute("""INSERT OR REPLACE INTO watchlist 
            (symbol, coin, added_date, sideways_days, range_pct, avg_vol, 
             low_price, high_price, current_price, score, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (r["symbol"], r["coin"], now, r["sideways_days"], r["range_pct"],
             r["avg_vol"], r["low_price"], r["high_price"], r["current_price"],
             r["score"], r["status"]))
    
    conn.commit()
    print(f"  💾 保存 {len(results)} 个标的到数据库")


def load_watchlist_symbols(conn):
    """从数据库加载标的池"""
    c = conn.cursor()
    c.execute("SELECT symbol FROM watchlist WHERE status != 'removed'")
    return [row[0] for row in c.fetchall()]


def save_market_snapshots(conn, coin_data, pool_map, mode):
    """保存每次扫描得到的市场快照，供后续回放使用"""
    c = conn.cursor()
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    rows = []

    for sym, data in coin_data.items():
        pool = pool_map.get(sym, {})
        oi_usd = data.get("oi_usd", 0)
        circ_supply = data.get("circ_supply", 0)
        rows.append((
            now,
            mode,
            sym,
            data.get("coin", sym.replace("USDT", "")),
            data.get("price", 0),
            data.get("px_chg", 0),
            data.get("vol", 0),
            data.get("fr_pct", 0) / 100,
            oi_usd,
            data.get("d1h", 0),
            data.get("d6h", 0),
            data.get("est_mcap", 0),
            circ_supply,
            1 if data.get("in_pool") else 0,
            pool.get("status", ""),
            pool.get("pool_score", 0),
            pool.get("sideways_days", 0),
        ))

    c.executemany("""INSERT OR REPLACE INTO market_snapshots
        (snapshot_time, mode, symbol, coin, price, px_chg_pct, vol_24h,
         funding_rate, oi_usd, oi_d1h_pct, oi_d6h_pct, est_mcap, circ_supply,
         in_watchlist, watchlist_status, radar_score, sideways_days)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", rows)
    conn.commit()
    print(f"  💾 保存 {len(rows)} 条市场快照")


def queue_entry_watch_candidates(conn, signals, coin_data):
    """把雷达候选放进观察池，等待更好的回调/突破点入场。"""
    if not signals:
        print("  👀 无候选进入观察池")
        return

    c = conn.cursor()
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    count = 0

    for signal in signals[:OBSERVE_MAX_CANDIDATES]:
        sym = signal["symbol"]
        market = coin_data.get(sym, {})
        c.execute(
            """INSERT OR REPLACE INTO entry_watchlist
            (symbol, candidate_time, source, source_reason, radar_status, radar_score,
             strength, confidence, reference_price, snapshot_price, snapshot_oi_d1h_pct,
             snapshot_oi_d6h_pct, snapshot_funding_rate, est_mcap, watch_status,
             last_analysis_time, trend, setup_type, support_price, resistance_price,
             suggested_entry, suggested_stop, trigger_time, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                sym,
                now,
                signal.get("source", "accumulation-radar"),
                signal.get("reason", ""),
                market.get("status", ""),
                market.get("pool_sc", signal.get("strength", 0)),
                signal.get("strength", 0),
                signal.get("confidence", 0),
                signal.get("entryPrice", market.get("price", 0)),
                market.get("price", 0),
                market.get("d1h", 0),
                market.get("d6h", 0),
                market.get("fr_pct", 0) / 100,
                market.get("est_mcap", 0),
                "WATCHING",
                now,
                "",
                "",
                signal.get("stopLoss", 0),
                signal.get("takeProfit", 0),
                0,
                0,
                None,
                "queued-from-radar",
            ),
        )
        count += 1

    conn.commit()
    print(f"  👀 加入观察池 {count} 个候选")


def load_active_watch_candidates(conn, limit=OBSERVE_MAX_CANDIDATES):
    """读取仍在观察窗口内的候选。"""
    c = conn.cursor()
    cutoff = (datetime.now(timezone(timedelta(hours=8))) - timedelta(hours=OBSERVE_LOOKBACK_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    rows = c.execute(
        """
        SELECT symbol, candidate_time, source, source_reason, radar_status, radar_score,
               strength, confidence, reference_price, snapshot_price, snapshot_oi_d1h_pct,
               snapshot_oi_d6h_pct, snapshot_funding_rate, est_mcap, watch_status
        FROM entry_watchlist
        WHERE datetime(candidate_time) >= datetime(?)
          AND watch_status IN ('WATCHING', 'READY')
        ORDER BY radar_score DESC, candidate_time DESC
        LIMIT ?
        """,
        (cutoff, limit),
    ).fetchall()

    candidates = []
    for row in rows:
        candidates.append({
            "symbol": row[0],
            "candidate_time": row[1],
            "source": row[2],
            "source_reason": row[3],
            "radar_status": row[4],
            "radar_score": float(row[5] or 0),
            "strength": float(row[6] or 0),
            "confidence": float(row[7] or 0),
            "reference_price": float(row[8] or 0),
            "snapshot_price": float(row[9] or 0),
            "snapshot_oi_d1h_pct": float(row[10] or 0),
            "snapshot_oi_d6h_pct": float(row[11] or 0),
            "snapshot_funding_rate": float(row[12] or 0),
            "est_mcap": float(row[13] or 0),
            "watch_status": row[14],
        })
    return candidates


def seed_watchlist_for_observer(conn, limit=None, include_sleeping=True):
    """把当前雷达池整体加入观察池，供 observe 模式持续跟踪。"""
    c = conn.cursor()
    latest_snapshot_time_row = c.execute("SELECT MAX(snapshot_time) FROM market_snapshots").fetchone()
    latest_snapshot_time = latest_snapshot_time_row[0] if latest_snapshot_time_row and latest_snapshot_time_row[0] else None

    query = """
        SELECT w.symbol, w.coin, w.status, w.score, w.current_price, w.low_price, w.high_price,
               ms.price, ms.oi_d1h_pct, ms.oi_d6h_pct, ms.funding_rate, ms.est_mcap
        FROM watchlist w
        LEFT JOIN market_snapshots ms
          ON ms.symbol = w.symbol
         AND ms.snapshot_time = ?
        WHERE w.status != 'removed'
    """
    params = [latest_snapshot_time]
    if not include_sleeping:
        query += " AND w.status IN ('🔥放量启动', '⚡开始放量')"
    query += " ORDER BY w.score DESC"
    if limit:
        query += " LIMIT ?"
        params.append(limit)

    rows = c.execute(query, params).fetchall()
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    count = 0
    for row in rows:
        symbol, coin, status, score, current_price, low_price, high_price, snap_price, oi_d1h, oi_d6h, funding_rate, est_mcap = row
        reference_price = float(snap_price or current_price or 0)
        c.execute(
            """INSERT OR REPLACE INTO entry_watchlist
            (symbol, candidate_time, source, source_reason, radar_status, radar_score,
             strength, confidence, reference_price, snapshot_price, snapshot_oi_d1h_pct,
             snapshot_oi_d6h_pct, snapshot_funding_rate, est_mcap, watch_status,
             last_analysis_time, trend, setup_type, support_price, resistance_price,
             suggested_entry, suggested_stop, trigger_time, notes)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                symbol,
                now,
                "accumulation-radar-watchlist",
                "seeded-from-current-watchlist",
                status,
                float(score or 0),
                float(score or 0),
                0.7,
                reference_price,
                float(snap_price or reference_price),
                float(oi_d1h or 0),
                float(oi_d6h or 0),
                float(funding_rate or 0),
                float(est_mcap or 0),
                "WATCHING",
                now,
                "",
                "",
                float(low_price or 0),
                float(high_price or 0),
                0,
                0,
                None,
                "seeded-current-radar-watchlist",
            ),
        )
        count += 1

    conn.commit()
    print(f"  🌱 当前雷达池已加入观察池: {count} 个")
    return count


def scan_short_fuel():
    """策略2: 空头燃料 — 涨了+费率负+OI大 = 庄家拉盘爆空单"""
    print("📊 扫描空头燃料（费率为负+在涨的币）...")
    
    tickers = api_get("/fapi/v1/ticker/24hr")
    premiums = api_get("/fapi/v1/premiumIndex")
    
    if not tickers or not premiums:
        return [], []
    
    funding_map = {p["symbol"]: float(p["lastFundingRate"]) 
                   for p in premiums if p["symbol"].endswith("USDT")}
    
    fuel_targets = []     # 已在涨+费率负 = 正在squeeze
    squeeze_targets = []  # 费率极负+还没大涨 = 潜在squeeze
    
    for t in tickers:
        sym = t["symbol"]
        if not sym.endswith("USDT"):
            continue
        
        px_chg = float(t["priceChangePercent"])
        vol = float(t["quoteVolume"])
        fr = funding_map.get(sym, 0)
        coin = sym.replace("USDT", "")
        price = float(t["lastPrice"])
        
        item = {
            "coin": coin, "symbol": sym,
            "px_chg": px_chg, "funding": fr,
            "vol": vol, "price": price,
        }
        
        # 正在squeeze: 涨>5% + 费率负 + Vol>$5M
        if px_chg > 5 and fr < -0.0003 and vol > 5_000_000:
            item["fuel_score"] = abs(fr) * 10000 * px_chg
            fuel_targets.append(item)
        
        # 潜在squeeze: 费率很负 + 还没大涨(<10%) + Vol>$2M
        elif fr < -0.0005 and px_chg < 10 and vol > 2_000_000:
            item["fuel_score"] = abs(fr) * 10000
            squeeze_targets.append(item)
    
    fuel_targets.sort(key=lambda x: x["fuel_score"], reverse=True)
    squeeze_targets.sort(key=lambda x: x["fuel_score"], reverse=True)
    
    print(f"  ✅ 正在squeeze: {len(fuel_targets)}个, 潜在squeeze: {len(squeeze_targets)}个")
    return fuel_targets, squeeze_targets


def build_fuel_report(fuel_targets, squeeze_targets):
    """生成空头燃料报告"""
    if not fuel_targets and not squeeze_targets:
        return ""
    
    now = datetime.now(timezone(timedelta(hours=8)))
    lines = [
        f"🔥 **空头燃料扫描**",
        f"⏰ {now.strftime('%Y-%m-%d %H:%M')} CST",
        f"━━━━━━━━━━━━━━━━━━",
        f"逻辑：费率负=大量做空，庄家拉盘爆空单+收资金费",
        "",
    ]
    
    if fuel_targets:
        lines.append(f"🚀 **正在Squeeze** ({len(fuel_targets)}个) — 涨了+空头还在扛")
        for t in fuel_targets[:8]:
            fr_pct = t["funding"] * 100
            flag = "🎯极度!" if fr_pct < -0.1 else "⚠️"
            lines.append(
                f"  {flag} **{t['coin']}** | 涨{t['px_chg']:+.1f}% | "
                f"费率🧊{fr_pct:.4f}% | Vol {format_usd(t['vol'])} | "
                f"现价：{format_price(t['price'])}"
            )
        lines.append("")
    
    if squeeze_targets:
        lines.append(f"🎯 **潜在Squeeze** ({len(squeeze_targets)}个) — 费率极负+还没大涨")
        for t in squeeze_targets[:8]:
            fr_pct = t["funding"] * 100
            lines.append(
                f"  🧊 {t['coin']} | 24h{t['px_chg']:+.1f}% | "
                f"费率{fr_pct:.4f}% | Vol {format_usd(t['vol'])} | "
                f"现价：{format_price(t['price'])}"
            )
    
    return "\n".join(lines)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "full"
    
    print(f"🏦 庄家收筹雷达 v1 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"   模式: {mode}\n")
    
    conn = init_db()
    
    if mode in ("full", "pool"):
        # === 模块A: 更新收筹标的池 ===
        results = scan_accumulation_pool()
        
        if results:
            save_watchlist(conn, results)
            report = build_pool_report(results)
            if report:
                send_telegram(report)
    
    if mode in ("full", "oi"):
        # === 综合扫描：OI + 费率 + 收筹 三维合一 ===
        watchlist = load_watchlist_symbols(conn)
        watchlist_set = set(watchlist)
        
        if not watchlist:
            print("⚠️ 标的池为空，先运行 pool 模式")
            conn.close()
            return
        
        # 1. 拿全市场费率+行情
        tickers_raw = api_get("/fapi/v1/ticker/24hr")
        premiums_raw = api_get("/fapi/v1/premiumIndex")
        
        if not tickers_raw or not premiums_raw:
            print("❌ API失败")
            conn.close()
            return
        
        ticker_map = {}
        for t in tickers_raw:
            if t["symbol"].endswith("USDT"):
                ticker_map[t["symbol"]] = {
                    "px_chg": float(t["priceChangePercent"]),
                    "vol": float(t["quoteVolume"]),
                    "price": float(t["lastPrice"]),
                }
        
        funding_map = {}
        for p in premiums_raw:
            if p["symbol"].endswith("USDT"):
                funding_map[p["symbol"]] = float(p["lastFundingRate"])
        
        # 1.5 拉真实流通市值（币安现货API，一次全量）
        mcap_map = {}  # coin名 -> marketCap
        try:
            import requests as _req
            _r = _req.get("https://www.binance.com/bapi/composite/v1/public/marketing/symbol/list", timeout=10)
            if _r.status_code == 200:
                for item in _r.json().get("data", []):
                    name = item.get("name", "")
                    mc = item.get("marketCap", 0)
                    if name and mc:
                        mcap_map[name] = float(mc)
                print(f"✅ 拉到 {len(mcap_map)} 个币的真实市值")
        except Exception as e:
            print(f"⚠️ 市值API失败，走fallback: {e}")
        
        # 2. 从DB读收筹数据
        c2 = conn.cursor()
        c2.execute("SELECT symbol, score, sideways_days, range_pct, avg_vol, status FROM watchlist")
        pool_map = {}
        for row in c2.fetchall():
            pool_map[row[0]] = {"pool_score": row[1], "sideways_days": row[2], "range_pct": row[3], "avg_vol": row[4], "status": row[5]}
        
        # 3. 扫OI（标的池中放量的 + Top100）
        scan_syms = set()
        for sym, pd in pool_map.items():
            if "放量" in pd.get("status", "") or "开始" in pd.get("status", ""):
                scan_syms.add(sym)
        top_by_vol = sorted(ticker_map.items(), key=lambda x: x[1]["vol"], reverse=True)[:100]
        for sym, _ in top_by_vol:
            scan_syms.add(sym)
        
        oi_map = {}
        for i, sym in enumerate(scan_syms):
            oi_hist = api_get("/futures/data/openInterestHist", {"symbol": sym, "period": "1h", "limit": 6})
            if oi_hist and len(oi_hist) >= 2:
                curr = float(oi_hist[-1]["sumOpenInterestValue"])
                prev_1h = float(oi_hist[-2]["sumOpenInterestValue"])
                prev_6h = float(oi_hist[0]["sumOpenInterestValue"])
                d1h = ((curr - prev_1h) / prev_1h * 100) if prev_1h > 0 else 0
                d6h = ((curr - prev_6h) / prev_6h * 100) if prev_6h > 0 else 0
                circ_supply = float(oi_hist[-1].get("CMCCirculatingSupply", 0))
                oi_map[sym] = {"oi_usd": curr, "d1h": d1h, "d6h": d6h, "circ_supply": circ_supply}
            if (i+1) % 10 == 0:
                import time; time.sleep(0.5)
        
        # 4. 三策略独立评分
        
        # 共用数据预处理
        all_syms = set(list(pool_map.keys()) + list(oi_map.keys()))
        coin_data = {}
        for sym in all_syms:
            tk = ticker_map.get(sym, {})
            if not tk: continue
            pool = pool_map.get(sym, {})
            oi = oi_map.get(sym, {})
            fr = funding_map.get(sym, 0)
            coin = sym.replace("USDT", "")
            
            d6h = oi.get("d6h", 0)
            fr_pct = fr * 100
            oi_usd = oi.get("oi_usd", 0)
            # 真实流通市值：优先现货API，fallback合约OI接口的CMC数据，最后粗估
            if coin in mcap_map:
                est_mcap = mcap_map[coin]
            else:
                circ_supply = oi.get("circ_supply", 0)
                price = tk.get("price", 0) if isinstance(tk, dict) else 0
                if circ_supply > 0 and price > 0:
                    est_mcap = circ_supply * price
                else:
                    est_mcap = max(tk["vol"] * 0.3, oi_usd * 2) if oi_usd > 0 else tk["vol"] * 0.3
            sw_days = pool.get("sideways_days", 0) if pool else 0
            pool_sc = pool.get("pool_score", 0) if pool else 0
            
            coin_data[sym] = {
                "coin": coin, "sym": sym,
                "px_chg": tk["px_chg"], "vol": tk["vol"],
                "price": tk["price"],
                "fr_pct": fr_pct, "d6h": d6h,
                "oi_usd": oi_usd, "est_mcap": est_mcap,
                "d1h": oi.get("d1h", 0),
                "circ_supply": oi.get("circ_supply", 0),
                "sw_days": sw_days, "pool_sc": pool_sc,
                "in_pool": bool(pool),
            }

        save_market_snapshots(conn, coin_data, pool_map, mode)

        # ═══════════════════════════════════════
        # 策略1: 追多 — 纯费率排名
        # ═══════════════════════════════════════
        chase = []
        for sym, d in coin_data.items():
            if d["px_chg"] > 3 and d["fr_pct"] < -0.005 and d["vol"] > 1_000_000:
                # 查费率趋势
                fr_hist = api_get("/fapi/v1/fundingRate", {"symbol": sym, "limit": 5})
                fr_rates = [float(f["fundingRate"]) * 100 for f in fr_hist] if fr_hist else [d["fr_pct"]]
                fr_prev = fr_rates[-2] if len(fr_rates) >= 2 else d["fr_pct"]
                fr_delta = d["fr_pct"] - fr_prev
                
                trend = "🔥加速" if fr_delta < -0.05 else "⬇️变负" if fr_delta < -0.01 else "➡️" if abs(fr_delta) < 0.01 else "⬆️回升"
                
                chase.append({**d, "fr_delta": fr_delta, "trend": trend,
                              "rates": " → ".join([f"{x:.3f}" for x in fr_rates[-3:]])})
                import time; time.sleep(0.2)
        
        # 纯按费率绝对值排序（越负越前）
        chase.sort(key=lambda x: x["fr_pct"])
        
        # ═══════════════════════════════════════
        # 策略2: 综合 — 各维度均衡(各25分)
        # ═══════════════════════════════════════
        combined = []
        for sym, d in coin_data.items():
            # 费率分(25) — 越负越好
            fr = d["fr_pct"]
            if fr < -0.5: f_sc = 25
            elif fr < -0.1: f_sc = 22
            elif fr < -0.05: f_sc = 18
            elif fr < -0.03: f_sc = 14
            elif fr < -0.01: f_sc = 10
            elif fr < 0: f_sc = 5
            else: f_sc = 0
            
            # 市值分(25) — 用真实流通市值
            mc = d["est_mcap"]
            if mc > 0 and mc < 50e6: m_sc = 25
            elif mc < 100e6: m_sc = 22
            elif mc < 200e6: m_sc = 20
            elif mc < 300e6: m_sc = 17
            elif mc < 500e6: m_sc = 12
            elif mc < 1e9: m_sc = 7
            else: m_sc = 0
            
            # 横盘分(25)
            sw = d["sw_days"]
            if sw >= 120: s_sc = 25
            elif sw >= 90: s_sc = 22
            elif sw >= 75: s_sc = 18
            elif sw >= 60: s_sc = 14
            elif sw >= 45: s_sc = 10
            else: s_sc = 0
            
            # OI分(25)
            abs6 = abs(d["d6h"])
            if abs6 >= 15: o_sc = 25
            elif abs6 >= 8: o_sc = 22
            elif abs6 >= 5: o_sc = 18
            elif abs6 >= 3: o_sc = 14
            elif abs6 >= 2: o_sc = 10
            else: o_sc = 0
            
            total = f_sc + m_sc + s_sc + o_sc
            if total < 25: continue
            
            combined.append({**d, "total": total,
                            "f_sc": f_sc, "m_sc": m_sc, "s_sc": s_sc, "o_sc": o_sc})
        
        combined.sort(key=lambda x: x["total"], reverse=True)
        
        # ═══════════════════════════════════════
        # 策略3: 埋伏 — 市值>OI>横盘>费率
        # ═══════════════════════════════════════
        ambush = []
        for sym, d in coin_data.items():
            if not d["in_pool"]: continue  # 必须在收筹池
            if d["px_chg"] > 50: continue  # 已经暴涨的排除
            
            # 1.市值(35分) — 核心！越低越好（真实流通市值）
            mc = d["est_mcap"]
            if mc > 0 and mc < 50e6: m_sc = 35
            elif mc < 100e6: m_sc = 32
            elif mc < 150e6: m_sc = 28
            elif mc < 200e6: m_sc = 25
            elif mc < 300e6: m_sc = 20
            elif mc < 500e6: m_sc = 12
            elif mc < 1e9: m_sc = 5
            else: m_sc = 0
            
            # 2.OI异动(30分) — OI涨+市值低=极好
            abs6 = abs(d["d6h"])
            if abs6 >= 10: o_sc = 30
            elif abs6 >= 5: o_sc = 25
            elif abs6 >= 3: o_sc = 20
            elif abs6 >= 2: o_sc = 14
            elif abs6 >= 1: o_sc = 8
            else: o_sc = 0
            # 暗流加分：OI涨但价格平
            if d["d6h"] > 2 and abs(d["px_chg"]) < 5:
                o_sc = min(o_sc + 5, 30)
            
            # 3.横盘(20分)
            sw = d["sw_days"]
            if sw >= 120: s_sc = 20
            elif sw >= 90: s_sc = 17
            elif sw >= 75: s_sc = 14
            elif sw >= 60: s_sc = 10
            elif sw >= 45: s_sc = 6
            else: s_sc = 0
            
            # 4.负费率(15分) — 有负费率是bonus
            fr = d["fr_pct"]
            if fr < -0.1: f_sc = 15
            elif fr < -0.05: f_sc = 12
            elif fr < -0.03: f_sc = 9
            elif fr < -0.01: f_sc = 6
            elif fr < 0: f_sc = 3
            else: f_sc = 0
            
            total = m_sc + o_sc + s_sc + f_sc
            if total < 20: continue
            
            ambush.append({**d, "total": total,
                          "m_sc": m_sc, "o_sc": o_sc, "s_sc": s_sc, "f_sc": f_sc})
        
        ambush.sort(key=lambda x: x["total"], reverse=True)

        recent_radar_symbols = load_recent_report_symbols(conn, "main_radar", limit=3)
        current_radar_symbols = set()

        def radar_name(item):
            current_radar_symbols.add(item["coin"])
            return coin_label(item["coin"], item["coin"] not in recent_radar_symbols)
        
        # ═══════════════════════════════════════
        # 5. 生成推送 + 值得关注提醒
        # ═══════════════════════════════════════
        def mcap_str(v):
            if v >= 1e6: return f"${v/1e6:.0f}M"
            if v >= 1e3: return f"${v/1e3:.0f}K"
            return f"${v:.0f}"
        
        now = datetime.now(timezone(timedelta(hours=8)))
        lines = [
            f"🏦 **庄家雷达** 三策略",
            f"⏰ {now.strftime('%Y-%m-%d %H:%M')} CST",
        ]
        
        # 表1: 追多
        lines.append(f"\n🔥 **追多** (按费率排名)")
        if chase:
            for s in chase[:8]:
                lines.append(
                    f"  {radar_name(s):<7} 费率{s['fr_pct']:+.3f}% {s['trend']}"
                    f" | 涨{s['px_chg']:+.0f}% | ~{mcap_str(s['est_mcap'])}"
                    f" | 现价：{format_price(s['price'])}"
                )
        else:
            lines.append("  暂无（需涨>3%+费率负）")
        
        # 表2: 综合
        lines.append(f"\n📊 **综合** (费率+市值+横盘+OI 各25)")
        for s in combined[:8]:
            dims = []
            if s["f_sc"] >= 10: dims.append(f"🧊{s['fr_pct']:.2f}%")
            if s["m_sc"] >= 12: dims.append(f"💎{mcap_str(s['est_mcap'])}")
            if s["s_sc"] >= 10: dims.append(f"💤{s['sw_days']}天")
            if s["o_sc"] >= 10: dims.append(f"⚡OI{s['d6h']:+.0f}%")
            dims.append(f"现价：{format_price(s['price'])}")
            lines.append(
                f"  {radar_name(s):<7} {s['total']}分 | {' '.join(dims)}"
            )
        
        # 表3: 埋伏
        lines.append(f"\n🎯 **埋伏** (市值35+OI30+横盘20+费率15)")
        for s in ambush[:8]:
            tags = [f"~{mcap_str(s['est_mcap'])}"]
            if abs(s["d6h"]) >= 2: tags.append(f"OI{s['d6h']:+.0f}%")
            if s["d6h"] > 2 and abs(s["px_chg"]) < 5: tags.append("🎯暗流")
            if s["sw_days"] >= 45: tags.append(f"横盘{s['sw_days']}天")
            if s["fr_pct"] < -0.01: tags.append(f"费率{s['fr_pct']:.2f}%")
            tags.append(f"现价：{format_price(s['price'])}")
            lines.append(
                f"  {radar_name(s):<7} {s['total']}分 | {' '.join(tags)}"
            )
        
        # ═══ 值得关注提醒 ═══
        highlights = []
        
        # 追多里费率加速恶化的前2
        chase_fire = [s for s in chase[:5] if "加速" in s.get("trend", "")]
        for s in chase_fire[:2]:
            highlights.append(f"🔥 {s['coin']} 费率{s['fr_pct']:.3f}%加速恶化，空头涌入中")
        
        # 三个表都出现的币
        chase_coins = set(s["coin"] for s in chase[:10])
        combined_coins = set(s["coin"] for s in combined[:10])
        ambush_coins = set(s["coin"] for s in ambush[:10])
        
        # 追多+综合都出现
        overlap_2 = chase_coins & combined_coins
        if overlap_2:
            for c in list(overlap_2)[:2]:
                highlights.append(f"⭐ {c} 追多+综合双榜上榜")
        
        # 埋伏里OI暗流涌动的
        ambush_dark = [s for s in ambush[:10] if s["d6h"] > 2 and abs(s["px_chg"]) < 5]
        for s in ambush_dark[:2]:
            highlights.append(f"🎯 {s['coin']} 暗流！OI{s['d6h']:+.0f}%但价格没动，市值仅{mcap_str(s['est_mcap'])}")
        
        # 埋伏里市值极低+OI异动的
        ambush_gem = [s for s in ambush[:10] if s["est_mcap"] < 100e6 and abs(s["d6h"]) >= 3]
        for s in ambush_gem[:2]:
            if s["coin"] not in [h.split(" ")[1] for h in highlights]:
                highlights.append(f"💎 {s['coin']} 低市值{mcap_str(s['est_mcap'])}+OI{s['d6h']:+.0f}%，埋伏首选")
        
        if highlights:
            lines.append(f"\n💡 **值得关注**")
            for h in highlights[:5]:
                lines.append(f"  {h}")
        
        # 图例说明
        lines.append(f"\n📖 **图例**")
        lines.append("  费率负=空头多(燃料) | 🔥加速/⬇️变负/⬆️回升=费率趋势")
        lines.append("  💎市值 | 💤横盘天数(收筹时长) | ⚡OI变化(资金异动)")
        lines.append("  🎯暗流=OI动但价没动(收筹信号)")
        
        report = "\n".join(lines)
        if send_telegram(report):
            save_report_symbols(conn, "main_radar", current_radar_symbols)

        trade_signals = build_trade_signals(chase, combined, ambush)
        if RADAR_OBSERVE_BEFORE_ENTRY:
            queue_entry_watch_candidates(conn, trade_signals, coin_data)
            run_entry_observer(conn)
        else:
            emit_trade_signals(trade_signals)

    if mode == "observe":
        run_entry_observer(conn)

    if mode == "seed-observe":
        seeded = seed_watchlist_for_observer(conn, limit=None, include_sleeping=True)
        run_entry_observer(conn, limit=seeded if seeded else OBSERVE_MAX_CANDIDATES)

    conn.close()
    print("\n✅ 完成")


if __name__ == "__main__":
    main()
