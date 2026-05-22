# 趋势对冲阶梯系统交接文档

本文档用于在另一台电脑继续开发当前项目。

## 1. 当前策略共识

这套系统不是预测多空，而是先用市价对冲进入市场，再等趋势走出来。

核心结构：

```text
L0      启动层：LONG_0 + SHORT_0 同时市价开，形成对冲试探仓
L1-L10  趋势层：确认方向后，只加趋势方向，反向趋势层取消
```

多头确认后：

```text
保留 LONG_0
SHORT_0 作为对冲腿，亏损达到 10% 时平掉
只执行 LONG_1 ... LONG_10
SHORT_1 ... SHORT_10 不执行
```

空头确认后：

```text
保留 SHORT_0
LONG_0 作为对冲腿，亏损达到 10% 时平掉
只执行 SHORT_1 ... SHORT_10
LONG_1 ... LONG_10 不执行
```

趋势退出时，策略组内剩余对冲腿和未成交订单必须一起处理，避免留下孤立仓位。

## 2. 当前已经实现

### 前端配置台

文件：

```text
web/strategy_configurator.html
web/radar_watchlist.json
web/radar_watchlist_embed.js
```

功能：

- 雷达池选币。
- 手动输入 symbol。
- 拉取 Binance USDT 永续公开行情现价。
- 按现价生成 L0-L10。
- L0 是市价对冲层。
- L1-L10 是趋势阶梯层。
- 计算每层价格、数量、名义金额和保证金。
- 保存/读取/导入/导出 JSON。
- 启动策略组。
- 确认多头。
- 确认空头。
- 关闭策略组。

详细控件说明：

```text
docs/strategy_configurator_frontend_controls.md
```

### 后端 dry-run 服务

文件：

```text
scripts/hedged_ladder_server.py
scripts/run_hedged_ladder_server.sh
```

功能：

- 本地 HTTP API。
- 接收前端策略 plan。
- 创建策略组。
- 保存 L0-L10 订单计划。
- dry-run 下把 L0 LONG/SHORT 标记为模拟成交。
- 确认 LONG 后只激活 LONG_1-LONG_10。
- 确认 SHORT 后只激活 SHORT_1-SHORT_10。
- 支持关闭策略组。
- SQLite 落库。

数据库：

```text
hedged-ladder.db
```

该数据库是本地运行产物，不推 GitHub。

### 后端接口

```text
GET  /api/strategy/health
POST /api/strategy/start
POST /api/strategy/confirm
POST /api/strategy/close
GET  /api/strategy/groups
GET  /api/strategy/groups/{id}
```

默认地址：

```text
http://127.0.0.1:8787
```

## 3. 如何在新电脑启动

### 安装依赖

```bash
git clone https://github.com/RollingScallop/accumulation-radar.git
cd accumulation-radar
pip install -r requirements.txt
```

### 启动后端

```bash
bash scripts/run_hedged_ladder_server.sh
```

健康检查：

```bash
curl http://127.0.0.1:8787/api/strategy/health
```

### 打开前端

方式一，直接打开文件：

```text
web/strategy_configurator.html
```

方式二，用本地静态服务：

```bash
python3 -m http.server 8765
```

然后打开：

```text
http://127.0.0.1:8765/web/strategy_configurator.html
```

## 4. 推荐测试流程

1. 打开前端页面。
2. 点击“回测模板”。
3. 从雷达池选择一个币。
4. 点击“刷新现价”。
5. 确认 L0-L10 价格和数量已更新。
6. 确认 `dry-run` 开启。
7. 点击“启动”。
8. 后端返回策略组 ID。
9. 点击“确认多头”或“确认空头”。
10. 点击“关闭策略组”。

查看后端记录：

```bash
sqlite3 hedged-ladder.db \
  "select id,symbol,status,dry_run,created_at from hedged_ladder_groups order by created_at desc limit 5;"
```

## 5. 当前没有真实下单

当前系统只使用 Binance 公开行情接口：

```text
https://fapi.binance.com/fapi/v1/ticker/24hr
https://fapi.binance.com/fapi/v1/exchangeInfo
https://fapi.binance.com/fapi/v1/premiumIndex
```

还没有接 Binance 私有交易 API。

前端点击“启动”不会产生真实仓位，只会：

- 创建策略组。
- 落库。
- L0 标记为 `DRY_RUN_FILLED`。
- L1-L10 标记为 `WAITING_TREND_CONFIRM`。

真实下单需要后续实现 Binance Futures 私有 API 签名、账户校验、下单、撤单和平仓。

## 6. 真实 API 接入建议顺序

1. API key/secret 配置读取。
2. 签名请求封装。
3. 查询账户双向持仓模式。
4. 查询/设置杠杆。
5. 查询/设置逐仓或全仓。
6. L0 小额真实市价对冲。
7. L0 单边失败后的自动回滚。
8. 人工确认方向后挂 L1-L10 趋势侧订单。
9. 对冲腿 10% 自动止损。
10. 策略组关闭时撤单 + 平仓。
11. 动态止盈和分批止盈。

## 7. 重要文件

```text
README.md
docs/trend_breakout_ladder_strategy.md
docs/trend_ladder_execution_backend_plan.md
docs/strategy_configurator_frontend_controls.md
docs/hedged_ladder_handoff.md
web/strategy_configurator.html
scripts/hedged_ladder_server.py
scripts/run_hedged_ladder_server.sh
scripts/export_radar_watchlist.py
scripts/backtest_trend_ladder.py
scripts/simulate_hedged_ladder.py
```

## 8. 不应提交到 GitHub 的文件

```text
.env*
*.db
*.db-shm
*.db-wal
*.log
本地状态 JSON
运行 pid
```

这些已加入 `.gitignore`。

