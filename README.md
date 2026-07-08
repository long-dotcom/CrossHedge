# CrossHedge

多交易所对冲交易系统。

## 交易所接入边界

- `MT5` 使用项目原生适配器。
- `Hyperliquid` 使用项目原生适配器。
- 其他交易所统一通过 NautilusTrader 接入，不再为每个交易所新增独立业务适配器。

非原生交易所的行情、订单簿、下单、撤单、订单状态、成交、持仓和账户余额都必须从 `NautilusRuntimeManager` 持有的长期 `TradingNode` 读取或提交。项目不再允许非原生交易所在执行、账户、行情模块里新增交易所专属 REST 分支。

当前项目内已接入的 Nautilus live runtime：

- `binance`：Binance USDT Futures，经 NautilusTrader Binance live data / execution client 运行。

NautilusTrader 1.229.0 包内存在但项目尚未完成 runtime 配置映射的交易所，会在启用时直接失败，不回退旧适配器。

## Nautilus 交易模式

非 `MT5` / `Hyperliquid` venue 只支持两种交易模式：

- `paper_probe`：提交真实交易所最小可成交订单，只使用真实成交价；系统 paper 账本仍按策略目标数量记账。
- `live`：按策略目标数量提交真实订单，数量会按交易所 instrument 规格校验并向下规整到步进，避免真实成交数量超过策略目标。

`paper_probe` 不是模拟请求。它会使用真实凭证向交易所提交订单，因此交易所配置必须启用、填写凭证，并关闭只读模式。

## Nautilus Runtime 加载流程

`NautilusRuntimeManager` 按 `venue + environment + credential_id` 缓存长期运行的 `TradingNode`：

1. 后端启动时读取已启用的非原生 `ExchangeCredential` 并预热 runtime；运行中访问某个 venue 时也会按需加载。
2. 检查 NautilusTrader 依赖是否可导入。
3. 解密凭证并构建 Nautilus venue data / execution client config。
4. 创建 `TradingNode`，注册 data client factory、exec client factory 和 instrument provider。
5. `build()` 后在后台线程长期 `run()`，启动日志会打印 NautilusTrader 海螺图标。
6. 账户、持仓、订单、成交和行情从 node cache / trader report 读取；首次读取账户会等待 Nautilus AccountState 完成同步，持仓页面和 SSE 直接读取 adapter 当前状态，不先落 `Position` 表。
7. 下单使用 Nautilus `OrderFactory + SubmitOrder + ExecEngine.execute`，不调用交易所专属 HTTP helper。

`Position` 表只用于执行对账、接管和历史状态沉淀，不作为仓位页面的实时数据源。

仓位页的当前价优先读取 Nautilus mark price cache，未实现盈亏按当前价和开仓均价实时估算；如果 Nautilus venue 的标准持仓报告没有暴露强平价，则强平价保持为空，不绕开 Nautilus 调交易所专属 REST。

新增交易所时只允许扩展 Nautilus venue runtime 配置注册表；如果该 venue 没有项目内 runtime builder，启动预热、凭证测试或首次访问时都会返回“尚未接入 Nautilus live runtime”。

## 开发约定

- 新增项目代码注释使用中文。
- 功能更新需要同步变更对应的 `.md` 文档。
- Windows 启动脚本固定使用 `py -3.14` 运行后端，避免落到本机旧版 Python。
- 非原生交易所新增能力时，优先改 `backend/app/adapters/nautilus_runtime.py` 和 Nautilus 相关凭证/API 封装。
