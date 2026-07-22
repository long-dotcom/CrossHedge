# MT5 Gateway 与 Redis 拆分架构

## 部署边界

CrossHedge 现在分为两类运行环境：

- Docker：FastAPI 后端、执行 Worker、React/Nginx 前端、PostgreSQL、Redis。
- Windows 主机：安装 MetaTrader 5 Terminal 与 `MetaTrader5` Python 包的独立 MT5 Gateway。

业务后端不再安装 `MetaTrader5`。MT5 账户、持仓、行情和交易请求均通过 Redis 交换。

代码边界也与进程边界一致：`backend/app/venues/mt5` 只保留 Redis 代理和协议编解码；
`mt5_gateway` 目录保存 Terminal 初始化、原生连接器、订单轮询和 Gateway 入口。后端容器只复制
`backend` 目录，因此不会包含或导入 MT5 原生实现。

## Redis 协议

默认键前缀为 `crosshedge`，可通过 `REDIS_KEY_PREFIX` 修改。

| 键 | 类型 | 用途 |
| --- | --- | --- |
| `crosshedge:mt5:commands` | Stream | 后端发送下单、撤单、查询等命令 |
| `crosshedge:mt5:response:{request_id}` | Stream | 单次 RPC 结果，60 秒自动过期 |
| `crosshedge:mt5:events` | Stream | 订单状态及成交事件 |
| `crosshedge:mt5:snapshot:account` | String/JSON | 当前账户完整快照 |
| `crosshedge:mt5:snapshot:positions` | String/JSON | 当前持仓完整快照 |
| `crosshedge:mt5:snapshot:instrument:{symbol}` | String/JSON | MT5 品种合约规格快照 |
| `crosshedge:mt5:ticker:{symbol}` | String/JSON | MT5 最新报价 |
| `crosshedge:mt5:health` | String/JSON | Gateway 心跳及连接状态 |
| `crosshedge:mt5:idempotency:*` | String/JSON | 交易命令幂等结果，默认保留 24 小时 |
| `crosshedge:mt5:idempotency-state:*` | String/JSON | 交易命令执行中状态，防止结果未知时自动重放 |

交易命令包含 `request_id`、`operation`、`response_stream`、`idempotency_key`、时间戳和协议版本。Gateway 使用 Consumer Group 消费，并在产生明确结果后 ACK。下单和撤单必须携带稳定幂等键。

## 启动顺序

1. 复制编排配置：`Copy-Item .env.example .env`。交易所账户从前端管理台配置；如需指定
   MT5 登录参数，另将 `.mt5-gateway.env.example` 复制为 `.mt5-gateway.env`。
2. 启动容器：`.\scripts\start_stack.ps1`。脚本会自动避让宿主机端口冲突。
3. Windows 虚拟环境安装 Gateway 依赖：

   ```powershell
   .\.venv\Scripts\python.exe -m pip install -r mt5_gateway\requirements.txt
   ```

4. 确认 MT5 Terminal 已登录目标账户，然后启动 Gateway：

   ```powershell
   .\scripts\start_mt5_gateway.ps1
   ```

5. 浏览器访问 `http://localhost:8080`。后端在 Gateway 未启动时保持运行，但健康状态为 degraded，MT5 实盘操作会被阻断。

## 安全与故障处理

- Redis 通过 `REDIS_BIND_ADDRESS` 和 `REDIS_HOST_PORT` 发布；当前部署示例使用 `0.0.0.0:6391` 供远程 Windows Gateway 通过服务器公网 IP 连接。服务器防火墙和云安全组应只允许 MT5 机器的固定公网出口 IP，禁止对所有来源开放。
- Redis 使用非默认端口并强制密码认证；Redis、JWT 与交易所加密密钥分别由 `REDIS_URL`、`REDIS_PASSWORD`、`JWT_SECRET`、`EXCHANGE_CONFIG_SECRET` 环境变量显式配置，系统不再自动生成或保存这些密钥。
- Redis 原生密码认证不提供传输加密；直接公网连接会暴露流量元数据和明文协议内容，必须使用强随机密码，并优先在防火墙层限制来源 IP。
- 账户和持仓快照具有 TTL。Gateway 停止后后端不会长期使用陈旧数据。
- Gateway 定时把品种合约规格写入 Redis，后端扫描直接读取快照；规格默认每 6 小时从 Terminal 刷新一次，避免跨公网逐方向 RPC。
- Redis 重启或网络短暂中断时 Gateway 保持 MT5 连接并按指数退避重连，不会退出；Redis 恢复后自动重建 Consumer Group、心跳和全部只读快照。
- Gateway 进程重启导致 consumer ID 变化时，后端会自动重发全部行情品种订阅。
- Redis 使用 AOF 和 `noeviction`，避免内存淘汰交易命令。生产环境仍需配置持久化、监控和容量告警。
- Redis Stream 提供至少一次投递语义；幂等键是避免重复下单的必要条件。
- 下单或撤单开始执行后会写入幂等状态。如果进程在明确结果落库前中断，Gateway 会拒绝自动重放该命令，需人工核对终端状态并清理对应状态键。
- Paper 命令携带 `environment=demo`，Gateway 在每次下单前读取账户 `trade_mode`；Terminal 不是 Demo 账户时拒绝下单。Live 命令也不能误发到 Demo 账户。
- MT5 平仓只匹配网关专属 `MT5_ORDER_MAGIC` 的 position ticket，不会选中同一 Demo 账户内的手工单或其他 EA 持仓。

