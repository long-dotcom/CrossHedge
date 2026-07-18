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
| `crosshedge:mt5:ticker:{symbol}` | String/JSON | MT5 最新报价 |
| `crosshedge:mt5:orderbook:{symbol}` | String/JSON | MT5 最新盘口 |
| `crosshedge:mt5:health` | String/JSON | Gateway 心跳及连接状态 |
| `crosshedge:mt5:idempotency:*` | String/JSON | 交易命令幂等结果，默认保留 24 小时 |
| `crosshedge:mt5:idempotency-state:*` | String/JSON | 交易命令执行中状态，防止结果未知时自动重放 |

交易命令包含 `request_id`、`operation`、`response_stream`、`idempotency_key`、时间戳和协议版本。Gateway 使用 Consumer Group 消费，并在产生明确结果后 ACK。下单和撤单必须携带稳定幂等键。

## 启动顺序

1. 复制配置：`Copy-Item .env.example .env`，设置 PostgreSQL、JWT、交易所和 MT5 参数。
2. 启动容器：`docker compose up -d --build`。
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

- Redis 端口只绑定 `127.0.0.1`。如果 Gateway 位于另一台 Windows 主机，必须使用受控内网、TLS/VPN 和 Redis ACL，不能直接暴露公网。
- 账户和持仓快照具有 TTL。Gateway 停止后后端不会长期使用陈旧数据。
- Redis 使用 AOF 和 `noeviction`，避免内存淘汰交易命令。生产环境仍需配置持久化、监控和容量告警。
- Redis Stream 提供至少一次投递语义；幂等键是避免重复下单的必要条件。
- 下单或撤单开始执行后会写入幂等状态。如果进程在明确结果落库前中断，Gateway 会拒绝自动重放该命令，需人工核对终端状态并清理对应状态键。

## 缓存边界

已迁移到 Redis 的缓存包括通用 TTL、行情历史/最新值、订单簿、策略配置、品种映射、统计信号、成本、FX、SSE 快照、扫描结果、对冲组共享快照、断路器配置、MT5 交易能力和自动执行确认状态。连接器生命周期、线程停止标记、Paper 撮合账本、断路器滑动窗口以及单次扫描累加器属于进程运行状态，不作为业务缓存迁移。

## 验证

默认测试使用隔离的 FakeRedis。启动本机 Redis 后，可额外运行真实 Redis 协议测试：

```powershell
$env:RUN_REDIS_INTEGRATION = "1"
$env:REDIS_INTEGRATION_URL = "redis://127.0.0.1:6379/15"
.\.venv\Scripts\python.exe -m pytest backend/tests/test_mt5_redis_integration.py -q
```

该测试覆盖下单命令与响应 Stream、稳定幂等键、账户/持仓快照和私有事件 Stream，不连接 MT5 Terminal，也不会产生真实订单。

真实 Terminal 的安全验收应先关闭 `MT5_LIVE_ORDER_ENABLED` 与 `MT5_DEMO_ORDER_ENABLED`，验证 Gateway 心跳、账户/持仓快照和只读查询。下单、撤单与成交验收必须使用专用 Demo 账户，并由操作者明确开启 Demo 开关后执行。

## 外部服务健康检查

执行 Worker 每 2 秒向 Redis 写入一次带 TTL 的心跳，API 与 Worker 分属不同容器时仍可正确判断存活。交易所连接器的 `health()` 只读取本地连接状态，不能为每次心跳发送 REST 请求。Hyperliquid 健康状态来自 WebSocket；REST 返回 429 时，所有容器通过 Redis 共享退避窗口，避免继续放大限流。
