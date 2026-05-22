# 🏦 庄家收筹雷达 (Accumulation Radar)

全自动扫描加密货币合约市场的庄家收筹信号 — 横盘吸筹检测 + OI异动监控 + 三策略独立评分。纯Python，零AI成本，Telegram推送。

## 核心理念

> 庄家拉盘前必须先收筹 → 长期横盘+低量 = 收筹中 → OI暴涨 = 大资金进场 = 即将拉盘

- **10x+才算暴涨**，要抓的是庄家盘（RAVE 138x, STO 38x），不是基本面慢涨
- 庄家收筹期3-4个月，横盘区间可达124%
- 空头燃料关键：涨完之后必须有大量人做空，没人做空就没燃料继续拉

## 三策略独立评分

### 🔥 追多 — 纯费率排名（短线轧空）
| 指标 | 含义 |
|------|------|
| 费率负值 | 越负=做空的人越多=空头燃料 |
| 🔥加速 | 费率比上期更负，空头还在加仓 |
| ⬇️变负 | 费率从正转负，刚有人做空 |
| ⬆️回升 | 空头在减少，燃料变少 |

前提条件：涨>3% + 费率为负 + 成交额>$1M

### 📊 综合 — 四维均衡（各25分=100分）
| 图标 | 维度 | 满分 |
|------|------|------|
| 🧊 | 费率（越负越好） | 25 |
| 💎 | 市值（越低越好） | 25 |
| 💤 | 横盘天数（越久越好） | 25 |
| ⚡ | OI变化（越大越好） | 25 |

### 🎯 埋伏 — 提前布局（中长线）
| 维度 | 权重 | 逻辑 |
|------|------|------|
| 💎市值 | **35** | <$50M满分，低市值=大空间 |
| ⚡OI | 30 | OI异动=大资金进场 |
| 💤横盘 | 20 | ≥120天满分，收筹时间 |
| 🧊费率 | 15 | 有负费率是bonus |

前提条件：在收筹池内 + 涨幅<50%

### 💡 值得关注（自动提醒）
- 🔥 费率加速恶化 — 空头疯狂涌入
- ⭐ 双榜上榜 — 多维度共振
- 🎯 暗流 — OI变但价没动（最经典庄家收筹信号）
- 💎 低市值+OI异动 — 埋伏首选

## 数据源

全部免费公开API，无需API Key：

| 数据 | 接口 | 说明 |
|------|------|------|
| 真实流通市值 | 币安现货 `bapi/composite/v1/public/marketing/symbol/list` | 一次请求434币全量市值 |
| K线/行情 | 币安合约 `/fapi/v1/klines`, `/fapi/v1/ticker/24hr` | 历史K线+24h行情 |
| OI历史 | 币安合约 `/futures/data/openInterestHist` | 含CMC流通量(备用) |
| 资金费率 | 币安合约 `/fapi/v1/premiumIndex` | 一次拿全部费率 |

**市值三级Fallback**：币安现货API → 合约OI接口CMCCirculatingSupply×价格 → 粗估公式

## 安装 & 配置

```bash
git clone https://github.com/connectfarm1/accumulation-radar.git
cd accumulation-radar

# Python 3.8+ 即可，唯一依赖是 requests
pip install requests

# 配置 Telegram 推送（可选）
cp .env.example .env.oi
# 编辑 .env.oi，填入你的 TG_BOT_TOKEN 和 TG_CHAT_ID
```

也可以直接用环境变量，不创建 `.env.oi`：

```bash
export TG_BOT_TOKEN=your_telegram_bot_token
export TG_CHAT_ID=your_telegram_chat_id
```

