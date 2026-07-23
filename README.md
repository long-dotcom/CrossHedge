# CrossHedge

CrossHedge 是面向跨市场对冲交易的 FastAPI + React 应用。目前只支持三个原生交易场所：

- Hyperliquid
- MetaTrader 5
- Binance USDⓈ-M Futures

交易所接入由项目自身维护，不依赖第三方交易运行时。Paper 与 Live 共用统一领域模型和订单状态机；当前 Paper 只采用“加密交易所真实最小探针 + MT5 Gateway Demo”混合执行，不再使用本地撮合作为成交结果。

## Redis 与独立 MT5 Gateway

MT5 Terminal 调用已从业务后端运行链路剥离。后端通过 Redis Stream 发送下单、撤单、订单查询和预检查命令，并从 Redis 读取账户、持仓、行情及 Gateway 心跳快照。`MetaTrader5` 依赖只安装在 Windows Gateway 环境中。

除 MT5 Gateway 外，FastAPI、执行 Worker、前端、PostgreSQL 和 Redis 使用 Docker Compose 部署：

```powershell
Copy-Item .env.example .env
Copy-Item .mt5-gateway.env.example .mt5-gateway.env
# 填写 JWT_SECRET、EXCHANGE_CONFIG_SECRET、Redis 密码及服务器公网 IP。
.\scripts\start_stack.ps1
.\.venv\Scripts\python.exe -m pip install -r mt5_gateway\requirements.txt
.\scripts\start_mt5_gateway.ps1
```

`start_stack.ps1` 会检查前端和 Redis 宿主机端口；默认端口被占用时自动选择下一个
可用端口，并将本次结果写入不提交仓库的 `.runtime.env`。Redis 容器内部使用
`6391`，并通过 `REDIS_BIND_ADDRESS=0.0.0.0` 发布到服务器全部网络接口。

Redis 地址和密码通过环境变量显式配置：Docker 服务读取根目录 `.env` 中的
`REDIS_URL`、`REDIS_PASSWORD`，Windows Gateway 读取 `.mt5-gateway.env` 中的同名变量。
两个文件的密码必须完全一致；Gateway 的 `REDIS_URL` 使用项目服务器公网 IP 及
`REDIS_HOST_PORT`。服务器系统防火墙和云安全组必须放行该 TCP 端口，建议仅允许
MT5 机器的固定公网出口 IP。Redis 密码认证不加密传输内容，不应使用弱密码。

Compose 中执行 Worker 使用 `entrypoint: null` 继承后端镜像入口，该写法兼容 Coolify
的 Compose 校验器；Worker 会在后端健康检查通过后执行迁移检查并启动。
Windows 启动脚本显式按 UTF-8 读取环境文件，兼容系统默认代码页不是 UTF-8 的
Windows PowerShell 5.1。

Binance 公共行情不要求配置 API 凭据；只有账户、交易和私有事件功能需要凭据。
未配置凭据时，手续费计算使用默认 Maker/Taker 费率（可通过
`BINANCE_DEFAULT_MAKER_FEE_RATE` 和 `BINANCE_DEFAULT_TAKER_FEE_RATE` 调整）。
单一交易所连接失败会被隔离，不会中断其他交易所的行情采集。

MT5 Gateway 会把账户、持仓、行情和品种合约规格定时写入 Redis；后端扫描读取
规格快照，不再为每个方向跨公网调用 Gateway。Redis 重启或网络短暂中断时 Gateway
保持运行并自动重连，恢复后会重新发布心跳和快照。

`JWT_SECRET` 和 `EXCHANGE_CONFIG_SECRET` 也由环境变量显式提供，不再启动一次性密钥
初始化容器。两者必须使用足够长的随机值并长期保持不变；修改 `JWT_SECRET` 会使已有
登录令牌失效，修改 `EXCHANGE_CONFIG_SECRET` 会导致数据库中的交易所凭据无法解密。
在 Coolify 中应把这些值配置为运行时 Secret，并在重新部署时继续使用原值。

