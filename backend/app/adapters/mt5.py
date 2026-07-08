"""
MetaTrader 5 交易所适配器
==========================

继承 PaperAdapter，在模拟交易基础上增加 MT5 终端真实下单和持仓读取功能。

主要功能：
- 实盘 / Demo 模式下的 market 订单
- reduce-only 平仓（自动匹配最接近的持仓）
- 多种 filling mode 自动降级尝试
- 链上真实持仓读取（通过 MT5 API）
- Demo 账户安全检查

注意：
    首版仅支持 market 订单类型。
"""

from __future__ import annotations

from dataclasses import dataclass

from app.adapters.base import AdapterOrder, AdapterOrderResult
from app.adapters.paper import PaperAdapter
from app.config.settings import get_settings
from app.core.logging import get_logger
from app.core.mt5_bootstrap import ensure_mt5_connected, ensure_mt5_symbol_selected
from app.core.type_utils import safe_float, safe_int

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# 辅助数据类
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class MT5DemoCheck:
    """MT5 Demo 账户安全检查结果。

    属性:
        allowed: 是否允许下单。
        message: 检查结果描述。
        login: 当前 MT5 登录号。
        server: 当前 MT5 服务器名。
    """

    allowed: bool
    message: str
    login: str = ""
    server: str = ""


@dataclass(frozen=True)
class MT5OrderCheck:
    """MT5 订单预检查结果。

    属性:
        allowed: 是否允许下单。
        message: 检查结果描述。
        retcode: MT5 返回码。
    """

    allowed: bool
    message: str
    retcode: int | None = None


