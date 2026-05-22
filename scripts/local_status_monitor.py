#!/usr/bin/env python3
import json
import os
import fcntl
import re
import sqlite3
import subprocess
import shlex
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests


BASE_DIR = Path("/Users/sabrina0x/accumulation-radar")
ENV_PATH = Path(os.getenv("RADAR_ENV_FILE", str(BASE_DIR / ".env.oi")))
STATE_PATH = Path(os.getenv("MONITOR_STATE_PATH", str(BASE_DIR / ".monitor_state.json")))
AI_SESSION_PATH = Path(os.getenv("AI_ANALYSIS_SESSION_PATH", str(BASE_DIR / ".ai_analysis_sessions.json")))
TG_COMMAND_DEDUP_PATH = Path(os.getenv("TG_COMMAND_DEDUP_PATH", str(BASE_DIR / ".tg_command_seen.json")))
EXECUTED_EVENT_DEDUP_PATH = Path(os.getenv("EXECUTED_EVENT_DEDUP_PATH", str(BASE_DIR / ".executed_event_seen.json")))
LOG_PATH = Path(os.getenv("MONITOR_LOG_PATH", str(BASE_DIR / "monitor_status.log")))
ACCUM_DB = Path(os.getenv("ACCUMULATION_DB_PATH", str(BASE_DIR / "accumulation.db")))
AI_TRADER_DB = Path(os.getenv("AI_TRADER_DB_PATH", "/Users/sabrina0x/ai project/ai-trader/backend/data/database/ai-trader.db"))
RUN_TASK_SCRIPT = Path(os.getenv("RUN_RADAR_TASK_SCRIPT", str(BASE_DIR / "scripts" / "run_radar_task.sh")))
MANAGE_MONITOR_SCRIPT = BASE_DIR / "scripts" / "manage_local_monitor.sh"
AI_TRADER_SERVICE_SCRIPT = Path(os.getenv("AI_TRADER_SERVICE_SCRIPT", str(BASE_DIR / "scripts" / "manage_ai_trader_demo.sh")))
TZ = timezone(timedelta(hours=8))


def env_label(env):
    label = (env.get("RADAR_ENV_LABEL") or "").strip()
    if label:
        return label
    prefix = (env.get("TG_COMMAND_PREFIX") or "").strip().lower()
    return "LIVE" if prefix == "/live" or "live" in str(ENV_PATH).lower() else "DEMO"


def http_no_env():
    session = requests.Session()
    session.trust_env = False
    return session


def load_env(path: Path):
    env = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env


def load_state():
    if not STATE_PATH.exists():
        return {
            "ready": {},
            "ready_notified": {},
            "executed_seen": {},
            "position_seen": {},
            "error_seen": [],
            "failed_seen": {},
            "failed_cooldown": {},
            "health_status": "",
            "tg_update_offset": 0,
            "last_manage_trigger_ts": 0,
        }
    try:
        return json.loads(STATE_PATH.read_text())
    except Exception:
        return {
            "ready": {},
            "ready_notified": {},
            "executed_seen": {},
            "position_seen": {},
            "error_seen": [],
            "failed_seen": {},
            "failed_cooldown": {},
            "health_status": "",
            "tg_update_offset": 0,
            "last_manage_trigger_ts": 0,
        }


def save_state(state):
    tmp_path = STATE_PATH.with_suffix(f"{STATE_PATH.suffix}.{os.getpid()}.tmp")
    tmp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2))
    tmp_path.replace(STATE_PATH)


def load_ai_sessions():
    if not AI_SESSION_PATH.exists():
        return {}
    try:
        return json.loads(AI_SESSION_PATH.read_text())
    except Exception:
        return {}


def save_ai_sessions(sessions):
    tmp_path = AI_SESSION_PATH.with_suffix(f"{AI_SESSION_PATH.suffix}.{os.getpid()}.tmp")
    tmp_path.write_text(json.dumps(sessions, ensure_ascii=False, indent=2))
    tmp_path.replace(AI_SESSION_PATH)


def claim_tg_update(update_id: int):
    TG_COMMAND_DEDUP_PATH.parent.mkdir(parents=True, exist_ok=True)
    with TG_COMMAND_DEDUP_PATH.open("a+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.seek(0)
            raw = f.read().strip()
            state = json.loads(raw) if raw else {"seen": {}}
        except Exception:
            state = {"seen": {}}

        seen = state.get("seen") or {}
        key = str(update_id)
        now_ts = int(datetime.now(timezone.utc).timestamp())
        cutoff = now_ts - 86400
        seen = {k: v for k, v in seen.items() if int(v or 0) >= cutoff}
        if key in seen:
            return False

        seen[key] = now_ts
        state["seen"] = dict(sorted(seen.items(), key=lambda item: int(item[0]))[-500:])
        f.seek(0)
        f.truncate()
        f.write(json.dumps(state, ensure_ascii=False))
        f.flush()
        os.fsync(f.fileno())
        return True


def claim_seen_key(path: Path, key: str, ttl_sec: int = 86400, keep: int = 2000):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.seek(0)
            raw = f.read().strip()
            state = json.loads(raw) if raw else {"seen": {}}
        except Exception:
            state = {"seen": {}}

        seen = state.get("seen") or {}
        now_ts = int(datetime.now(timezone.utc).timestamp())
        cutoff = now_ts - ttl_sec
        seen = {k: v for k, v in seen.items() if int(v or 0) >= cutoff}
        if key in seen:
            return False

        seen[key] = now_ts
        state["seen"] = dict(sorted(seen.items(), key=lambda item: int(item[1]))[-keep:])
        f.seek(0)
        f.truncate()
        f.write(json.dumps(state, ensure_ascii=False))
        f.flush()
        os.fsync(f.fileno())
        return True


def now_cst():
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def log_line(message: str):
    with LOG_PATH.open("a") as f:
        f.write(f"[{now_cst()}] {message}\n")


def notify_desktop(title: str, message: str):
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{message}" with title "{title}"'],
            check=False,
            capture_output=True,
            text=True,
        )
    except Exception:
        pass


def notify_telegram(env, message: str):
    token = env.get("TG_BOT_TOKEN")
    chat_id = env.get("TG_OBSERVER_CHAT_ID") or env.get("TG_CHAT_ID")
    if not token or not chat_id:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": message},
            timeout=10,
        )
    except Exception:
        pass


def should_emit_private(title: str) -> bool:
    allowed_prefixes = (
        "新成交",
        "新仓位",
        "持仓状态变化",
        "下单失败",
        "下单未成交",
        "执行端状态异常",
        "关键异常",
    )
    normalized = title.replace("LIVE ", "").replace("DEMO ", "").strip()
    return normalized.startswith(allowed_prefixes)


def telegram_api(env, method: str, payload: dict | None = None, timeout: int = 15):
    token = env.get("TG_BOT_TOKEN")
    if not token:
        return None
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/{method}",
            json=payload or {},
            timeout=timeout,
        )
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        return None
    return None


def emit(env, title: str, message: str):
    label = env_label(env)
    titled = f"{label} {title}" if label and not title.startswith(label) else title
    full = f"{titled}: {message}"
    log_line(full)
    notify_desktop(titled, message)
    if should_emit_private(titled):
        notify_telegram(env, full)