默认访问地址为 `http://localhost:8080`。完整键名、启动顺序、幂等约束和故障处理见 [MT5 Gateway 架构](docs/MT5_GATEWAY_ARCHITECTURE.md)。
行情管道当前延迟指标的口径、潜在瓶颈和后续采样顺序见 [行情管道高延迟调查](docs/PIPELINE_LATENCY_INVESTIGATION.md)。
扫描慢阶段默认以 `SCANNER_SLOW_PHASE_MS=50` 为阈值输出结构化日志，可通过同名环境变量调整。
统计信号默认每 `SIGNAL_STATS_REFRESH_INTERVAL_MS=10000` 毫秒后台刷新，Redis 缓存以 `SIGNAL_STATS_CACHE_TTL_MS=60000` 毫秒保留旧结果；缓存有效期必须长于刷新间隔，避免扫描线程在刷新空窗同步查询历史价差。
扫描器每轮用批量快照一次性读取全部启用品种的双腿 BBO、统计结果和 MT5 交易能力；策略与品种映射使用设置 API 主动失效的进程内缓存，品种循环不再逐项读取这些共享数据。

## 原生交易所架构

后端连接器代码位于 `backend/app/venues`，Windows 原生实现位于仓库根目录的 `mt5_gateway`：

```text
venues/
├── domain/            # Decimal 领域模型、状态、事件、能力声明
├── paper/             # 测试辅助模型（不参与当前 Paper 下单）
├── hybrid_probe.py    # 加密交易所真实最小探针及回平恢复
├── binance/           # REST 签名、公共/私有 WS、增量订单簿、连接器
├── hyperliquid/       # Info/Exchange API、公共/私有 WS、连接器
├── mt5/               # 后端 Redis 代理与协议编解码，不依赖 MetaTrader5
├── protocols.py       # 窄接口协议
├── registry.py        # 可扩展连接器注册表
└── manager.py         # 长生命周期实例、订阅和凭据失效管理

mt5_gateway/
├── main.py            # Redis Stream 命令消费与快照发布
├── native_connector.py # 仅 Gateway 加载的 MetaTrader5 原生连接器
├── poller.py          # 活动订单与成交轮询
└── mt5_bootstrap.py   # Terminal 初始化与连接恢复
```

统一连接器提供以下能力：

- 账户余额、可用余额、保证金与权益
- 当前持仓、活动挂单和订单查询
- 品种数量步进、最小数量、最小名义额、价格精度
- Maker/Taker 交易手续费率
- Ticker/BBO 和动态订阅；完整订单簿仅保留连接器按需查询能力
- 凭据的结构、读权限、交易权限和环境校验
- 提交、撤销、查询订单及成交查询
- 私有订单/成交事件订阅与健康状态

新增交易所时实现 `VenueConnector` 的窄协议并注册到 manager；业务模块不得新增交易所专属分支。

## 订单生命周期

FastAPI 只创建不可变 Intent、ExecutionLeg 和 Outbox。独立执行 Worker 是唯一允许调用 `submit_order` 的业务进程：

- 对冲组的“触发价差”保存策略满足条件时的双腿行情快照；“开仓价差”在双腿成交前保持未确认，成交后按交易所返回的累计成交均价计算。新执行模型的 `VenueOrder.average_price/filled_quantity` 为主数据源，旧 `orders/fills` 仅用于兼容历史记录。
- 仪表盘“今日盈亏”按 UTC 自然日统计今日已实现盈亏，再加当前开放对冲组的实时未实现盈亏；“已实现盈亏”卡片仍展示全部历史已平仓汇总。
- 扫描、执行和盈亏的成本口径仅包含可执行 bid/ask 已体现的双腿点差，以及两腿开仓、平仓交易手续费；不再读取、预测或累计 Funding、MT5 Swap、滑点和 FX 附加成本。

