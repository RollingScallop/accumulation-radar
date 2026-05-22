# 趋势对冲阶梯交易后端规划

## 1. 前后端职责

前端负责：

- 选择雷达池代币或手动输入 symbol。
- 拉取现价并按现价生成 L0-L10 订单计划。
- 保存和导出策略 JSON。
- 点击启动后，把完整 plan 提交给后端。

后端负责：

- 校验策略 plan。
- 校验 Binance 账户处于 Hedge Mode。
- 设置杠杆、逐仓/全仓。
- 启动时同时提交 L0 LONG 与 L0 SHORT 市价单。
- 记录 strategy_group、订单、成交、仓位和状态。
- 等待人工或行情确认趋势方向后，只执行 L1-L10 的趋势侧订单。
- 管理对冲腿止损、趋势腿止盈、联动平仓和撤单。

## 2. 前端启动接口

```http
POST /api/strategy/start
Content-Type: application/json
```

请求体：

```json
{
  "action": "start_hedged_trend_ladder",
  "dryRun": true,
  "requestedAt": "2026-05-21T00:00:00.000Z",
  "plan": {
    "config": {},
    "market": {},
    "probeLayer": {},
    "trendLayers": [],
    "layers": []
  }
}
```

后端返回：

```json
{
  "ok": true,
  "strategyGroupId": "HTL_BABYUSDT_20260521_000001",
  "status": "HEDGED_PROBE_OPENED",
  "dryRun": true,
  "acceptedOrders": []
}
```

## 3. 订单层定义

### L0：启动市价对冲

启动按钮只负责 L0：

```text
LONG_0  Market Buy   positionSide=LONG
SHORT_0 Market Sell  positionSide=SHORT
```

L0 可调参数：

- 单边名义金额
- 杠杆
- margin mode
- 对冲腿止损百分比
- 是否 dry-run

L0 不参与趋势阶梯金额 `totalNotional`。

### L1-L10：趋势确认后执行

L1-L10 只在趋势方向确认后执行：

```text
确认多头：只提交 LONG_1 ... LONG_10
确认空头：只提交 SHORT_1 ... SHORT_10
```

反向方向的 L1-L10 不下单。

## 4. 后端状态机

```text
CREATED
VALIDATED
HEDGED_PROBE_OPENING
HEDGED_PROBE_OPENED
WAITING_TREND_CONFIRM
LONG_ACTIVE
SHORT_ACTIVE
HEDGE_LEG_CLOSED
PROFIT_TAKING
CLOSING_GROUP
COMPLETED
FAILED
ABORTED
```

关键规则：

- `HEDGED_PROBE_OPENED` 后，必须同时存在 LONG_0 与 SHORT_0。
- 未确认方向前，不允许提交 L1-L10。
- 确认方向后，只允许提交趋势侧订单。
- 对冲腿达到 10% 亏损时，平掉对冲腿。
- 趋势腿退出时，剩余对冲腿和未成交订单必须一起处理。

## 5. 建议数据库表

### strategy_groups

- id
- symbol
- status
- dry_run
- timeframe
- leverage
- margin_mode
- reference_price
- hedge_stop_pct
- created_at
- updated_at
- raw_plan_json

### strategy_orders

- id
- strategy_group_id
- layer_index
- side: LONG / SHORT
- role: PROBE / TREND
- order_type
- planned_price
- notional
- margin_used
- quantity
- binance_order_id
- status
- created_at
- filled_at
- raw_response_json

### strategy_positions

- strategy_group_id
- symbol
- position_side
- entry_qty
- entry_avg_price
- realized_pnl
- unrealized_pnl
- hedge_closed
- updated_at

### strategy_events

- strategy_group_id
- event_type
- message
- payload_json
- created_at

## 6. Binance 执行注意

后端启动前必须校验：

- `dualSidePosition = true`
- symbol 是 USDT 永续可交易合约
- leverage 设置成功
- margin mode 设置成功或已是目标状态
- notional 满足最小下单金额
- quantity 满足 stepSize
- price 满足 tickSize

L0 市价单示例：

```text
POST /fapi/v1/order
symbol=BABYUSDT
side=BUY
positionSide=LONG
type=MARKET
quantity=...

POST /fapi/v1/order
symbol=BABYUSDT
side=SELL
positionSide=SHORT
type=MARKET
quantity=...
```

两个 L0 订单必须属于同一个 `strategyGroupId`。如果其中一边失败，后端必须立刻回滚/平掉已成交的一边。

## 7. 第一版实现顺序

1. 做 `POST /api/strategy/start`，只 dry-run，落库保存 plan 和预估订单。
2. 加 Binance 账户校验，不真实下单。
3. 接入 L0 双向市价单，先小额实盘。
4. 加策略组监控，能看到 LONG_0 / SHORT_0 成交状态。
5. 加人工确认趋势方向接口。
6. 接入 L1-L10 趋势侧下单。
7. 接入对冲腿 10% 止损。
8. 接入动态止盈和策略组联动平仓。
