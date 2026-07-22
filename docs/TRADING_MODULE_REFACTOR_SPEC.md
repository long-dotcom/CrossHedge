# CrossHedge 原生交易模块规范

更新时间：2026-07-18

## 1. 目标与范围

交易模块由项目自身实现，当前只允许 Hyperliquid、MT5、Binance Futures。目标是让账户、行情、交易和订单生命周期通过稳定的领域协议组合，后续新增交易所只扩展连接器，不改业务状态机。

本规范覆盖：

1. 账户、余额、持仓、挂单和成交读取。
2. 品种交易手续费、最小数量、步进与 BBO 行情维护。
3. API Key、签名身份、账户环境和交易权限校验。
4. 混合 Paper（加密真实最小探针 + MT5 Demo）与 Live 执行。
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

### 2.4 Paper 执行边界

Paper 不使用本地撮合结果。加密腿由 `HybridPaperProbeConnector` 调用 Live 连接器提交真实最小探针并立即回平，MT5 腿只调用 Gateway Demo 连接器。探针与 Live 策略订单使用不同稳定 ClientOrderId，并受独立总开关、空仓专用账户、分布式锁、名义额/次数限额及恢复状态约束。

### 2.5 异常组作废归档

对冲组不得物理删除。管理员可以对确认无外部敞口的异常组执行软作废：服务端必须拒绝包含真实成交、状态未知订单或未安全回平 ProbeRun 的请求；通过检查后将活动 Intent 标记为 `VOIDED`、未提交腿/订单标记为取消、组标记为 `voided`，同时保留全部订单、Fill、执行事件和审计日志。列表默认隐藏 `voided`，但允许显式查看归档记录。

## 3. 交易所职责

| 能力 | Hyperliquid | Binance Futures | MT5 |
|---|---|---|---|
| 账户/持仓 | Info API | 签名 REST | terminal API |
| 公共行情 | bbo WS | bookTicker WS | symbol tick |
| 私有订单事件 | orderUpdates/userFills WS | User Data Stream | 活动订单轮询 |
| 实盘提交 | 官方 Exchange SDK | 项目 HMAC REST | Redis Stream → Gateway → order_send |
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

私有 WebSocket 是 Paper 真实探针和 Live 的统一订单确认主路径。Hyperliquid 使用 `orderUpdates`/`userFills`/`clearinghouseState`；Binance listen key 每 30 分钟续期，连接断开后自动重建，`ORDER_TRADE_UPDATE` 负责 ACK/状态，`TRADE_LITE` 或成交字段负责 Fill，`ACCOUNT_UPDATE` 负责余额和持仓增量。账户及持仓同时写入数据库和 Redis。Maker TTL 由本地状态机触发撤单，正常运行不再固定间隔 REST 查单或同步持仓；REST 查询只允许用于 Worker 启动恢复、私有流重连后的单次缺口补偿和人工对账。

### Hyperliquid

`orderUpdates` 与 `userFills` 是主路径。cloid 让重启后的订单仍可定位；重复 Fill 按 tid 去重。

### MT5

MT5 Python 与 Terminal 只运行在 Windows Gateway。业务后端通过 Redis Stream 发送命令，并从 Redis 快照读取账户、持仓、行情和品种数据。终端没有同等的账户私有 WS，因此 Gateway 采用只轮询活动订单的 75ms 默认周期。没有活动订单时不轮询历史；发现终态后一次性读取 deals 并按 ticket 去重，然后把统一事件发布到 Redis Stream。

## 6. 行情边界

扫描链路仅维护两腿 BBO：Hyperliquid 使用 bbo WS，Binance 使用 bookTicker WS，MT5 使用 symbol tick。扫描器不订阅增量深度、不轮询 terminal market book，也不把完整订单簿写入 Redis。连接器仍可为真实执行探针等明确的单次操作按需查询订单簿，但该查询不得进入周期行情和扫描循环。

## 7. 品种与成本刷新

Instrument 的执行必需信息至少包含：数量步进、最小数量、价格 tick、最小名义额、合约乘数、Maker/Taker 手续费和交易状态。

- 启动时按启用映射加载。
- 默认每 6 小时强制刷新。
- Binance 账户级 commission 覆盖公开或默认费率。
- Funding/Swap 不进入扫描、执行成本或 PnL，也不由持仓维护任务同步。
- MT5 只读取执行所需的 volume_min、volume_step、trade_contract_size 等品种规格。

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
- Paper 在有效加密交易凭据和 MT5 Gateway Demo 会话下完成混合订单生命周期；无本地撮合回退。
