# CrossHedge

CrossHedge 是面向跨市场对冲交易的 FastAPI + React 应用。目前只支持三个原生交易场所：

- Hyperliquid
- MetaTrader 5
- Binance USDⓈ-M Futures

交易所接入由项目自身维护，不依赖第三方交易运行时。Paper 与 Live 共用统一领域模型和订单状态机，但 Paper 使用独立本地撮合引擎，绝不继承实盘连接器。

## 原生交易所架构

核心代码位于 `backend/app/venues`：

```text
venues/
├── domain/            # Decimal 领域模型、状态、事件、能力声明
├── paper/             # 独立 Paper 撮合引擎
├── binance/           # REST 签名、公共/私有 WS、增量订单簿、连接器
├── hyperliquid/       # Info/Exchange API、公共/私有 WS、连接器
├── mt5/               # MT5 终端调用、活跃订单轮询、连接器
├── protocols.py       # 窄接口协议
├── registry.py        # 可扩展连接器注册表
└── manager.py         # 长生命周期实例、订阅和凭据失效管理
```

统一连接器提供以下能力：

- 账户余额、可用余额、保证金与权益
- 当前持仓、活动挂单和订单查询
- 品种数量步进、最小数量、最小名义额、价格精度
- Maker/Taker 费率、资金费或 MT5 swap
- Ticker、L2 订单簿和动态订阅
- 凭据的结构、读权限、交易权限和环境校验
- 提交、撤销、查询订单及成交查询
- 私有订单/成交事件订阅与健康状态

新增交易所时实现 `VenueConnector` 的窄协议并注册到 manager；业务模块不得新增交易所专属分支。

## 订单生命周期

FastAPI 只创建不可变 Intent、ExecutionLeg 和 Outbox。独立执行 Worker 是唯一允许调用 `submit_order` 的业务进程：

1. 先持久化稳定 `client_order_id`。
2. 并行提交双腿命令。
3. Binance/Hyperliquid 以私有 WebSocket 订单和成交事件作为低延迟主路径。
4. MT5 只轮询活动订单，默认 75ms；成交单据按 ticket 去重。
5. 网络超时或 5xx 被视为“结果未知”，只能按稳定 ID 查询，禁止盲目重发。
6. REST、历史成交与账户快照仅用于断线、重启和漏事件兜底。
7. 原始事件按 event ID 幂等持久化，再投影 VenueOrder、Fill、Intent 和 HedgeGroup。

Maker-then-Market、部分成交、撤单确认、单腿失败、补偿和人工恢复继续由持久化状态机处理。订单没有真实成交事件或明确 `FILLED` 状态时不会生成 Fill。

## Binance 订单簿与私有流

- 公共 WS 订阅 `bookTicker` 与增量 depth。
- 本地订单簿按 `U/u/pu` 校验连续性；发现缺口立即丢弃局部状态并重新拉取快照。
- 私有用户流维护 listen key，定时续期并自动重连。
- `ORDER_TRADE_UPDATE` / `TRADE_LITE` 转换为统一订单和成交事件。
- Hedge Mode 使用明确 `LONG/SHORT` PositionSide；是否传 `reduceOnly` 由连接器能力规则决定。

## Hyperliquid 事件流

- 公共 WS 维护 `l2Book`。
- 执行 Worker 单独订阅 `orderUpdates` 和 `userFills`。
- 业务 ClientOrderId 映射为确定性的 16 字节 cloid。
- Fill 按交易 ID 去重；断线后使用订单状态和用户成交历史补齐。

## MT5 执行确认

MT5 没有与加密交易所完全等价的账户私有 WebSocket。连接器使用终端 API，并仅对活动订单进行高频轮询：

- 默认 `MT5_ORDER_POLL_INTERVAL_MS=75`。
- 市价与挂单依据 symbol 支持的 filling mode 选择请求参数。
- 平仓按 position ticket 定位，避免误平同品种另一方向仓位。
- 订单完成后查询 history deals，按 deal ticket 去重生成 Fill。

## Paper 模式

Paper 引擎位于 `venues/paper`，使用统一 `OrderRequest/OrderSnapshot/Fill`：

- 无实盘凭据也可运行。
- 可使用实时 Quote/OrderBook 缓存撮合。
- 支持 Market、Limit、Post-only、IOC/FOK/GTC 基础语义。
- 本地订单和成交同样发出统一 VenueEvent。

真实最小量连通性探针属于 Live 命令，不是 Paper 成交。探针必须显式确认、使用幂等键，并在退出成交后验证仓位回到运行前基线。

## 定时维护

- `VENUE_INSTRUMENT_REFRESH_SECONDS`：品种规格、账户费率和当前资金费刷新，默认 21600 秒。
- `VENUE_ACCOUNT_RECONCILE_SECONDS`：账户余额快照兜底同步，默认 60 秒。
- `MT5_ORDER_POLL_INTERVAL_MS`：MT5 活动订单轮询，默认 75ms。
- `VENUE_STARTUP_TIMEOUT_SECONDS`：启动行情等待上限，默认 30 秒。

行情 manager 会从已启用映射构造动态订阅白名单。修改凭据或品种映射后，只失效对应进程内连接器；下一次访问按新配置重建。

## 凭据安全

交易所凭据加密存储，保存后不回显明文。校验分为：

- 字段结构检查
- 环境与账户读取检查
- 签名或终端身份检查
- 只读/交易权限检查

Binance 必须通过设置页配置；Hyperliquid 与 MT5 兼容环境变量配置。Live 下单仍受系统实盘总开关、只读标志和连接器自身权限共同约束。

## 启动

```powershell
.\scripts\create_env.ps1
.\scripts\install_packages.ps1
.\scripts\start_project.ps1
```

也可分别启动：

```powershell
.\scripts\start_backend.ps1
.\scripts\start_frontend.ps1
```

执行 Worker 由后端启动脚本独立拉起。`GET /health` 返回 API 进程的 `venue_runtimes` 和执行 Worker 心跳；心跳超过 5 秒或任一已加载连接器降级时整体状态为 `degraded`。

## 测试

```powershell
.venv\Scripts\python.exe -m pytest backend/tests -q
```

原生连接器测试覆盖签名、凭据校验、订单簿断档恢复、私有订单事件、Paper 撮合、MT5 活跃订单轮询、稳定 ClientOrderId 和订单生命周期投影。
