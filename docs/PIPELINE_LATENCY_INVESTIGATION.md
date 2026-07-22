# 行情管道高延迟调查

调查日期：2026-07-22

## 结论

后端扫描器本身存在真实延迟，不能归因于前端。2026-07-22 的运行日志显示 `run_scan` 一轮耗时 3214ms，且 JPY 日志到 XAG 日志相隔约 994ms；由于单品种 DEBUG 日志在该品种全部方向处理完成后输出，这段间隔基本位于 XAG 的后端处理区间。

页面同时还存在指标语义失真：诊断数据混合了报价年龄、整段扫描耗时和重叠计时，前端又把它们画成相邻阶段的独立延迟。它会影响页面归因，但不是上述 3214ms 后端耗时的原因。

调查阶段没有修改行情采集、扫描调度或诊断指标代码。随后按成本口径要求移除了扫描阶段的 Funding/Swap 预测及其 MT5 数据读取；其余延迟问题仍只调查、未修正。由于本机 Docker/PostgreSQL 未运行，无法取得线上同一时刻的指标和日志，以下结论来自代码链路、运行配置和计时点审计。

## 已确认的指标口径问题

1. `leg_a_age_ms` / `leg_b_age_ms` 是生成诊断快照时距离最后报价接收时间的“数据年龄”，不是交易所到同步器的处理耗时。当前 SSE 每 1000ms 推送一次，因此健康链路上也会自然显示数百毫秒到约 1 秒。
2. 前端的 `syncToScan` 使用 `symbol_scan_duration_ms`，该值覆盖单个品种从进入扫描到全部方向处理完成的总耗时，并非“同步到扫描”的单段耗时。
3. 扫描器在同一个位置同时启动 `cost_duration_ms`、`signal_duration_ms`、`candidate_sync_duration_ms` 和 `persist_duration_ms` 的计时，随后在同一批方向计算完成后陆续停止。四个值大量重叠，不具备可相加的阶段含义。
4. 前端仍把上述重叠值分别绘制为“扫描 → 信号 → 候选”的串行延迟，所以页面会放大对真实瓶颈的观感。

## 可能产生真实延迟的位置

### 1. 扫描热路径包含大量串行 Redis/数据库往返

调查时，扫描并非只在已取出的报价上做本地计算。以一个包含 MT5 腿、启用统计信号的品种为例，在缓存命中时每轮大致包含：

- MT5 session 缓存读取 1 次 Redis；
- 双腿报价同步读取 2 次 Redis；
- Hyperliquid 用户费率与市场元数据缓存读取 2 次 Redis；
- 两个方向各执行一次 `mt5_cost_inputs`，合计 2 次 `ExchangeCredential` 数据库查询，以及 instrument/ticker/account 共 6 次 Redis；
- 每个方向进行两轮信号评估，合计 4 次信号统计 Redis 缓存读取；
- 非 USD 结算品种还会增加 FX 缓存/MT5 tick 读取。

因此调查时单个品种稳定状态下也可能达到约 15 次 Redis 往返和 2 次 PostgreSQL 查询，而且都在扫描线程中串行等待。如果 Redis/数据库存在几十毫秒 RTT、连接池等待或宿主机资源竞争，单品种接近 1 秒是合理结果。`native_venue_manager.connector_for("mt5")` 即使连接器已经存在，也会先新建数据库 Session 查询凭据；`mt5_cost_inputs` 又在两个方向分别调用它，是最明确的重复数据库访问点。

### 已随成本口径调整消除的重复读取

扫描器现已完全移除 `mt5_cost_inputs`：每个品种不再为两个方向重复查询交易所凭据，也不再为了预测 Swap 读取 MT5 instrument、ticker 和 account。按上述调用审计，这消除了每品种每轮 2 次 PostgreSQL 查询和 6 次 Redis 读取。扫描成本现在只读取 venue 的 Maker/Taker 费率；可执行 bid/ask 点差直接来自本轮同步报价。

### 2. Hyperliquid 成本缓存会产生周期性远端 HTTP 尖峰

Hyperliquid 用户费率和 `metaAndAssetCtxs` 缓存默认 60 秒。市场元数据缓存未命中时，扫描线程会直接同步调用 Hyperliquid `/info`，默认 HTTP 超时 10 秒。不同 HIP-3 dex 使用不同缓存键，因此首次访问或 TTL 到期时都可能额外阻塞数百毫秒到数秒。若配置了账户地址，`userFees` 也有同样的同步远端调用。

### 3. MT5 Gateway 快照循环

MT5 Gateway 每轮先读取账户和全部持仓，再逐个订阅品种串行读取 instrument、ticker 和 order book，最后一次性写入 Redis。任一 MT5 Terminal 调用变慢，后续品种的快照都会一起变旧。配置虽然是 `MT5_QUOTE_POLL_INTERVAL_MS=200`，但下一轮等待发生在整轮工作完成之后，真实周期是“本轮耗时 + 200ms”。

### 4. 后端行情投影循环

后端行情 manager 同样按映射和双腿串行执行 `get_ticker`、`get_order_book` 并写 Redis。它也在整轮结束后再等待配置间隔。因此品种越多，靠后的品种年龄越高；某个 venue 的慢调用也会拖住同一线程中的其他映射。

### 5. 扫描器串行处理与重叠外部读取

主扫描按品种串行运行，每个方向执行两轮信号评估。统计缓存未命中时还会查询历史价差数据。当前 `SCANNER_INTERVAL_MS=1000` 也是在整轮扫描结束后重新计时，因此结果刷新周期为“扫描总耗时 + 1000ms”。MT5 Swap 预测读取已经移除，不再属于当前热路径。

### 6. SSE 展示刷新

Pipeline SSE 默认每 1000ms 生成一次快照，并使用约 800ms 的共享缓存。它不是行情慢的主要来源，但会给页面再增加 0～1 秒的观察延迟；页面显示的 SSE 延迟是快照生成到浏览器接收的时间，与各腿报价年龄不是同一个指标。

## 建议的下一轮验证顺序

1. 首先在扫描器内部对 `session`、`quote_sync`、`sizing/fx`、`venue_costs`、每次信号缓存读取分别计时；现有重叠计时无法确认 994ms 落在哪个调用。
2. 同时记录扫描期间 Redis GET/HGET 次数与总耗时、PostgreSQL 查询次数与连接池等待时间。若单次 Redis RTT 达到 30～50ms，现有调用数量已经足以解释主要延迟。
3. 将慢扫描是否约每 60 秒出现一次与 Hyperliquid 成本缓存 TTL 对齐；若吻合，再记录 `userFees` 和 `metaAndAssetCtxs` HTTP 耗时及 dex 缓存键。
4. 在线上连续采样 10～15 分钟，按品种记录 P50/P95/P99，并对比品种在映射列表中的顺序；若每个品种都稳定增加近似耗时，优先确认串行 Redis/数据库往返。
5. 在 MT5 Gateway 单独记录 account、positions、ticker、book 各调用耗时，重点检查 `market_book_add/get/release` 和网络 Redis `pipeline.execute()`。
6. 指标可信后再决定是否消除其余方向内重复读取、改用进程内热缓存、批量读取或并行化；除已移除的 Funding/Swap 成本链路外，本次不实施这些修正。