- 下单前校验异常、确定性提交失败、结果未知异常以及交易所拒绝都会写入旧订单的
  `error_message`、Intent、Outbox/ExecutionEvent 和 `SystemLog(category=execution)`；
  系统日志上下文包含 Intent、执行腿、ClientOrderId、场所、品种、执行模式和原始异常类型。
- `outcome_unknown=true` 的网络/服务端异常只会进入恢复对账，不会自动重发；确定性错误会直接
  投影为失败并保留交易场所原始错误。执行记录列表会直接展示失败原因，无需展开订单。
- 当前 Paper 执行不是纯本地撮合：加密交易所腿使用真实最小单探针采样并立即反向回平，
  MT5 腿使用 Gateway Demo。只有探针开关、账户空仓、限额、冷却或凭证等前置保护未通过时，
  才会在发送真实订单之前拒绝；该拒绝原因会完整写入执行记录。独立 `PaperConnector` 的
  Post-only 模拟拒绝也会保留明确原因，避免被误报为普通 Maker 超时。

1. 先持久化稳定 `client_order_id`。
2. 并行提交双腿命令。
3. Binance/Hyperliquid 的 Paper 探针与 Live 订单统一以账户私有 WebSocket 作为成交确认主路径：Hyperliquid 订阅 `orderUpdates`/`userFills`/`clearinghouseState`，Binance 使用 User Data Stream 的订单与 `ACCOUNT_UPDATE`，并每 30 分钟续期 listenKey。账户和持仓事件同步投影到数据库及 Redis。正常运行不轮询 REST 查单或持仓；仅在 Worker 启动或私有流断线重连后执行一次补偿快照。
4. MT5 Gateway 只轮询活动订单，默认 75ms，并通过 Redis Stream 发布事件；成交单据按 ticket 去重。
5. 网络超时或 5xx 被视为“结果未知”，只能按稳定 ID 查询，禁止盲目重发。
6. REST、历史成交与账户快照仅用于断线、重启和漏事件兜底。
7. 原始事件按 event ID 幂等持久化，再投影 VenueOrder、Fill、Intent 和 HedgeGroup。

Maker-then-Market、部分成交、撤单确认、单腿失败、补偿和人工恢复继续由持久化状态机处理。Maker 买单直接挂买一价、卖单直接挂卖一价，不再应用额外 bps 偏移。连续执行同一组的平仓 Intent 时，终态事件按 Intent 独立幂等，失败且无成交会恢复平仓前状态。订单没有真实成交事件或明确 `FILLED` 状态时不会生成 Fill。

## Binance 行情与私有流

- 公共 WS 只订阅 `bookTicker`，扫描阶段不订阅增量 depth，也不维护本地订单簿。
- 私有用户流维护 listen key，定时续期并自动重连。
- `ORDER_TRADE_UPDATE` / `TRADE_LITE` 转换为统一订单和成交事件。
- Hedge Mode 使用明确 `LONG/SHORT` PositionSide；是否传 `reduceOnly` 由连接器能力规则决定。

## Hyperliquid 事件流

- 公共 WS 订阅 `bbo`，在最优买一/卖一变化时更新扫描行情；执行前价格复核同样只刷新 BBO。
- 同步 Hyperliquid 规格时，标准永续读取默认 `metaAndAssetCtxs`；`xyz:JPY`、`xyz:JP225` 等 HIP-3 品种会按冒号前缀自动选择对应 perp dex，并始终以完整 `dex:symbol` 作为规格缓存键。
- 执行 Worker 单独订阅 `orderUpdates` 和 `userFills`。
- 业务 ClientOrderId 映射为确定性的 16 字节 cloid。
- Fill 按交易 ID 去重；断线后使用订单状态和用户成交历史补齐。

## MT5 执行确认

MT5 没有与加密交易所完全等价的账户私有 WebSocket。Windows Gateway 使用终端 API，并仅对活动订单进行高频轮询，再把事件写入 Redis Stream：

