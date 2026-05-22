# 趋势对冲阶梯配置台前端控件说明

本文档解释页面 `web/strategy_configurator.html` 里所有按钮、输入框、下拉选项，以及它们对应的计算逻辑和后端挂钩关系。

当前页面的核心结构是：

```text
L0      启动市价对冲层：LONG_0 + SHORT_0 同时市价开
L1-L10  趋势阶梯层：确认多头后只激活 LONG_1-LONG_10；确认空头后只激活 SHORT_1-SHORT_10
```

## 1. 顶部按钮

| 按钮 | 前端 id | 作用 | 挂钩逻辑 |
| --- | --- | --- | --- |
| 读取 | `loadBtn` | 从浏览器 `localStorage` 读取之前保存的方案 | 调用 `loadPlan()`，恢复 `config` 和 `layers` |
| 保存方案 | `saveBtn` | 保存当前页面所有参数 | 调用 `savePlan()`，写入 `localStorage`，不触发后端 |
| 导出 JSON | `exportBtn` | 导出当前完整策略 JSON | 调用 `exportPlan()`，下载 `{symbol}-trend-ladder-plan.json` |
| 导入 JSON | `importBtn` | 粘贴 JSON 后恢复方案 | 调用 `importPlan()`，要求 JSON 内有 `config` 和 `layers` |
| 回测模板 | `backtestTemplateBtn` | 一键套用当前回测默认模板 | 调用 `applyBacktestTemplate()`，重置 1h、10 层、1000U、L0=220U、dry-run 等 |
| 刷新现价 | `refreshMarketBtn` | 从 Binance USDT 永续公开接口拉现价和精度 | 调用 `refreshMarket()`，更新参考价格并按现价重算 L0-L10 |
| 启动 | `startStrategyBtn` | 提交当前方案给后端启动接口 | 调用 `startStrategy()`，POST 到 `backendStartUrl` |

## 2. 基础参数

### 方案名称

- 前端 id：`planName`
- 默认值：`1H 手动趋势对冲阶梯`
- JSON 位置：`plan.config.planName`
- 作用：仅用于识别方案，不参与计算。

### 雷达池下拉

- 前端 id：`poolSymbol`
- 数据来源：
  - 优先读取 `web/radar_watchlist.json`
  - 如果页面用 `file://` 打开，则使用 `web/radar_watchlist_embed.js`
- 作用：
  - 选择某个币后写入 `symbol`
  - 自动调用 `refreshMarket({ syncOrders: true })`
  - 拉取现价后重算 L0-L10 价格、数量、保证金

### 交易标的

- 前端 id：`symbol`
- 默认值：`BTCUSDT`
- JSON 位置：`plan.config.symbol`
- 后端用途：
  - 后端校验合约是否可交易
  - 后端按 symbol 查询交易规则
  - 后端生成 `strategyGroupId`

输入会自动转成大写，并去掉非字母数字字符。

### 参考价格

- 前端 id：`referencePrice`
- JSON 位置：`plan.config.referencePrice`
- 作用：
  - L0 市价对冲参考价
  - L1-L10 按百分比推导多空挂单价格

计算方式：

```text
L0 longPrice  = referencePrice
L0 shortPrice = referencePrice

L1-L10 longPrice  = referencePrice * (1 + offsetPct)
L1-L10 shortPrice = referencePrice * (1 - offsetPct)
```

如果点击“刷新现价”，该字段会被最新现价覆盖。

### K 线级别

- 前端 id：`timeframe`
- JSON 位置：`plan.config.timeframe`
- 可选项：
  - `1h`
  - `4h`
  - `1d`

当前后端只记录该字段；趋势确认逻辑暂时由前端按钮“确认多头/确认空头”人工触发。后续自动确认时会使用这个级别读取 K 线。

## 3. 账户与仓位参数

### 账户模式

- 前端 id：`positionMode`
- JSON 位置：`plan.config.positionMode`
- 可选项：
  - `hedge`：双向持仓
  - `oneway`：单向持仓

后端校验：

```text
positionMode 必须是 hedge
```

因为 L0 必须同时开 `LONG_0 + SHORT_0`。

### 保证金模式