class MT5Adapter(PaperAdapter):
    """MetaTrader 5 交易所适配器。

    继承 PaperAdapter 的模拟交易逻辑，在 live / demo 模式下使用 MT5 终端真实下单。

    参数:
        live: 是否启用实盘下单。
        demo: 是否启用 Demo 下单（live 为 True 时 demo 无效）。
    """

    def __init__(self, live: bool = False, demo: bool = False) -> None:
        super().__init__("mt5", price_bias_bps=20.0)
        self.live = live
        self.demo = bool(demo and not live)
        self.settings = get_settings()

    def place_order(self, order: AdapterOrder) -> AdapterOrderResult:
        """MT5 下单请求。

        非 MT5 模式时走 Paper 模拟。MT5 模式下：
        1. 检查实盘 / Demo 开关
        2. 初始化 MT5 终端连接
        3. Demo 模式下进行账户安全检查
        4. 选择品种并获取 tick 行情
        5. 构建下单请求并发送
        """
        if not self._uses_mt5():
            return super().place_order(order)
        if self.live and not self.settings.mt5.live_order_enabled:
            return AdapterOrderResult(
                False, "", "failed", 0.0, 0.0, 0.0,
                "MT5 实盘下单开关未开启",
            )
        if self.demo and not self.settings.mt5.demo_order_enabled:
            return AdapterOrderResult(
                False, "", "failed", 0.0, 0.0, 0.0,
                "MT5 demo 下单开关未开启",
            )
        if order.order_type != "market":
            return AdapterOrderResult(
                False, "", "failed", 0.0, 0.0, 0.0,
                "MT5 首版仅支持 market 订单",
            )
        try:
            import MetaTrader5 as mt5  # type: ignore
        except Exception as exc:
            return AdapterOrderResult(
                False, "", "failed", 0.0, 0.0, 0.0,
                f"MetaTrader5 包不可用: {exc}",
            )

        # 使用公共模块初始化 MT5 连接
        if not _initialize_mt5(mt5, self.settings):
            return AdapterOrderResult(
                False, "", "failed", 0.0, 0.0, 0.0,
                f"MT5 initialize 失败: {mt5.last_error()}",
            )

        # Demo 模式安全检查
        if self.demo:
            demo_check = mt5_demo_order_check(mt5, self.settings)
            if not demo_check.allowed:
                return AdapterOrderResult(
                    False, "", "failed", 0.0, 0.0, 0.0,
                    demo_check.message,
                )

        symbol = order.venue_symbol or order.symbol
        # 使用公共模块确保品种已选择
        if not ensure_mt5_symbol_selected(symbol):
            return AdapterOrderResult(
                False, "", "failed", 0.0, 0.0, 0.0,
                f"MT5 symbol_select 失败: {symbol}; {mt5.last_error()}",
            )

        tick = mt5.symbol_info_tick(symbol)
        if not tick:
            return AdapterOrderResult(
                False, "", "failed", 0.0, 0.0, 0.0,
                f"MT5 tick 不可用: {symbol}",
            )

        side = order.side.lower()
        if side not in {"buy", "sell"}:
            return AdapterOrderResult(
                False, "", "failed", 0.0, 0.0, 0.0,
                f"MT5 不支持的方向: {order.side}",
            )

        order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
        price = float(tick.ask if side == "buy" else tick.bid)
        request = {
            "action": mt5.TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": float(order.quantity),
            "type": order_type,
            "price": price,
            "deviation": int(self.settings.mt5.order_deviation_points),
            "magic": int(self.settings.mt5.order_magic),
            "comment": "mt5-hedge",
            "type_time": mt5.ORDER_TIME_GTC,
        }

        # reduce-only：查找可平仓的持仓
        if order.reduce_only:
            position = _matching_reduce_position(mt5, symbol, side, float(order.quantity))
            if position is None:
                return AdapterOrderResult(
                    False, "", "failed", 0.0, 0.0, 0.0,
                    f"MT5 reduce-only 未找到可平仓持仓: {symbol} {side} {order.quantity}",
                )
            position_volume = safe_float(getattr(position, "volume", 0.0))
            if float(order.quantity) > position_volume + 1e-9:
                return AdapterOrderResult(
                    False, "", "failed", 0.0, 0.0, 0.0,
                    f"MT5 reduce-only 平仓数量超过持仓: request={order.quantity}, position={position_volume}",
                )
            request["position"] = safe_int(getattr(position, "ticket", 0))

        # 尝试不同的 filling mode
        result = None
        rejected_messages: list[str] = []
        for filling_mode in _mt5_filling_modes(mt5, symbol):
            result = mt5.order_send({**request, "type_filling": filling_mode})
            if result is None:
                rejected_messages.append(f"filling={filling_mode}: {mt5.last_error()}")
                continue
            retcode = safe_int(getattr(result, "retcode", 0))
            if retcode == safe_int(getattr(mt5, "TRADE_RETCODE_INVALID_FILL", 10030)):
                rejected_messages.append(f"filling={filling_mode}: {getattr(result, 'comment', '')}")
                continue
            break

        if result is None:
            return AdapterOrderResult(
                False, "", "failed", 0.0, 0.0, 0.0,
                f"MT5 order_send 无返回: {'; '.join(rejected_messages) or mt5.last_error()}",
            )

        retcode = safe_int(getattr(result, "retcode", 0))
        done_codes = {mt5.TRADE_RETCODE_DONE, mt5.TRADE_RETCODE_DONE_PARTIAL, mt5.TRADE_RETCODE_PLACED}
        if retcode not in done_codes:
            comment = getattr(result, "comment", "")
            return AdapterOrderResult(
                False,
                str(safe_int(getattr(result, "order", ""))),
                "rejected",
                0.0, 0.0, 0.0,
                f"MT5 order_send 失败 retcode={retcode}: {comment}",
            )

        filled = safe_float(getattr(result, "volume", order.quantity), float(order.quantity))
        avg_price = safe_float(getattr(result, "price", price), price)
        external_id = str(safe_int(getattr(result, "order", 0)) or safe_int(getattr(result, "deal", 0)))
        status = (
            "filled" if retcode == mt5.TRADE_RETCODE_DONE
            else "partially_filled" if retcode == mt5.TRADE_RETCODE_DONE_PARTIAL
            else "accepted"
        )
        return AdapterOrderResult(True, external_id, status, filled, avg_price, 0.0)

    def get_positions(self) -> list[dict]:
        """获取 MT5 真实持仓列表。非 MT5 模式时走 Paper 模拟。"""
        if not self._uses_mt5():
            return super().get_positions()
        try:
            import MetaTrader5 as mt5  # type: ignore
        except Exception:
            return []
        if not _initialize_mt5(mt5, self.settings):
            return []
        try:
            positions = mt5.positions_get()
        except Exception:
            positions = None

        rows = []
        for position in positions or []:
            quantity = safe_float(getattr(position, "volume", 0.0))
            if quantity <= 0:
                continue
            side = (
                "long"
                if safe_int(getattr(position, "type", 0)) == safe_int(getattr(mt5, "POSITION_TYPE_BUY", 0))
                else "short"
            )
            rows.append(
                {
                    "platform": "mt5",
                    "symbol": str(getattr(position, "symbol", "")),
                    "side": side,
                    "quantity": quantity,
                    "ticket": str(getattr(position, "ticket", "") or ""),
                    "entry_price": safe_float(getattr(position, "price_open", 0.0)),
                    "mark_price": safe_float(getattr(position, "price_current", 0.0)),
                    "unrealized_pnl": safe_float(getattr(position, "profit", 0.0)),
                    "margin_used": 0.0,
                    "liquidation_price": None,
                }
            )
        return rows

    def get_order(self, order_id: str) -> dict:
        """查询 MT5 订单状态。非 MT5 模式时走 Paper 模拟。"""
        if not self._uses_mt5():
            return super().get_order(order_id)
        try:
            import MetaTrader5 as mt5  # type: ignore
        except Exception as exc:
            return {
                "status": "failed",
                "external_order_id": order_id,
                "message": f"MetaTrader5 包不可用: {exc}",
            }
        if not _initialize_mt5(mt5, self.settings):
            return {
                "status": "failed",
                "external_order_id": order_id,
                "message": f"MT5 initialize 失败: {mt5.last_error()}",
            }

        ticket = _ticket(order_id)
        if ticket is None:
            return {
                "status": "failed",
                "external_order_id": order_id,
                "message": "MT5 order_id 不是有效 ticket",
            }

        order = _first_mt5_result(lambda: mt5.orders_get(ticket=ticket))
        if order is None:
            order = _first_mt5_result(lambda: mt5.history_orders_get(ticket=ticket))
        if order is None:
            return {"status": "not_found", "external_order_id": order_id}

        status = _mt5_order_status(mt5, safe_int(getattr(order, "state", -1)))
        filled_quantity = _mt5_order_filled_quantity(order)
        average_price = safe_float(
            getattr(order, "price_current", 0.0)
            or getattr(order, "price_done", 0.0)
            or getattr(order, "price_open", 0.0)
            or 0.0
        )
        return {
            "status": status,
            "external_order_id": str(getattr(order, "ticket", order_id)),
            "filled_quantity": filled_quantity,
            "average_price": average_price,
            "message": str(getattr(order, "comment", "") or ""),
        }

    def get_trades(self, order_id: str) -> list[dict]:
        """查询 MT5 订单成交明细。非 MT5 模式时走 Paper 模拟。"""
        if not self._uses_mt5():
            return super().get_trades(order_id)
        try:
            import MetaTrader5 as mt5  # type: ignore
        except Exception:
            return []
        if not _initialize_mt5(mt5, self.settings):
            return []

        ticket = _ticket(order_id)
        if ticket is None:
            return []

        deals = _mt5_deals_for_order(mt5, ticket)
        trades = []
        for deal in deals:
            quantity = safe_float(getattr(deal, "volume", 0.0))
            if quantity <= 0:
                continue
            trades.append(
                {
                    "order_id": order_id,
                    "quantity": quantity,
                    "price": safe_float(getattr(deal, "price", 0.0)),
                    "fee": safe_float(getattr(deal, "commission", 0.0))
                    + safe_float(getattr(deal, "fee", 0.0)),
                }
            )
        return trades

    def _uses_mt5(self) -> bool:
        """判断当前是否使用 MT5 真实交易（live 或 demo 模式）。"""
        return bool(self.live or self.demo)


