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
from decimal import Decimal, ROUND_DOWN, InvalidOperation
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse

# === 加载 .env ===
env_file = Path(os.getenv("RADAR_ENV_FILE", str(Path(__file__).parent / ".env.oi")))
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
ENABLE_PRIVATE_POSITION_PUSH = os.getenv("ENABLE_PRIVATE_POSITION_PUSH", "true").lower() == "true"
ENABLE_BARK_PUSH = os.getenv("ENABLE_BARK_PUSH", "false").lower() == "true"
BARK_PUSH_URL = os.getenv("BARK_PUSH_URL", "")
BARK_SERVER_URL = os.getenv("BARK_SERVER_URL", "https://api.day.app")
BARK_DEVICE_KEY = os.getenv("BARK_DEVICE_KEY", "")
BARK_GROUP = os.getenv("BARK_GROUP", "Radar Trades")
AI_TRADER_WEBHOOK_URL = os.getenv("AI_TRADER_WEBHOOK_URL", "http://127.0.0.1:3000/api/webhooks/radar")
AI_TRADER_FANOUT_TARGETS = os.getenv("AI_TRADER_FANOUT_TARGETS", "")
AI_TRADER_TRACK_TARGET = os.getenv("AI_TRADER_TRACK_TARGET", "")
AI_TRADER_WEBHOOK_SECRET = os.getenv("AI_TRADER_WEBHOOK_SECRET", "")
AI_TRADER_AUTO_EXECUTE = os.getenv("AI_TRADER_AUTO_EXECUTE", "false").lower() == "true"
AI_TRADER_SIGNAL_LIMIT = int(os.getenv("AI_TRADER_SIGNAL_LIMIT", "3"))
AI_TRADER_RETRY_ATTEMPTS = int(os.getenv("AI_TRADER_RETRY_ATTEMPTS", "3"))
AI_TRADER_RETRY_DELAY_SEC = float(os.getenv("AI_TRADER_RETRY_DELAY_SEC", "2"))
RADAR_OBSERVE_BEFORE_ENTRY = os.getenv("RADAR_OBSERVE_BEFORE_ENTRY", "true").lower() == "true"
OBSERVE_MAX_CANDIDATES = int(os.getenv("OBSERVE_MAX_CANDIDATES", "8"))
OBSERVE_LOOKBACK_HOURS = int(os.getenv("OBSERVE_LOOKBACK_HOURS", "48"))
OBSERVER_RISK_BUDGET_USD = float(os.getenv("OBSERVER_RISK_BUDGET_USD", "100"))
OBSERVER_LIQUIDATION_BUFFER_PCT = float(os.getenv("OBSERVER_LIQUIDATION_BUFFER_PCT", "0.015"))
OBSERVER_MIN_ACHIEVABLE_RISK_FRACTION = float(os.getenv("OBSERVER_MIN_ACHIEVABLE_RISK_FRACTION", "0.80"))
POSITION_MONITOR_ENABLED = os.getenv("POSITION_MONITOR_ENABLED", "true").lower() == "true"
POSITION_EARLY_LOSS_FRACTION = float(os.getenv("POSITION_EARLY_LOSS_FRACTION", "0.50"))
POSITION_EARLY_LOSS_MIN_USD = float(os.getenv("POSITION_EARLY_LOSS_MIN_USD", "5"))
POSITION_EARLY_EXIT_SCORE = int(os.getenv("POSITION_EARLY_EXIT_SCORE", "3"))
POSITION_ROI_GUARD_TIER1 = float(os.getenv("POSITION_ROI_GUARD_TIER1", "0.50"))
POSITION_ROI_GUARD_TIER2 = float(os.getenv("POSITION_ROI_GUARD_TIER2", "1.00"))
POSITION_PROFIT_GUARD_MIN_USD = float(os.getenv("POSITION_PROFIT_GUARD_MIN_USD", "180"))
POSITION_PROFIT_GUARD_MIN_MOVE = float(os.getenv("POSITION_PROFIT_GUARD_MIN_MOVE", "0.035"))
POSITION_GUARD_EARLY_MOVE = float(os.getenv("POSITION_GUARD_EARLY_MOVE", "0.025"))
POSITION_GUARD_BREAKEVEN_MOVE = float(os.getenv("POSITION_GUARD_BREAKEVEN_MOVE", "0.045"))
POSITION_GUARD_PROFIT_MOVE = float(os.getenv("POSITION_GUARD_PROFIT_MOVE", "0.075"))
POSITION_STRONG_DOWNLEG_RATIO = float(os.getenv("POSITION_STRONG_DOWNLEG_RATIO", "0.65"))
POSITION_STRONG_DOWNLEG_MIN_DOWN = float(os.getenv("POSITION_STRONG_DOWNLEG_MIN_DOWN", "0.012"))
POSITION_PROFIT_GIVEBACK_MIN_MOVE = float(os.getenv("POSITION_PROFIT_GIVEBACK_MIN_MOVE", "0.05"))
POSITION_PROFIT_GIVEBACK_RATIO = float(os.getenv("POSITION_PROFIT_GIVEBACK_RATIO", "0.60"))
MOMENTUM_POOL_LIMIT = int(os.getenv("MOMENTUM_POOL_LIMIT", "20"))
NEW_LISTING_POOL_LIMIT = int(os.getenv("NEW_LISTING_POOL_LIMIT", "20"))
GUARD_ALERT_MIN_CHANGE_PCT = float(os.getenv("GUARD_ALERT_MIN_CHANGE_PCT", "0.0015"))
GUARD_ALERT_MIN_LOCK_USD_CHANGE = float(os.getenv("GUARD_ALERT_MIN_LOCK_USD_CHANGE", "5"))
ENV_LABEL = os.getenv(
    "RADAR_ENV_LABEL",
    "LIVE" if "live" in os.getenv("RADAR_ENV_FILE", "").lower() else "DEMO",
)
FAPI = "https://fapi.binance.com"
RULES_FAPI = os.getenv("AI_TRADER_RULES_BASE_URL", FAPI)
DB_PATH = Path(os.getenv("ACCUMULATION_DB_PATH", str(Path(__file__).parent / "accumulation.db")))
SYMBOL_RULES_CACHE = {}

parsed_webhook = urlparse(AI_TRADER_WEBHOOK_URL) if AI_TRADER_WEBHOOK_URL else None
AI_TRADER_API_BASE = (
    f"{parsed_webhook.scheme}://{parsed_webhook.netloc}/api"
    if parsed_webhook and parsed_webhook.scheme and parsed_webhook.netloc
    else "http://127.0.0.1:3333/api"
)
AI_TRADER_DB_PATH = Path(os.getenv("AI_TRADER_DB_PATH", "/Users/sabrina0x/ai project/ai-trader/backend/data/database/ai-trader.db"))

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

# 趋势双向阶梯策略参数
TREND_MIN_24H_VOL_USD = float(os.getenv("TREND_MIN_24H_VOL_USD", "10000000"))
TREND_MIN_SCORE = float(os.getenv("TREND_MIN_SCORE", "70"))
TREND_MIN_ADX = float(os.getenv("TREND_MIN_ADX", "25"))
TREND_MIN_RANGE_PCT = float(os.getenv("TREND_MIN_RANGE_PCT", "4"))
TREND_MIN_VOLUME_RATIO = float(os.getenv("TREND_MIN_VOLUME_RATIO", "1.35"))
TREND_MIN_ATR_EXPANSION = float(os.getenv("TREND_MIN_ATR_EXPANSION", "1.10"))
TREND_LOOKBACK_5M = int(os.getenv("TREND_LOOKBACK_5M", "96"))
TREND_RANGE_BARS = int(os.getenv("TREND_RANGE_BARS", "48"))
TREND_RISK_BUDGET_USD = float(os.getenv("TREND_RISK_BUDGET_USD", "100"))
TREND_STOP_LOSS_PCT = float(os.getenv("TREND_STOP_LOSS_PCT", "0.10"))
TREND_PLAN_LIMIT = int(os.getenv("TREND_PLAN_LIMIT", "12"))
TREND_ORDER_STEPS = [0.20, 0.35, 0.50, 0.70, 0.95, 1.25, 1.60, 2.00, 2.50, 3.10]
TREND_ORDER_WEIGHTS = [0.22, 0.18, 0.15, 0.12, 0.10, 0.08, 0.06, 0.04, 0.03, 0.02]

# 选币策略：涨幅榜前排 + ATH/近ATH + 趋势脱离震荡
LEADER_TOP_GAINERS = int(os.getenv("LEADER_TOP_GAINERS", "20"))
LEADER_MAX_ATH_DRAWDOWN_PCT = float(os.getenv("LEADER_MAX_ATH_DRAWDOWN_PCT", "30"))
LEADER_MIN_24H_VOL_USD = float(os.getenv("LEADER_MIN_24H_VOL_USD", "5000000"))
LEADER_MIN_ADX = float(os.getenv("LEADER_MIN_ADX", "22"))
LEADER_MIN_EMA_SPREAD_PCT = float(os.getenv("LEADER_MIN_EMA_SPREAD_PCT", "2"))
LEADER_DAILY_LOOKBACK = int(os.getenv("LEADER_DAILY_LOOKBACK", "1000"))
LEADER_MIN_DATA_DAYS = int(os.getenv("LEADER_MIN_DATA_DAYS", "60"))
LEADER_POOL_LIMIT = int(os.getenv("LEADER_POOL_LIMIT", "12"))


def api_get(endpoint, params=None, base_url=FAPI):
    """币安API请求"""
    url = f"{base_url}{endpoint}"
    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code == 200:
                return resp.json()
            elif resp.status_code == 429:
                time.sleep(2)
            else:
                return None
        except requests.RequestException:
            try:
                direct = requests.Session()
                direct.trust_env = False
                resp = direct.get(url, params=params, timeout=10)
                if resp.status_code == 200:
                    return resp.json()
            except requests.RequestException:
                pass
            time.sleep(1)
    return None


def get_symbol_rules(symbol):
    """获取交易对精度规则，避免观察器给出不可下单的数量/价格"""
    cached = SYMBOL_RULES_CACHE.get(symbol)
    if cached:
        return cached

    info = api_get("/fapi/v1/exchangeInfo", {"symbol": symbol}, base_url=RULES_FAPI)
    symbols = (info or {}).get("symbols", [])
    symbol_info = next((item for item in symbols if item.get("symbol") == symbol), None)
    if not symbol_info:
        # Testnet exchangeInfo can lag behind the web/demo trading surface.
        # If the symbol is absent, keep the trade attempt alive and let the
        # actual execution/market-data call decide. Explicit Invalid symbol
        # responses are handled by emit_trade_signals().
        return None

    if symbol_info.get("status") != "TRADING":
        rules = {
            "tradable": False,
            "reason": f"{symbol} status={symbol_info.get('status')}",
        }
        SYMBOL_RULES_CACHE[symbol] = rules
        return rules

    if symbol_info.get("contractType") not in {None, "PERPETUAL"}:
        rules = {
            "tradable": False,
            "reason": f"{symbol} contractType={symbol_info.get('contractType')}",
        }
        SYMBOL_RULES_CACHE[symbol] = rules
        return rules

    price_filter = next((f for f in symbol_info.get("filters", []) if f.get("filterType") == "PRICE_FILTER"), {})
    lot_filter = next((f for f in symbol_info.get("filters", []) if f.get("filterType") == "LOT_SIZE"), {})
    market_lot_filter = next((f for f in symbol_info.get("filters", []) if f.get("filterType") == "MARKET_LOT_SIZE"), {})
    min_notional_filter = next((f for f in symbol_info.get("filters", []) if f.get("filterType") == "MIN_NOTIONAL"), {})

    rules = {
        "tradable": True,
        "tick_size": str(price_filter.get("tickSize", "0.0001")),
        "step_size": str(lot_filter.get("stepSize", "0.001")),
        "min_qty": str(market_lot_filter.get("minQty", lot_filter.get("minQty", "0.001"))),
        "max_qty": str(market_lot_filter.get("maxQty", lot_filter.get("maxQty", "0"))),
        "min_notional": str(min_notional_filter.get("notional", "0")),
        "price_precision": int(symbol_info.get("pricePrecision", 6)),
        "quantity_precision": int(symbol_info.get("quantityPrecision", 3)),
    }
    SYMBOL_RULES_CACHE[symbol] = rules
    return rules


def quantize_down(value, step_text, fallback_precision=8):
    """按交易所步进向下取整"""
    try:
        step = Decimal(str(step_text))
        if step <= 0:
            return round(float(value), fallback_precision)
        decimal_value = Decimal(str(value))
        quantized = (decimal_value / step).to_integral_value(rounding=ROUND_DOWN) * step
        precision = max(0, -step.as_tuple().exponent)
        return round(float(quantized), precision)
    except (InvalidOperation, ValueError, TypeError):
        return round(float(value), fallback_precision)


def normalize_trade_plan(symbol, entry_price, stop_price, take_profit, quantity):
    """把观察器算出来的价格和数量对齐到交易所规则"""
    rules = get_symbol_rules(symbol)
    if not rules or not rules.get("tradable", True):
        return {
            "entry_price": round(entry_price, 8),
            "stop_price": round(stop_price, 8),
            "take_profit": round(take_profit, 8),
            "quantity": round(quantity, 6),
        }

    normalized_entry = quantize_down(entry_price, rules["tick_size"], rules["price_precision"])
    normalized_stop = quantize_down(stop_price, rules["tick_size"], rules["price_precision"])
    normalized_tp = quantize_down(take_profit, rules["tick_size"], rules["price_precision"])
    normalized_qty = max(quantity, float(rules["min_qty"]))
    max_qty = float(rules["max_qty"])
    if max_qty > 0:
        normalized_qty = min(normalized_qty, max_qty)
    min_notional = float(rules["min_notional"])
    if min_notional > 0 and normalized_entry > 0:
        normalized_qty = max(normalized_qty, min_notional / normalized_entry)
        if max_qty > 0:
            normalized_qty = min(normalized_qty, max_qty)
    normalized_qty = quantize_down(normalized_qty, rules["step_size"], rules["quantity_precision"])

    tick_value = float(rules["tick_size"])
    price_distorted = (
        normalized_entry <= 0
        or abs(normalized_entry - entry_price) / max(entry_price, 1e-12) > 0.05
    )
    if price_distorted:
        normalized_entry = round(entry_price, 10)
        normalized_stop = round(stop_price, 10)
        normalized_tp = round(take_profit, 10)
    else:
        if normalized_stop >= normalized_entry:
            normalized_stop = quantize_down(max(normalized_entry - tick_value, tick_value), rules["tick_size"], rules["price_precision"])
        if normalized_tp <= normalized_entry:
            normalized_tp = quantize_down(normalized_entry + max(tick_value * 2, normalized_entry * 0.02), rules["tick_size"], rules["price_precision"])

    return {
        "entry_price": normalized_entry,
        "stop_price": normalized_stop,
        "take_profit": normalized_tp,
        "quantity": normalized_qty,
    }


