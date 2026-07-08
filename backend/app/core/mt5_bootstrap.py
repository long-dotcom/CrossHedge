"""
MT5 连接初始化模块

统一 MetaTrader5 终端连接和品种选择的初始化逻辑，消除源项目中多处重复的
``mt5.initialize()`` / ``mt5.symbol_select()`` 调用模式：
- ``adapters/mt5.py`` 的 ``_initialize_mt5``
- ``market/fx.py``、``market/mt5_sessions.py``
- ``accounts/sync.py``、``workers/market_data.py``
- ``strategy/live_costs.py``、``execution/readiness.py``

使用方式::

    from app.core.mt5_bootstrap import ensure_mt5_connected, ensure_mt5_symbol_selected

    if ensure_mt5_connected():
        if ensure_mt5_symbol_selected("EURUSD"):
            tick = mt5.symbol_info_tick("EURUSD")
"""

from __future__ import annotations

from app.core.logging import get_logger

logger = get_logger(__name__)


def ensure_mt5_connected(
    *,
    login: str | int | None = None,
    password: str | None = None,
    server: str | None = None,
) -> bool:
    """确保 MT5 终端已连接。

    连接策略（与源项目 ``_initialize_mt5`` 一致）：
    1. 如果已连接（``mt5.terminal_info()`` 非 None），直接返回 True
    2. 如果提供了凭证（login/password/server），尝试带凭证 initialize
    3. 否则尝试无凭证 initialize（使用终端已保存的凭证）
    4. 记录日志并返回结果

    参数:
        login: MT5 登录号（可选，通常从凭证管理或配置中获取）。
        password: MT5 密码（可选）。
        server: MT5 服务器名（可选）。

    返回:
        ``True`` 表示已成功连接，``False`` 表示连接失败。
    """
    try:
        import MetaTrader5 as mt5  # type: ignore
    except Exception as exc:
        logger.warning("MetaTrader5 包不可用: {}", exc)
        return False

    # 1. 检查是否已连接
    try:
        terminal_info = mt5.terminal_info()
        if terminal_info is not None:
            # 该函数会被行情、交易能力、风控和执行链路高频调用；
            # 已连接属于正常快路径，不逐次打印，避免启动和定时刷新时刷屏。
            logger.trace("MT5 终端已连接")
            return True
    except Exception:
        pass

    # 2. 尝试带凭证 initialize
    has_credentials = bool(login and password and server)
    if has_credentials:
        try:
            result = mt5.initialize(
                login=int(login),
                password=str(password),
                server=str(server),
            )
            if result:
                logger.info("MT5 带凭证连接成功: login={}, server={}", login, server)
                return True
            logger.warning("MT5 带凭证连接失败: {}", mt5.last_error())
        except Exception as exc:
            logger.warning("MT5 带凭证连接异常: {}", exc)

    # 3. 尝试无凭证 initialize（使用终端已保存的凭证）
    try:
        result = mt5.initialize()
        if result:
            logger.info("MT5 无凭证连接成功（使用终端已有会话）")
            return True
        logger.warning("MT5 无凭证连接失败: {}", mt5.last_error())
    except Exception as exc:
        logger.warning("MT5 无凭证连接异常: {}", exc)

    return False


def ensure_mt5_symbol_selected(symbol: str) -> bool:
    """确保 MT5 品种已被选中（在市场观察中可见）。

    封装 ``mt5.symbol_select()`` 调用及错误处理。

    参数:
        symbol: MT5 品种名称，例如 ``"EURUSD"``。

    返回:
        ``True`` 表示品种已选中，``False`` 表示选择失败。
    """
    try:
        import MetaTrader5 as mt5  # type: ignore
    except Exception as exc:
        logger.warning("MetaTrader5 包不可用，无法选择品种 {}: {}", symbol, exc)
        return False

    try:
        result = mt5.symbol_select(symbol, True)
        if result:
            return True
        logger.warning("MT5 symbol_select 失败: symbol={}, error={}", symbol, mt5.last_error())
        return False
    except Exception as exc:
        logger.warning("MT5 symbol_select 异常: symbol={}, error={}", symbol, exc)
        return False