# ---------------------------------------------------------------------------
# 模块级辅助函数
# ---------------------------------------------------------------------------

def _initialize_mt5(mt5, settings) -> bool:
    """初始化 MT5 终端连接。

    使用公共模块 ``ensure_mt5_connected`` 统一处理连接逻辑。
    """
    return ensure_mt5_connected(
        login=settings.mt5.login or None,
        password=settings.mt5.password or None,
        server=settings.mt5.server or None,
    )


def mt5_demo_order_check(mt5, settings) -> MT5DemoCheck:
    """检查当前 MT5 账户是否为合法的 Demo 账户。

    验证项：
    1. Demo 下单开关是否开启
    2. 账户信息是否可读
    3. 当前账户是否为 DEMO 模式
    4. 登录号和服务器是否与配置一致
    """
    if not settings.mt5.demo_order_enabled:
        return MT5DemoCheck(False, "MT5_DEMO_ORDER_ENABLED 未开启")

    try:
        info = mt5.account_info()
    except Exception as exc:
        return MT5DemoCheck(False, f"MT5 account_info 读取失败: {exc}")
    if not info:
        return MT5DemoCheck(False, f"MT5 account_info 为空: {mt5.last_error()}")

    login = str(getattr(info, "login", "") or "")
    server = str(getattr(info, "server", "") or "")
    trade_mode = safe_int(getattr(info, "trade_mode", -1))
    demo_mode = safe_int(getattr(mt5, "ACCOUNT_TRADE_MODE_DEMO", 0))
    if trade_mode != demo_mode:
        return MT5DemoCheck(
            False,
            f"当前 MT5 账户不是 DEMO，禁止 paper 模式下单: login={login} server={server} trade_mode={trade_mode}",
            login,
            server,
        )

    expected_login = str(settings.mt5.login or "").strip()
    if expected_login and login != expected_login:
        return MT5DemoCheck(
            False,
            f"当前 MT5 demo 登录号与 MT5_LOGIN 不匹配: expected={expected_login}, actual={login}",
            login,
            server,
        )
    expected_server = str(settings.mt5.server or "").strip()
    if expected_server and server.lower() != expected_server.lower():
        return MT5DemoCheck(
            False,
            f"当前 MT5 demo 服务器与 MT5_SERVER 不匹配: expected={expected_server}, actual={server}",
            login,
            server,
        )
    return MT5DemoCheck(True, f"MT5 demo 账户检查通过: {login} {server}".strip(), login, server)