- 前端 id：`marginMode`
- JSON 位置：`plan.config.marginMode`
- 可选项：
  - `isolated`：逐仓
  - `cross`：全仓

当前后端记录该字段。实盘阶段会用它设置 Binance margin type。

### 杠杆

- 前端 id：`leverage`
- 默认值：`3`
- 范围：1-125
- JSON 位置：`plan.config.leverage`
- 影响计算：

```text
每层占用保证金 = 每层名义金额 / leverage
```

后端用途：

- 校验必须大于 0
- 实盘阶段用于设置 Binance 杠杆

### 最多层数

- 前端 id：`maxLayers`
- 默认值：`10`
- 范围：1-10
- JSON 位置：`plan.config.maxLayers`
- 作用：
  - 只影响趋势层 L1-L10
  - 不影响 L0
  - 修改后调用 `recalcNotional()`

例如 `maxLayers=5`，页面会启用：

```text
L0 + L1-L5
```

## 4. 实时行情显示

这些字段只显示，不直接手填。

| 显示项 | 前端 id | 来源 |
| --- | --- | --- |
| Binance 现价 | `livePriceText` | `/fapi/v1/ticker/24hr`，失败时 fallback 到 `/fapi/v1/premiumIndex` |
| 24h 涨跌 | `change24hText` | ticker 的 `priceChangePercent` |
| 价格 / 数量精度 | `precisionText` | `/fapi/v1/exchangeInfo` |
| 数据状态 | `marketStatusText` | 前端刷新状态 |

如果交易所精度获取失败，页面会使用当前价格自动推断价格精度，避免低价币挂单价格被取整成 0。

## 5. 风险与金额参数

### 趋势总名义金额 USDT

- 前端 id：`totalNotional`
- 默认值：`1000`
- JSON 位置：`plan.config.totalNotional`
- 作用：
  - 只分配给 L1-L10 趋势层
  - 不包含 L0 对冲层

默认权重：

```text
22%, 18%, 15%, 12%, 10%, 8%, 6%, 4%, 3%, 2%
```

如果总金额为 1000U，则 L1-L10 默认是：

```text
L1  220U
L2  180U
L3  150U
L4  120U
L5  100U
L6   80U
L7   60U
L8   40U
L9   30U
L10  20U
```

### 第0层单边对冲金额 USDT

- 前端 id：`probeNotionalInput`
- 默认值：`220`
- JSON 位置：`plan.config.probeNotionalInput`
- 对应层：`L0`
- 作用：
  - 启动时 `LONG_0` 的名义金额
  - 启动时 `SHORT_0` 的名义金额

如果填 220U，启动时计划是：

```text
LONG_0  220U 市价
SHORT_0 220U 市价
```

页面摘要里的“首组对冲”显示的是两边合计：

```text
首组对冲 = probeNotionalInput * 2
```

### 对冲腿止损 %

- 前端 id：`hedgeStopPct`
- 默认值：`10`
- JSON 位置：`plan.config.hedgeStopPct`
- 后端记录字段：`hedge_stop_pct`
- 规则含义：
  - 确认多头后，`SHORT_0` 亏损达到该比例时平掉
  - 确认空头后，`LONG_0` 亏损达到该比例时平掉

当前后端第一版只记录该参数，自动监控止损还未接入。

### 策略组最大净亏损 USDT

- 前端 id：`maxGroupLoss`
- 默认值：`100`
- JSON 位置：`plan.config.maxGroupLoss`
- 作用：
  - 未来用于策略组净值风控
  - 当前后端第一版只保存，不自动触发

### 手续费 + 滑点 %

- 前端 id：`feeSlipPct`
- 默认值：`0.12`
- JSON 位置：`plan.config.feeSlipPct`
- 作用：
  - 用于预估真实执行损耗
  - 当前前端/后端第一版不参与下单数量计算

## 6. 趋势确认与退出参数

### 方向确认方式

- 前端 id：`confirmMode`
- JSON 位置：`plan.config.confirmMode`
- 可选项：
  - `manual`：手动确认后只加趋势边
  - `close_outside`：收盘突破区间后确认
  - `two_close_outside`：连续两根收盘在区间外

当前状态：

- 前端已有“确认多头/确认空头”按钮。
- 后端已有 `/api/strategy/confirm`。
- 自动 K 线确认还未接入。

