# 交易模块外部验收运行手册

适用版本：CrossHedge 当前工作区  
适用环境：Binance Futures Testnet/Demo、MT5 Demo  
禁止环境：Binance Live 自动验收

## 1. 目的

自动化测试只能证明状态机与适配器契约，不能代替交易场所的真实订单、成交和仓位报告。本手册用于取得重构完成定义中的外部证据：

1. Binance Hedge Mode 开多、平多、开空、平空。
2. Binance 平仓使用相同 LONG/SHORT PositionId 的明确反向订单，并且不发送 `reduce_only`。
3. Probe 每次都恢复运行前仓位基线，残量为零。
4. MT5 Demo 市价平仓、部分成交/拒绝和无仓位场景。
5. 验收全过程不触碰 Binance Live。

## 2. Binance 前置条件

1. 后台“交易所配置”中的 Binance `environment` 必须为 `testnet` 或 `demo`。
2. 凭证必须启用、完整配置、关闭只读，并具有合约测试交易权限。
3. 品种映射必须启用，并指向 Testnet/Demo 实际存在的 Binance Futures symbol。
4. 设置页“混合 Paper（真实最小探针 + MT5 Demo）”总开关已经显式启用。
5. 后端与独立执行 Worker 已按新配置重启。
6. `/health` 必须为 `ok`，Worker 的 Binance runtime 必须满足：
   - `runtime_status=running`
   - `data_connected=true`
   - `execution_enabled=true`
   - `execution_connected=true`
   - runtime `environment` 与凭证环境一致
7. 使用管理员 Bearer Token。Token 只作为进程内参数传递，不写入证据文件。

## 3. 安全预检

先执行 dry-run：

```powershell
.\scripts\run_binance_testnet_acceptance.ps1 `
  -Token '<ADMIN_BEARER_TOKEN>' `
  -Symbol 'GOLD' `
  -DryRun
```

Dry-run 只读取脱敏凭证信息和健康状态，不提交订单。只要凭证或 runtime 为 `live`，脚本必须以“仅允许 testnet/demo”失败。当前 live 配置已经实际验证该保护会生效且 `probes=0`。

## 4. 执行 Binance 四方向验收

只有 dry-run 成功后才执行：

```powershell
.\scripts\run_binance_testnet_acceptance.ps1 `
  -Token '<ADMIN_BEARER_TOKEN>' `
  -Symbol 'GOLD' `
  -Confirmation 'RUN BINANCE TESTNET ACCEPTANCE'
```

脚本严格串行执行：

1. BUY Probe：入口 BUY + LONG，覆盖开多。
2. 等待入口真实 Fill。
3. 自动创建退出 SELL + LONG，覆盖平多。
4. 等待退出真实 Fill、订单残量为零、账户 LONG 仓位恢复运行前基线。
5. 只有 ProbeRun=`FLAT` 才继续。
6. SELL Probe：入口 SELL + SHORT，覆盖开空。
7. 自动创建退出 BUY + SHORT，覆盖平空。
8. 再次确认订单残量为零、账户 SHORT 仓位恢复运行前基线。

任何一轮进入 `RECOVERY_REQUIRED/FAILED/FAILED_NO_EXPOSURE`，或在限定时间内未恢复 `FLAT`，脚本立即停止，不会继续创建下一轮订单。

## 5. Binance 证据文件

默认输出：

```text
.run/acceptance/binance-YYYYMMDD-HHMMSS.json
```

通过证据必须满足：

- `status=PASSED`
- `credential.environment` 为 `testnet` 或 `demo`
- `health_before.status=ok`
- `health_after.status=ok`
- 恰好两个 ProbeRun，分别为 BUY 与 SELL
- 每个 ProbeRun：
  - `status=FLAT`
  - `residual_quantity=0`
  - `final_position_quantity=baseline_position_quantity`
  - entry/exit Intent 均为 `COMPLETED`
  - entry/exit VenueOrder 均为 `FILLED`
  - BUY 轮为 `BUY+LONG`、`SELL+LONG`
  - SELL 轮为 `SELL+SHORT`、`BUY+SHORT`
  - entry/exit 的 `venue_reduce_only=false`
  - 保存 ClientOrderId、VenueOrderId、PositionId、成交量、均价和最近原生交易所事件

真实探针必须使用专用空仓账户。同品种已有任何人工仓位时系统会在真实提交前拒绝，避免把人工仓位误认为探针敞口；不得通过修改数据库或 Redis 绕过该检查。

## 6. 失败处理

1. 不得重复使用新的幂等键盲目重跑失败步骤。
2. 先查看 ProbeRun 的入口、退出订单和最近原生事件。
3. 若存在 pending/unknown 订单，等待原生连续对账，不得手工再次发单。
4. 若 ProbeRun=`RECOVERY_REQUIRED`，核对交易所真实仓位与运行前基线，使用受控恢复流程。
5. 保存失败证据文件和 Worker 日志，修复后使用新的验收批次。
6. 禁止为了让报告通过而手工修改数据库状态。
7. 只有服务端“作废归档”资格检查明确确认无真实成交、无结果未知外部订单且 Probe 已安全回平时，才能作废垃圾组；作废后必须仍能查看原 Intent、订单、Fill、事件及审计记录。

## 7. MT5 Demo 验收

MT5 必须连接 Demo 账户并使用最小合法手数。依次保存以下证据：

1. 开仓后按 ticket 市价反向平仓，确认成交 ticket、成交量和最终无归属残量。
2. 模拟/触发部分成交或交易服务器拒绝，确认 Intent 进入 `RECOVERY_REQUIRED`，不得伪报 closed。
3. 无仓位时发起普通平仓预览，服务端应拒绝创建外部订单。
4. MT5 订单量必须按 lot、最小手数和 volume step 规整，证据同时保存目标名义金额与最终 lots。

MT5 Demo 证据需要包含请求、订单/成交 ticket、执行 retcode、最终持仓快照和对应 Intent 状态。完成后把文件路径补入主设计文档第 20.1 节证据矩阵。

## 8. 放行规则

只有 Binance 和 MT5 的外部证据均满足要求，主设计文档第 20 节全部条目才可标记完成。在此之前：

- 保持完整目标未完成；
- 不开放自动 Live Probe 验收；
- 不以单元测试、dry-run 或一次连接成功替代真实成交与仓位报告。