- 默认 `MT5_ORDER_POLL_INTERVAL_MS=75`。
- 市价与挂单依据 symbol 支持的 filling mode 选择请求参数。
- 平仓按 position ticket 和网关专属 magic 定位，避免误平同品种另一方向或手工/其他 EA 持仓。
- 订单完成后查询 history deals，按 deal ticket 去重生成 Fill。

## Paper 混合模式

Paper 不再提供本地撮合回退。品种映射必须恰好包含一个受支持的加密交易所腿（Hyperliquid 或 Binance）和一个 MT5 腿：

- 加密腿按交易所最小数量/最小名义金额提交真实探针，取得真实成交价格后立即以反向市价单回平；策略账本仍按策略目标数量记录模拟成交。
- MT5 腿通过 Redis Stream 发往 Gateway，并且每次下单都要求 Terminal 当前登录 Demo 账户。Demo 持仓保留到策略平仓。
- Market 策略使用真实 L2 深度估算目标数量的额外滑点，双腿按原执行状态机并发提交。
- Maker 策略提交 Post-only 最小探针，等待可配置超时后撤销；允许部分成交，但必须等待撤单终态并按真实成交量立即回平，然后才提交 MT5 Demo 对冲腿。
- Maker 超时由本地持久化状态机计时触发，撤单、部分成交、完全成交和回平终态均等待账户私有 WS 推送，不以固定间隔 REST 查单。
- 探针只允许使用同品种空仓的专用加密账户；同一账户/品种有 Redis 分布式锁，并受单次名义额、每日次数、每日往返名义额、冷却时间和回平超时限制。
- 稳定 ClientOrderId、Redis 恢复状态和 `ProbeRun` 防止进程重启后盲目重复下单。任何回平不完整都会进入 `RECOVERY_REQUIRED`，阻止继续当作 Paper 成交。
- 设置页的混合 Paper 总开关默认关闭，也是阻止新探针的紧急停止开关。执行页“真实探针”可查看真实数量、残量及恢复提示。
- 对于旧版本或异常中断留下的垃圾对冲组，可在“对冲组”页面执行“作废归档”。服务端仅在确认没有真实成交敞口、结果未知外部订单和未回平探针时允许操作；作废只终止未完成投影并隐藏该组，不删除 Intent、订单、Fill、事件和审计记录。

按当前成本假设，系统不把真实探针手续费计入 Paper 策略账本；`ProbeRun` 仍保留真实开仓/回平均价用于审计。

## 品种规格同步

设置页“品种映射”的“同步规格”会同时刷新映射两腿，而不是只同步 MT5：

- Binance 从 `exchangeInfo` 同步数量步进、最小数量、价格 tick 和最小名义金额；通用价格/数量精度以加密交易所规则为准。
- Hyperliquid 同步数量小数位、最小数量和最小名义金额；其价格采用动态有效数字规则，`price_tick=0` 时不会覆盖人工价格配置。
- MT5 继续同步最小手数、手数步进、合约大小和结算币种，但不会再覆盖加密交易所价格精度。
- Binance 连接器在提交前还会按最新交易所规格规整数量和限价，作为映射缓存陈旧时的最后保护；数量只向下取整，买价向下、卖价向上对齐 tick。

旧版 `sync-broker` API 暂时保留兼容，新前端使用 `sync-instruments`。

## 定时维护

- `VENUE_INSTRUMENT_REFRESH_SECONDS`：品种规格和账户交易手续费刷新，默认 21600 秒。
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

Binance 与 Hyperliquid 必须通过设置页配置。MT5 登录信息和下单权限只属于独立
Gateway，可放在 `.mt5-gateway.env`；业务后端不会接收交易所账户密钥。Live 下单仍受
系统实盘总开关、只读标志和连接器自身权限共同约束。

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

原生连接器测试覆盖签名、凭据校验、订单簿断档恢复、私有订单事件、混合 Paper 真实探针、MT5 活跃订单轮询、稳定 ClientOrderId 和订单生命周期投影。
