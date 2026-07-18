# CrossHedge 原生交易模块规范

更新时间：2026-07-18

## 1. 目标与范围

交易模块由项目自身实现，当前只允许 Hyperliquid、MT5、Binance Futures。目标是让账户、行情、交易和订单生命周期通过稳定的领域协议组合，后续新增交易所只扩展连接器，不改业务状态机。

本规范覆盖：

1. 账户、余额、持仓、挂单和成交读取。
2. 品种费率、资金费、最小数量、步进与订单簿维护。
3. API Key、签名身份、账户环境和交易权限校验。
4. 独立 Paper 与 Live 执行。
5. 低延迟订单事件、重连、兜底查询和完整订单生命周期。

## 2. 模块边界

### 2.1 领域层

`venues/domain` 只包含不可变 Decimal 模型和枚举：

- AccountSnapshot / Balance / Position
- Instrument / Ticker / OrderBookSnapshot
- OrderRequest / OrderSnapshot / Fill
- VenueEvent / OrderStatus / PositionSide / TimeInForce

领域层不得导入数据库、HTTP SDK 或具体交易所模块。

### 2.2 协议层

`VenueConnector` 用多个窄协议组合账户、市场数据、凭据校验、交易和事件能力。连接器必须声明能力，不允许调用方通过交易所名称猜测功能。

### 2.3 生命周期管理

`NativeVenueManager` 按 `venue + mode + credential identity` 缓存连接器。凭据或映射更新会失效缓存；API 进程维护公共行情，执行 Worker 维护私有事件和交易副作用。

### 2.4 Paper 隔离

PaperConnector 不继承任何 Live 连接器。它可以消费 Live 行情缓存，但拥有独立账户、订单、成交、延迟和费率模型。Live 连接器的凭据、网络错误和状态不可泄漏到 Paper。

## 3. 交易所职责

| 能力 | Hyperliquid | Binance Futures | MT5 |
|---|---|---|---|
| 账户/持仓 | Info API | 签名 REST | terminal API |
| 公共行情 | l2Book WS | bookTicker + depth WS | symbol tick/book |
| 私有订单事件 | orderUpdates/userFills WS | User Data Stream | 活动订单轮询 |
| 实盘提交 | 官方 Exchange SDK | 项目 HMAC REST | order_send |
| 幂等标识 | 确定性 cloid | newClientOrderId | magic/comment + ticket |
| 断线兜底 | orderStatus/userFills | query order/userTrades | history orders/deals |

## 4. 订单真相与状态机

交易所事件是原始交易事实，数据库是业务投影的持久化真相源，内存缓存只用于加速读取。

状态集合：

```text
CREATED -> SUBMITTING -> SUBMITTED -> ACCEPTED
                                  -> PARTIALLY_FILLED -> FILLED
                                  -> PENDING_CANCEL -> CANCELED
                                  -> REJECTED / EXPIRED / UNKNOWN
```

规则：

- 稳定 ClientOrderId 必须先于外部调用落库。
- 超时、连接中断和 HTTP 5xx 均为 UNKNOWN，不代表失败。
- UNKNOWN 只能查询恢复，不能自动用新 ID 重发。
- Fill 必须来自成交事件、成交历史或明确累计成交差值。
- 订单事件用交易所 event/trade ID 幂等；没有 ID 时使用规范化载荷哈希。
- 部分成交的累计量、均价和佣金只能单调前进。
- 撤单请求成功不等于撤单终态，市价兜底必须等待 CANCELED 或其他明确终态。

## 5. 低延迟确认策略

### Binance

私有 WebSocket 是主路径。listen key 定时续期，连接断开后自动重建；`ORDER_TRADE_UPDATE` 负责 ACK/状态，`TRADE_LITE` 或成交字段负责 Fill。REST 查询只用于恢复。

### Hyperliquid

`orderUpdates` 与 `userFills` 是主路径。cloid 让重启后的订单仍可定位；重复 Fill 按 tid 去重。

### MT5

终端没有同等的账户私有 WS，因此采用只轮询活动订单的 75ms 默认周期。没有活动订单时不轮询历史；发现终态后一次性读取 deals 并按 ticket 去重。这比全账户固定轮询延迟更低、负载更小。

## 6. 订单簿一致性

Binance 本地簿必须：

1. 缓存增量事件。
2. 拉取 REST 快照。
3. 丢弃 `u <= lastUpdateId` 的事件。
4. 第一条应用事件满足覆盖快照序号。
5. 后续严格校验 `pu == previous_u`。
6. 缺口时标记失步、停止发布并重新同步。

Hyperliquid 使用交易所完整 l2Book 更新。MT5 使用 terminal market book；不可用时只发布 ticker，不伪造 Live 深度。

## 7. 品种与成本刷新

Instrument 至少包含：数量步进、最小数量、价格 tick、最小名义额、合约乘数、费率、资金费或 swap 和交易状态。

- 启动时按启用映射加载。
- 默认每 6 小时强制刷新。
- Binance 账户级 commission 覆盖公开或默认费率。
- 当前 funding 可高频缓存；历史 funding 使用公开历史端点。
- MT5 读取 volume_min、volume_step、trade_contract_size、swap mode/long/short。

## 8. 凭据校验

校验返回结构化 CredentialCheck：

- `valid`：所有阻塞项是否通过。
- `can_read`：账户数据是否可读。
- `can_trade`：签名身份有效、非只读且交易接口可用。
- `items`：每项名称、结果、说明和是否阻塞。

禁止通过真实下单测试普通凭据。交易权限校验使用官方只读端点、签名校验或最小风险的权限查询；真实 Probe 必须由用户显式确认。

## 9. 进程与故障边界

- API 进程：公共行情、扫描、配置和查询。
- 执行 Worker：Outbox、Live 下单、私有事件和订单恢复。
- WS 回调只复制不可变事件并写入无阻塞队列，不做数据库 I/O。
- Worker 以 50ms 周期处理队列和 Outbox；事件到达会在下一周期投影。
- `/health` 暴露每个连接器环境、只读状态、公共/私有连接和订单簿同步状态。

## 10. 新交易所接入清单

1. 在 `venues/<name>` 实现 REST/SDK、WS、映射器和 Connector。
2. 实现统一协议并声明能力。
3. 使用 Decimal，明确 symbol、PositionSide 和数量单位。
4. 实现稳定 ClientOrderId 与结果未知恢复。
5. 实现公共行情订阅、订单簿重同步和私有事件去重。
6. 实现凭据校验、费率和品种规格刷新。
7. 注册到 manager 与设置页支持列表。
8. 增加确定性单元测试、断线恢复测试和 Testnet/Demo 验收。

业务层不得以 `if venue == ...` 新增下单、查询或状态转换逻辑；差异必须封装在连接器或能力声明中。

## 11. 验收门槛

- 全量单元测试通过。
- 仓库中不存在被移除运行时的依赖、导入、配置或文档说明。
- Binance Testnet 验证 ACK、部分成交、Fill、撤单、重连和订单簿断档恢复。
- Hyperliquid Testnet 验证 cloid、orderUpdates/userFills 和重启恢复。
- MT5 Demo 验证 Market/Pending、部分成交、撤单、双向持仓与 75ms 活动轮询。
- 任意提交超时场景均无重复下单。
- Paper 在无实盘凭据时可独立完成订单生命周期。