### 创建 Telegram Bot
1. 找 [@BotFather](https://t.me/BotFather)，发 `/newbot`
2. 获得 Bot Token
3. 给 bot 发条消息，然后访问 `https://api.telegram.org/bot<TOKEN>/getUpdates` 获取你的 Chat ID

## 使用

```bash
# 每天跑一次：全市场535合约扫描收筹标的池
python3 accumulation_radar.py pool

# 每小时跑一次：三策略评分 + OI异动监控
python3 accumulation_radar.py oi

# 全部都跑
python3 accumulation_radar.py full

# 新策略：扫描大波动趋势币，生成多空双向10阶挂单计划
python3 accumulation_radar.py trend

# 选币策略：涨幅榜前20 + ATH/近ATH + 已走出趋势
python3 accumulation_radar.py select
```

### 涨幅榜 ATH 趋势选币

`select` 模式用于从“当天最强势”的币里筛出真正走出趋势的观察标的：

1. 先取 24h 涨幅榜前 `20` 个 USDT 永续合约。
2. 日线必须创历史新高，或距离历史高点不超过 `30%`。
3. 日线趋势 5 项至少通过 3 项：ADX 达标、EMA 多头排列、20 日高点突破、EMA 明显分离、低点抬高。

默认参数可用环境变量调整：

| 参数 | 默认值 | 含义 |
| --- | ---: | --- |
| `LEADER_TOP_GAINERS` | `20` | 只复核涨幅榜前 N |
| `LEADER_MAX_ATH_DRAWDOWN_PCT` | `30` | 距 ATH 最大回撤百分比 |
| `LEADER_MIN_24H_VOL_USD` | `5000000` | 最低 24h 成交额 |
| `LEADER_MIN_ADX` | `22` | 日线趋势强度门槛 |
| `LEADER_MIN_EMA_SPREAD_PCT` | `2` | EMA8/21/55 最小分离度 |
| `LEADER_POOL_LIMIT` | `12` | 最多输出标的数 |

结果会保存到 `candidate_pools` 表，`pool_name=leader_ath_trend_pool`。它是选币观察池，不是直接追高买入信号；实际入场仍建议等待 5m/15m 回踩或突破确认。

### 趋势双向阶梯策略

新策略文档见：[docs/trend_breakout_ladder_strategy.md](docs/trend_breakout_ladder_strategy.md)。

它的目标不是预测方向，而是筛出不再横盘、具备大波动趋势条件的合约，然后生成：

- 上方突破多单 10 阶挂单计划
- 下方突破空单 10 阶挂单计划
- 每边按 `22/18/15/12/10/8/6/4/3/2%` 递减分配仓位
- 触发一边后撤销另一边
- 成交均价反向 `10%` 作为硬止损
- 10 单全满后进入 K 线动态分批止盈

当前 `trend` 模式只生成计划、落库和推送，不直接实盘下单。计划保存在 SQLite 表 `trend_breakout_plans`。

### 趋势对冲阶梯交易配置台

当前已新增一套本地前后端，用于把策略参数配置成可启动的 dry-run 策略组：

- 前端配置台：[web/strategy_configurator.html](web/strategy_configurator.html)
- 后端 dry-run 服务：[scripts/hedged_ladder_server.py](scripts/hedged_ladder_server.py)
- 启动脚本：[scripts/run_hedged_ladder_server.sh](scripts/run_hedged_ladder_server.sh)
- 前端控件说明：[docs/strategy_configurator_frontend_controls.md](docs/strategy_configurator_frontend_controls.md)
- 后端规划：[docs/trend_ladder_execution_backend_plan.md](docs/trend_ladder_execution_backend_plan.md)
- 换电脑开发交接：[docs/hedged_ladder_handoff.md](docs/hedged_ladder_handoff.md)

启动后端：

```bash
bash scripts/run_hedged_ladder_server.sh
```

打开前端：

```bash
python3 -m http.server 8765
```

然后访问：

```text
http://127.0.0.1:8765/web/strategy_configurator.html
```

当前后端默认是 `dry-run`，不会真实下单；它会创建策略组、保存 L0-L10 订单计划、支持确认多头/空头和关闭策略组。真实 Binance 私有 API 下单尚未接入。

### 趋势策略批量回测

```bash
# 指定代币回测
python3 scripts/backtest_trend_ladder.py \
  --symbols BTCUSDT,ETHUSDT,SOLUSDT \
  --start 2026-01-01 \
  --end 2026-05-01

# 使用庄家雷达收筹池选币回测
python3 scripts/backtest_trend_ladder.py \
  --radar-watchlist \
  --watchlist-limit 50 \
  --start 2026-01-01 \
  --end 2026-05-01

# 只回测雷达池里已经开始放量/放量启动的标的
python3 scripts/backtest_trend_ladder.py \
  --radar-watchlist \
  --active-radar-only \
  --watchlist-limit 50 \
  --start 2026-01-01 \
  --end 2026-05-01

# 也可以按当前24h成交额选前20个合约做对照组
python3 scripts/backtest_trend_ladder.py \
  --top-volume 20 \
  --start 2026-01-01 \
  --end 2026-05-01
```

回测结果会输出到：

- `backtests/trend_ladder_backtest.json`
- `backtests/trend_ladder_backtest.csv`

第一版回测使用 K 线级模拟，重点验证雷达选币、首组对冲、趋势确认、盈利方向递增加仓、策略组净利润锁利和整体退出；手续费、滑点、资金费率和真实撮合队列后续再加入。

当前回测已经加入两条执行约束：

- 首组 `LONG_1 + SHORT_1` 同时开，对冲试探；方向确认后只加趋势方向第 2-10 单。
- 10 单权重为 `22/18/15/12/10/8/6/4/3/2%`，成本越低仓位越大，越接近顶部/底部仓位越小。
- 对冲腿有独立 10% 止损；趋势确认后，反方向对冲腿亏到 10% 就平掉，剩余趋势腿继续吃利润。
- 对冲腿没有平掉之前，不触发普通利润回撤/动态止盈，避免过早拿掉真正突破行情。
- 浮盈加仓后按成交单数上移策略组净利润保护线，5/8/10 单成交会逐步提高锁利比例。
- 所有交易都标记 `strategy_group_exit=True`，实盘执行时趋势腿退出必须联动撤销或平掉对冲腿和同组残留订单/仓位。

只做一个突破样本可以加：

```bash
python3 scripts/backtest_trend_ladder.py \
  --symbols IOUSDT \
  --start 2026-04-01 \
  --end 2026-05-01 \
  --max-trades 1
```

单币实时纸上模拟使用 1h 以上周期：

```bash
python3 scripts/simulate_hedged_ladder.py --symbol IOUSDT --interval 1h --reset
python3 scripts/simulate_hedged_ladder.py --symbol IOUSDT --interval 1h
```

也可以切到 `4h`，但确认会更慢：

```bash
python3 scripts/simulate_hedged_ladder.py --symbol IOUSDT --interval 4h --reset
```

### 推荐 Crontab 配置

```crontab
# 每天10:00更新收筹标的池
0 10 * * *  cd /path/to/accumulation-radar && python3 accumulation_radar.py pool >> accumulation.log 2>&1

# 每小时:30扫描OI异动+三策略评分
30 * * * *  cd /path/to/accumulation-radar && python3 accumulation_radar.py oi >> accumulation_oi.log 2>&1
```

## GitHub Actions 部署

如果你不想依赖本机开机，可以直接部署到 GitHub Actions。仓库里已经包含
`.github/workflows/accumulation-radar.yml`。

### 需要配置的 GitHub Secrets

- `TG_BOT_TOKEN`
- `TG_CHAT_ID`

### 默认调度

- `0 2 * * *`：每天 `10:00`（Asia/Shanghai）运行 `pool`
- `30 * * * *`：每小时 `:30` 运行 `oi`

### 为什么需要缓存数据库

`oi` 模式依赖本地 `accumulation.db` 里的收筹池。GitHub Actions 每次运行都是临时环境，
所以工作流会自动恢复和保存 `accumulation.db`，让后续任务能接着上一轮继续跑。

## 推送示例

```
🏦 庄家雷达 三策略
⏰ 2026-04-24 09:51 CST

🔥 追多 (按费率排名)
  RED     费率-1.003% 🔥加速 | 涨+17% | ~$57M
  KAT     费率-0.627% 🔥加速 | 涨+45% | ~$36M
  MOVR    费率-0.146% 🔥加速 | 涨+56% | ~$30M

📊 综合 (费率+市值+横盘+OI 各25)
  MOVR    86分 | 🧊-0.15% 💎$30M 💤71天 ⚡OI-22%
  KAT     75分 | 🧊-0.63% 💎$36M ⚡OI+33%

🎯 埋伏 (市值35+OI30+横盘20+费率15)
  RARE    82分 | ~$18M OI-24% 横盘75天
  SAGA    74分 | ~$15M OI+4% 🎯暗流 横盘77天

💡 值得关注
  🔥 RED 费率-1.003%加速恶化，空头涌入中
  🎯 SAGA 暗流！OI+4%但价格没动，市值仅$15M

📖 图例
  费率负=空头多(燃料) | 🔥加速/⬇️变负/⬆️回升=费率趋势
  💎市值 | 💤横盘天数(收筹时长) | ⚡OI变化(资金异动)
  🎯暗流=OI动但价没动(收筹信号)
```

## OI异动信号解读

| OI | 价格 | 信号 | 含义 |
|----|------|------|------|
| ↑ | ↑ | 🟢主动加仓做多 | 趋势确立 |
| ↑ | ↓ | 🔴主动加仓做空 | 空头建仓 |
| ↑ | 平 | ⚡暗流涌动 | **最佳埋伏时机** |
| ↓ | ↑ | 💪Squeeze | 空头爆仓 |
| ↓ | ↓ | 💨平仓潮 | 多头止损 |

## 成本

- **$0/月** — 纯Python + 公开API
- 无AI调用，无付费API Key
- 币安API免费，限速宽松

## License

MIT