### 浮盈加仓锁利方式

- 前端 id：`profitLockMode`
- JSON 位置：`plan.config.profitLockMode`
- 可选项：
  - `trail_group`：策略组浮盈回撤锁利
  - `move_stop_to_cost`：回本线 + 对冲亏损补偿
  - `partial_take_profit`：分批止盈后移动保护线

当前后端第一版只记录该字段；动态止盈执行器后续接入。

### 趋势腿退出时

- 前端 id：`exitMode`
- JSON 位置：`plan.config.exitMode`
- 可选项：
  - `close_group`：趋势腿与剩余对冲腿一起平
  - `close_trend_only`：只平趋势腿

建议默认使用 `close_group`，因为你的核心规则是趋势退出时不能留下孤立对冲腿。

### 动态止盈启动 %

- 前端 id：`takeProfitStartPct`
- 默认值：`8`
- JSON 位置：`plan.config.takeProfitStartPct`
- 作用：未来动态止盈模块开始工作的最低浮盈阈值。

### 回撤锁利 %

- 前端 id：`trailGivebackPct`
- 默认值：`3`
- JSON 位置：`plan.config.trailGivebackPct`
- 作用：未来用于浮盈回撤保护。

### 满 10 层后分批止盈 %

- 前端 id：`fullPositionTakePct`
- 默认值：`25`
- JSON 位置：`plan.config.fullPositionTakePct`
- 作用：未来用于 L1-L10 全部打满后，开始分批止盈的比例。

## 7. 后端与确认参数

### 后端启动接口

- 前端 id：`backendStartUrl`
- 默认值：`http://127.0.0.1:8787/api/strategy/start`
- 作用：
  - “启动”按钮会 POST 到这里
  - “确认多头/确认空头/关闭策略组”会基于这个地址推导后端 base url

推导逻辑：

```text
backendStartUrl = http://127.0.0.1:8787/api/strategy/start
baseUrl         = http://127.0.0.1:8787/api/strategy
确认接口        = baseUrl + /confirm
关闭接口        = baseUrl + /close
```

### 确认突破 ATR buffer

- 前端 id：`confirmBufferAtr`
- 默认值：`0.20`
- JSON 位置：`plan.config.confirmBufferAtr`
- 作用：未来自动确认趋势时使用。

含义示例：

```text
多头确认价 = 区间上沿 + ATR * confirmBufferAtr
空头确认价 = 区间下沿 - ATR * confirmBufferAtr
```

### 确认量能倍数

- 前端 id：`confirmVolumeMult`
- 默认值：`1.20`
- JSON 位置：`plan.config.confirmVolumeMult`
- 作用：未来自动确认趋势时使用，要求突破 K 线量能达到近期均量的倍数。

### 启动价格来源

- 前端 id：`priceSource`
- JSON 位置：`plan.config.priceSource`
- 可选项：
  - `live`：按现价自动生成
  - `manual`：使用手动参考价

实际注意：

- 点击“刷新现价”时，会强制使用最新现价重算挂单。
- 手动改 `referencePrice` 时，也会重算 L0-L10。

## 8. 开关选项

### 启动时同时市价开 LONG_0 + SHORT_0

- 前端 id：`openProbeHedge`
- 默认：开启
- JSON 位置：`plan.config.openProbeHedge`
- 后端规则：
  - 开启时，后端要求 L0 存在且启用。
  - 启动后创建 `LONG_0` 和 `SHORT_0` 两条 PROBE 订单。

### 确认方向后不再增加反向单

- 前端 id：`cancelOppositeAfterConfirm`
- 默认：开启
- JSON 位置：`plan.config.cancelOppositeAfterConfirm`
- 当前后端行为：
  - 确认 LONG 后，LONG 趋势层变成 `DRY_RUN_PLACED`
  - SHORT 趋势层变成 `CANCELLED_OPPOSITE_SIDE`
  - 确认 SHORT 则反过来

### 趋势退出时同步平掉剩余对冲腿

- 前端 id：`closeHedgeWithTrendExit`
- 默认：开启
- JSON 位置：`plan.config.closeHedgeWithTrendExit`
- 策略含义：避免趋势腿退出后留下孤立对冲腿。