def assess_achievable_risk(entry_price, stop_price, quantity):
    """检查归一化后实际能承担的风险是否接近目标风险。"""
    actual_risk = quantity * max(entry_price - stop_price, 0)
    min_required = OBSERVER_RISK_BUDGET_USD * OBSERVER_MIN_ACHIEVABLE_RISK_FRACTION
    return actual_risk, min_required, actual_risk >= min_required


def is_demo_rules_compatible(symbol, entry_price):
    """筛掉 testnet 规则明显失真的币，避免观察器触发后必然无法下单"""
    rules = get_symbol_rules(symbol)
    if not rules:
        return True, ""
    if not rules.get("tradable", True):
        return False, rules.get("reason", "symbol not tradable in execution exchangeInfo")
    tick_size = float(rules["tick_size"])
    if tick_size >= entry_price:
        return False, f"tickSize={tick_size} >= entry={entry_price:.8f}"
    return True, ""


def init_db():
    """初始化数据库"""
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")  # WAL 模式允许并发读写
    conn.execute("PRAGMA busy_timeout=30000")  # 等待锁30秒
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
    c.execute("""CREATE TABLE IF NOT EXISTS candidate_pools (
        pool_name TEXT,
        symbol TEXT,
        snapshot_time TEXT,
        score REAL,
        source_reason TEXT,
        price REAL,
        px_chg_pct REAL,
        vol_24h REAL,
        oi_d1h_pct REAL,
        oi_d6h_pct REAL,
        funding_rate REAL,
        est_mcap REAL,
        data_days INT,
        status TEXT DEFAULT 'ACTIVE',
        PRIMARY KEY(pool_name, symbol, snapshot_time)
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
    c.execute("""CREATE TABLE IF NOT EXISTS position_watchlist (
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
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS trend_breakout_plans (
        symbol TEXT PRIMARY KEY,
        plan_time TEXT,
        score REAL,
        status TEXT DEFAULT 'PENDING_ORDERS',
        current_price REAL,
        upper_price REAL,
        lower_price REAL,
        atr REAL,
        adx REAL,
        range_pct REAL,
        volume_ratio REAL,
        atr_expansion REAL,
        ema_spread_pct REAL,
        long_full_avg REAL,
        short_full_avg REAL,
        long_stop_full REAL,
        short_stop_full REAL,
        side_notional REAL,
        long_orders_json TEXT,
        short_orders_json TEXT,
        notes TEXT
    )""")
    try:
        c.execute("ALTER TABLE position_watchlist ADD COLUMN guard_price REAL")
    except sqlite3.OperationalError:
        pass
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


def load_snapshot_rows(conn, snapshot_time, mode="oi"):
    c = conn.cursor()
    rows = c.execute(
        """SELECT symbol, coin, price, px_chg_pct, vol_24h, funding_rate,
                  oi_d1h_pct, oi_d6h_pct, est_mcap, circ_supply,
                  in_watchlist, watchlist_status, radar_score, sideways_days
           FROM market_snapshots
           WHERE snapshot_time = ? AND mode = ?""",
        (snapshot_time, mode),
    ).fetchall()
    result = []
    for row in rows:
        result.append({
            "sym": row[0],
            "coin": row[1],
            "price": float(row[2] or 0),
            "px_chg": float(row[3] or 0),
            "vol": float(row[4] or 0),
            "fr_pct": float(row[5] or 0) * 100,
            "d1h": float(row[6] or 0),
            "d6h": float(row[7] or 0),
            "est_mcap": float(row[8] or 0),
            "circ_supply": float(row[9] or 0),
            "in_pool": bool(row[10]),
            "status": row[11] or "",
            "pool_sc": float(row[12] or 0),
            "sw_days": int(row[13] or 0),
        })
    return result


def derive_radar_report_symbols_from_snapshot(conn, snapshot_time):
    """从历史 market_snapshots 近似重建当时主雷达里出现过的代币。"""
    coin_data = load_snapshot_rows(conn, snapshot_time, mode="oi")
    if not coin_data:
        return set()

    chase = []
    combined = []
    ambush = []

    for d in coin_data:
        if d["px_chg"] > 3 and d["fr_pct"] < -0.005 and d["vol"] > 1_000_000:
            chase.append(d)

        fr = d["fr_pct"]
        if fr < -0.5: f_sc = 25
        elif fr < -0.1: f_sc = 22
        elif fr < -0.05: f_sc = 18
        elif fr < -0.03: f_sc = 14
        elif fr < -0.01: f_sc = 10
        elif fr < 0: f_sc = 5
        else: f_sc = 0

        mc = d["est_mcap"]
        if mc > 0 and mc < 50e6: m_sc = 25
        elif mc < 100e6: m_sc = 22
        elif mc < 200e6: m_sc = 20
        elif mc < 300e6: m_sc = 17
        elif mc < 500e6: m_sc = 12
        elif mc < 1e9: m_sc = 7
        else: m_sc = 0

        sw = d["sw_days"]
        if sw >= 120: s_sc = 25
        elif sw >= 90: s_sc = 22
        elif sw >= 75: s_sc = 18
        elif sw >= 60: s_sc = 14
        elif sw >= 45: s_sc = 10
        else: s_sc = 0

        abs6 = abs(d["d6h"])
        if abs6 >= 15: o_sc = 25
        elif abs6 >= 8: o_sc = 22
        elif abs6 >= 5: o_sc = 18
        elif abs6 >= 3: o_sc = 14
        elif abs6 >= 2: o_sc = 10
        else: o_sc = 0

        total = f_sc + m_sc + s_sc + o_sc
        if total >= 25:
            combined.append({**d, "total": total})

        if not d["in_pool"] or d["px_chg"] > 50:
            continue

        if mc > 0 and mc < 50e6: m2 = 35
        elif mc < 100e6: m2 = 32
        elif mc < 150e6: m2 = 28
        elif mc < 200e6: m2 = 25
        elif mc < 300e6: m2 = 20
        elif mc < 500e6: m2 = 12
        elif mc < 1e9: m2 = 5
        else: m2 = 0

        if abs6 >= 10: o2 = 30
        elif abs6 >= 5: o2 = 25
        elif abs6 >= 3: o2 = 20
        elif abs6 >= 2: o2 = 14
        elif abs6 >= 1: o2 = 8
        else: o2 = 0
        if d["d6h"] > 2 and abs(d["px_chg"]) < 5:
            o2 = min(o2 + 5, 30)

        if sw >= 120: s2 = 20
        elif sw >= 90: s2 = 17
        elif sw >= 75: s2 = 14
        elif sw >= 60: s2 = 10
        elif sw >= 45: s2 = 6
        else: s2 = 0

        if fr < -0.1: f2 = 15
        elif fr < -0.05: f2 = 12
        elif fr < -0.03: f2 = 9
        elif fr < -0.01: f2 = 6
        elif fr < 0: f2 = 3
        else: f2 = 0

        total2 = m2 + o2 + s2 + f2
        if total2 >= 20:
            ambush.append({**d, "total": total2})

    chase.sort(key=lambda x: x["fr_pct"])
    combined.sort(key=lambda x: x["total"], reverse=True)
    ambush.sort(key=lambda x: x["total"], reverse=True)

    symbols = set()
    for item in chase[:8]:
        symbols.add(item["coin"])
    for item in combined[:8]:
        symbols.add(item["coin"])
    for item in ambush[:8]:
        symbols.add(item["coin"])
    return symbols


def load_recent_report_symbols_with_fallback(conn, report_key, limit=3, exclude_snapshot_time=None):
    seen = load_recent_report_symbols(conn, report_key, limit=limit)
    if len(seen) > 0 and limit <= 1:
        return seen

    c = conn.cursor()
    snapshot_rows = c.execute(
        """SELECT DISTINCT snapshot_time
           FROM market_snapshots
           WHERE mode = 'oi'
           AND (? IS NULL OR snapshot_time != ?)
           ORDER BY snapshot_time DESC
           LIMIT ?""",
        (exclude_snapshot_time, exclude_snapshot_time, limit),
    ).fetchall()

    for (snapshot_time,) in snapshot_rows:
        seen.update(derive_radar_report_symbols_from_snapshot(conn, snapshot_time))
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


def true_ranges(highs, lows, closes):
    ranges = []
    for i in range(len(highs)):
        if i == 0:
            ranges.append(highs[i] - lows[i])
        else:
            ranges.append(max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1]),
            ))
    return ranges


def atr_values(highs, lows, closes, period=14):
    ranges = true_ranges(highs, lows, closes)
    if not ranges:
        return []
    result = []
    for i, value in enumerate(ranges):
        if i == 0:
            result.append(value)
        elif i < period:
            result.append(sum(ranges[:i + 1]) / (i + 1))
        else:
            result.append((result[-1] * (period - 1) + value) / period)
    return result


def adx_value(highs, lows, closes, period=14):
    if len(closes) < period * 2 + 2:
        return 0

    plus_dm = [0]
    minus_dm = [0]
    tr = true_ranges(highs, lows, closes)
    for i in range(1, len(closes)):
        up_move = highs[i] - highs[i - 1]
        down_move = lows[i - 1] - lows[i]
        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0)
        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0)

    dx_values = []
    for i in range(period, len(closes)):
        tr_sum = sum(tr[i - period + 1:i + 1])
        if tr_sum <= 0:
            dx_values.append(0)
            continue
        plus_di = 100 * sum(plus_dm[i - period + 1:i + 1]) / tr_sum
        minus_di = 100 * sum(minus_dm[i - period + 1:i + 1]) / tr_sum
        denom = plus_di + minus_di
        dx_values.append(100 * abs(plus_di - minus_di) / denom if denom > 0 else 0)
    return sum(dx_values[-period:]) / min(len(dx_values), period) if dx_values else 0


def normalize_ladder_order(symbol, side, price, notional):
    rules = get_symbol_rules(symbol)
    quantity = notional / price if price > 0 else 0
    if not rules or not rules.get("tradable", True):
        return round(price, 10), round(quantity, 6), round(notional, 2)

    normalized_price = quantize_down(price, rules["tick_size"], rules["price_precision"])
    if normalized_price <= 0 or abs(normalized_price - price) / max(price, 1e-12) > 0.05:
        normalized_price = round(price, 10)

    min_qty = float(rules["min_qty"])
    max_qty = float(rules["max_qty"])
    normalized_qty = max(quantity, min_qty)
    min_notional = float(rules["min_notional"])
    if min_notional > 0 and normalized_price > 0:
        normalized_qty = max(normalized_qty, min_notional / normalized_price)
    if max_qty > 0:
        normalized_qty = min(normalized_qty, max_qty)
    normalized_qty = quantize_down(normalized_qty, rules["step_size"], rules["quantity_precision"])
    normalized_notional = normalized_price * normalized_qty
    return normalized_price, normalized_qty, round(normalized_notional, 2)


def build_trend_ladder_orders(symbol, upper_price, lower_price, atr, side_notional):
    long_orders = []
    short_orders = []
    for idx, (step, weight) in enumerate(zip(TREND_ORDER_STEPS, TREND_ORDER_WEIGHTS), start=1):
        order_notional = side_notional * weight
        long_price = upper_price + atr * step
        short_price = max(lower_price - atr * step, lower_price * 0.5)
        long_price, long_qty, long_notional = normalize_ladder_order(symbol, "LONG", long_price, order_notional)
        short_price, short_qty, short_notional = normalize_ladder_order(symbol, "SHORT", short_price, order_notional)
        long_orders.append({
            "index": idx,
            "side": "LONG",
            "step_atr": step,
            "weight": weight,
            "price": long_price,
            "quantity": long_qty,
            "notional": long_notional,
        })
        short_orders.append({
            "index": idx,
            "side": "SHORT",
            "step_atr": step,
            "weight": weight,
            "price": short_price,
            "quantity": short_qty,
            "notional": short_notional,
        })
    return long_orders, short_orders


def weighted_average_entry(orders):
    total_qty = sum(float(o["quantity"]) for o in orders)
    if total_qty <= 0:
        return 0
    total_value = sum(float(o["price"]) * float(o["quantity"]) for o in orders)
    return total_value / total_qty


def analyze_trend_breakout_candidate(symbol, ticker):
    klines_5m = fetch_intraday_klines(symbol, "5m", max(TREND_LOOKBACK_5M, TREND_RANGE_BARS + 30))
    klines_15m = fetch_intraday_klines(symbol, "15m", 120)
    if len(klines_5m) < TREND_RANGE_BARS + 20 or len(klines_15m) < 60:
        return None

    closes_5m = [k["close"] for k in klines_5m]
    highs_5m = [k["high"] for k in klines_5m]
    lows_5m = [k["low"] for k in klines_5m]
    vols_5m = [k["volume"] for k in klines_5m]
    closes_15m = [k["close"] for k in klines_15m]
    highs_15m = [k["high"] for k in klines_15m]
    lows_15m = [k["low"] for k in klines_15m]

    current_price = float(ticker.get("price") or closes_5m[-1])
    if current_price <= 0:
        return None

    recent_range = klines_5m[-TREND_RANGE_BARS:]
    upper_price = max(k["high"] for k in recent_range)
    lower_price = min(k["low"] for k in recent_range)
    range_pct = (upper_price - lower_price) / current_price * 100 if current_price > 0 else 0

    adx = adx_value(highs_15m, lows_15m, closes_15m, 14)
    atr_series = atr_values(highs_15m, lows_15m, closes_15m, 14)
    atr = atr_series[-1] if atr_series else 0
    recent_atr = sum(atr_series[-8:]) / min(len(atr_series), 8) if atr_series else 0
    prior_atr_window = atr_series[-40:-8] if len(atr_series) >= 40 else atr_series[:-8]
    prior_atr = sum(prior_atr_window) / len(prior_atr_window) if prior_atr_window else recent_atr
    atr_expansion = recent_atr / prior_atr if prior_atr > 0 else 0

    recent_vol = sum(vols_5m[-12:]) / 12
    prior_vol_window = vols_5m[-60:-12] if len(vols_5m) >= 60 else vols_5m[:-12]
    prior_vol = sum(prior_vol_window) / len(prior_vol_window) if prior_vol_window else recent_vol
    volume_ratio = recent_vol / prior_vol if prior_vol > 0 else 0

    ema9 = ema(closes_15m, 9)
    ema21 = ema(closes_15m, 21)
    ema55 = ema(closes_15m, 55)
    ema_spread_pct = (max(ema9[-1], ema21[-1], ema55[-1]) - min(ema9[-1], ema21[-1], ema55[-1])) / current_price * 100

    upper_distance = abs(upper_price - current_price) / current_price
    lower_distance = abs(current_price - lower_price) / current_price
    near_edge = min(upper_distance, lower_distance)

    volatility_score = min(max((atr_expansion - 1.0) / 0.8, 0), 1) * 25
    volume_score = min(max((volume_ratio - 1.0) / 1.5, 0), 1) * 20
    adx_score = min(max((adx - 15) / 25, 0), 1) * 25
    ema_score = min(ema_spread_pct / 3.0, 1) * 15
    edge_score = min(max((0.08 - near_edge) / 0.08, 0), 1) * 15
    total_score = volatility_score + volume_score + adx_score + ema_score + edge_score

    if (
        total_score < TREND_MIN_SCORE
        or adx < TREND_MIN_ADX
        or range_pct < TREND_MIN_RANGE_PCT
        or volume_ratio < TREND_MIN_VOLUME_RATIO
        or atr_expansion < TREND_MIN_ATR_EXPANSION
        or atr <= 0
    ):
        return None

    side_notional = TREND_RISK_BUDGET_USD / max(TREND_STOP_LOSS_PCT, 1e-6)
    long_orders, short_orders = build_trend_ladder_orders(symbol, upper_price, lower_price, atr, side_notional)
    long_full_avg = weighted_average_entry(long_orders)
    short_full_avg = weighted_average_entry(short_orders)

    return {
        "symbol": symbol,
        "coin": symbol.replace("USDT", ""),
        "score": round(total_score, 1),
        "price": current_price,
        "px_chg": float(ticker.get("px_chg") or 0),
        "vol": float(ticker.get("vol") or 0),
        "upper": upper_price,
        "lower": lower_price,
        "atr": atr,
        "adx": adx,
        "range_pct": range_pct,
        "volume_ratio": volume_ratio,
        "atr_expansion": atr_expansion,
        "ema_spread_pct": ema_spread_pct,
        "near_edge_pct": near_edge * 100,
        "side_notional": side_notional,
        "long_orders": long_orders,
        "short_orders": short_orders,
        "long_full_avg": long_full_avg,
        "short_full_avg": short_full_avg,
        "long_stop_full": long_full_avg * (1 - TREND_STOP_LOSS_PCT) if long_full_avg else 0,
        "short_stop_full": short_full_avg * (1 + TREND_STOP_LOSS_PCT) if short_full_avg else 0,
        "notes": (
            f"score={total_score:.1f} adx={adx:.1f} range={range_pct:.1f}% "
            f"volx={volume_ratio:.2f} atrx={atr_expansion:.2f} ema_spread={ema_spread_pct:.2f}%"
        ),
    }


def scan_trend_breakout_plans():
    print("📊 扫描趋势双向阶梯候选...")
    tickers_raw = api_get("/fapi/v1/ticker/24hr")
    if not tickers_raw:
        print("❌ ticker API失败")
        return []

    exclude = {"USDC", "USDP", "TUSD", "FDUSD", "BTCDOM", "DEFI", "USDM"}
    tickers = []
    for t in tickers_raw:
        symbol = t.get("symbol", "")
        coin = symbol.replace("USDT", "")
        if not symbol.endswith("USDT") or coin in exclude:
            continue
        vol = float(t.get("quoteVolume") or 0)
        if vol < TREND_MIN_24H_VOL_USD:
            continue
        tickers.append({
            "symbol": symbol,
            "px_chg": float(t.get("priceChangePercent") or 0),
            "vol": vol,
            "price": float(t.get("lastPrice") or 0),
        })

    tickers.sort(key=lambda item: item["vol"], reverse=True)
    print(f"  成交额过滤后 {len(tickers)} 个合约")

    plans = []
    for i, ticker in enumerate(tickers):
        plan = analyze_trend_breakout_candidate(ticker["symbol"], ticker)
        if plan:
            plans.append(plan)
            print(f"  ✅ {plan['symbol']} score={plan['score']} adx={plan['adx']:.1f} volx={plan['volume_ratio']:.2f}")
        if (i + 1) % 10 == 0:
            time.sleep(0.5)
        if len(plans) >= TREND_PLAN_LIMIT * 2:
            break

    plans.sort(key=lambda item: item["score"], reverse=True)
    print(f"  ✅ 生成 {len(plans[:TREND_PLAN_LIMIT])} 个趋势阶梯计划")
    return plans[:TREND_PLAN_LIMIT]


def save_trend_breakout_plans(conn, plans):
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    c = conn.cursor()
    for plan in plans:
        c.execute(
            """INSERT OR REPLACE INTO trend_breakout_plans (
                   symbol, plan_time, score, status, current_price, upper_price, lower_price,
                   atr, adx, range_pct, volume_ratio, atr_expansion, ema_spread_pct,
                   long_full_avg, short_full_avg, long_stop_full, short_stop_full, side_notional,
                   long_orders_json, short_orders_json, notes
               ) VALUES (?, ?, ?, 'PENDING_ORDERS', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                plan["symbol"],
                now,
                plan["score"],
                plan["price"],
                plan["upper"],
                plan["lower"],
                plan["atr"],
                plan["adx"],
                plan["range_pct"],
                plan["volume_ratio"],
                plan["atr_expansion"],
                plan["ema_spread_pct"],
                plan["long_full_avg"],
                plan["short_full_avg"],
                plan["long_stop_full"],
                plan["short_stop_full"],
                plan["side_notional"],
                json.dumps(plan["long_orders"], ensure_ascii=False),
                json.dumps(plan["short_orders"], ensure_ascii=False),
                plan["notes"],
            ),
        )
    conn.commit()


def build_trend_breakout_report(plans):
    if not plans:
        return ""
    now = datetime.now(timezone(timedelta(hours=8)))
    lines = [
        "🧭 **趋势双向阶梯计划**",
        f"⏰ {now.strftime('%Y-%m-%d %H:%M')} CST",
        "只筛大波动趋势，不预测多空；触发一边后另一边撤单。",
    ]
    for plan in plans[:8]:
        long_first = plan["long_orders"][0]
        short_first = plan["short_orders"][0]
        lines.append(
            f"\n{plan['coin']}  {plan['score']:.0f}分 | ADX {plan['adx']:.1f} | "
            f"区间{plan['range_pct']:.1f}% | Vol x{plan['volume_ratio']:.2f} | ATR x{plan['atr_expansion']:.2f}"
        )
        lines.append(
            f"  现价 {format_price(plan['price'])} | 上沿 {format_price(plan['upper'])} | 下沿 {format_price(plan['lower'])}"
        )
        lines.append(
            f"  多首单 {format_price(long_first['price'])} | 满仓均价 {format_price(plan['long_full_avg'])} | 止损 {format_price(plan['long_stop_full'])}"
        )
        lines.append(
            f"  空首单 {format_price(short_first['price'])} | 满仓均价 {format_price(plan['short_full_avg'])} | 止损 {format_price(plan['short_stop_full'])}"
        )
    lines.append("\n📌 每边10阶：22/18/15/12/10/8/6/4/3/2%。10单全满后进入K线动态分批止盈。")
    return "\n".join(lines)


def fetch_daily_klines(symbol, limit=1000):
    rows = api_get("/fapi/v1/klines", {"symbol": symbol, "interval": "1d", "limit": limit})
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


def analyze_leader_selection_candidate(ticker, rank):
    """涨幅榜选币：前排涨幅 + ATH/近ATH + 日线趋势脱离震荡。"""
    symbol = ticker["symbol"]
    klines = fetch_daily_klines(symbol, LEADER_DAILY_LOOKBACK)
    if len(klines) < LEADER_MIN_DATA_DAYS:
        return None

    closes = [k["close"] for k in klines]
    highs = [k["high"] for k in klines]
    lows = [k["low"] for k in klines]
    vols = [k["volume"] for k in klines]
    current_price = float(ticker.get("price") or closes[-1])
    if current_price <= 0:
        return None

    prior_highs = highs[:-1] or highs
    previous_ath = max(prior_highs)
    full_ath = max(highs)
    ath_price = max(previous_ath, full_ath)
    ath_drawdown_pct = (ath_price - current_price) / ath_price * 100 if ath_price > 0 else 100
    new_ath = current_price >= previous_ath * 0.995 or highs[-1] >= previous_ath * 1.001
    near_ath = ath_drawdown_pct <= LEADER_MAX_ATH_DRAWDOWN_PCT
    if not (new_ath or near_ath):
        return None

    ema8 = ema(closes, 8)
    ema21 = ema(closes, 21)
    ema55 = ema(closes, 55)
    adx = adx_value(highs, lows, closes, 14)
    ema_spread_pct = (
        (max(ema8[-1], ema21[-1], ema55[-1]) - min(ema8[-1], ema21[-1], ema55[-1]))
        / current_price
        * 100
        if current_price > 0 else 0
    )

    prior_20_high = max(highs[-21:-1]) if len(highs) >= 21 else max(prior_highs)
    recent_20_low = min(lows[-20:]) if len(lows) >= 20 else min(lows)
    prior_20_low = min(lows[-41:-21]) if len(lows) >= 41 else min(lows[:-20] or lows)
    range_20_pct = (prior_20_high - recent_20_low) / current_price * 100 if current_price > 0 else 0
    breakout_20d = current_price >= prior_20_high * 0.995
    ema_stack = ema8[-1] > ema21[-1] > ema55[-1] and current_price > ema21[-1]
    higher_lows = recent_20_low >= prior_20_low * 0.98
    closes_above_ema21 = sum(1 for close, avg in zip(closes[-5:], ema21[-5:]) if close > avg) >= 3

    trend_checks = [
        adx >= LEADER_MIN_ADX,
        ema_stack,
        breakout_20d,
        ema_spread_pct >= LEADER_MIN_EMA_SPREAD_PCT,
        higher_lows and closes_above_ema21,
    ]
    trend_check_count = sum(1 for passed in trend_checks if passed)
    if trend_check_count < 3:
        return None

    avg_vol_20d = sum(vols[-20:]) / min(len(vols), 20) if vols else 0
    rank_score = max(0, (LEADER_TOP_GAINERS - rank + 1) / LEADER_TOP_GAINERS) * 20
    if new_ath:
        ath_score = 25
        ath_status = "ATH突破"
    elif ath_drawdown_pct <= 10:
        ath_score = 22
        ath_status = "距ATH≤10%"
    elif ath_drawdown_pct <= 20:
        ath_score = 18
        ath_status = "距ATH≤20%"
    else:
        ath_score = 14
        ath_status = "距ATH≤30%"

    adx_score = min(max((adx - 15) / 25, 0), 1) * 20
    ema_score = min(ema_spread_pct / 5, 1) * 15
    breakout_score = 15 if breakout_20d else 8 if current_price >= prior_20_high * 0.97 else 0
    volume_score = min(float(ticker.get("vol") or 0) / 100_000_000, 1) * 10
    total = rank_score + ath_score + adx_score + ema_score + breakout_score + volume_score

    tags = [ath_status]
    if breakout_20d:
        tags.append("20D突破")
    if ema_stack:
        tags.append("EMA多头")
    if higher_lows:
        tags.append("低点抬高")

    return {
        "symbol": symbol,
        "sym": symbol,
        "coin": symbol.replace("USDT", ""),
        "pool_name": "leader_ath_trend_pool",
        "rank": rank,
        "total": round(total, 2),
        "price": current_price,
        "px_chg": float(ticker.get("px_chg") or 0),
        "vol": float(ticker.get("vol") or 0),
        "fr_pct": 0,
        "d1h": 0,
        "d6h": 0,
        "oi_usd": 0,
        "est_mcap": 0,
        "data_days": len(klines),
        "ath_price": ath_price,
        "ath_drawdown_pct": ath_drawdown_pct,
        "new_ath": new_ath,
        "adx": adx,
        "ema_spread_pct": ema_spread_pct,
        "range_20_pct": range_20_pct,
        "avg_vol_20d": avg_vol_20d,
        "trend_check_count": trend_check_count,
        "tags": tags,
        "reason": (
            f"leader rank={rank} px24h={float(ticker.get('px_chg') or 0):+.1f}% "
            f"ath_dd={ath_drawdown_pct:.1f}% adx={adx:.1f} "
            f"ema_spread={ema_spread_pct:.1f}% checks={trend_check_count}/5"
        ),
    }


def scan_leader_ath_trend_pool():
    print("📊 扫描涨幅榜ATH趋势选币...")
    tickers_raw = api_get("/fapi/v1/ticker/24hr")
    if not tickers_raw:
        print("❌ ticker API失败")
        return [], []

    exclude = {"USDC", "USDP", "TUSD", "FDUSD", "BTCDOM", "DEFI", "USDM"}
    top_gainers = []
    for t in tickers_raw:
        symbol = t.get("symbol", "")
        coin = symbol.replace("USDT", "")
        if not symbol.endswith("USDT") or coin in exclude:
            continue
        vol = float(t.get("quoteVolume") or 0)
        px_chg = float(t.get("priceChangePercent") or 0)
        price = float(t.get("lastPrice") or 0)
        if vol < LEADER_MIN_24H_VOL_USD or px_chg <= 0 or price <= 0:
            continue
        top_gainers.append({
            "symbol": symbol,
            "px_chg": px_chg,
            "vol": vol,
            "price": price,
        })

    top_gainers.sort(key=lambda item: item["px_chg"], reverse=True)
    top_gainers = top_gainers[:LEADER_TOP_GAINERS]
    print(f"  涨幅榜前 {len(top_gainers)} 个进入ATH/趋势复核")

    selected = []
    for idx, ticker in enumerate(top_gainers, start=1):
        item = analyze_leader_selection_candidate(ticker, idx)
        if item:
            selected.append(item)
            print(
                f"  ✅ #{idx} {item['symbol']} {item['px_chg']:+.1f}% "
                f"ATH回撤{item['ath_drawdown_pct']:.1f}% ADX{item['adx']:.1f}"
            )
        else:
            print(f"  · #{idx} {ticker['symbol']} 未通过ATH/趋势过滤")
        if idx % 8 == 0:
            time.sleep(0.5)

    selected.sort(key=lambda item: item["total"], reverse=True)
    return selected[:LEADER_POOL_LIMIT], top_gainers


def build_leader_selection_report(selected, top_gainers):
    now = datetime.now(timezone(timedelta(hours=8)))
    lines = [
        "🧲 **涨幅榜ATH趋势选币**",
        f"⏰ {now.strftime('%Y-%m-%d %H:%M')} CST",
        f"范围：24h涨幅榜前{len(top_gainers)}，过滤ATH/近ATH + 日线趋势脱离震荡。",
    ]

    if not selected:
        lines.append("\n暂无通过项：前排涨幅币里没有同时满足ATH距离和趋势结构的标的。")
        return "\n".join(lines)

    lines.append("\n✅ **入选标的**")
    for item in selected:
        tag_text = " ".join(item["tags"])
        lines.append(
            f"  #{item['rank']} {item['coin']} {item['total']:.0f}分 | "
            f"24h{item['px_chg']:+.1f}% | {tag_text}"
        )
        lines.append(
            f"     现价 {format_price(item['price'])} | ATH {format_price(item['ath_price'])} "
            f"| 回撤{item['ath_drawdown_pct']:.1f}% | ADX {item['adx']:.1f} "
            f"| EMA距{item['ema_spread_pct']:.1f}% | Vol {format_usd(item['vol'])}"
        )

    lines.append("\n📌 过滤规则：必须在涨幅榜前排，且创ATH或距ATH≤30%；日线趋势5项至少过3项：ADX、EMA多头、20D突破、EMA分离、低点抬高。")
    lines.append("⚠️ 用途：选币观察池，不是直接追高买入信号；入场仍等5m/15m回踩或突破确认。")
    return "\n".join(lines)


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
        quantity = OBSERVER_RISK_BUDGET_USD / stop_distance
        stop_distance_pct = stop_distance / entry_price
        max_safe_leverage = max(1, int(1 / (stop_distance_pct + OBSERVER_LIQUIDATION_BUFFER_PCT)))
        return quantity, max_safe_leverage

    if pullback_ready:
        entry_price = current_price
        stop_price = round(support_price * 0.996, 10)
        rules_ok, rules_note = is_demo_rules_compatible(candidate["symbol"], entry_price)
        if not rules_ok:
            return {
                "watch_status": "WATCHING",
                "trend": "BULLISH",
                "setup_type": "wait-demo-compatible",
                "support_price": support_price,
                "resistance_price": resistance_price,
                "suggested_entry": 0,
                "suggested_stop": 0,
                "notes": f"execution rules incompatible: {rules_note}",
                "trigger_signal": None,
            }
        quantity, leverage = build_position_fields(entry_price, stop_price)
        normalized = normalize_trade_plan(
            candidate["symbol"],
            entry_price,
            stop_price,
            entry_price * 1.25,
            quantity,
        )
        actual_risk, min_required_risk, risk_ok = assess_achievable_risk(
            normalized["entry_price"],
            normalized["stop_price"],
            normalized["quantity"],
        )
        if not risk_ok:
            return {
                "watch_status": "WATCHING",
                "trend": "BULLISH",
                "setup_type": "wait-achievable-risk",
                "support_price": support_price,
                "resistance_price": resistance_price,
                "suggested_entry": normalized["entry_price"],
                "suggested_stop": normalized["stop_price"],
                "notes": (
                    f"risk capped by exchange quantity rules: actual={actual_risk:.2f}u "
                    f"required>={min_required_risk:.2f}u qty={normalized['quantity']}"
                ),
                "trigger_signal": None,
            }
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
            "suggested_entry": normalized["entry_price"],
            "suggested_stop": normalized["stop_price"],
            "notes": f"{reason} qty={normalized['quantity']}",
            "trigger_signal": {
                "symbol": candidate["symbol"],
                "signalType": "BUY",
                "strength": min(92, max(74, int(candidate["strength"] + 8))),
                "confidence": round(min(0.9, max(0.72, candidate["confidence"] + 0.08)), 2),
                "entryPrice": normalized["entry_price"],
                "stopLoss": normalized["stop_price"],
                "leverage": leverage,
                "quantity": normalized["quantity"],
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
        rules_ok, rules_note = is_demo_rules_compatible(candidate["symbol"], entry_price)
        if not rules_ok:
            return {
                "watch_status": "WATCHING",
                "trend": "BULLISH",
                "setup_type": "wait-demo-compatible",
                "support_price": support_price,
                "resistance_price": resistance_price,
                "suggested_entry": 0,
                "suggested_stop": 0,
                "notes": f"execution rules incompatible: {rules_note}",
                "trigger_signal": None,
            }
        quantity, leverage = build_position_fields(entry_price, stop_price)
        normalized = normalize_trade_plan(
            candidate["symbol"],
            entry_price,
            stop_price,
            entry_price * 1.28,
            quantity,
        )
        actual_risk, min_required_risk, risk_ok = assess_achievable_risk(
            normalized["entry_price"],
            normalized["stop_price"],
            normalized["quantity"],
        )
        if not risk_ok:
            return {
                "watch_status": "WATCHING",
                "trend": "BULLISH",
                "setup_type": "wait-achievable-risk",
                "support_price": support_price,
                "resistance_price": resistance_price,
                "suggested_entry": normalized["entry_price"],
                "suggested_stop": normalized["stop_price"],
                "notes": (
                    f"risk capped by exchange quantity rules: actual={actual_risk:.2f}u "
                    f"required>={min_required_risk:.2f}u qty={normalized['quantity']}"
                ),
                "trigger_signal": None,
            }
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
            "suggested_entry": normalized["entry_price"],
            "suggested_stop": normalized["stop_price"],
            "notes": f"{reason} qty={normalized['quantity']}",
            "trigger_signal": {
                "symbol": candidate["symbol"],
                "signalType": "BUY",
                "strength": min(95, max(76, int(candidate["strength"] + 10))),
                "confidence": round(min(0.92, max(0.74, candidate["confidence"] + 0.1)), 2),
                "entryPrice": normalized["entry_price"],
                "stopLoss": normalized["stop_price"],
                "leverage": leverage,
                "quantity": normalized["quantity"],
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


def send_telegram(text, chat_id=None, force=False):
    """发送TG消息"""
    if not force and not ENABLE_TG_PUSH:
        print("[TG] Disabled by ENABLE_TG_PUSH=false")
        return False
    if not TG_BOT_TOKEN:
        print("[TG] No token configured")
        return False
    # General radar/OI reports should go to the group only. Private trade alerts
    # must opt in by passing TG_OBSERVER_CHAT_ID explicitly.
    target_chat_id = chat_id or TG_CHAT_ID
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


def send_bark_trade_alert(title, body, level="active"):
    """Bark 只用于开仓/平仓结果，不承载失败、重试、健康检查等噪音。"""
    if not ENABLE_BARK_PUSH:
        return False

    endpoint = BARK_PUSH_URL.strip()
    payload = {
        "title": title,
        "body": body,
        "group": BARK_GROUP,
        "level": level,
    }
    if not endpoint:
        if not BARK_DEVICE_KEY:
            print("[BARK] BARK_PUSH_URL or BARK_DEVICE_KEY not configured")
            return False
        endpoint = f"{BARK_SERVER_URL.rstrip('/')}/push"
        payload["device_key"] = BARK_DEVICE_KEY

    try:
        resp = requests.post(endpoint, json=payload, timeout=10)
        if resp.status_code < 300:
            return True
        print(f"[BARK] failed {resp.status_code}: {resp.text[:200]}")
    except Exception as e:
        print(f"[BARK] failed: {e}")
    return False


def fmt_price(value):
    value = float(value or 0)
    if value >= 1:
        return f"{value:.4f}"
    return f"{value:.8f}".rstrip("0").rstrip(".")


def build_bark_open_body(label, signal, executed_qty, entry_price, stop_price, leverage, notional_value, estimated_margin, risk_value, order_id, entry_reason_text):
    symbol = signal.get("symbol", "")
    side = signal.get("signalType", "")
    return "\n".join([
        f"{label} {symbol} {side}",
        f"入 {fmt_price(entry_price)} | 止 {fmt_price(stop_price)}",
        f"数 {executed_qty:g} | 仓值 {notional_value:.2f}U",
        f"因: {entry_reason_text}",
    ])


def build_bark_close_body(label, position_row, exit_price, pnl_value, pnl_pct, roi_pct, reason):
    symbol = position_row.get("symbol", "")
    side = position_row.get("side", "")
    entry_price = float(position_row.get("entry_price") or 0)
    stop_price = float(position_row.get("stop_price") or 0)
    quantity = float(position_row.get("quantity") or 0)
    exit_value = exit_price * quantity if exit_price > 0 and quantity > 0 else 0
    return "\n".join([
        f"{label} {symbol} {side} CLOSED",
        f"入 {fmt_price(entry_price)} | 出 {fmt_price(exit_price)} | 止 {fmt_price(stop_price)}",
        f"数 {quantity:g} | 仓值 {exit_value:.2f}U",
        f"盈亏 {pnl_value:+.2f}U | ROI {roi_pct:+.0f}% | 涨跌 {pnl_pct:+.2f}%",
        f"因: {describe_exit_reason(reason)}",
    ])


def build_bark_guard_body(label, position_row, old_guard, new_guard, current_price, peak_price):
    symbol = position_row.get("symbol", "")
    entry_price = float(position_row.get("entry_price") or 0)
    quantity = float(position_row.get("quantity") or 0)
    lock_value = (new_guard - entry_price) * quantity if entry_price > 0 and quantity > 0 else 0
    current_pnl = (current_price - entry_price) * quantity if current_price > 0 and quantity > 0 else 0
    return "\n".join([
        f"{label} {symbol} 保护线上移",
        f"旧 {fmt_price(old_guard)} -> 新 {fmt_price(new_guard)}",
        f"现 {fmt_price(current_price)} | 峰 {fmt_price(peak_price)}",
        f"锁定 {lock_value:+.2f}U | 浮盈 {current_pnl:+.2f}U",
    ])


def should_alert_guard_change(position_row, old_guard, new_guard):
    if old_guard <= 0 or new_guard <= 0:
        return False
    if new_guard <= old_guard:
        return False
    entry_price = float(position_row.get("entry_price") or 0)
    stop_price = float(position_row.get("stop_price") or 0)
    quantity = float(position_row.get("quantity") or 0)
    pct_change = (new_guard - old_guard) / max(old_guard, 1e-12)
    lock_change = (new_guard - old_guard) * quantity
    first_meaningful_lift = old_guard <= max(stop_price, 0) * 1.001 and new_guard > max(stop_price, entry_price * 0.995)
    return (
        first_meaningful_lift
        or pct_change >= GUARD_ALERT_MIN_CHANGE_PCT
        or lock_change >= GUARD_ALERT_MIN_LOCK_USD_CHANGE
    )


def send_private_guard_update_alert(position_row, old_guard, new_guard, analysis, peak_price):
    """动态保护线变化，单独推送到私聊和 Bark。"""
    if not ENABLE_PRIVATE_POSITION_PUSH and not ENABLE_BARK_PUSH:
        return False

    current_price = float(analysis.get("current_price") or position_row.get("last_price") or 0)
    entry_price = float(position_row.get("entry_price") or 0)
    quantity = float(position_row.get("quantity") or 0)
    leverage = max(float(position_row.get("leverage") or 1), 1)
    lock_value = (new_guard - entry_price) * quantity if entry_price > 0 and quantity > 0 else 0
    current_pnl = (current_price - entry_price) * quantity if current_price > 0 and quantity > 0 else 0
    estimated_margin = (entry_price * quantity / leverage) if entry_price > 0 and quantity > 0 else 0
    lock_roi = (lock_value / estimated_margin * 100) if estimated_margin > 0 else 0
    current_roi = (current_pnl / estimated_margin * 100) if estimated_margin > 0 else 0
    label = ENV_LABEL

    bark_title = f"{label} 保护线 {position_row.get('symbol', '')}"
    bark_body = build_bark_guard_body(label, position_row, old_guard, new_guard, current_price, peak_price)
    send_bark_trade_alert(bark_title, bark_body, level="active")

    lines = [
        f"🛡️ {label} 动态保护线更新",
        f"代币：{position_row.get('symbol', '')}",
        f"方向：{position_row.get('side', '')}",
        f"入场价：{entry_price:.10f}",
        f"当前价：{current_price:.10f}",
        f"峰值价：{float(peak_price or 0):.10f}",
        f"保护线：{old_guard:.10f} -> {new_guard:.10f}",
        f"当前浮盈：{current_pnl:+.2f} USDT，ROI {current_roi:+.0f}%",
        f"保护线锁定：{lock_value:+.2f} USDT，ROI {lock_roi:+.0f}%",
        f"更新原因：{analysis.get('reason', '')}",
    ]
    if ENABLE_PRIVATE_POSITION_PUSH and TG_OBSERVER_CHAT_ID:
        return send_telegram("\n".join(lines), chat_id=TG_OBSERVER_CHAT_ID, force=True)
    return True


def send_private_position_open_alert(signal, response_body):
    """新仓位成交后，单独推送到私聊。"""
    if not ENABLE_PRIVATE_POSITION_PUSH and not ENABLE_BARK_PUSH:
        return False
    if ENABLE_PRIVATE_POSITION_PUSH and not TG_OBSERVER_CHAT_ID:
        print("[TG] TG_OBSERVER_CHAT_ID not configured for private position alerts")

    decision = response_body.get("decision", {}) if isinstance(response_body, dict) else {}
    execution = decision.get("execution", {}) if isinstance(decision, dict) else {}
    executed_qty = float(execution.get("quantity") or signal.get("quantity") or 0)
    entry_price = float(signal.get("entryPrice") or 0)
    stop_price = float(signal.get("stopLoss") or 0)
    take_profit = signal.get("takeProfit")
    leverage = signal.get("leverage")
    notional_value = executed_qty * entry_price if executed_qty > 0 and entry_price > 0 else 0
    risk_value = executed_qty * max(entry_price - stop_price, 0) if executed_qty > 0 else 0
    stop_distance_pct = ((entry_price - stop_price) / entry_price * 100.0) if entry_price > 0 and stop_price > 0 else 0
    estimated_margin = (notional_value / float(leverage)) if leverage and notional_value > 0 else 0
    order_id = execution.get("orderId")
    label = signal.get("envLabel") or ENV_LABEL

    entry_reason = signal.get("reason", "")
    if "pullback" in entry_reason:
        entry_reason_text = "回调到短线支撑附近后重新走强，观察器确认是更稳的低风险切入点。"
    elif "breakout" in entry_reason:
        entry_reason_text = "短线整理后重新放量突破，价格与结构同时转强，观察器确认可以参与。"
    else:
        entry_reason_text = "雷达候选经过短周期趋势、支撑和风险模型筛选后，满足当前入场条件。"

    lines = [
        f"🟢 **{label} 新仓位已开启**",
        f"代币：`{signal.get('symbol', '')}`",
        f"方向：`{signal.get('signalType', '')}`",
        f"入场价：`{entry_price:.10f}`",
        f"止损价：`{stop_price:.10f}`",
    ]
    if take_profit is not None:
        lines.append(f"参考止盈：`{float(take_profit):.10f}`")
    else:
        lines.append("止盈策略：`无固定止盈，动态观察离场`")
    if leverage:
        lines.append(f"杠杆：`{leverage}x`")
    lines.extend([
        f"数量：`{executed_qty:.6f}`",
        f"仓位价值：`{notional_value:.2f} USDT`",
        f"名义价值：`{notional_value:.2f} USDT`",
        f"预估保证金：`{estimated_margin:.2f} USDT`",
        f"风险额：`{risk_value:.2f} USDT`",
        f"止损距离：`{stop_distance_pct:.2f}%`",
    ])
    if order_id:
        lines.append(f"订单号：`{order_id}`")
    lines.append(f"入场理由说明：{entry_reason_text}")
    if signal.get("reason"):
        lines.append(f"入场规则：{signal['reason']}")

    bark_title = f"{label} 开仓 {signal.get('symbol', '')}"
    bark_body = build_bark_open_body(
        label,
        signal,
        executed_qty,
        entry_price,
        stop_price,
        leverage,
        notional_value,
        estimated_margin,
        risk_value,
        order_id,
        entry_reason_text,
    )
    send_bark_trade_alert(bark_title, bark_body)

    if ENABLE_PRIVATE_POSITION_PUSH and TG_OBSERVER_CHAT_ID:
        return send_telegram("\n".join(lines), chat_id=TG_OBSERVER_CHAT_ID, force=True)
    return True


def describe_exit_reason(reason):
    reason_text = reason or ""
    if reason_text.startswith("hard stop hit"):
        return "价格跌破了预设止损位，系统按纪律直接止损离场。"
    if reason_text.startswith("early invalidation"):
        return "浮亏已接近试错警戒线，同时短线 K 线、均线或 OI 出现多项走坏证据，系统不等硬止损，先小亏撤退。"
    if reason_text.startswith("profit protection retrace"):
        return "这笔单已经有浮盈，随后从高点明显回撤并跌破利润保护结构位，系统选择先锁住利润。"
    if reason_text.startswith("profit structure break"):
        return "这笔单虽然还有利润，但短线 K 线结构已经跌破保护位，系统优先保护已有收益。"
    if reason_text.startswith("short build-up"):
        return "检测到价格走弱的同时空头 OI 明显增加，说明更像是真正建立空头，系统选择离场。"
    if reason_text.startswith("fuel exhaustion + structure break"):
        return "空头燃料开始衰减，同时价格结构也走坏，继续持有的赔率已经变差，所以离场。"
    if reason_text.startswith("structure under pressure"):
        return "短线结构开始承压，系统进入谨慎处理阶段。"
    return "系统检测到持仓结构或资金面条件不再支持继续持有，因此执行离场。"


def send_private_position_close_alert(position_row, analysis):
    """仓位离场后，单独推送到私聊。"""
    if not ENABLE_PRIVATE_POSITION_PUSH and not ENABLE_BARK_PUSH:
        return False

    exit_price = float(analysis.get("current_price") or position_row.get("last_price") or 0)
    entry_price = float(position_row.get("entry_price") or 0)
    quantity = float(position_row.get("quantity") or 0)
    leverage = max(float(position_row.get("leverage") or 1), 1)
    pnl_value = (exit_price - entry_price) * quantity if exit_price > 0 and entry_price > 0 else 0
    pnl_pct = ((exit_price - entry_price) / entry_price * 100.0) if exit_price > 0 and entry_price > 0 else 0
    estimated_margin = (entry_price * quantity / leverage) if entry_price > 0 and quantity > 0 else 0
    roi_pct = (pnl_value / estimated_margin * 100.0) if estimated_margin > 0 else 0
    reason = analysis.get("reason") or position_row.get("exit_reason") or ""

    lines = [
        "🔴 **仓位已离场**",
        f"代币：`{position_row.get('symbol', '')}`",
        f"方向：`{position_row.get('side', '')}`",
        f"入场价：`{entry_price:.10f}`",
        f"出场价：`{exit_price:.10f}`",
        f"数量：`{quantity:.6f}`",
        f"盈亏：`{pnl_value:.2f} USDT ({pnl_pct:+.2f}%)`",
        f"出场理由说明：{describe_exit_reason(reason)}",
    ]
    if reason:
        lines.append(f"出场规则：{reason}")

    label = position_row.get("envLabel") or ENV_LABEL
    bark_title = f"{label} 平仓 {position_row.get('symbol', '')}"
    bark_body = build_bark_close_body(label, position_row, exit_price, pnl_value, pnl_pct, roi_pct, reason)
    send_bark_trade_alert(bark_title, bark_body)

    if ENABLE_PRIVATE_POSITION_PUSH and TG_OBSERVER_CHAT_ID:
        return send_telegram("\n".join(lines), chat_id=TG_OBSERVER_CHAT_ID, force=True)
    return True


def is_retryable_order_failure(status_code, body_text):
    """判断 ai-trader/webhook 下单失败是否值得重试。"""
    body_lower = (body_text or "").lower()
    non_retryable_keywords = [
        "indicator_error",
        "failed to calculate ema",
        "insufficient kline data for ema",
        "precision",
        "min notional",
        "notional",
        "min qty",
        "max qty",
        "lot size",
        "price filter",
        "invalid symbol",
        "invalid symbol status",
        "mandatory parameter",
        "insufficient margin",
        "margin is insufficient",
        "quantity less than",
        "would immediately trigger",
        "position side",
        "reduceonly",
        "exceeded maximum",
    ]
    if any(keyword in body_lower for keyword in non_retryable_keywords):
        return False

    if status_code in {408, 409, 425, 429, 500, 502, 503, 504}:
        return True

    if status_code == 400 and (
        "failed to place order" in body_lower
        or "\"trading_error\"" in body_lower
        or "request failed with status code 400" in body_lower
    ):
        return True

    return False


def classify_order_failure(status_code, body_text):
    """把 webhook/执行端失败归类，便于对观察池做后续处理。"""
    text = (body_text or "").lower()
    if "invalid symbol" in text:
        return "invalid-symbol"
    if "invalid symbol status" in text:
        return "invalid-symbol"
    if "market_data_error" in text and "invalid symbol" in text:
        return "invalid-symbol"
    if "exceeded the maximum allowable position" in text or "exceeded maximum" in text:
        return "position-limit"
    if "insufficient margin" in text or "margin is insufficient" in text:
        return "insufficient-margin"
    if "indicator_error" in text or "failed to calculate ema" in text or "insufficient kline data for ema" in text:
        return "insufficient-indicator-data"
    if "precision" in text or "lot size" in text or "price filter" in text:
        return "rule-mismatch"
    if is_retryable_order_failure(status_code, body_text):
        return "retryable"
    return "non-retryable"


def update_entry_watch_status(symbol, watch_status, notes, setup_type=None):
    """更新观察池状态，避免已知失败币被反复盯盘和重试。"""
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            """UPDATE entry_watchlist
               SET watch_status = ?,
                   setup_type = COALESCE(?, setup_type),
                   notes = ?,
                   last_analysis_time = ?
               WHERE symbol = ?""",
            (watch_status, setup_type, notes[:500], now, symbol),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[WATCHLIST] failed to update {symbol} -> {watch_status}: {e}")


def emit_trade_signals(signals):
    """把雷达候选信号推送给 ai-trader 执行层。"""
    if not signals:
        print("[WEBHOOK] No trade signals to emit")
        return []
    if not AI_TRADER_AUTO_EXECUTE:
        print("[WEBHOOK] AI_TRADER_AUTO_EXECUTE=false; skip all execution fanout")
        return []

    targets = build_fanout_targets()
    if not targets:
        print("[WEBHOOK] No enabled AI trader fanout targets configured")
        return []

    headers = {"Content-Type": "application/json"}
    if AI_TRADER_WEBHOOK_SECRET:
        headers["x-webhook-secret"] = AI_TRADER_WEBHOOK_SECRET

    accepted = []
    for signal in signals[:AI_TRADER_SIGNAL_LIMIT]:
        for target in targets:
            target_signal = scale_signal_for_target(signal, target)
            payload = {"signal": target_signal}
            attempts = max(1, AI_TRADER_RETRY_ATTEMPTS)
            for attempt in range(1, attempts + 1):
                try:
                    resp = requests.post(target["url"], json=payload, headers=headers, timeout=15)
                    if resp.status_code == 200:
                        body = {}
                        try:
                            body = resp.json()
                        except Exception:
                            body = {}
                        print(f"[WEBHOOK:{target['label']}] {target_signal['symbol']} {target_signal['signalType']} accepted")
                        accepted.append({
                            "symbol": target_signal["symbol"],
                            "signal": target_signal,
                            "response": body,
                            "target": target,
                        })
                        break

                    body_text = resp.text[:300]
                    print(f"[WEBHOOK:{target['label']}] {target_signal['symbol']} failed (attempt {attempt}/{attempts}): {resp.status_code} {body_text[:200]}")
                    failure_type = classify_order_failure(resp.status_code, body_text)
                    if failure_type == "invalid-symbol" and target.get("invalidate_on_failure", True):
                        update_entry_watch_status(
                            target_signal["symbol"],
                            "INVALIDATED",
                            f"execution invalidated: invalid symbol from {target['label']} ({body_text[:180]})",
                            setup_type="invalid-symbol",
                        )
                        print(f"[WEBHOOK:{target['label']}] {target_signal['symbol']} invalidated due to invalid symbol; stop retrying")
                        break
                    if failure_type == "insufficient-indicator-data" and target.get("invalidate_on_failure", True):
                        update_entry_watch_status(
                            target_signal["symbol"],
                            "INVALIDATED",
                            f"execution invalidated: insufficient indicator data from {target['label']} ({body_text[:180]})",
                            setup_type="insufficient-indicator-data",
                        )
                        print(f"[WEBHOOK:{target['label']}] {target_signal['symbol']} invalidated due to insufficient indicator data; stop retrying")
                        break
                    if attempt < attempts and is_retryable_order_failure(resp.status_code, body_text):
                        time.sleep(AI_TRADER_RETRY_DELAY_SEC * attempt)
                        continue

                    print(f"[WEBHOOK:{target['label']}] {target_signal['symbol']} giving up after {attempt} attempt(s); candidate will stay READY for later observe retry")
                    break
                except Exception as e:
                    print(f"[WEBHOOK:{target['label']}] {target_signal['symbol']} error (attempt {attempt}/{attempts}): {e}")
                    if attempt < attempts:
                        time.sleep(AI_TRADER_RETRY_DELAY_SEC * attempt)
                        continue
                    print(f"[WEBHOOK:{target['label']}] {target_signal['symbol']} giving up after {attempt} attempt(s); candidate will stay READY for later observe retry")
                    break
    return accepted


def build_fanout_targets():
    """解析单雷达源的执行端 fanout 配置。

    格式:
      LABEL|WEBHOOK_URL|QUANTITY_SCALE|ENABLED
    多个目标用分号分隔。未配置时回退到 AI_TRADER_WEBHOOK_URL。
    """
    targets = []
    if AI_TRADER_FANOUT_TARGETS.strip():
        for raw in AI_TRADER_FANOUT_TARGETS.split(";"):
            parts = [part.strip() for part in raw.split("|")]
            if len(parts) < 2:
                continue
            label = parts[0] or "TARGET"
            url = parts[1]
            scale = float(parts[2]) if len(parts) >= 3 and parts[2] else 1.0
            enabled = True
            if len(parts) >= 4 and parts[3]:
                enabled = parts[3].lower() in {"1", "true", "yes", "on", "enabled"}
            if enabled and url:
                targets.append({
                    "label": label.upper(),
                    "url": url,
                    "quantity_scale": scale,
                    "invalidate_on_failure": label.upper() != "LIVE",
                })
        return targets

    if AI_TRADER_WEBHOOK_URL:
        targets.append({
            "label": ENV_LABEL,
            "url": AI_TRADER_WEBHOOK_URL,
            "quantity_scale": 1.0,
            "invalidate_on_failure": True,
        })
    return targets


def scale_signal_for_target(signal, target):
    scaled = dict(signal)
    label = target.get("label") or ENV_LABEL
    scale = float(target.get("quantity_scale") or 1.0)
    if scale != 1.0 and scaled.get("quantity"):
        scaled["quantity"] = round(float(scaled["quantity"]) * scale, 8)
    scaled["envLabel"] = label
    scaled["fanoutTarget"] = label
    if label == "LIVE":
        scaled["reason"] = f"{scaled.get('reason', '')} | fanout=LIVE risk_scale={scale:g}".strip()
    return scaled


def build_trade_signals(chase, combined, ambush, momentum_pool=None, new_listing_pool=None):
    """从雷达结果里提炼保守的自动交易信号。默认只发 BUY。"""
    candidates = []
    seen = set()
    now = datetime.now(timezone.utc).isoformat()
    momentum_pool = momentum_pool or []
    new_listing_pool = new_listing_pool or []

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

    for item in new_listing_pool:
        if item["sym"] in seen:
            continue
        if item["total"] < 55 or max(abs(item["d1h"]), abs(item["d6h"])) < 4:
            continue
        if item["vol"] < 10_000_000 or item["px_chg"] < -45 or item["px_chg"] > 120:
            continue

        price = item["price"]
        confidence = min(0.86, max(0.66, 0.48 + item["total"] / 180))
        candidates.append({
            "symbol": item["sym"],
            "signalType": "BUY",
            "strength": min(90, max(72, int(item["total"]))),
            "confidence": round(confidence, 2),
            "entryPrice": price,
            "stopLoss": round(price * 0.92, 10),
            "takeProfit": round(price * 1.20, 10),
            "leverage": 3,
            "interval": "5m",
            "confirmInterval": "15m",
            "autoExecute": AI_TRADER_AUTO_EXECUTE,
            "source": "new-listing-pool",
            "reason": item["reason"],
            "timestamp": now,
        })
        seen.add(item["sym"])

    for item in momentum_pool:
        if item["sym"] in seen:
            continue
        if item["total"] < 60 or item["d6h"] < 3:
            continue
        if item["px_chg"] < -25 or item["px_chg"] > 60:
            continue

        price = item["price"]
        confidence = min(0.84, max(0.64, 0.48 + item["total"] / 220))
        candidates.append({
            "symbol": item["sym"],
            "signalType": "BUY",
            "strength": min(88, max(70, int(item["total"]))),
            "confidence": round(confidence, 2),
            "entryPrice": price,
            "stopLoss": round(price * 0.93, 10),
            "takeProfit": round(price * 1.16, 10),
            "leverage": 3,
            "interval": "15m",
            "confirmInterval": "1h",
            "autoExecute": AI_TRADER_AUTO_EXECUTE,
            "source": "momentum-pool",
            "reason": item["reason"],
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


def track_executed_observer_position(conn, signal, response_body):
    """把 observer 已成交仓位纳入持仓观察池。"""
    decision = response_body.get("decision", {}) if isinstance(response_body, dict) else {}
    execution = decision.get("execution", {}) if isinstance(decision, dict) else {}
    if not execution.get("executed"):
        return

    c = conn.cursor()
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    c.execute(
        """INSERT OR REPLACE INTO position_watchlist
           (symbol, source, side, entry_price, stop_price, reference_support, quantity, leverage,
            status, opened_at, last_check_time, peak_price, last_price, guard_price, exit_signal, exit_reason, orders_canceled)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?, ?, ?, '', '', 0)""",
        (
            signal["symbol"],
            signal.get("source", "accumulation-radar-observer"),
            "LONG" if signal.get("signalType") == "BUY" else "SHORT",
            signal.get("entryPrice", 0),
            signal.get("stopLoss", 0),
            signal.get("stopLoss", 0),
            execution.get("quantity", signal.get("quantity", 0)),
            signal.get("leverage", 1),
            now,
            now,
            signal.get("entryPrice", 0),
            signal.get("entryPrice", 0),
            signal.get("stopLoss", 0),
        ),
    )
    conn.commit()
    send_private_position_open_alert(signal, response_body)


def ai_trader_request(method, path, payload=None):
    url = f"{AI_TRADER_API_BASE}{path}"
    try:
        session = requests.Session()
        session.trust_env = False
        resp = session.request(method, url, json=payload, timeout=15)
        if resp.status_code == 200:
            try:
                return True, resp.json()
            except Exception:
                return True, {}
        return False, {"status": resp.status_code, "body": resp.text[:300]}
    except Exception as e:
        return False, {"error": str(e)}


def fetch_oi_delta(symbol, period="5m", limit=4):
    data = api_get("/futures/data/openInterestHist", {
        "symbol": symbol,
        "period": period,
        "limit": limit,
    })
    if not data or len(data) < 2:
        return 0.0
    first = float(data[0].get("sumOpenInterestValue") or data[0].get("sumOpenInterest", 0) or 0)
    last = float(data[-1].get("sumOpenInterestValue") or data[-1].get("sumOpenInterest", 0) or 0)
    if first <= 0:
        return 0.0
    return (last - first) / first


def fetch_current_funding(symbol):
    data = api_get("/fapi/v1/premiumIndex", {"symbol": symbol})
    if isinstance(data, dict):
        return float(data.get("lastFundingRate", 0) or 0)
    return 0.0


def sync_executed_positions_from_ai_trader(conn):
    """从 ai-trader 的 signal_events 里同步已成交 observer 仓位。"""
    if not AI_TRADER_DB_PATH.exists():
        return
    ext_conn = sqlite3.connect(str(AI_TRADER_DB_PATH))
    ext_cur = ext_conn.cursor()
    ext_cur.execute(
        """SELECT normalizedPayload FROM signal_events
           WHERE source = 'accumulation-radar-observer' AND status = 'EXECUTED'
           ORDER BY receivedAt DESC LIMIT 20"""
    )
    rows = ext_cur.fetchall()
    ext_conn.close()

    c = conn.cursor()
    for (payload_text,) in rows:
        try:
            payload = json.loads(payload_text)
        except Exception:
            continue
        symbol = payload.get("symbol")
        if not symbol:
            continue
        exists = c.execute("SELECT 1 FROM position_watchlist WHERE symbol = ?", (symbol,)).fetchone()
        if exists:
            continue
        c.execute(
            """INSERT OR REPLACE INTO position_watchlist
               (symbol, source, side, entry_price, stop_price, reference_support, quantity, leverage,
                status, opened_at, last_check_time, peak_price, last_price, guard_price, exit_signal, exit_reason, orders_canceled)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?, ?, ?, '', '', 0)""",
            (
                symbol,
                payload.get("source", "accumulation-radar-observer"),
                "LONG" if payload.get("signalType") == "BUY" else "SHORT",
                payload.get("entryPrice", 0),
                payload.get("stopLoss", 0),
                payload.get("stopLoss", 0),
                payload.get("quantity", 0),
                payload.get("leverage", 1),
                datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"),
                datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S"),
                payload.get("entryPrice", 0),
                payload.get("entryPrice", 0),
                payload.get("stopLoss", 0),
            ),
        )
    conn.commit()


def analyze_open_position(position_row):
    """基于K线波段的动态止盈止损分析。
    核心逻辑：
    1. 分析入场后价格处于哪一波段（上涨中/回调中/下跌中）
    2. 识别上涨是否已经结束，进入下跌波段
    3. 若下跌波段强度超过上涨波段的40%，立即出场
    4. 动态调整止损线：跟随波段低点，不断上移
    """
    if not isinstance(position_row, dict):
        position_row = dict(position_row)
    symbol = position_row["symbol"]
    entry_price = position_row["entry_price"]
    stop_price = position_row["stop_price"]
    quantity = float(position_row.get("quantity") or 0)
    leverage = max(float(position_row.get("leverage") or 1), 1)
    previous_guard = float(position_row.get("guard_price") or 0)

    klines_5m = fetch_intraday_klines(symbol, "5m", 120)
    klines_15m = fetch_intraday_klines(symbol, "15m", 80)

    def apply_high_profit_guard(current_price, peak_price, base_guard):
        peak_unrealized = (peak_price - entry_price) * quantity
        peak_move = (peak_price - entry_price) / entry_price if entry_price > 0 else 0
        estimated_margin = (entry_price * quantity / leverage) if entry_price > 0 and quantity > 0 else 0
        peak_roi = peak_unrealized / estimated_margin if estimated_margin > 0 else 0
        if (
            peak_unrealized < POSITION_PROFIT_GUARD_MIN_USD
            and peak_move < POSITION_PROFIT_GUARD_MIN_MOVE
            and peak_roi < POSITION_ROI_GUARD_TIER1
        ):
            return base_guard
        lock_ratio = 0.35
        if peak_roi >= POSITION_ROI_GUARD_TIER1:
            lock_ratio = max(lock_ratio, 0.45)
        if peak_unrealized >= 300 or peak_move >= 0.055 or peak_roi >= POSITION_ROI_GUARD_TIER2:
            lock_ratio = 0.50
        if peak_unrealized >= 600 or peak_move >= 0.10:
            lock_ratio = 0.60
        profit_floor = entry_price + (peak_price - entry_price) * lock_ratio
        return max(base_guard, profit_floor)

    if len(klines_5m) < 40 or len(klines_15m) < 20:
        fallback_price = float(position_row.get("last_price") or entry_price or 0)
        fallback_peak = max(float(position_row.get("peak_price") or 0), fallback_price, entry_price)
        fallback_guard = apply_high_profit_guard(fallback_price, fallback_peak, max(previous_guard, stop_price))
        if fallback_price > 0 and fallback_price <= fallback_guard and fallback_guard > stop_price:
            return {
                "action": "WATCH",
                "reason": f"profit guard armed but intraday data insufficient guard={fallback_guard:.6f} current={fallback_price:.6f}",
                "current_price": fallback_price,
                "guard_price": fallback_guard,
            }
        return {
            "action": "HOLD",
            "reason": f"insufficient intraday data, guarded at {fallback_guard:.6f}",
            "current_price": fallback_price,
            "guard_price": fallback_guard,
        }

    closes_5m = [k["close"] for k in klines_5m]
    opens_5m = [k["open"] for k in klines_5m]
    highs_5m = [k["high"] for k in klines_5m]
    lows_5m = [k["low"] for k in klines_5m]
    volumes_5m = [k["volume"] for k in klines_5m]
    closes_15m = [k["close"] for k in klines_15m]
    highs_15m = [k["high"] for k in klines_15m]
    lows_15m = [k["low"] for k in klines_15m]

    current_price = closes_5m[-1]

    # === 基础指标 ===
    move_from_entry = (current_price - entry_price) / entry_price if entry_price > 0 else 0
    peak_price = max(float(position_row.get("peak_price") or 0), current_price, entry_price)
    estimated_margin = (entry_price * quantity / leverage) if entry_price > 0 and quantity > 0 else 0
    oi_delta_15m = fetch_oi_delta(symbol, "5m", 4)
    oi_delta_30m = fetch_oi_delta(symbol, "5m", 7)
    funding_rate = fetch_current_funding(symbol)
    price_change_15m = (closes_5m[-1] - closes_5m[-4]) / closes_5m[-4] if closes_5m[-4] > 0 else 0
    planned_risk_usd = quantity * max(entry_price - stop_price, 0)
    risk_reference_usd = planned_risk_usd if planned_risk_usd > 0 else OBSERVER_RISK_BUDGET_USD
    early_loss_limit_usd = max(POSITION_EARLY_LOSS_MIN_USD, risk_reference_usd * POSITION_EARLY_LOSS_FRACTION)
    current_unrealized = (current_price - entry_price) * quantity
    current_roi = current_unrealized / estimated_margin if estimated_margin > 0 else 0

    ema8_5m = ema(closes_5m, 8)
    ema21_5m = ema(closes_5m, 21)
    ema55_5m = ema(closes_5m, 55)
    ema21_15m = ema(closes_15m, 21)

    # === 1. 硬止损检查 ===
    if current_price <= stop_price:
        return {
            "action": "EXIT",
            "reason": f"hard stop hit current={current_price:.6f} stop={stop_price:.6f}",
            "current_price": current_price, "oi_delta": oi_delta_15m, "funding_rate": funding_rate,
        }

    # === 1b. 试错成本保护 ===
    # 妖币策略允许用小亏损试错，但如果浮亏已接近风险预算的一半，
    # 且K线/趋势/OI同时给出走坏证据，就不等硬止损，先退出保留弹药。
    loss_usd = max(0, -current_unrealized)
    recent_red_count = sum(1 for i in range(-3, 0) if closes_5m[i] < opens_5m[i])
    lower_low_break = closes_5m[-1] < min(lows_5m[-8:-1]) * 0.999 if len(lows_5m) >= 9 else False
    ema_bearish_stack = closes_5m[-1] < ema8_5m[-1] < ema21_5m[-1]
    failed_reclaim = highs_5m[-1] < ema8_5m[-1] and closes_5m[-1] < closes_5m[-2]
    sell_volume_expands = (
        volumes_5m[-1] > (sum(volumes_5m[-12:-1]) / max(len(volumes_5m[-12:-1]), 1)) * 1.25
        and closes_5m[-1] < opens_5m[-1]
    )
    oi_down_or_short_pressure = oi_delta_15m <= -0.02 or (oi_delta_15m >= 0.025 and price_change_15m <= -0.006)
    early_weakness_flags = [
        ("3 red 5m candles", recent_red_count >= 3),
        ("lower-low break", lower_low_break),
        ("ema8/21 bearish", ema_bearish_stack),
        ("failed ema reclaim", failed_reclaim),
        ("sell volume expands", sell_volume_expands),
        ("oi pressure", oi_down_or_short_pressure),
    ]
    early_weakness_score = sum(1 for _, enabled in early_weakness_flags if enabled)
    if loss_usd >= early_loss_limit_usd and early_weakness_score >= POSITION_EARLY_EXIT_SCORE:
        active_flags = ", ".join(name for name, enabled in early_weakness_flags if enabled)
        return {
            "action": "EXIT",
            "reason": (
                f"early invalidation: loss={loss_usd:.1f}u limit={early_loss_limit_usd:.1f}u "
                f"score={early_weakness_score} [{active_flags}]"
            ),
            "current_price": current_price,
            "guard_price": max(previous_guard, stop_price),
            "oi_delta": oi_delta_15m,
            "funding_rate": funding_rate,
        }

    # === 2. 分析入场后的波段结构 ===
    entry_time = position_row.get("opened_at")
    if entry_time:
        try:
            from dateutil import parser as dateutil_parser
            entry_dt = dateutil_parser.parse(entry_time)
            entry_index = None
            for i in range(len(klines_5m) - 1, -1, -1):
                candle_time = datetime.fromtimestamp(klines_5m[i]["open_time"] / 1000, tz=timezone.utc)
                if candle_time >= entry_dt:
                    entry_index = i
                    break
        except Exception:
            entry_index = None
    if entry_index is None:
        # 无法确定入场位置，使用最近20根K线
        entry_index = max(0, len(closes_5m) - 20)

    # 入场后的K线
    post_entry_closes = closes_5m[entry_index:]
    post_entry_highs = highs_5m[entry_index:]
    post_entry_lows = lows_5m[entry_index:]
    post_entry_volumes = volumes_5m[entry_index:]

    if len(post_entry_closes) < 4:
        return {
            "action": "HOLD",
            "reason": f"entry too recent, hold move={move_from_entry*100:.1f}%",
            "current_price": current_price, "oi_delta": oi_delta_15m, "funding_rate": funding_rate,
        }

    # === 3. 识别波段高低点 ===
    def find_swing_points(data, is_low=True, min_candles=2):
        """找出波段的高低点"""
        points = []
        for i in range(min_candles, len(data) - min_candles):
            if is_low:
                if all(data[i] <= data[i - j] for j in range(1, min_candles + 1)) and \
                   all(data[i] <= data[i + j] for j in range(1, min_candles + 1)):
                    points.append((i, data[i]))
            else:
                if all(data[i] >= data[i - j] for j in range(1, min_candles + 1)) and \
                   all(data[i] >= data[i + j] for j in range(1, min_candles + 1)):
                    points.append((i, data[i]))
        return points

    swing_lows = find_swing_points(post_entry_lows, is_low=True, min_candles=2)
    swing_highs = find_swing_points(post_entry_highs, is_low=False, min_candles=2)

    # === 4. 分析当前处于哪一波段 ===
    last_swing_low_idx = swing_lows[-1][0] if swing_lows else 0
    last_swing_high_idx = swing_highs[-1][0] if swing_highs else 0

    # 当前是否在下跌波段中？
    # 判断：如果最后一个波段低点在最后一个波段高点之后，且价格已跌破最近低点
    in_downleg = False
    downleg_strength = 0.0
    upleg_strength = 0.0
    wave_phase = "unknown"

    if len(swing_lows) >= 2 and len(swing_highs) >= 1:
        # 最近的低点和次低的低点
        recent_low = swing_lows[-1][1]
        prev_low = swing_lows[-2][1]
        recent_high = swing_highs[-1][1] if swing_highs else post_entry_highs[-1]

        # 上一上涨波段：从 prev_low 到 recent_high
        upleg_pct = (recent_high - prev_low) / prev_low if prev_low > 0 else 0

        # 当前下跌波段：从 recent_high 到 current_price
        downleg_pct = (recent_high - current_price) / recent_high if recent_high > 0 else 0

        # 判断是否在下跌波段：价格跌破最近的波段低点
        in_downleg = current_price < recent_low * 1.002  # 轻微穿透也算

        # 波段强度比：下跌 / 上涨
        if upleg_pct > 0.001:  # 避免除以接近0的数
            wave_ratio = downleg_pct / upleg_pct
        else:
            wave_ratio = downleg_pct * 10  # 如果上涨波段太小，放大下跌影响

        wave_phase = f"upleg={upleg_pct*100:.2f}% downleg={downleg_pct*100:.2f}% ratio={wave_ratio:.2f}"

        # 强下跌信号：下跌幅度超过上涨的40% 且 当前在下跌波段中
        if in_downleg:
            downleg_strength = downleg_pct
            upleg_strength = upleg_pct
            strong_downleg_confirmed = (
                wave_ratio >= POSITION_STRONG_DOWNLEG_RATIO
                and downleg_pct >= POSITION_STRONG_DOWNLEG_MIN_DOWN
                and (
                    closes_5m[-1] < ema21_5m[-1]
                    or oi_delta_15m <= -0.02
                    or (oi_delta_15m >= 0.025 and price_change_15m <= -0.006)
                )
            )
            if strong_downleg_confirmed:
                # 强下跌波段 + 趋势/OI确认 → 出场；普通洗盘先给空间。
                guard = max(stop_price, recent_low * 1.002)
                return {
                    "action": "EXIT",
                    "reason": f"strong downleg: down={downleg_pct*100:.2f}% up={upleg_pct*100:.2f}% ratio={wave_ratio:.2f} low={recent_low:.6f}",
                    "current_price": current_price, "guard_price": guard,
                    "oi_delta": oi_delta_15m, "funding_rate": funding_rate,
                }

    # === 5. 动态止损线：跟随波段低点 ===
    # 如果有足够的波段低点，以波段低点作为动态止损
    if swing_lows:
        recent_swing_low = min(p[1] for p in swing_lows[-3:])  # 最近3个波段低点的最低
        dynamic_stop = max(stop_price, recent_swing_low * 0.998)
    else:
        # 没有明显波段，使用最近低点
        recent_swing_low = min(post_entry_lows[-5:])
        dynamic_stop = max(stop_price, recent_swing_low * 0.997)

    # 动态止损线也要跟随入场价：最低不能低于入场价
    # 除非已经有一定盈利，才允许止损高于入场价
    if move_from_entry >= POSITION_GUARD_EARLY_MOVE:
        dynamic_stop = max(dynamic_stop, entry_price * 0.998)
    if move_from_entry >= POSITION_GUARD_BREAKEVEN_MOVE:
        dynamic_stop = max(dynamic_stop, entry_price * 1.001)
    if move_from_entry >= POSITION_GUARD_PROFIT_MOVE:
        dynamic_stop = max(dynamic_stop, entry_price * 1.005)

    # 合并之前的 guard
    structure_guard = max(dynamic_stop, previous_guard, stop_price)

    # 高浮盈仓位要额外保护利润：当峰值利润已经足够大时，
    # guard 至少锁住一部分峰值收益，避免大赚单又吐回原始结构位。
    structure_guard = apply_high_profit_guard(current_price, peak_price, structure_guard)
    peak_unrealized = (peak_price - entry_price) * quantity
    peak_roi = peak_unrealized / estimated_margin if estimated_margin > 0 else 0

    # 检查是否触发动态止损
    guard_armed_for_profit = (
        move_from_entry >= POSITION_GUARD_BREAKEVEN_MOVE
        or peak_unrealized >= POSITION_PROFIT_GUARD_MIN_USD
        or peak_roi >= POSITION_ROI_GUARD_TIER1
    )
    guard_break_confirmed = (
        current_price <= structure_guard
        and structure_guard > stop_price
        and (
            guard_armed_for_profit
            or closes_5m[-1] < ema21_5m[-1]
            or oi_delta_15m <= -0.02
        )
    )
    if guard_break_confirmed:
        return {
            "action": "EXIT",
            "reason": f"wave stop hit: {wave_phase} guard={structure_guard:.6f} current={current_price:.6f}",
            "current_price": current_price, "guard_price": structure_guard,
            "oi_delta": oi_delta_15m, "funding_rate": funding_rate,
        }

    # === 6. 其他离场条件 ===

    # 趋势破位：跌破15m EMA21 + 5m EMA21
    trend_broken = (
        closes_5m[-1] < ema21_5m[-1] * 0.998
        and closes_5m[-1] < ema21_15m[-1] * 0.998
        and closes_5m[-1] < min(post_entry_lows[-5:]) * 1.003
    )

    # OI 空头积聚 + 下跌
    short_buildup = (
        oi_delta_15m >= 0.03
        and oi_delta_30m >= 0.05
        and price_change_15m <= -0.01
        and closes_5m[-1] < ema21_5m[-1]
    )

    # 燃料衰竭：大幅上涨 + 高费率 + OI下降
    fuel_exhaustion = (
        move_from_entry >= 0.08
        and funding_rate > 0.00025
        and oi_delta_15m <= -0.02
    )

    # 利润回撤保护：只有明显拉开利润后，才用回撤比例做离场。
    current_unrealized = (current_price - entry_price) * quantity
    if peak_unrealized > 0 and current_unrealized > 0:
        giveback_ratio = (peak_unrealized - current_unrealized) / peak_unrealized
        if giveback_ratio >= POSITION_PROFIT_GIVEBACK_RATIO and move_from_entry >= POSITION_PROFIT_GIVEBACK_MIN_MOVE:
            return {
                "action": "EXIT",
                "reason": f"profit giveback: peak={peak_unrealized:.0f}u cur={current_unrealized:.0f}u {giveback_ratio*100:.0f}% returned",
                "current_price": current_price, "guard_price": structure_guard,
                "oi_delta": oi_delta_15m, "funding_rate": funding_rate,
            }

    # 深度回撤：从峰值回撤超过20% 且 进入下跌波段
    retrace_from_peak = (peak_price - current_price) / peak_price if peak_price > 0 else 0
    if move_from_entry >= 0.04 and retrace_from_peak >= 0.18 and in_downleg:
        return {
            "action": "EXIT",
            "reason": f"deep retrace: {retrace_from_peak*100:.1f}% in downleg phase, exit",
            "current_price": current_price, "guard_price": structure_guard,
            "oi_delta": oi_delta_15m, "funding_rate": funding_rate,
        }

    # 强下跌波段 + 趋势破位
    if trend_broken and in_downleg and downleg_strength > 0.02:
        return {
            "action": "EXIT",
            "reason": f"trend broken in downleg: {wave_phase} trend broken, exit",
            "current_price": current_price, "guard_price": structure_guard,
            "oi_delta": oi_delta_15m, "funding_rate": funding_rate,
        }

    if short_buildup and trend_broken:
        return {
            "action": "EXIT",
            "reason": f"short build-up + trend break: oi15m={oi_delta_15m*100:.1f}%",
            "current_price": current_price, "guard_price": structure_guard,
            "oi_delta": oi_delta_15m, "funding_rate": funding_rate,
        }

    if fuel_exhaustion:
        return {
            "action": "EXIT",
            "reason": f"fuel exhaustion: move={move_from_entry*100:.1f}% funding={funding_rate*100:.4f}% oi={oi_delta_15m*100:.1f}%",
            "current_price": current_price, "guard_price": structure_guard,
            "oi_delta": oi_delta_15m, "funding_rate": funding_rate,
        }

    # 趋势预警
    if trend_broken and not in_downleg:
        return {
            "action": "WATCH",
            "reason": f"trend under pressure: ema21 broken, monitoring for wave confirmation",
            "current_price": current_price, "guard_price": structure_guard,
            "oi_delta": oi_delta_15m, "funding_rate": funding_rate,
        }

    # 默认持仓
    return {
        "action": "HOLD",
        "reason": (
            f"wave phase: {wave_phase}, move={move_from_entry*100:.1f}%, "
            f"roi={current_roi*100:.0f}% peak_roi={peak_roi*100:.0f}%, guard={structure_guard:.6f}"
        ),
        "current_price": current_price, "guard_price": structure_guard,
        "oi_delta": oi_delta_15m, "funding_rate": funding_rate,
    }


def sync_positions_with_actual(conn):
    """自动同步 position_watchlist 与币安实际持仓。
    - 自动关闭：观察池有但实际已平仓的记录
    - 自动添加：实际持仓有但观察池没有的记录
    """
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")

    # 1. 从币安获取实际持仓
    ok, data = ai_trader_request("GET", "/account/positions")
    if not ok:
        print(f" ⚠️ 同步失败: 无法获取实际持仓 {data}")
        return

    c = conn.cursor()
    c.row_factory = sqlite3.Row

    # 2. 获取观察池中 ACTIVE 的记录
    watch_rows = c.execute("SELECT * FROM position_watchlist WHERE status = 'ACTIVE'").fetchall()
    actual_positions = data.get("data", []) if isinstance(data, dict) else []
    if watch_rows and not actual_positions:
        print(f" ⚠️ 同步跳过: account/positions returned empty while watching {len(watch_rows)} active positions")
        return
    actual_symbols = {p["symbol"]: p for p in actual_positions if float(p.get("amount", 0)) != 0}
    watched_symbols = {dict(row)["symbol"] for row in watch_rows}
    missing_symbols = watched_symbols - set(actual_symbols.keys())
    if missing_symbols:
        time.sleep(1)
        retry_ok, retry_data = ai_trader_request("GET", "/account/positions")
        retry_positions = retry_data.get("data", []) if retry_ok and isinstance(retry_data, dict) else []
        retry_symbols = {p["symbol"]: p for p in retry_positions if float(p.get("amount", 0)) != 0}
        for symbol, pos in retry_symbols.items():
            actual_symbols[symbol] = pos
        still_missing = missing_symbols - set(actual_symbols.keys())
        if still_missing:
            print(f" ⚠️ 持仓缺失二次确认: {', '.join(sorted(still_missing))}")

    closed_count = 0
    added_count = 0
    side_changed_count = 0

    for row in watch_rows:
        row = dict(row)
        symbol = row["symbol"]

        if symbol not in actual_symbols:
            # 观察池有但实际已平仓 → 自动关闭
            c.execute(
                """UPDATE position_watchlist
                SET status = 'CLOSED', closed_at = ?, exit_signal = 'SYNC_CLOSED',
                exit_reason = ? WHERE symbol = ?""",
                (now, f"sync_closed: position not found on exchange (manually closed or expired)", symbol)
            )
            closed_count += 1
            print(f" 🔵 {symbol} 自动关闭 (实际已平仓)")
        else:
            pos = actual_symbols[symbol]
            entry_price = float(pos.get("entryPrice") or row.get("entry_price") or 0)
            mark_price = float(pos.get("markPrice") or row.get("last_price") or row.get("entry_price") or 0)
            amount = abs(float(pos.get("amount") or row.get("quantity") or 0))
            leverage = float(pos.get("leverage") or row.get("leverage") or 1)
            actual_side = pos.get("side") or row.get("side") or "LONG"
            peak_price = max(float(row.get("peak_price") or 0), mark_price, entry_price)
            if actual_side != row.get("side"):
                side_changed_count += 1
                print(f" ⚠️ {symbol} 持仓方向同步: radar={row.get('side')} exchange={actual_side}")
            c.execute(
                """UPDATE position_watchlist
                   SET side = ?, entry_price = ?, last_price = ?, peak_price = ?, quantity = ?, leverage = ?
                   WHERE symbol = ?""",
                (actual_side, entry_price, mark_price, peak_price, amount, leverage, symbol),
            )

    # 3. 检查实际持仓是否在观察池中；若曾被误判 CLOSED，也要重新激活。
    for symbol, pos in actual_symbols.items():
        existing = c.execute("SELECT * FROM position_watchlist WHERE symbol = ?", (symbol,)).fetchone()
        entry_price = float(pos.get("entryPrice", 0))
        quantity = abs(float(pos.get("amount", 0)))
        leverage = int(float(pos.get("leverage", 1)))
        side = pos.get("side", "LONG")
        mark_price = float(pos.get("markPrice") or entry_price)

        if not existing:
            stop_price = entry_price * 0.97 if side == "LONG" else entry_price * 1.03
            c.execute(
                """INSERT OR REPLACE INTO position_watchlist
                (symbol, source, side, entry_price, stop_price, reference_support,
                quantity, leverage, status, opened_at, last_check_time, peak_price,
                last_price, guard_price, exit_signal, exit_reason, orders_canceled)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?, ?, ?, '', '', 0)""",
                (
                    symbol, "binance_sync", side, entry_price, stop_price, stop_price,
                    quantity, leverage, now, now, max(entry_price, mark_price), mark_price, stop_price
                )
            )
            added_count += 1
            print(f" 🟢 {symbol} 自动添加 (从实际持仓同步)")
            continue

        existing = dict(existing)
        if existing.get("status") != "ACTIVE":
            stop_price = float(existing.get("stop_price") or (entry_price * 0.97 if side == "LONG" else entry_price * 1.03))
            guard_price = max(float(existing.get("guard_price") or 0), stop_price)
            peak_price = max(float(existing.get("peak_price") or 0), mark_price, entry_price)
            c.execute(
                """UPDATE position_watchlist
                   SET status = 'ACTIVE',
                       closed_at = NULL,
                       exit_signal = 'HOLD',
                       exit_reason = 'sync_reopened: position exists on exchange',
                       entry_price = ?,
                       quantity = ?,
                       leverage = ?,
                       side = ?,
                       peak_price = ?,
                       last_price = ?,
                       guard_price = ?,
                       last_check_time = ?
                   WHERE symbol = ?""",
                (entry_price, quantity, leverage, side, peak_price, mark_price, guard_price, now, symbol),
            )
            added_count += 1
            print(f" 🟢 {symbol} 重新激活 (实际持仓仍存在)")

    if closed_count > 0 or added_count > 0 or side_changed_count > 0:
        conn.commit()
        print(f" 📊 持仓同步完成: 关闭 {closed_count} 个, 新增 {added_count} 个, 方向修正 {side_changed_count} 个")
    elif not watch_rows and not actual_symbols:
        print(f" 👀 持仓同步: 无观察仓位")

def monitor_open_positions(conn):
    """观察已成交仓位，必要时通过 ai-trader 平仓。"""
    if not POSITION_MONITOR_ENABLED:
        return

    # 自动同步实际持仓（识别手动操作）
    sync_positions_with_actual(conn)

    sync_executed_positions_from_ai_trader(conn)
    c = conn.cursor()
    c.row_factory = sqlite3.Row
    rows = c.execute("SELECT * FROM position_watchlist WHERE status = 'ACTIVE'").fetchall()
    if not rows:
        return

    report_lines = [f"📍 **持仓观察** {datetime.now(timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S')} CST"]
    for row in rows:
        row = dict(row)
        if row.get("side") != "LONG":
            now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
            reason = f"actual side is {row.get('side')}; radar auto-exit monitor only supports LONG, skipped"
            c.execute(
                """UPDATE position_watchlist
                   SET last_check_time = ?, exit_signal = 'SIDE_MISMATCH', exit_reason = ?
                   WHERE symbol = ?""",
                (now, reason, row["symbol"]),
            )
            report_lines.append(f"  ⚠️ {row['symbol']} SKIP | {reason}")
            continue
        if not row.get("orders_canceled"):
            ai_trader_request("DELETE", "/trading/orders", {"symbol": row["symbol"]})
            c.execute("UPDATE position_watchlist SET orders_canceled = 1 WHERE symbol = ?", (row["symbol"],))
            conn.commit()

        analysis = analyze_open_position(row)
        now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
        peak_price = max(row.get("peak_price") or 0, analysis.get("current_price", row["entry_price"]))
        old_guard = float(row.get("guard_price") or row.get("stop_price") or 0)
        new_guard = float(analysis.get("guard_price", row.get("guard_price", row["stop_price"])) or 0)
        if should_alert_guard_change(row, old_guard, new_guard):
            send_private_guard_update_alert(row, old_guard, new_guard, analysis, peak_price)
        c.execute(
            """UPDATE position_watchlist
               SET last_check_time = ?, peak_price = ?, last_price = ?, guard_price = ?, last_oi_delta_pct = ?,
                   last_funding_rate = ?, exit_signal = ?, exit_reason = ?
               WHERE symbol = ?""",
            (
                now,
                peak_price,
                analysis.get("current_price", row["entry_price"]),
                new_guard,
                analysis.get("oi_delta", 0) * 100,
                analysis.get("funding_rate", 0),
                analysis["action"],
                analysis["reason"],
                row["symbol"],
            ),
        )

        if analysis["action"] == "EXIT":
            ok, payload = ai_trader_request("POST", "/trading/close-position", {
                "symbol": row["symbol"],
                "side": row["side"],
            })
            if ok:
                c.execute(
                    "UPDATE position_watchlist SET status = 'CLOSED', closed_at = ?, exit_signal = 'EXIT', exit_reason = ? WHERE symbol = ?",
                    (now, analysis["reason"], row["symbol"]),
                )
                send_private_position_close_alert(row, analysis)
                report_lines.append(f"  🔴 {row['symbol']} EXIT | {analysis['reason']}")
            else:
                report_lines.append(f"  ⚠️ {row['symbol']} close failed | {analysis['reason']} | {payload}")
        elif analysis["action"] == "WATCH":
            report_lines.append(f"  🟡 {row['symbol']} WATCH | {analysis['reason']}")
        else:
            report_lines.append(f"  🟢 {row['symbol']} HOLD  | {analysis['reason']}")

    conn.commit()
    if len(report_lines) > 1:
        print("\n".join(report_lines))


def expire_stale_ready_signals(conn):
    """过期已经离开观察窗口的 READY 信号，避免旧入场点长期挂在面板上。"""
    c = conn.cursor()
    now = datetime.now(timezone(timedelta(hours=8)))
    cutoff = (now - timedelta(hours=OBSERVE_LOOKBACK_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
    now_text = now.strftime("%Y-%m-%d %H:%M:%S")
    stale_rows = c.execute(
        """
        SELECT symbol, candidate_time, last_analysis_time
        FROM entry_watchlist
        WHERE watch_status = 'READY'
          AND datetime(candidate_time) < datetime(?)
        """,
        (cutoff,),
    ).fetchall()
    if not stale_rows:
        return 0

    c.executemany(
        """
        UPDATE entry_watchlist
        SET watch_status = 'INVALIDATED',
            setup_type = 'expired-ready',
            last_analysis_time = ?,
            notes = ?
        WHERE symbol = ?
          AND watch_status = 'READY'
        """,
        [
            (
                now_text,
                f"expired: READY signal left {OBSERVE_LOOKBACK_HOURS}h observe window "
                f"(candidate_time={row[1]}, last_analysis_time={row[2]})",
                row[0],
            )
            for row in stale_rows
        ],
    )
    conn.commit()
    symbols = ", ".join(row[0] for row in stale_rows[:12])
    extra = "" if len(stale_rows) <= 12 else f" +{len(stale_rows) - 12}"
    print(f"  🧹 已过期 {len(stale_rows)} 个旧 READY 信号: {symbols}{extra}")
    return len(stale_rows)


def run_entry_observer(conn, limit=OBSERVE_MAX_CANDIDATES):
    """对观察池做 5m/15m 实时跟踪，只在更好的点位触发最终 BUY。"""
    expire_stale_ready_signals(conn)
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

    # 观察池细节不再推群，只保留 oi 主报告和私聊开仓提醒。
    if len(report_lines) > 1:
        print("\n".join(report_lines[:15]))

    if ready_signals:
        print(f"  ✅ 观察器触发 {len(ready_signals)} 个最终入场信号")
        accepted_results = emit_trade_signals(ready_signals[:AI_TRADER_SIGNAL_LIMIT])
        for item in accepted_results:
            target_label = (item.get("target") or {}).get("label", ENV_LABEL)
            sym = item["symbol"]
            c.execute(
                "UPDATE entry_watchlist SET watch_status = 'TRIGGERED', trigger_time = ? WHERE symbol = ?",
                (now, sym),
            )
            track_target = (AI_TRADER_TRACK_TARGET or ENV_LABEL).upper()
            if target_label.upper() == track_target:
                track_executed_observer_position(conn, item["signal"], item.get("response", {}))
            else:
                send_private_position_open_alert(item["signal"], item.get("response", {}))
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
    return now


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


def fetch_daily_profile(symbol, limit=60):
    """给新池补充日线历史长度，不影响原始收筹池规则。"""
    klines = api_get("/fapi/v1/klines", {"symbol": symbol, "interval": "1d", "limit": limit})
    if not klines or not isinstance(klines, list):
        return {
            "data_days": 0,
            "range_pct": 0,
            "avg_vol_7d": 0,
        }
    lows = [float(k[3]) for k in klines if float(k[3]) > 0]
    highs = [float(k[2]) for k in klines]
    vols = [float(k[7]) for k in klines]
    low = min(lows) if lows else 0
    high = max(highs) if highs else 0
    range_pct = ((high - low) / low * 100) if low > 0 else 0
    recent_vols = vols[-7:] if len(vols) >= 7 else vols
    return {
        "data_days": len(klines),
        "range_pct": range_pct,
        "avg_vol_7d": sum(recent_vols) / len(recent_vols) if recent_vols else 0,
    }


def build_extended_pools(coin_data):
    """构建两个旁路池：
    1. momentum_pool: 去掉横盘限制，覆盖次新/正常/偏老币的 OI+成交异动。
    2. new_listing_pool: 短历史/新币池，专门覆盖上市时间短但 OI/成交剧烈的币。
    """
    candidate_rows = []
    for d in coin_data.values():
        if d.get("vol", 0) < 3_000_000:
            continue
        if d.get("oi_usd", 0) < 1_000_000:
            continue
        if max(abs(d.get("d1h", 0)), abs(d.get("d6h", 0))) < 2 and abs(d.get("px_chg", 0)) < 10:
            continue
        candidate_rows.append(d)

    # 优先给真正有异动/成交的币补日线历史，避免全量日线扫描拖慢主流程。
    candidate_rows.sort(
        key=lambda d: (
            max(abs(d.get("d1h", 0)), abs(d.get("d6h", 0))),
            d.get("vol", 0),
            abs(d.get("px_chg", 0)),
        ),
        reverse=True,
    )

    momentum_pool = []
    new_listing_pool = []

    for d in candidate_rows[:160]:
        profile = fetch_daily_profile(d["sym"], 60)
        enriched = {**d, **profile}
        data_days = enriched.get("data_days", 0)
        abs_oi = max(abs(enriched.get("d1h", 0)), abs(enriched.get("d6h", 0)))
        vol = enriched.get("vol", 0)
        px_chg = enriched.get("px_chg", 0)
        fr_pct = enriched.get("fr_pct", 0)
        est_mcap = enriched.get("est_mcap", 0)

        oi_score = min(abs_oi / 10, 1.0) * 35
        vol_score = min(vol / 100_000_000, 1.0) * 20
        mcap_score = 20 if 0 < est_mcap < 100e6 else 15 if est_mcap < 300e6 else 8 if est_mcap < 1e9 else 2
        funding_score = min(abs(fr_pct) / 0.15, 1.0) * 15
        price_score = 10 if -8 <= px_chg <= 25 else 7 if -20 <= px_chg <= 45 else 3

        # 第二池：不要求横盘，但要求不是“极短历史的新币”，避免和第三池混在一起。
        if data_days >= 30 and abs_oi >= 3 and vol >= 5_000_000 and -35 <= px_chg <= 80:
            total = oi_score + vol_score + mcap_score + funding_score + price_score
            momentum_pool.append({
                **enriched,
                "pool_name": "momentum_pool",
                "total": round(total, 2),
                "reason": (
                    f"momentum score={total:.0f} oi1h={enriched['d1h']:+.1f}% "
                    f"oi6h={enriched['d6h']:+.1f}% px24h={px_chg:+.1f}% days={data_days}"
                ),
            })

        # 第三池：短历史/新币，不要求横盘，但门槛更严，防止刚上市噪音太多。
        if data_days < 50 and vol >= 10_000_000 and abs_oi >= 4 and -45 <= px_chg <= 120:
            new_bonus = max(0, (50 - data_days) / 50) * 15
            total = oi_score + vol_score + funding_score + price_score + new_bonus
            new_listing_pool.append({
                **enriched,
                "pool_name": "new_listing_pool",
                "total": round(total, 2),
                "reason": (
                    f"new-listing score={total:.0f} days={data_days} "
                    f"oi1h={enriched['d1h']:+.1f}% oi6h={enriched['d6h']:+.1f}% "
                    f"px24h={px_chg:+.1f}%"
                ),
            })

        time.sleep(0.05)

    momentum_pool.sort(key=lambda x: x["total"], reverse=True)
    new_listing_pool.sort(key=lambda x: x["total"], reverse=True)
    return momentum_pool[:MOMENTUM_POOL_LIMIT], new_listing_pool[:NEW_LISTING_POOL_LIMIT]


def save_candidate_pools(conn, snapshot_time, pools):
    """保存旁路候选池，便于后续复盘，不覆盖原 watchlist。"""
    rows = []
    for pool_name, items in pools.items():
        for item in items:
            rows.append((
                pool_name,
                item["sym"],
                snapshot_time,
                item.get("total", 0),
                item.get("reason", ""),
                item.get("price", 0),
                item.get("px_chg", 0),
                item.get("vol", 0),
                item.get("d1h", 0),
                item.get("d6h", 0),
                item.get("fr_pct", 0) / 100,
                item.get("est_mcap", 0),
                item.get("data_days", 0),
            ))
    if not rows:
        return
    conn.executemany(
        """INSERT OR REPLACE INTO candidate_pools
           (pool_name, symbol, snapshot_time, score, source_reason, price,
            px_chg_pct, vol_24h, oi_d1h_pct, oi_d6h_pct, funding_rate,
            est_mcap, data_days)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        rows,
    )
    conn.commit()
    print(f"  💾 保存旁路候选池 {len(rows)} 条")


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
        
        # 3. 扫OI：默认覆盖主网全部USDT永续，避免漏掉新上线/热门但未进旧池子的币。
        scan_all_perps = os.getenv("OI_SCAN_ALL_PERP_SYMBOLS", "true").lower() == "true"
        if scan_all_perps:
            scan_syms = {sym for sym in get_all_perp_symbols() if sym in ticker_map}
            print(f"✅ OI扫描范围: 主网USDT永续全量 {len(scan_syms)} 个")
        else:
            scan_syms = set()
            for sym, pd in pool_map.items():
                if "放量" in pd.get("status", "") or "开始" in pd.get("status", ""):
                    scan_syms.add(sym)
            top_by_vol = sorted(ticker_map.items(), key=lambda x: x[1]["vol"], reverse=True)[:100]
            for sym, _ in top_by_vol:
                scan_syms.add(sym)
            print(f"✅ OI扫描范围: 收筹池+成交额Top100 {len(scan_syms)} 个")
        
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

        current_snapshot_time = save_market_snapshots(conn, coin_data, pool_map, mode)

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
        momentum_pool, new_listing_pool = build_extended_pools(coin_data)
        save_candidate_pools(conn, current_snapshot_time, {
            "momentum_pool": momentum_pool,
            "new_listing_pool": new_listing_pool,
        })

        recent_radar_symbols = load_recent_report_symbols_with_fallback(
            conn,
            "main_radar",
            limit=3,
            exclude_snapshot_time=current_snapshot_time,
        )
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

        lines.append(f"\n⚡ **动量池** (无横盘限制：OI+成交+资金)")
        if momentum_pool:
            for s in momentum_pool[:8]:
                lines.append(
                    f"  {radar_name(s):<7} {s['total']:.0f}分 | "
                    f"OI{s['d6h']:+.1f}% | 24h{s['px_chg']:+.1f}% | "
                    f"Vol {format_usd(s['vol'])} | {s.get('data_days', 0)}天 | "
                    f"现价：{format_price(s['price'])}"
                )
        else:
            lines.append("  暂无（需OI/成交额/价格异动同时达标）")

        lines.append(f"\n🆕 **新币池** (短历史：新上线/短K线)")
        if new_listing_pool:
            for s in new_listing_pool[:8]:
                lines.append(
                    f"  {radar_name(s):<7} {s['total']:.0f}分 | "
                    f"{s.get('data_days', 0)}天 | OI{s['d6h']:+.1f}% | "
                    f"24h{s['px_chg']:+.1f}% | Vol {format_usd(s['vol'])} | "
                    f"现价：{format_price(s['price'])}"
                )
        else:
            lines.append("  暂无（需短历史+强OI/成交异动）")
        
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
        lines.append("  🎯暗流=OI动但价没动(收筹信号) | ⚡动量池=不要求横盘 | 🆕新币池=短历史")
        
        report = "\n".join(lines)
        if send_telegram(report):
            save_report_symbols(conn, "main_radar", current_radar_symbols)

        trade_signals = build_trade_signals(chase, combined, ambush, momentum_pool, new_listing_pool)
        if RADAR_OBSERVE_BEFORE_ENTRY:
            queue_entry_watch_candidates(conn, trade_signals, coin_data)
            run_entry_observer(conn)
        else:
            emit_trade_signals(trade_signals)
        monitor_open_positions(conn)

    if mode == "observe":
        run_entry_observer(conn)
        monitor_open_positions(conn)

    if mode == "manage":
        monitor_open_positions(conn)

    if mode == "seed-observe":
        seeded = seed_watchlist_for_observer(conn, limit=None, include_sleeping=True)
        run_entry_observer(conn, limit=seeded if seeded else OBSERVE_MAX_CANDIDATES)
        monitor_open_positions(conn)

    if mode == "trend":
        plans = scan_trend_breakout_plans()
        if plans:
            save_trend_breakout_plans(conn, plans)
            report = build_trend_breakout_report(plans)
            if report:
                send_telegram(report)

    if mode in ("select", "leader"):
        selected, top_gainers = scan_leader_ath_trend_pool()
        snapshot_time = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
        if selected:
            save_candidate_pools(conn, snapshot_time, {
                "leader_ath_trend_pool": selected,
            })
        report = build_leader_selection_report(selected, top_gainers)
        if report and send_telegram(report):
            save_report_symbols(conn, "leader_ath_trend_pool", {item["coin"] for item in selected})

    conn.close()
    print("\n✅ 完成")


if __name__ == "__main__":
    main()