## Paper 混合执行边界

Paper 的 MT5 腿由 Gateway Demo 账户实际持有；加密交易所腿则由后端真实最小探针采样并立即回平。两者都通过统一 Intent/Outbox 生命周期驱动，但加密探针另外把恢复状态写入 Redis，并写入数据库 `ProbeRun` 供执行页查看。

Market 模式按现有双腿调度并发提交。Maker 模式先完成加密 Post-only 探针的“成交或超时撤单 → 按实际成交量回平”，确认真实残量为零后再提交 MT5 Demo 对冲腿。加密探针和后续 Live 均由账户私有 WS 确认订单及成交；Binance listenKey 每 30 分钟续期，断线重连后只做一次 REST 补偿对账。关闭设置页的混合 Paper 总开关只阻止新探针；已经进入恢复流程的稳定 ClientOrderId 仍可继续查询和回平。

## 缓存边界

需要跨进程共享的缓存包括行情历史/最新值、统计信号、成本、FX、SSE 快照、扫描结果、对冲组共享快照、断路器配置、MT5 交易能力、自动执行确认状态以及真实探针的锁、限额和恢复状态。策略配置、品种映射及 MT5 会话兜底结果保存在 API/调度所在进程内，由设置接口主动失效并保留短 TTL 兜底。扫描器每轮通过批量 Redis 快照读取 BBO、统计结果和交易能力，品种循环不再逐项访问共享缓存。扫描行情只维护 BBO，不再持续生成或缓存 MT5 订单簿。连接器生命周期、线程停止标记、断路器滑动窗口以及单次扫描累加器属于进程运行状态，不作为业务缓存迁移。

## 验证

默认测试使用隔离的 FakeRedis。启动本机 Redis 后，可额外运行真实 Redis 协议测试：

```powershell
$env:RUN_REDIS_INTEGRATION = "1"
$redisContainer = (docker compose ps -q redis).Trim()
$redisPort = ((docker port $redisContainer 6391/tcp) -split ':')[-1]
$redisPassword = (Get-Content .env | Where-Object { $_ -match '^REDIS_PASSWORD=' } | Select-Object -First 1).Split('=', 2)[1]
$env:REDIS_INTEGRATION_URL = "redis://:$redisPassword@127.0.0.1:$redisPort/15"
.\.venv\Scripts\python.exe -m pytest backend/tests/test_mt5_redis_integration.py -q
```

该测试覆盖下单命令与响应 Stream、稳定幂等键、账户/持仓快照和私有事件 Stream，不连接 MT5 Terminal，也不会产生真实订单。

真实 Terminal 的安全验收应先关闭 `MT5_LIVE_ORDER_ENABLED` 与 `MT5_DEMO_ORDER_ENABLED`，验证 Gateway 心跳、账户/持仓快照和只读查询。下单、撤单与成交验收必须使用专用 Demo 账户，并由操作者明确开启 Demo 开关后执行。

## 外部服务健康检查

执行 Worker 每 2 秒向 Redis 写入一次带 TTL 的心跳，API 与 Worker 分属不同容器时仍可正确判断存活。交易所连接器的 `health()` 只读取本地连接状态，不能为每次心跳发送 REST 请求。Hyperliquid 健康状态来自 WebSocket；REST 返回 429 时，所有容器通过 Redis 共享退避窗口，避免继续放大限流。