### 平仓单使用 reduceOnly

- 前端 id：`useReduceOnlyExit`
- 默认：开启
- JSON 位置：`plan.config.useReduceOnlyExit`
- 实盘阶段用途：平仓订单加 `reduceOnly`，避免平仓失败后反向开仓。

### 启动先走 dry-run

- 前端 id：`dryRunStart`
- 默认：开启
- JSON 位置：
  - `plan.config.dryRunStart`
  - 请求顶层 `dryRun`
- 后端规则：
  - `dryRun=true`：只落库模拟，不真实下单。
  - `dryRun=false`：后端仍会检查环境变量 `HEDGED_LADDER_ALLOW_LIVE=true`，否则拒绝实盘。

## 9. 每层挂单参数

表格由 `buildRows()` 自动生成，共 11 行：

```text
L0, L1, L2, ..., L10
```

### 启用

- class：`layer-enabled`
- JSON 位置：`layer.enabled`
- 作用：
  - L0 关闭时，不启动市价对冲。
  - L1-L10 关闭时，该趋势层不参与计划。

### 层

- 显示值：`0` 到 `10`
- JSON 位置：`layer.index`

层定义：

```text
0     市价对冲层
1-10  趋势加仓层
```

### 角色

- class：`layer-role`
- JSON 位置：`layer.role`
- 可选项：
  - `probe`：市价对冲
  - `trend`：趋势加仓

建议：

```text
L0 固定 probe
L1-L10 固定 trend
```

### 订单类型

- class：`order-type`
- JSON 位置：`layer.orderType`
- 可选项：
  - `market`
  - `limit`
  - `stop_limit`

默认：

```text
L0 = market
L1-L10 = limit
```

当前 dry-run 后端：

- L0 会被记录为 `MARKET`
- L1-L10 会按页面选择记录
- 实盘 limit / stop_limit 细节后续接入 Binance 执行器

### 价格间隔 %

- class：`offset-pct`
- JSON 位置：`layer.offsetPct`
- L0：固定按 0 处理
- L1-L10：用于从参考价推导多空价格

默认 L1-L10 间隔：

```text
1.2%, 2.5%, 4%, 5.8%, 7.8%, 10%, 12.5%, 15%, 18%, 21%
```

计算：

```text
longPrice  = referencePrice * (1 + offsetPct / 100)
shortPrice = referencePrice * (1 - offsetPct / 100)
```

### 多单价格

- class：`long-price`
- JSON 位置：`layer.longPrice`
- 自动计算，可手动改。
- 后端生成 LONG 订单时使用。

### 空单价格

- class：`short-price`
- JSON 位置：`layer.shortPrice`
- 自动计算，可手动改。
- 后端生成 SHORT 订单时使用。

### 金额 USDT

- class：`notional`
- JSON 位置：`layer.notional`
- 含义：该层单边名义金额。

例如 L3 金额 150U：

```text
LONG_3  150U
SHORT_3 150U
```

但确认方向后只会激活其中一边。

### 占用保证金

- class：`margin-used`
- JSON 位置：`layer.marginUsed`
- 只读，自动计算：

```text
marginUsed = notional / leverage
```

### 多单数量

- class：`long-qty`
- JSON 位置：`layer.longQty`
- 自动计算：

```text
longQty = notional / longPrice
```

会按 Binance 数量精度向下取整。

### 空单数量

- class：`short-qty`
- JSON 位置：`layer.shortQty`
- 自动计算：

```text
shortQty = notional / shortPrice
```

会按 Binance 数量精度向下取整。

### 止盈动作

- class：`take-action`
- JSON 位置：`layer.takeAction`
- 可选项：
  - `hold`：继续持有
  - `partial`：允许分批止盈
  - `watch_top`：满仓重点盯 K

当前后端只记录，后续动态止盈模块会使用。

### 备注

- class：`layer-note`
- JSON 位置：`layer.note`
- 仅用于人工说明和后端记录。

## 10. 摘要区显示项