def mt5_market_order_check(
    symbol: str,
    side: str,
    quantity: float,
    *,
    demo: bool = False,
    reduce_only: bool = False,
) -> MT5OrderCheck:
    """MT5 订单预检查（不实际下单）。

    用于前端 UI 在下单前验证订单是否可行。

    参数:
        symbol: 品种名称。
        side: 买卖方向。
        quantity: 下单数量。
        demo: 是否为 Demo 模式。
        reduce_only: 是否为只减仓。
    """
    settings = get_settings()
    try:
        import MetaTrader5 as mt5  # type: ignore
    except Exception as exc:
        return MT5OrderCheck(False, f"MetaTrader5 包不可用: {exc}")

    if not _initialize_mt5(mt5, settings):
        return MT5OrderCheck(False, f"MT5 initialize 失败: {mt5.last_error()}")

    if demo:
        demo_check = mt5_demo_order_check(mt5, settings)
        if not demo_check.allowed:
            return MT5OrderCheck(False, demo_check.message)

    if not ensure_mt5_symbol_selected(symbol):
        return MT5OrderCheck(
            False, f"MT5 symbol_select 失败: {symbol}; {mt5.last_error()}"
        )

    tick = mt5.symbol_info_tick(symbol)
    if not tick:
        return MT5OrderCheck(False, f"MT5 tick 不可用: {symbol}")

    normalized_side = side.lower()
    if normalized_side not in {"buy", "sell"}:
        return MT5OrderCheck(False, f"MT5 不支持的方向: {side}")

    order_type = mt5.ORDER_TYPE_BUY if normalized_side == "buy" else mt5.ORDER_TYPE_SELL
    price = float(tick.ask if normalized_side == "buy" else tick.bid)
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(quantity),
        "type": order_type,
        "price": price,
        "deviation": int(settings.mt5.order_deviation_points),
        "magic": int(settings.mt5.order_magic),
        "comment": "mt5-hedge-check",
        "type_time": mt5.ORDER_TIME_GTC,
    }

    if reduce_only:
        position = _matching_reduce_position(mt5, symbol, normalized_side, float(quantity))
        if position is None:
            return MT5OrderCheck(
                False,
                f"MT5 reduce-only 未找到可平仓持仓: {symbol} {side} {quantity}",
            )
        request["position"] = safe_int(getattr(position, "ticket", 0))

    done_codes = {
        0,
        safe_int(getattr(mt5, "TRADE_RETCODE_DONE", 10009)),
        safe_int(getattr(mt5, "TRADE_RETCODE_DONE_PARTIAL", 10010)),
        safe_int(getattr(mt5, "TRADE_RETCODE_PLACED", 10008)),
    }
    invalid_fill = safe_int(getattr(mt5, "TRADE_RETCODE_INVALID_FILL", 10030))
    failures: list[str] = []
    last_retcode: int | None = None

    for filling_mode in _mt5_filling_modes(mt5, symbol):
        result = mt5.order_check({**request, "type_filling": filling_mode})
        if result is None:
            failures.append(f"filling={filling_mode}: {mt5.last_error()}")
            continue
        retcode = safe_int(getattr(result, "retcode", -1))
        last_retcode = retcode
        comment = str(getattr(result, "comment", "") or "")
        if retcode in done_codes:
            return MT5OrderCheck(
                True,
                f"{comment or 'MT5 order_check 通过'}; filling={filling_mode}",
                retcode,
            )
        failures.append(f"filling={filling_mode}: retcode={retcode} {comment}".strip())
        if retcode != invalid_fill:
            break

    return MT5OrderCheck(
        False, f"MT5 order_check 失败: {'; '.join(failures)}", last_retcode
    )