def query_rows(db_path: Path, sql: str):
    if not db_path.exists():
        return []
    conn = None
    try:
        conn = sqlite3.connect(str(db_path), timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error as e:
        log_line(f"sqlite read skipped: {db_path.name} {e}")
        return []
    finally:
        if conn:
            conn.close()


def query_rows_params(db_path: Path, sql: str, params: tuple = ()):
    if not db_path.exists():
        return []
    conn = None
    try:
        conn = sqlite3.connect(str(db_path), timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error as e:
        log_line(f"sqlite read skipped: {db_path.name} {e}")
        return []
    finally:
        if conn:
            conn.close()


def run_local_command(args: list[str], timeout: int = 120):
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
        output = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        text = output if output else err
        return proc.returncode, text[:3000]
    except Exception as e:
        return 1, str(e)


def ai_trader_post(path: str, payload: dict):
    base = load_env(ENV_PATH).get("AI_TRADER_API_BASE", "http://127.0.0.1:3333/api").rstrip("/")
    try:
        resp = http_no_env().post(
            f"{base}{path}",
            json=payload,
            timeout=15,
        )
        body = {}
        try:
            body = resp.json()
        except Exception:
            body = {"raw": resp.text[:800]}
        return resp.status_code, body
    except Exception as e:
        return 0, {"error": str(e)}


def ai_trader_get(env, path: str, timeout: int = 10):
    base = env.get("AI_TRADER_API_BASE", "http://127.0.0.1:3333/api").rstrip("/")
    try:
        resp = http_no_env().get(
            f"{base}{path}",
            timeout=timeout,
        )
        body = {}
        try:
            body = resp.json()
        except Exception:
            body = {"raw": resp.text[:800]}
        return resp.status_code, body
    except Exception as e:
        return 0, {"error": str(e)}


def normalize_symbol(raw: str):
    symbol = (raw or "").strip().upper().replace("/", "").replace("-", "")
    if not symbol:
        return ""
    if not symbol.endswith("USDT"):
        symbol = f"{symbol}USDT"
    return symbol


def looks_like_symbol(raw: str):
    text = (raw or "").strip().upper().replace("/", "").replace("-", "")
    if not text:
        return False
    if text.endswith("USDT"):
        base = text[:-4]
    else:
        base = text
    return bool(re.fullmatch(r"[A-Z0-9]{2,15}", base))


def fmt_price(value, digits: int = 8):
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "-"
    if abs(num) >= 100:
        return f"{num:.2f}"
    if abs(num) >= 1:
        return f"{num:.4f}"
    return f"{num:.{digits}f}".rstrip("0").rstrip(".")


def fmt_usd(value):
    try:
        return f"{float(value):+.2f}U"
    except (TypeError, ValueError):
        return "-"


def compact_value(value):
    if isinstance(value, float):
        return round(value, 8)
    if isinstance(value, str) and len(value) > 600:
        return value[:600]
    return value


def compact_row(row, keys):
    return {key: compact_value(row.get(key)) for key in keys if key in row}


def load_exchange_position(env, symbol: str):
    code, body = ai_trader_get(env, "/account/positions", timeout=12)
    if code != 200 or not body.get("success"):
        return None, f"position api http={code}"
    for row in body.get("data") or []:
        if str(row.get("symbol", "")).upper() == symbol:
            return row, ""
    return None, ""


def fetch_public_klines(symbol: str, interval: str, limit: int = 120):
    try:
        resp = http_no_env().get(
            "https://fapi.binance.com/fapi/v1/klines",
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=12,
        )
        if resp.status_code != 200:
            return []
        rows = resp.json()
        if not isinstance(rows, list):
            return []
        parsed = []
        for row in rows:
            parsed.append(
                {
                    "open_time": int(row[0]),
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": float(row[5]),
                    "quote_volume": float(row[7]),
                }
            )
        return parsed
    except Exception:
        return []


def ema(values, period):
    if len(values) < period:
        return None
    alpha = 2 / (period + 1)
    result = sum(values[:period]) / period
    for value in values[period:]:
        result = value * alpha + result * (1 - alpha)
    return result


def summarize_kline_structure(symbol: str):
    summaries = {}
    for interval, limit in (("5m", 120), ("15m", 120), ("1h", 120)):
        klines = fetch_public_klines(symbol, interval, limit)
        if len(klines) < 20:
            summaries[interval] = {"status": "insufficient", "count": len(klines)}
            continue
        closes = [row["close"] for row in klines]
        highs = [row["high"] for row in klines]
        lows = [row["low"] for row in klines]
        volumes = [row["quote_volume"] for row in klines]
        current = closes[-1]
        recent_low = min(lows[-12:])
        recent_high = max(highs[-12:])
        swing_low = min(lows)
        swing_high = max(highs)
        ema20 = ema(closes, 20)
        ema60 = ema(closes, 60)
        avg_volume = sum(volumes[-30:-1]) / max(len(volumes[-30:-1]), 1)
        last_volume_ratio = volumes[-1] / avg_volume if avg_volume > 0 else 0
        up_candles = sum(1 for row in klines[-8:] if row["close"] > row["open"])
        down_candles = sum(1 for row in klines[-8:] if row["close"] < row["open"])
        change_12 = (current / closes[-13] - 1) * 100 if len(closes) >= 13 and closes[-13] > 0 else 0
        change_36 = (current / closes[-37] - 1) * 100 if len(closes) >= 37 and closes[-37] > 0 else 0
        drawdown_from_swing_high = (current / swing_high - 1) * 100 if swing_high > 0 else 0
        lift_from_swing_low = (current / swing_low - 1) * 100 if swing_low > 0 else 0
        support_gap = (current / recent_low - 1) * 100 if recent_low > 0 else 0
        resistance_gap = (recent_high / current - 1) * 100 if current > 0 else 0
        trend = "unknown"
        if ema20 and ema60:
            if current > ema20 > ema60:
                trend = "bullish"
            elif current < ema20 < ema60:
                trend = "bearish"
            else:
                trend = "mixed"
        summaries[interval] = {
            "status": "ok",
            "current": round(current, 10),
            "trend": trend,
            "ema20": round(ema20, 10) if ema20 else None,
            "ema60": round(ema60, 10) if ema60 else None,
            "recent_support_low": round(recent_low, 10),
            "recent_resistance_high": round(recent_high, 10),
            "support_gap_pct": round(support_gap, 3),
            "resistance_gap_pct": round(resistance_gap, 3),
            "change_12_bars_pct": round(change_12, 3),
            "change_36_bars_pct": round(change_36, 3),
            "lift_from_window_low_pct": round(lift_from_swing_low, 3),
            "drawdown_from_window_high_pct": round(drawdown_from_swing_high, 3),
            "last_quote_volume_ratio": round(last_volume_ratio, 3),
            "last_8_up_candles": up_candles,
            "last_8_down_candles": down_candles,
        }
    return summaries


def load_cc_switch_provider(provider_hint: str):
    db_path = Path(os.getenv("CC_SWITCH_DB_PATH", "/Users/sabrina0x/.cc-switch/cc-switch.db"))
    if not db_path.exists() or not provider_hint:
        return {}
    rows = query_rows_params(
        db_path,
        """
        SELECT id, name, settings_config
        FROM providers
        WHERE lower(id) = lower(?) OR lower(name) = lower(?)
        ORDER BY is_current DESC
        LIMIT 1
        """,
        (provider_hint, provider_hint),
    )
    if not rows:
        return {}
    try:
        config = json.loads(rows[0].get("settings_config") or "{}")
    except Exception:
        return {}

    env_config = config.get("env") or {}
    models = config.get("models") or []
    first_model = models[0].get("id") if models and isinstance(models[0], dict) else ""
    return {
        "api": config.get("api") or "openai-completions",
        "base_url": config.get("baseUrl") or env_config.get("OPENAI_BASE_URL") or env_config.get("ANTHROPIC_BASE_URL"),
        "api_key": config.get("apiKey") or env_config.get("OPENAI_API_KEY") or env_config.get("ANTHROPIC_AUTH_TOKEN"),
        "model": first_model
        or env_config.get("OPENAI_MODEL")
        or env_config.get("ANTHROPIC_MODEL")
        or env_config.get("ANTHROPIC_DEFAULT_SONNET_MODEL"),
        "provider": rows[0].get("name") or rows[0].get("id"),
    }


def resolve_ai_analysis_config(env):
    provider_hint = env.get("AI_ANALYSIS_CC_PROVIDER", "nvidia2")
    cc_config = load_cc_switch_provider(provider_hint)
    return {
        "enabled": (env.get("AI_ANALYSIS_ENABLED", "true").lower() == "true"),
        "provider": env.get("AI_ANALYSIS_PROVIDER") or cc_config.get("provider") or provider_hint,
        "api": env.get("AI_ANALYSIS_API") or cc_config.get("api") or "openai-completions",
        "base_url": env.get("AI_ANALYSIS_BASE_URL") or cc_config.get("base_url") or "",
        "api_key": env.get("AI_ANALYSIS_API_KEY") or cc_config.get("api_key") or "",
        "model": env.get("AI_ANALYSIS_MODEL") or cc_config.get("model") or "minimaxai/minimax-m2.7",
        "timeout": int(env.get("AI_ANALYSIS_TIMEOUT_SEC", "45") or 45),
        "max_tokens": int(env.get("AI_ANALYSIS_MAX_TOKENS", "900") or 900),
        "temperature": float(env.get("AI_ANALYSIS_TEMPERATURE", "0.25") or 0.25),
        "session_chars": int(env.get("AI_ANALYSIS_SESSION_MAX_CHARS", "16000") or 16000),
    }


def call_openai_compatible_chat(config, messages):
    base_url = (config.get("base_url") or "").rstrip("/")
    api_key = config.get("api_key") or ""
    if not base_url or not api_key:
        return ""
    url = f"{base_url}/chat/completions"
    resp = http_no_env().post(
        url,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": config.get("model"),
            "messages": messages,
            "temperature": config.get("temperature", 0.25),
            "max_tokens": config.get("max_tokens", 900),
        },
        timeout=config.get("timeout", 45),
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"AI http={resp.status_code} {resp.text[:300]}")
    body = resp.json()
    return ((body.get("choices") or [{}])[0].get("message") or {}).get("content", "").strip()


def looks_complete_response(content: str):
    text = (content or "").strip()
    if not text:
        return False
    return text.endswith(("。", "！", "？", ".", "!", "?", "）", ")", "】", "]"))


def ai_system_prompt():
    return (
        "你是 accumulation-radar + ai-trader 的只读策略分析层，不是独立交易机器人。"
        "你必须基于系统策略上下文和输入数据分析，不能编造行情，不能声称已经下单，不能绕过风控。"
        "系统目标：从 Binance U本位合约全量池里识别异动/高控/潜在爆拉山寨币，用小试错成本博高收益。"
        "雷达侧负责发现：OI异动、费率、24h涨跌、成交量、市值、横盘/蓄势、新币/动量池、观察器支撑/突破。"
        "执行侧 ai-trader 只负责按雷达信号执行，策略和风控由 accumulation-radar 管。"
        "入场原则：雷达先给候选，观察器再看K线趋势、支撑、回调/突破，尽量早进但避免追在过度拉升末端。"
        "风险原则：demo 单仓风险通常100U，live等比例10U；亏损接近一半风险且K线/动能走坏时要提前离场，"
        "不要机械等满额止损。盈利后不固定2R止盈，而是看结构、动态guard、峰值回撤、ROI、OI/费率变化。"
        "持仓管理：动态保护线/guard 上移后要优先保护收益；若当前价跌破guard，判断为EXIT/等待重入。"
        "高收益仓：ROI达到50%/100%后要更关注峰值回撤、下跌动能、空头OI建立、散户空头清算后的真实转弱。"
        "输出要像熟悉这套系统的交易助理：明确当前处于 候选/观察/已入场/保护收益/离场后等待 哪个阶段，"
        "并给出关键价位、触发条件、反证条件。必须区分“系统已有数据”和“你的推断”。"
    )


def build_strategy_context():
    return {
        "radar_pools": {
            "accumulation_pool": "原始蓄势池：偏老币/横盘蓄势/放量启动，可能漏掉次新和短历史币。",
            "momentum_pool": "动量池：去掉老币横盘限制，关注OI/成交量/价格/费率组合，覆盖次新、正常币和偏老币。",
            "new_listing_pool": "新币池：短历史币，重点看上市后OI、价格结构、资金费率和是否快速走弱。",
        },
        "entry_logic": [
            "雷达事件后不立即无脑买，先进入观察池。",
            "观察器看 5m/15m 趋势、短线支撑、阻力、回调重新走强或突破确认。",
            "如果已经涨起来，要结合 OI、费率、拉升幅度、相对底部涨幅判断是否还可参与。",
            "越早越好，但止损要放在合理支撑下方，避免大涨前被普通波动洗掉。",
        ],
        "position_management": [
            "单仓按风险额计算数量，不是固定保证金。",
            "浮亏接近风险额50%且K线走坏时，要考虑提前离场。",
            "盈利后使用动态guard和结构回撤保护利润，不用固定2R止盈。",
            "ROI达到50%或100%以上，要收紧保护，防止利润大幅回吐。",
        ],
        "ai_role": "AI只能辅助解释和建议观察/触发条件，不直接下单、不直接平仓。",
    }


def build_symbol_context(env, symbol: str, template_analysis: str):
    position, entry, pools, snapshot = latest_symbol_rows(symbol)
    exchange_pos, pos_err = load_exchange_position(env, symbol)
    position_history = query_rows_params(
        ACCUM_DB,
        """
        SELECT symbol, status, entry_price, stop_price, quantity, leverage, opened_at,
               last_check_time, peak_price, last_price, guard_price, exit_signal, exit_reason, closed_at
        FROM position_watchlist
        WHERE symbol = ?
        ORDER BY rowid DESC
        LIMIT 5
        """,
        (symbol,),
    )
    entry_history = query_rows_params(
        ACCUM_DB,
        """
        SELECT symbol, candidate_time, source, source_reason, radar_score, strength, confidence,
               reference_price, snapshot_price, snapshot_oi_d1h_pct, snapshot_oi_d6h_pct,
               snapshot_funding_rate, watch_status, last_analysis_time, trend, setup_type,
               support_price, resistance_price, suggested_entry, suggested_stop, trigger_time, notes
        FROM entry_watchlist
        WHERE symbol = ?
        ORDER BY coalesce(last_analysis_time, candidate_time) DESC
        LIMIT 5
        """,
        (symbol,),
    )
    snapshots = query_rows_params(
        ACCUM_DB,
        """
        SELECT snapshot_time, mode, symbol, price, px_chg_pct, vol_24h, funding_rate,
               oi_usd, oi_d1h_pct, oi_d6h_pct, est_mcap, in_watchlist, watchlist_status,
               radar_score, sideways_days
        FROM market_snapshots
        WHERE symbol = ?
        ORDER BY snapshot_time DESC
        LIMIT 10
        """,
        (symbol,),
    )
    pool_rows = query_rows_params(
        ACCUM_DB,
        """
        SELECT pool_name, snapshot_time, score, source_reason, price, px_chg_pct, vol_24h,
               oi_d1h_pct, oi_d6h_pct, funding_rate, est_mcap, data_days, status
        FROM candidate_pools
        WHERE symbol = ?
        ORDER BY snapshot_time DESC, score DESC
        LIMIT 8
        """,
        (symbol,),
    )

    def compact_rows(rows, keys):
        return [compact_row(row, keys) for row in rows]

    return {
        "symbol": symbol,
        "env": env_label(env),
        "template_analysis": template_analysis,
        "exchange_position": exchange_pos or {},
        "exchange_position_error": pos_err,
        "latest_position_watch": compact_row(
            position,
            [
                "status",
                "side",
                "entry_price",
                "stop_price",
                "reference_support",
                "quantity",
                "leverage",
                "opened_at",
                "last_check_time",
                "peak_price",
                "last_price",
                "guard_price",
                "exit_signal",
                "exit_reason",
                "closed_at",
            ],
        ),
        "latest_entry_watch": compact_row(
            entry,
            [
                "candidate_time",
                "source",
                "source_reason",
                "radar_score",
                "strength",
                "confidence",
                "reference_price",
                "snapshot_price",
                "snapshot_oi_d1h_pct",
                "snapshot_oi_d6h_pct",
                "snapshot_funding_rate",
                "watch_status",
                "last_analysis_time",
                "trend",
                "setup_type",
                "support_price",
                "resistance_price",
                "suggested_entry",
                "suggested_stop",
                "trigger_time",
                "notes",
            ],
        ),
        "position_history": compact_rows(
            position_history,
            [
                "status",
                "entry_price",
                "stop_price",
                "opened_at",
                "last_check_time",
                "peak_price",
                "last_price",
                "guard_price",
                "exit_signal",
                "exit_reason",
                "closed_at",
            ],
        ),
        "entry_history": compact_rows(
            entry_history,
            [
                "candidate_time",
                "source",
                "source_reason",
                "radar_score",
                "watch_status",
                "trend",
                "setup_type",
                "support_price",
                "resistance_price",
                "suggested_entry",
                "suggested_stop",
                "trigger_time",
                "notes",
            ],
        ),
        "recent_market_snapshots": compact_rows(
            snapshots,
            [
                "snapshot_time",
                "mode",
                "price",
                "px_chg_pct",
                "vol_24h",
                "funding_rate",
                "oi_usd",
                "oi_d1h_pct",
                "oi_d6h_pct",
                "est_mcap",
                "watchlist_status",
                "radar_score",
                "sideways_days",
            ],
        ),
        "candidate_pool_records": compact_rows(
            pool_rows,
            [
                "pool_name",
                "snapshot_time",
                "score",
                "source_reason",
                "price",
                "px_chg_pct",
                "oi_d1h_pct",
                "oi_d6h_pct",
                "funding_rate",
                "data_days",
                "status",
            ],
        ),
        "kline_structure": summarize_kline_structure(symbol),
    }


def build_ai_user_prompt(symbol: str, symbol_context: dict, user_question: str = ""):
    context_text = json.dumps(symbol_context, ensure_ascii=False, indent=2)
    question = user_question.strip() or "请按当前策略分析这个代币状态。"
    return (
        f"用户问题：{question}\n"
        f"分析对象：{symbol}\n\n"
        "系统策略上下文：\n"
        f"{json.dumps(build_strategy_context(), ensure_ascii=False, indent=2)}\n\n"
        "当前代币综合上下文(JSON，来自本地雷达DB、ai-trader持仓接口、Binance主网K线)：\n"
        f"{context_text}\n\n"
        "请按这个格式输出：\n"
        "1. 阶段判断：候选/观察/已入场/保护收益/离场后等待/放弃 中选一个。\n"
        "2. 你的核心判断：结合雷达、OI/费率、K线、持仓/guard 说明，不要只复述数据。\n"
        "3. 关键价位：支撑、危险位、动态保护线、突破确认位；说明每个价位的意义。\n"
        "4. 操作建议：已持仓就说持有/收紧/离场条件；未持仓就说等待/回调/突破入场条件。\n"
        "5. 反证和风险：最多3条，说明什么情况证明判断错了。\n"
        "要求：简洁、中文、实战，不要写免责声明，不要建议超过系统风险规则的操作。"
    )


def build_global_radar_context(env):
    snapshot_time_rows = query_rows_params(
        ACCUM_DB,
        "SELECT MAX(snapshot_time) AS snapshot_time FROM market_snapshots WHERE mode = 'oi'",
        (),
    )
    latest_snapshot_time = (snapshot_time_rows[0] or {}).get("snapshot_time") if snapshot_time_rows else None
    latest_oi = []
    if latest_snapshot_time:
        latest_oi = query_rows_params(
            ACCUM_DB,
            """
            SELECT snapshot_time, symbol, price, px_chg_pct, vol_24h, funding_rate, oi_usd,
                   oi_d1h_pct, oi_d6h_pct, est_mcap, watchlist_status, radar_score, sideways_days
            FROM market_snapshots
            WHERE mode = 'oi' AND snapshot_time = ?
            ORDER BY
              CASE WHEN in_watchlist = 1 THEN 0 ELSE 1 END,
              abs(coalesce(oi_d6h_pct, 0)) DESC,
              abs(coalesce(px_chg_pct, 0)) DESC
            LIMIT 8
            """,
            (latest_snapshot_time,),
        )

    ready_rows = query_rows_params(
        ACCUM_DB,
        """
        SELECT symbol, candidate_time, source_reason, radar_score, strength, confidence,
               watch_status, last_analysis_time, trend, setup_type, support_price,
               resistance_price, suggested_entry, suggested_stop, trigger_time, notes
        FROM entry_watchlist
        WHERE watch_status IN ('READY', 'WATCHING', 'TRIGGERED')
        ORDER BY coalesce(last_analysis_time, candidate_time) DESC
        LIMIT 8
        """,
        (),
    )
    pool_rows = query_rows_params(
        ACCUM_DB,
        """
        SELECT pool_name, snapshot_time, symbol, score, source_reason, price, px_chg_pct,
               vol_24h, oi_d1h_pct, oi_d6h_pct, funding_rate, est_mcap, data_days, status
        FROM candidate_pools
        ORDER BY snapshot_time DESC, score DESC
        LIMIT 8
        """,
        (),
    )
    positions = query_rows_params(
        ACCUM_DB,
        """
        SELECT symbol, status, side, entry_price, stop_price, quantity, leverage, opened_at,
               last_check_time, peak_price, last_price, guard_price, exit_signal, exit_reason, closed_at
        FROM position_watchlist
        ORDER BY coalesce(last_check_time, opened_at) DESC
        LIMIT 6
        """,
        (),
    )
    code, exchange_body = ai_trader_get(env, "/account/positions", timeout=12)
    exchange_positions = exchange_body.get("data") if code == 200 and exchange_body.get("success") else []

    return {
        "env": env_label(env),
        "latest_oi_snapshot_time": latest_snapshot_time,
        "latest_oi_focus": [
            compact_row(
                row,
                [
                    "snapshot_time",
                    "symbol",
                    "price",
                    "px_chg_pct",
                    "vol_24h",
                    "funding_rate",
                    "oi_usd",
                    "oi_d1h_pct",
                    "oi_d6h_pct",
                    "est_mcap",
                    "watchlist_status",
                    "radar_score",
                    "sideways_days",
                ],
            )
            for row in latest_oi
        ],
        "entry_watch_focus": [
            compact_row(
                row,
                [
                    "symbol",
                    "candidate_time",
                    "source_reason",
                    "radar_score",
                    "strength",
                    "watch_status",
                    "last_analysis_time",
                    "trend",
                    "setup_type",
                    "support_price",
                    "resistance_price",
                    "suggested_entry",
                    "suggested_stop",
                    "trigger_time",
                    "notes",
                ],
            )
            for row in ready_rows
        ],
        "candidate_pools_focus": [
            compact_row(
                row,
                [
                    "pool_name",
                    "snapshot_time",
                    "symbol",
                    "score",
                    "source_reason",
                    "price",
                    "px_chg_pct",
                    "oi_d1h_pct",
                    "oi_d6h_pct",
                    "funding_rate",
                    "data_days",
                    "status",
                ],
            )
            for row in pool_rows
        ],
        "position_watch_focus": [
            compact_row(
                row,
                [
                    "symbol",
                    "status",
                    "side",
                    "entry_price",
                    "stop_price",
                    "opened_at",
                    "last_check_time",
                    "peak_price",
                    "last_price",
                    "guard_price",
                    "exit_signal",
                    "exit_reason",
                    "closed_at",
                ],
            )
            for row in positions
        ],
        "exchange_open_positions": exchange_positions,
    }


def build_global_ai_prompt(global_context: dict, user_question: str):
    def line_for_oi(row):
        return (
            f"{row.get('symbol')} price={row.get('price')} px24h={row.get('px_chg_pct')}% "
            f"oi1h={row.get('oi_d1h_pct')}% oi6h={row.get('oi_d6h_pct')}% "
            f"funding={row.get('funding_rate')} score={row.get('radar_score')} {row.get('watchlist_status')}"
        )

    def line_for_entry(row):
        return (
            f"{row.get('symbol')} {row.get('watch_status')} {row.get('setup_type')} "
            f"support={row.get('support_price')} resistance={row.get('resistance_price')} "
            f"entry={row.get('suggested_entry')} stop={row.get('suggested_stop')} reason={row.get('source_reason')}"
        )

    def line_for_pool(row):
        return (
            f"{row.get('symbol')} {row.get('pool_name')} score={row.get('score')} "
            f"px24h={row.get('px_chg_pct')}% oi6h={row.get('oi_d6h_pct')}% reason={row.get('source_reason')}"
        )

    def line_for_position(row):
        return (
            f"{row.get('symbol')} {row.get('status')} entry={row.get('entry_price')} "
            f"last={row.get('last_price')} peak={row.get('peak_price')} guard={row.get('guard_price')} "
            f"signal={row.get('exit_signal')} reason={row.get('exit_reason')}"
        )

    context_text = "\n".join(
        [
            f"env={global_context.get('env')} latest_oi_snapshot={global_context.get('latest_oi_snapshot_time')}",
            "latest_oi_focus:",
            *[line_for_oi(row) for row in global_context.get("latest_oi_focus", [])],
            "entry_watch_focus:",
            *[line_for_entry(row) for row in global_context.get("entry_watch_focus", [])],
            "candidate_pools_focus:",
            *[line_for_pool(row) for row in global_context.get("candidate_pools_focus", [])],
            "position_watch_focus:",
            *[line_for_position(row) for row in global_context.get("position_watch_focus", [])],
            f"exchange_open_positions={global_context.get('exchange_open_positions')}",
        ]
    )
    return (
        f"用户问题：{user_question.strip() or '请解读当前雷达/OI推送整体情况。'}\n\n"
        "系统策略上下文摘要：雷达从Binance U本位全量池找OI/费率/价格/成交量/横盘/新币/动量异动；"
        "候选先入观察池，观察器看5m/15m趋势、支撑、回调或突破；持仓按风险额开仓，亏损接近半R且走坏要早退，"
        "盈利后用动态guard、峰值回撤和ROI分层保护，不固定2R止盈；AI只做只读分析，不下单。\n\n"
        "当前系统综合上下文(JSON，来自本地雷达DB、候选池、持仓池、ai-trader持仓接口)：\n"
        f"{context_text}\n\n"
        "请按这个格式输出：\n"
        "1. 当前雷达/OI整体判断：市场是否有值得盯的异动，别泛泛而谈。\n"
        "2. 重点代币分层：最值得盯、等待确认、暂时放弃/风险高，各列2-4个并说明原因。\n"
        "3. 已持仓处理：结合guard、峰值回撤、OI/价格状态，说明是否继续拿或收紧。\n"
        "4. 下一步观察条件：哪些价位/结构/OI变化会触发进场或离场关注。\n"
        "5. 风险和反证：最多3条。\n"
        "要求：简洁、中文、实战；不要使用表格；总长度控制在900个中文字符以内；"
        "必须基于给定数据，数据不足就明确说不足。"
    )


def run_ai_global_analysis(env, user_question: str, chat_id: str = "local"):
    config = resolve_ai_analysis_config(env)
    if not config.get("enabled"):
        return "AI分析当前未开启：AI_ANALYSIS_ENABLED=false"
    if config.get("api") not in {"openai-completions", "openai-chat"}:
        return f"当前只支持 OpenAI-compatible chat/completions，配置为 {config.get('api')}"

    global_context = build_global_radar_context(env)
    sessions = load_ai_sessions()
    session_key = f"{env_label(env)}:{chat_id}:GLOBAL"
    session = sessions.get(session_key) or {"turns": [], "generation": 1}
    prior_turns = session.get("turns") or []

    messages = [{"role": "system", "content": ai_system_prompt()}]
    messages.extend(prior_turns[-6:])
    user_prompt = build_global_ai_prompt(global_context, user_question)
    messages.append({"role": "user", "content": user_prompt})

    try:
        content = call_openai_compatible_chat(config, messages)
    except Exception as e:
        return f"AI分析失败：{str(e)[:500]}"

    if not content:
        return "AI分析失败：模型返回为空"
    if not looks_complete_response(content):
        short_messages = [
            {"role": "system", "content": ai_system_prompt()},
            {
                "role": "user",
                "content": (
                    f"用户问题：{user_question.strip() or '请解读当前雷达/OI推送整体情况。'}\n"
                    f"当前雷达上下文：\n{user_prompt[:6000]}\n\n"
                    "上一次回答被截断。请重新输出，必须完整，最多6行，每行不超过45个中文字符，"
                    "只说：整体判断、最值得盯、等待确认、放弃/风险高、当前持仓、下一步。不要表格。"
                ),
            },
        ]
        short_config = {**config, "max_tokens": min(int(config.get("max_tokens", 900) or 900), 500)}
        try:
            retry_content = call_openai_compatible_chat(short_config, short_messages)
            if retry_content:
                content = retry_content
        except Exception:
            pass

    prior_turns.extend(
        [
            {"role": "user", "content": user_prompt[-3000:]},
            {"role": "assistant", "content": content[-3000:]},
        ]
    )
    total_chars = sum(len(item.get("content", "")) for item in prior_turns)
    if total_chars > config.get("session_chars", 16000):
        session["generation"] = int(session.get("generation", 1)) + 1
        prior_turns = [
            {
                "role": "system",
                "content": "上一段全局AI分析会话已达到长度上限，系统已自动开启新的压缩上下文，仅保留最近结论。",
            },
            *prior_turns[-2:],
        ]
    session["turns"] = prior_turns[-8:]
    session["updated_at"] = now_cst()
    sessions[session_key] = session
    save_ai_sessions(sessions)

    header = f"{env_label(env)} 雷达AI综合分析 ({config.get('model')})"
    return f"{header}\n{content}"[:3500]


def run_ai_symbol_analysis(env, raw_symbol: str, chat_id: str = "local", user_question: str = ""):
    symbol = normalize_symbol(raw_symbol)
    if not symbol:
        return "用法：/ai MASK 或 /ask MASK 现在怎么样"

    config = resolve_ai_analysis_config(env)
    if not config.get("enabled"):
        return "AI分析当前未开启：AI_ANALYSIS_ENABLED=false"
    if config.get("api") not in {"openai-completions", "openai-chat"}:
        return f"当前只支持 OpenAI-compatible chat/completions，配置为 {config.get('api')}"

    template_analysis = format_symbol_analysis(env, symbol)
    symbol_context = build_symbol_context(env, symbol, template_analysis)
    sessions = load_ai_sessions()
    session_key = f"{env_label(env)}:{chat_id}:{symbol}"
    session = sessions.get(session_key) or {"turns": [], "generation": 1}
    prior_turns = session.get("turns") or []

    messages = [{"role": "system", "content": ai_system_prompt()}]
    messages.extend(prior_turns[-6:])
    user_prompt = build_ai_user_prompt(symbol, symbol_context, user_question=user_question)
    messages.append({"role": "user", "content": user_prompt})

    try:
        content = call_openai_compatible_chat(config, messages)
    except Exception as e:
        return f"AI分析失败：{str(e)[:500]}"

    if not content:
        return "AI分析失败：模型返回为空"

    prior_turns.extend(
        [
            {"role": "user", "content": user_prompt[-3000:]},
            {"role": "assistant", "content": content[-3000:]},
        ]
    )
    total_chars = sum(len(item.get("content", "")) for item in prior_turns)
    if total_chars > config.get("session_chars", 16000):
        session["generation"] = int(session.get("generation", 1)) + 1
        prior_turns = [
            {
                "role": "system",
                "content": "上一段AI分析会话已达到长度上限，系统已自动开启新的压缩上下文，仅保留最近结论。",
            },
            *prior_turns[-2:],
        ]
    session["turns"] = prior_turns[-8:]
    session["updated_at"] = now_cst()
    sessions[session_key] = session
    save_ai_sessions(sessions)

    header = f"{env_label(env)} {symbol} AI辅助分析 ({config.get('model')})"
    return f"{header}\n{content}"[:3500]


def latest_symbol_rows(symbol: str):
    position = query_rows_params(
        ACCUM_DB,
        """
        SELECT *
        FROM position_watchlist
        WHERE symbol = ?
        ORDER BY coalesce(last_check_time, opened_at) DESC
        LIMIT 1
        """,
        (symbol,),
    )
    entry = query_rows_params(
        ACCUM_DB,
        """
        SELECT *
        FROM entry_watchlist
        WHERE symbol = ?
        ORDER BY coalesce(last_analysis_time, candidate_time) DESC
        LIMIT 1
        """,
        (symbol,),
    )
    pool = query_rows_params(
        ACCUM_DB,
        """
        SELECT *
        FROM candidate_pools
        WHERE symbol = ?
        ORDER BY snapshot_time DESC, score DESC
        LIMIT 3
        """,
        (symbol,),
    )
    snapshots = query_rows_params(
        ACCUM_DB,
        """
        SELECT *
        FROM market_snapshots
        WHERE symbol = ?
        ORDER BY snapshot_time DESC
        LIMIT 1
        """,
        (symbol,),
    )
    return (
        position[0] if position else {},
        entry[0] if entry else {},
        pool,
        snapshots[0] if snapshots else {},
    )


def build_symbol_judgement(position, exchange_pos, snapshot):
    if not position and not exchange_pos and not snapshot:
        return "暂未在本地雷达/持仓记录中找到这个币，可能不在当前扫描结果或还没有触发过。"

    current = None
    if exchange_pos:
        current = exchange_pos.get("markPrice")
    if current is None:
        current = position.get("last_price") or snapshot.get("price")

    guard = position.get("guard_price") or position.get("stop_price")
    entry = position.get("entry_price") or (exchange_pos or {}).get("entryPrice")
    peak = position.get("peak_price")
    qty = position.get("quantity") or (exchange_pos or {}).get("amount")
    pnl = (exchange_pos or {}).get("unrealizedProfit")

    try:
        current_f = float(current)
        guard_f = float(guard) if guard not in (None, "") else None
        entry_f = float(entry) if entry not in (None, "") else None
        peak_f = float(peak) if peak not in (None, "") else None
        qty_f = abs(float(qty)) if qty not in (None, "") else None
    except (TypeError, ValueError):
        return "数据不完整，先按观察处理。"

    if guard_f and current_f <= guard_f:
        return "EXIT区：当前价已经触及/跌破动态保护线，系统应优先保护利润或控制亏损。"
    if guard_f and current_f <= guard_f * 1.005:
        return "WATCH区：距离动态保护线小于0.5%，需要盯紧，继续下压就容易触发离场。"

    if pnl is not None and entry_f and qty_f:
        try:
            notional = entry_f * qty_f
            leverage = float((exchange_pos or {}).get("leverage") or position.get("leverage") or 1)
            margin = notional / leverage if leverage else 0
            roi = float(pnl) / margin * 100 if margin else 0
            if roi >= 50:
                return f"HOLD但收紧：浮盈ROI约{roi:.0f}%，已经进入利润保护阶段，动态guard应持续上移。"
        except (TypeError, ValueError, ZeroDivisionError):
            pass

    if peak_f and current_f < peak_f * 0.985:
        return "WATCH区：相对峰值已有明显回撤，若K线继续走弱，需要优先防回撤。"

    return "HOLD区：当前没有触发保护线，继续观察趋势和OI/费率是否支持上攻。"


def format_symbol_analysis(env, raw_symbol: str):
    symbol = normalize_symbol(raw_symbol)
    if not symbol:
        return "用法：/coin MASK 或 /analyze MASK"

    position, entry, pools, snapshot = latest_symbol_rows(symbol)
    exchange_pos, pos_err = load_exchange_position(env, symbol)
    judgement = build_symbol_judgement(position, exchange_pos, snapshot)

    current = (exchange_pos or {}).get("markPrice") or position.get("last_price") or snapshot.get("price")
    entry_price = position.get("entry_price") or (exchange_pos or {}).get("entryPrice") or entry.get("suggested_entry")
    stop = position.get("stop_price") or entry.get("suggested_stop")
    guard = position.get("guard_price")
    peak = position.get("peak_price")
    qty = position.get("quantity") or (exchange_pos or {}).get("amount")
    pnl = (exchange_pos or {}).get("unrealizedProfit")

    roi_text = "-"
    locked_text = "-"
    try:
        pnl_f = float(pnl)
        entry_f = float(entry_price)
        qty_f = abs(float(qty))
        lev = float((exchange_pos or {}).get("leverage") or position.get("leverage") or 1)
        margin = entry_f * qty_f / lev if lev else 0
        roi_text = f"{pnl_f / margin * 100:.1f}%" if margin else "-"
    except (TypeError, ValueError, ZeroDivisionError):
        pass

    try:
        locked_text = fmt_usd((float(guard) - float(entry_price)) * abs(float(qty)))
    except (TypeError, ValueError):
        pass

    pool_lines = []
    for row in pools[:2]:
        pool_lines.append(
            f"{row.get('pool_name')} score={float(row.get('score') or 0):.1f} {row.get('source_reason') or ''}".strip()
        )

    source_reason = (
        entry.get("source_reason")
        or snapshot.get("watchlist_status")
        or (pool_lines[0] if pool_lines else "")
        or "-"
    )

    lines = [
        f"{env_label(env)} {symbol} 分析",
        f"结论：{judgement}",
        "",
        "持仓/价格",
        f"状态：{position.get('status') or ('ACTIVE' if exchange_pos else '-')}",
        f"当前：{fmt_price(current)} | 入场：{fmt_price(entry_price)}",
        f"浮盈：{fmt_usd(pnl)} | ROI：{roi_text}",
        f"数量：{fmt_price(qty, 2)}",
        f"止损：{fmt_price(stop)} | 动态保护线：{fmt_price(guard)}",
        f"峰值：{fmt_price(peak)} | 已锁定：{locked_text}",
        "",
        "雷达/资金面",
        f"来源：{source_reason}",
        f"OI 1h：{float(snapshot.get('oi_d1h_pct') or entry.get('snapshot_oi_d1h_pct') or 0):+.1f}% | OI 6h：{float(snapshot.get('oi_d6h_pct') or entry.get('snapshot_oi_d6h_pct') or 0):+.1f}%",
        f"费率：{float(snapshot.get('funding_rate') or entry.get('snapshot_funding_rate') or 0) * 100:+.4f}% | 24h涨跌：{float(snapshot.get('px_chg_pct') or 0):+.1f}%",
        f"最近检查：{position.get('last_check_time') or snapshot.get('snapshot_time') or '-'}",
        f"系统状态：{position.get('exit_signal') or '-'} | {position.get('exit_reason') or '-'}",
    ]
    if pool_lines:
        lines.extend(["", "候选池", *pool_lines])
    if pos_err:
        lines.extend(["", f"提示：{pos_err}"])
    return "\n".join(lines)[:3500]


def summarize_positions():
    rows = query_rows(
        ACCUM_DB,
        """
        SELECT symbol, status, last_price, guard_price, exit_reason
        FROM position_watchlist
        ORDER BY coalesce(last_check_time, opened_at) DESC
        LIMIT 8
        """,
    )
    active = [r for r in rows if r.get("status") == "ACTIVE"]
    if not active:
        return "当前没有 ACTIVE 持仓"
    lines = []
    for row in active[:6]:
        lines.append(
            f"{row['symbol']} HOLD last={row.get('last_price') or 0:.6f} guard={row.get('guard_price') or 0:.6f}"
        )
    return "\n".join(lines)


def summarize_ready():
    rows = query_rows(
        ACCUM_DB,
        """
        SELECT symbol, setup_type, suggested_entry, suggested_stop
        FROM entry_watchlist
        WHERE watch_status = 'READY'
        ORDER BY last_analysis_time DESC
        LIMIT 8
        """,
    )
    if not rows:
        return "当前没有 READY 候选"
    return "\n".join(
        f"{r['symbol']} {r.get('setup_type') or ''} entry={r.get('suggested_entry') or 0:.6f} stop={r.get('suggested_stop') or 0:.6f}"
        for r in rows
    )


def summarize_runtime(env):
    code, body = ai_trader_get(env, "/runtime/health")
    if code != 200:
        return f"ai-trader health http={code} {body.get('error') or body.get('raw') or ''}".strip()
    data = body.get("data", {})
    status = data.get("status")
    rt = data.get("runtimeState", {})
    orch = data.get("orchestrator", {})
    return (
        f"ai-trader health={status}\n"
        f"orchestrator={orch.get('status')} tradingEnabled={rt.get('tradingEnabled')} autoStart={rt.get('autoStartOnBoot')}"
    )


def maybe_run_manage(env, state):
    interval_sec = int(env.get("LOCAL_MANAGE_INTERVAL_SEC", "120") or 120)
    now_ts = int(datetime.now(timezone.utc).timestamp())
    last_ts = int(state.get("last_manage_trigger_ts", 0) or 0)
    if now_ts - last_ts < interval_sec:
        return

    state["last_manage_trigger_ts"] = now_ts
    rc, output = run_local_command([str(RUN_TASK_SCRIPT), "manage"], timeout=180)
    log_line(f"LOCAL_MANAGE rc={rc} {output[:500] if output else ''}".strip())


def handle_tg_command(env, text: str, chat_id: str = "local"):
    cmd = text.strip()
    if not cmd:
        return "空命令，发送 /help 查看支持的命令"

    parts = shlex.split(cmd)
    head = parts[0].lower()

    if head in {"/help", "help"}:
        return (
            "可用命令:\n"
            "/status - 查看系统状态\n"
            "/positions - 查看当前持仓\n"
            "/ready - 查看 READY 候选\n"
            "/coin SYMBOL - 分析某个代币，例如 /coin MASK\n"
            "/analyze SYMBOL - 同 /coin\n"
            "/ai SYMBOL - AI辅助单币解读，例如 /ai MASK\n"
            "/ai 问题 - AI辅助综合解读，例如 /ai 最近OI推送怎么看\n"
            "/run oi|observe|manage|seed-observe - 立即跑一次\n"
            "/retry SYMBOL - 手动重试某个候选\n"
            "/trading on|off - 开关 ai-trader 执行\n"
            "/orchestrator start|pause|resume|stop - 控制执行端"
        )

    if head in {"/status", "status"}:
        return f"{summarize_runtime(env)}\n\n{summarize_positions()}\n\n{summarize_ready()}"

    if head in {"/positions", "positions"}:
        return summarize_positions()

    if head in {"/ready", "ready"}:
        return summarize_ready()

    if head in {"/coin", "coin", "/analyze", "analyze"}:
        if len(parts) < 2:
            return "用法：/coin MASK 或 /analyze MASK"
        return format_symbol_analysis(env, parts[1])

    if head in {"/ai", "ai", "/ask", "ask"}:
        if len(parts) < 2:
            return "用法：/ai MASK 或 /ai 最近OI推送怎么看"
        if looks_like_symbol(parts[1]):
            return run_ai_symbol_analysis(env, parts[1], chat_id=chat_id, user_question=" ".join(parts[2:]))
        return run_ai_global_analysis(env, " ".join(parts[1:]), chat_id=chat_id)

    if head in {"/run", "run"} and len(parts) >= 2:
        mode = parts[1]
        if mode not in {"oi", "observe", "manage", "seed-observe", "pool"}:
            return "只支持 /run oi|observe|manage|seed-observe|pool"
        code, output = run_local_command([str(RUN_TASK_SCRIPT), mode], timeout=300)
        return f"/run {mode} rc={code}\n{output or 'done'}"

    if head in {"/retry", "retry"} and len(parts) >= 2:
        symbol = parts[1].upper()
        code, output = run_local_command([str(MANAGE_MONITOR_SCRIPT), "retry", symbol], timeout=180)
        return f"/retry {symbol} rc={code}\n{output or 'done'}"

    if head in {"/trading", "trading"} and len(parts) >= 2:
        enabled = parts[1].lower() in {"on", "true", "1", "enable", "enabled"}
        code, body = ai_trader_post("/runtime/trading-enabled", {"enabled": enabled})
        return f"trading {'on' if enabled else 'off'} http={code}\n{json.dumps(body, ensure_ascii=False)[:1200]}"

    if head in {"/orchestrator", "orchestrator"} and len(parts) >= 2:
        action = parts[1].lower()
        if action not in {"start", "pause", "resume", "stop"}:
            return "只支持 /orchestrator start|pause|resume|stop"
        code, body = ai_trader_post(f"/runtime/{action}", {})
        return f"orchestrator {action} http={code}\n{json.dumps(body, ensure_ascii=False)[:1200]}"

    return "不支持的命令，发送 /help 查看可用命令"


def check_telegram_commands(env, state):
    if env.get("TG_COMMANDS_ENABLED", "true").lower() != "true":
        return

    token = env.get("TG_BOT_TOKEN")
    allowed_chat_id = str(env.get("TG_OBSERVER_CHAT_ID") or env.get("TG_CHAT_ID") or "").strip()
    command_prefix = (env.get("TG_COMMAND_PREFIX") or "").strip()
    if not token or not allowed_chat_id:
        return

    offset = int(state.get("tg_update_offset", 0) or 0)
    try:
        resp = requests.get(
            f"https://api.telegram.org/bot{token}/getUpdates",
            params={"timeout": 1, "offset": offset},
            timeout=10,
        )
        if resp.status_code != 200:
            return
        data = resp.json()
        if not data.get("ok"):
            return
        results = data.get("result", [])
    except Exception:
        return

    max_update_id = offset
    for item in results:
        update_id = int(item.get("update_id", 0))
        max_update_id = max(max_update_id, update_id + 1)
        msg = item.get("message") or item.get("edited_message") or {}
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id", "")).strip()
        text = (msg.get("text") or "").strip()
        if not text or chat_id != allowed_chat_id:
            continue
        if not claim_tg_update(update_id):
            continue
        if command_prefix:
            if not text.startswith(command_prefix):
                continue
            text = text[len(command_prefix):].strip()
            if not text:
                text = "/status"
        reply = handle_tg_command(env, text, chat_id=chat_id)
        telegram_api(env, "sendMessage", {"chat_id": chat_id, "text": reply}, timeout=20)

    state["tg_update_offset"] = max_update_id


def check_ready(env, state):
    rows = query_rows(
        ACCUM_DB,
        """
        SELECT symbol, watch_status, setup_type, suggested_entry, suggested_stop, last_analysis_time
        FROM entry_watchlist
        WHERE watch_status = 'READY'
        ORDER BY last_analysis_time DESC
        """,
    )
    current = {}
    notified = state.get("ready_notified", {})
    for row in rows:
        current[row["symbol"]] = row["last_analysis_time"] or ""
        if row["symbol"] not in notified:
            emit(
                env,
                "新 READY 候选",
                f"{row['symbol']} {row.get('setup_type') or ''} entry={row.get('suggested_entry', 0):.6f} stop={row.get('suggested_stop', 0):.6f}",
            )
            notified[row["symbol"]] = current[row["symbol"]]
    state["ready"] = current
    state["ready_notified"] = {symbol: notified.get(symbol, ts) for symbol, ts in current.items()}


def check_executed(env, state):
    lookback_sec = int(env.get("EXECUTED_NOTIFY_LOOKBACK_SEC", "900") or 900)
    cutoff_ms = int((datetime.now(timezone.utc).timestamp() - lookback_sec) * 1000)
    rows = query_rows(
        AI_TRADER_DB,
        f"""
        SELECT symbol, status, reason, normalizedPayload, receivedAt
        FROM signal_events
        WHERE source = 'accumulation-radar-observer' AND status = 'EXECUTED'
          AND receivedAt >= {cutoff_ms}
        ORDER BY receivedAt DESC
        LIMIT 30
        """,
    )
    seen = state.get("executed_seen", {})
    current = dict(seen)
    for row in rows:
        key = f"{row['symbol']}:{row['receivedAt']}"
        current[key] = True
        global_key = f"{env_label(env)}:{key}"
        if key not in seen and claim_seen_key(EXECUTED_EVENT_DEDUP_PATH, global_key, ttl_sec=7 * 86400):
            detail = build_executed_message(row)
            emit(env, "新成交", detail)
    state["executed_seen"] = dict(list(current.items())[-500:])


def build_executed_message(row):
    payload = {}
    try:
        payload = json.loads(row.get("normalizedPayload") or "{}")
    except Exception:
        payload = {}

    symbol = row.get("symbol") or payload.get("symbol") or ""
    side = payload.get("signalType") or ""
    entry = float(payload.get("entryPrice") or 0)
    stop = float(payload.get("stopLoss") or 0)
    qty = float(payload.get("quantity") or 0)
    leverage = payload.get("leverage")
    reason = payload.get("reason") or row.get("reason") or ""
    notional = qty * entry if qty > 0 and entry > 0 else 0
    risk = qty * max(entry - stop, 0) if qty > 0 else 0

    parts = [f"{symbol} EXECUTED"]
    if side:
        parts.append(f"方向={side}")
    if qty:
        parts.append(f"数量={qty:g}")
    if notional:
        parts.append(f"仓位价值={notional:.2f} USDT")
    if entry:
        parts.append(f"入场={entry:.10g}")
    if stop:
        parts.append(f"止损={stop:.10g}")
    if leverage:
        parts.append(f"杠杆={leverage}x")
    if risk:
        parts.append(f"风险≈{risk:.2f} USDT")
    if reason:
        parts.append(f"理由={reason[:180]}")
    return " | ".join(parts)


def check_failed_or_stuck(env, state):
    rows = query_rows(
        AI_TRADER_DB,
        """
        SELECT symbol, status, reason, receivedAt
        FROM signal_events
        WHERE source = 'accumulation-radar-observer'
          AND status IN ('REJECTED', 'FAILED', 'RECEIVED')
        ORDER BY receivedAt DESC
        LIMIT 80
        """,
    )
    active_rows = query_rows(
        ACCUM_DB,
        """
        SELECT symbol, status
        FROM position_watchlist
        WHERE status = 'ACTIVE'
        """,
    )
    watch_rows = query_rows(
        ACCUM_DB,
        """
        SELECT symbol, watch_status
        FROM entry_watchlist
        WHERE watch_status IN ('READY', 'WATCHING', 'INVALIDATED', 'TRIGGERED')
        """,
    )
    active_symbols = {row["symbol"] for row in active_rows}
    watch_status_map = {row["symbol"]: row["watch_status"] for row in watch_rows}
    seen = state.get("failed_seen", {})
    cooldowns = state.get("failed_cooldown", {})
    stuck_notified = state.get("stuck_notified", {})
    received_only_logged = state.get("received_only_logged", {})
    current = {}
    grouped = {}
    for row in rows:
        grouped.setdefault(row["symbol"], []).append(row)

    for symbol, items in grouped.items():
        latest = items[0]
        latest_ts = int(latest.get("receivedAt") or 0)
        reason = (latest.get("reason") or "").strip()
        reason_lower = reason.lower()
        if (
            "indicator_error" in reason_lower
            or "failed to calculate ema" in reason_lower
            or "insufficient kline data for ema" in reason_lower
        ):
            continue
        cooldown_key = f"{symbol}:{reason or latest.get('status')}"
        now_ts = int(datetime.now(timezone.utc).timestamp())
        last_cooldown_ts = int(cooldowns.get(cooldown_key, 0) or 0)
        if latest.get("status") in {"REJECTED", "FAILED"}:
            key = f"{symbol}:{latest_ts}:{latest.get('status')}"
            current[key] = True
            if key not in seen and now_ts - last_cooldown_ts >= 1800:
                emit(env, "下单失败", f"{symbol} {latest.get('status')} | {reason}")
                cooldowns[cooldown_key] = now_ts
            continue

        if symbol in active_symbols:
            continue

        if watch_status_map.get(symbol) == "INVALIDATED":
            continue

        if all(item.get("status") == "RECEIVED" for item in items):
            received_marker = f"{symbol}:RECEIVED_ONLY"
            last_received_log_ts = int(received_only_logged.get(received_marker, 0) or 0)
            if now_ts - last_received_log_ts >= 1800:
                log_line(f"执行端仅收到信号但未完成决策，跳过未成交告警: {symbol} attempts={len(items)}")
                received_only_logged[received_marker] = now_ts
            continue

        attempts = len(items)
        if attempts < 3:
            continue

        age_sec = max(0, int(datetime.now(timezone.utc).timestamp() * 1000 - latest_ts) // 1000)
        if age_sec < 300:
            continue

        key = f"{symbol}:STUCK"
        current[key] = True
        stuck_marker = f"{symbol}:{reason or latest.get('status')}"
        if stuck_marker not in stuck_notified and now_ts - last_cooldown_ts >= 1800:
            emit(env, "下单未成交", f"{symbol} 重试{attempts}次仍未成交，可手动重试")
            cooldowns[cooldown_key] = now_ts
            stuck_notified[stuck_marker] = now_ts

    state["failed_seen"] = current
    state["failed_cooldown"] = cooldowns
    state["stuck_notified"] = {
        key: ts for key, ts in stuck_notified.items()
        if int(ts or 0) >= int(datetime.now(timezone.utc).timestamp()) - 86400
    }
    state["received_only_logged"] = {
        key: ts for key, ts in received_only_logged.items()
        if int(ts or 0) >= int(datetime.now(timezone.utc).timestamp()) - 86400
    }


def check_positions(env, state):
    rows = query_rows(
        ACCUM_DB,
        """
        SELECT symbol, status, exit_signal, exit_reason, last_check_time
        FROM position_watchlist
        ORDER BY last_check_time DESC
        """,
    )
    current = {}
    prev_map = state.get("position_seen", {})
    for row in rows:
        symbol = row["symbol"]
        signature = {
            "status": row.get("status") or "",
            "exit_signal": row.get("exit_signal") or "",
            "exit_reason": row.get("exit_reason") or "",
            "last_check_time": row.get("last_check_time") or "",
        }
        current[symbol] = signature
        prev = prev_map.get(symbol, {})
        prev_marker = f"{prev.get('status')}|{prev.get('exit_signal')}"
        curr_marker = f"{signature.get('status')}|{signature.get('exit_signal')}"
        if prev and prev_marker != curr_marker:
            if signature["status"] == "CLOSED" or signature["exit_signal"] in {"WATCH", "EXIT"}:
                emit(env, "持仓状态变化", f"{symbol} {curr_marker} | {signature['exit_reason']}")
    state["position_seen"] = current


def check_errors(env, state):
    error_patterns = ("close failed", "Traceback", "ERROR", "Error:")
    paths = [BASE_DIR / "accumulation_manage.log", BASE_DIR / "accumulation_observe.log", BASE_DIR / "accumulation_oi.log"]
    seen = set(state.get("error_seen", []))
    keep = []
    for path in paths:
        if not path.exists():
            continue
        try:
            lines = path.read_text(errors="ignore").splitlines()[-80:]
        except Exception:
            continue
        for line in lines:
            if "(attempt 1/3): 400" in line or "(attempt 2/3): 400" in line or "(attempt 3/3): 400" in line:
                continue
            if "(attempt 1/3): 503" in line or "(attempt 2/3): 503" in line or "(attempt 3/3): 503" in line:
                continue
            if "Failed to place order" in line and "\"TRADING_ERROR\"" in line:
                continue
            if "INDICATOR_ERROR" in line or "Failed to calculate EMA" in line or "Insufficient kline data for EMA" in line:
                continue
            if "MARKET_DATA_ERROR" in line and "Invalid symbol" in line:
                continue
            if "close failed" in line and "ProxyError" in line and "127.0.0.1:7897" in line:
                continue
            if "ECONNRESET" in line or "read ECONNRESET" in line:
                continue
            if "Failed to fetch positions" in line:
                continue
            if any(pat in line for pat in error_patterns):
                key = f"{path.name}:{line[-220:]}"
                keep.append(key)
                if key not in seen:
                    emit(env, "关键异常", line[-220:])
    state["error_seen"] = keep[-50:]


def check_ai_trader_health(env, state):
    code, body = ai_trader_get(env, "/runtime/health", timeout=8)
    if code == 0:
        error_text = str(body.get("error", "unknown"))
        if "Operation not permitted" in error_text and "127.0.0.1" in error_text:
            log_line(f"执行端健康检查跳过: local permission denied ({error_text[:180]})")
            return

    if code != 200:
        status = f"http-{code}" if code else f"down:{body.get('error', 'unknown')}"
    else:
        data = body.get("data", {}) if isinstance(body, dict) else {}
        status = str(data.get("status") or "unknown")
        orchestrator = data.get("orchestrator", {}) if isinstance(data, dict) else {}
        services = orchestrator.get("services", {}) if isinstance(orchestrator, dict) else {}
        issues = orchestrator.get("issues", []) if isinstance(orchestrator, dict) else []
        trading_ping_only_degraded = (
            status == "degraded"
            and orchestrator.get("status") == "RUNNING"
            and services.get("marketData") is True
            and services.get("account") is True
            and issues == ["Trading service is not available"]
        )
        if trading_ping_only_degraded:
            log_line("执行端健康检查降级但核心服务可用: trading ping unavailable")
            status = "ok"

    healed = False
    if status not in {"ok", "healthy"}:
        if AI_TRADER_SERVICE_SCRIPT.exists():
            try:
                subprocess.run(
                    [str(AI_TRADER_SERVICE_SCRIPT), "start"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=20,
                )
                time.sleep(2)
                service_code, service_body = ai_trader_get(env, "/runtime/health", timeout=8)
                if service_code == 200:
                    service_data = service_body.get("data", {}) if isinstance(service_body, dict) else {}
                    service_status = str(service_data.get("status") or "unknown")
                    if service_status in {"ok", "healthy"}:
                        healed = True
                        status = service_status
                        log_line("执行端自动自愈成功: ai-trader demo service restored")
            except Exception as exc:
                log_line(f"执行端服务自愈失败: {str(exc)[:180]}")

        restart_code, _ = ai_trader_post("/runtime/start", {})
        time.sleep(1)
        recheck_code, recheck_body = ai_trader_get(env, "/runtime/health", timeout=8)
        if recheck_code == 200:
            recheck_data = recheck_body.get("data", {}) if isinstance(recheck_body, dict) else {}
            recheck_status = str(recheck_data.get("status") or "unknown")
            if recheck_status in {"ok", "healthy"}:
                healed = True
                status = recheck_status
                log_line("执行端自动自愈成功: ai-trader health restored to ok")
            else:
                status = recheck_status
        elif restart_code == 200:
            status = f"recheck-http-{recheck_code}" if recheck_code else f"recheck-down:{recheck_body.get('error', 'unknown')}"

        if status not in {"ok", "healthy"}:
            positions_code, _ = ai_trader_get(env, "/account/positions", timeout=8)
            if positions_code == 200:
                log_line(f"执行端健康检查降级但交易接口可用: ai-trader health={status}")
                status = "ok"

    prev = state.get("health_status", "")
    last_health_alert_ts = int(state.get("last_health_alert_ts", 0) or 0)
    now_ts = int(datetime.now(timezone.utc).timestamp())
    state["health_status"] = status

    if not healed and status != prev and status not in {"ok", "healthy"} and now_ts - last_health_alert_ts >= 1800:
        emit(env, "执行端状态异常", f"ai-trader health={status}")
        state["last_health_alert_ts"] = now_ts


def main():
    env = load_env(ENV_PATH)
    state = load_state()
    check_telegram_commands(env, state)
    maybe_run_manage(env, state)
    check_ready(env, state)
    check_executed(env, state)
    check_failed_or_stuck(env, state)
    check_positions(env, state)
    check_errors(env, state)
    check_ai_trader_health(env, state)
    save_state(state)


if __name__ == "__main__":
    main()