| 显示项 | 前端 id | 逻辑 |
| --- | --- | --- |
| 启用层数 | `enabledLayers` | 显示 `L0 + 趋势层数量` |
| 趋势金额 | `trendNotional` | 启用的 L1-L10 名义金额合计 |
| 首组对冲 | `probeNotional` | L0 单边金额 * 2 |
| 对冲止损 | `hedgeStopText` | `hedgeStopPct` |
| 预计最大保证金 | `marginUsedText` | `(趋势总金额 + L0 单边金额) / leverage` |
| 当前参考价 | `referencePriceText` | 当前 `referencePrice` |
| 配置校验 | `validationText` | 前端校验结果 |
| 保存状态 | `dirtyText` | 是否有未保存修改 |
| 启动状态 | `startStatusText` | 后端启动/确认/关闭结果 |
| 启动模式 | `startModeText` | `dry-run` 或 `live` |

## 11. 摘要区按钮

### 确认多头

- 前端 id：`confirmLongBtn`
- 调用：`confirmTrend("LONG")`
- 后端接口：`POST /api/strategy/confirm`
- 请求：

```json
{
  "strategyGroupId": "HTL_xxx",
  "direction": "LONG"
}
```

后端行为：

```text
LONG_1-LONG_10  => DRY_RUN_PLACED
SHORT_1-SHORT_10 => CANCELLED_OPPOSITE_SIDE
```

### 确认空头

- 前端 id：`confirmShortBtn`
- 调用：`confirmTrend("SHORT")`
- 后端接口：`POST /api/strategy/confirm`

后端行为：

```text
SHORT_1-SHORT_10 => DRY_RUN_PLACED
LONG_1-LONG_10   => CANCELLED_OPPOSITE_SIDE
```

### 关闭策略组

- 前端 id：`closeGroupBtn`
- 调用：`closeCurrentGroup()`
- 后端接口：`POST /api/strategy/close`
- 用途：
  - dry-run 中把策略组标为 `COMPLETED`
  - 未来实盘中用于联动撤单和平仓

## 12. 挂单预览

- 容器 id：`previewList`
- 逻辑：
  - L0 显示 `LONG_0 / PROBE` 和 `SHORT_0 / PROBE`
  - L1-L10 显示 `TREND_ONLY_AFTER_CONFIRM`

预览不代表真实下单状态，只是当前 plan 的本地计算结果。

## 13. 方案 JSON

- 容器 id：`jsonBox`
- 内容来自 `getPlan()`

核心结构：

```json
{
  "version": 1,
  "savedAt": "...",
  "config": {},
  "market": {},
  "layers": [],
  "probeLayer": {},
  "trendLayers": [],
  "executionGuardrails": {}
}
```

重要字段：

```text
probeLayer   L0 市价对冲层
trendLayers  L1-L10 趋势层
layers       全部 L0-L10
```

## 14. JSON 操作按钮

### 复制

- 前端 id：`copyBtn`
- 调用：`copyPlan()`
- 作用：复制完整策略 JSON 到剪贴板。

### 清空保存

- 前端 id：`clearBtn`
- 作用：清空浏览器 `localStorage` 里的保存方案。
- 不会删除后端数据库记录。

## 15. 后端接口对应关系

| 前端动作 | 后端接口 | 后端结果 |
| --- | --- | --- |
| 启动 | `POST /api/strategy/start` | 创建策略组，记录 L0-L10，dry-run 下 L0 标为 `DRY_RUN_FILLED` |
| 确认多头 | `POST /api/strategy/confirm` | 策略组变成 `LONG_ACTIVE` |
| 确认空头 | `POST /api/strategy/confirm` | 策略组变成 `SHORT_ACTIVE` |
| 关闭策略组 | `POST /api/strategy/close` | 策略组变成 `COMPLETED` |

后端数据库：

```text
hedged-ladder.db
```

主要表：

```text
hedged_ladder_groups
hedged_ladder_orders
hedged_ladder_events
```

## 16. 当前已实现与未实现

已实现：

- 前端参数填写
- 雷达池选币
- 现价刷新
- L0-L10 自动计算
- JSON 保存/导入/导出
- dry-run 启动
- dry-run 确认多空
- dry-run 关闭策略组
- SQLite 落库

未实现：

- Binance 私有 API 真实下单
- 自动读取 K 线确认趋势
- 对冲腿 10% 自动止损
- 动态止盈/分批止盈
- 策略组实时持仓监控
- 异常成交后的自动回滚和平仓