def _mt5_filling_mode(mt5, symbol: str) -> int:
    """获取品种的首选 filling mode。"""
    return _mt5_filling_modes(mt5, symbol)[0]


def _mt5_filling_modes(mt5, symbol: str) -> list[int]:
    """获取品种支持的所有 filling mode，按优先级排序。

    优先使用品种声明的 filling mode，其余按 IOC → FOK → RETURN 顺序尝试。
    """
    info = mt5.symbol_info(symbol)
    filling = safe_int(getattr(info, "filling_mode", 0)) if info else 0
    modes: list[int] = []
    # 先添加品种声明支持的 mode
    for mode in ("ORDER_FILLING_IOC", "ORDER_FILLING_RETURN", "ORDER_FILLING_FOK"):
        value = getattr(mt5, mode, None)
        if value is not None and (filling == 0 or filling & int(value)):
            modes.append(int(value))
    # 再补充其他 mode（去重）
    for mode in ("ORDER_FILLING_IOC", "ORDER_FILLING_FOK", "ORDER_FILLING_RETURN"):
        value = getattr(mt5, mode, None)
        if value is not None and int(value) not in modes:
            modes.append(int(value))
    return modes or [safe_int(getattr(mt5, "ORDER_FILLING_IOC", 1))]


def _matching_reduce_position(mt5, symbol: str, close_side: str, quantity: float):
    """查找最接近目标数量的反向持仓（用于 reduce-only 平仓）。

    参数:
        mt5: MetaTrader5 模块实例。
        symbol: 品种名称。
        close_side: 平仓方向（``"buy"`` 表示平空仓，``"sell"`` 表示平多仓）。
        quantity: 目标平仓数量。

    返回:
        最匹配的持仓对象，或 None（无可平仓持仓）。
    """
    try:
        positions = mt5.positions_get(symbol=symbol)
    except TypeError:
        positions = mt5.positions_get()
    except Exception:
        positions = None

    target_type = (
        getattr(mt5, "POSITION_TYPE_SELL", 1)
        if close_side == "buy"
        else getattr(mt5, "POSITION_TYPE_BUY", 0)
    )
    candidates = []
    for position in positions or []:
        if str(getattr(position, "symbol", "")) != symbol:
            continue
        if safe_int(getattr(position, "type", -1)) != safe_int(target_type):
            continue
        volume = safe_float(getattr(position, "volume", 0.0))
        if volume <= 0:
            continue
        candidates.append((abs(volume - quantity), position))

    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _ticket(order_id: str) -> int | None:
    """将 order_id 转换为 MT5 ticket 号（整数），无效时返回 None。"""
    try:
        return int(str(order_id).strip())
    except (TypeError, ValueError):
        return None


def _first_mt5_result(reader):
    """安全执行 MT5 查询并返回第一条结果。"""
    try:
        rows = reader()
    except TypeError:
        return None
    except Exception:
        return None
    if not rows:
        return None
    return rows[0]


def _mt5_order_status(mt5, state: int) -> str:
    """将 MT5 订单状态码映射为标准字符串。"""
    if state == safe_int(getattr(mt5, "ORDER_STATE_FILLED", -1000)):
        return "filled"
    if state == safe_int(getattr(mt5, "ORDER_STATE_PARTIAL", -1001)):
        return "partially_filled"
    if state in {
        safe_int(getattr(mt5, "ORDER_STATE_STARTED", -1002)),
        safe_int(getattr(mt5, "ORDER_STATE_PLACED", -1003)),
        safe_int(getattr(mt5, "ORDER_STATE_REQUEST_ADD", -1004)),
        safe_int(getattr(mt5, "ORDER_STATE_REQUEST_MODIFY", -1005)),
    }:
        return "accepted"
    if state in {
        safe_int(getattr(mt5, "ORDER_STATE_CANCELED", -1006)),
        safe_int(getattr(mt5, "ORDER_STATE_EXPIRED", -1007)),
    }:
        return "canceled"
    if state == safe_int(getattr(mt5, "ORDER_STATE_REJECTED", -1008)):
        return "rejected"
    return "unknown"


def _mt5_order_filled_quantity(order) -> float:
    """计算 MT5 订单已成交数量。"""
    initial = safe_float(getattr(order, "volume_initial", 0.0))
    current = safe_float(getattr(order, "volume_current", 0.0))
    if initial > 0:
        return max(initial - current, 0.0)
    return safe_float(getattr(order, "volume", 0.0))


def _mt5_deals_for_order(mt5, ticket: int) -> tuple:
    """查询 MT5 指定订单的所有成交记录。"""
    readers = [
        lambda: mt5.history_deals_get(order=ticket),
        lambda: mt5.history_deals_get(ticket=ticket),
    ]
    for reader in readers:
        try:
            rows = reader()
        except TypeError:
            rows = None
        except Exception:
            rows = None
        if rows:
            return tuple(rows)
    return ()
